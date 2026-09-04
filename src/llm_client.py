"""LLMClient 抽象层：统一 DeepSeek / OpenAI / 本地 Ollama / 离线 Mock 接口。

设计目标：
  - 换模型只改运行时配置，不改动 Agent 代码（满足国产化/信创叙事）。
  - 有 Key 走真实大模型；无 Key 自动降级为确定性 Mock，保证脚手架在任何机器
    上都能端到端跑通（面试现场不怕环境缺依赖/缺网）。
  - Mock 用 [TASK:xxx] 标记路由，输出可被 Agent 稳定解析，演示故事连贯。
"""
import re
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_llm_config
from src.core import usage
from src.triage_rules import rule_triage, alert_from_prompt


def extract_json(text):
    """从模型输出里尽量稳健地抠出 JSON（兼容 ```json 代码块）。"""
    if not text:
        return {}
    # 优先匹配代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 退而求其次：找第一个 { 到最后一个 }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


class LLMClient:
    def __init__(self, cfg: dict = None):
        self._cfg = (cfg or load_llm_config()).copy()
        self._client = None
        self.mode = "live"  # live | mock
        self._cached_key = None
        self._budget_noticed = False  # 预算熔断提示只打一次，避免刷屏
        self._budget_fallback_active = False  # 当前是否因预算熔断而降级（区别于「本来就没 Key」）

    def _recover_from_budget_fallback(self):
        """预算熔断是临时状态：预算调高或取消后应自动恢复 live 模式。

        _ensure_client 只在 provider/api_key 等配置变化时重建客户端，预算变化
        不在其判断范围内——若不在这里主动恢复，调高预算后客户端会一直卡在
        mock 模式，必须重启服务才能继续用真实模型。
        """
        if not self._budget_fallback_active:
            return
        exceeded, _ = usage.check_budget()
        if exceeded:
            return
        print("  [LLMClient] 预算限制已解除，恢复真实模型调用")
        self._budget_fallback_active = False
        self._budget_noticed = False
        self._client = None
        self._cached_key = None
        self._ensure_client()  # 重新建连，内部会把 mode 重置为 live

    @staticmethod
    def _task_tag(prompt: str) -> str:
        """从 prompt 里提取 [TASK:XXX] 标签，用于按任务类型统计用量。"""
        m = re.search(r"\[TASK:([A-Z_0-9]+)\]", prompt or "")
        return m.group(1) if m else "other"

    def _cfg_key(self):
        return (
            self._cfg.get("provider"),
            self._cfg.get("api_key"),
            self._cfg.get("base_url"),
            self._cfg.get("model"),
            self._cfg.get("local_endpoint"),
            self._cfg.get("local_model"),
        )

    def _ensure_client(self):
        # 每次调用都重新读取运行时配置，支持前端修改后无需重启
        self._cfg = load_llm_config()
        key = self._cfg_key()
        if self._client is not None and key == self._cached_key:
            return
        self._client = None
        self._cached_key = key
        self.mode = "live"
        provider = self._cfg.get("provider", "mock")
        api_key = self._cfg.get("api_key", "")
        base_url = self._cfg.get("base_url", "")
        local_endpoint = self._cfg.get("local_endpoint", "http://localhost:11434/v1")
        if provider in ("deepseek", "openai", "siliconflow", "custom"):
            if not api_key or api_key.strip() in ("", "your_key_here"):
                self.mode = "mock"
                return
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=api_key.strip(), base_url=base_url.strip() or None)
            except Exception as e:
                print(f"  [LLMClient] 创建 OpenAI client 失败：{e}")
                self.mode = "mock"
        elif provider == "local":
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key="ollama", base_url=local_endpoint.strip() or None)
            except Exception as e:
                print(f"  [LLMClient] 创建 local client 失败：{e}")
                self.mode = "mock"
        else:
            self.mode = "mock"

    def complete(self, prompt: str, system: str = None, temperature: float = 0.2) -> str:
        self._ensure_client()
        self._recover_from_budget_fallback()  # 预算恢复后自动脱离 Mock
        try:
            from src.core import metrics
            metrics.inc("teleops_llm_calls_total", mode=self.mode)
        except Exception:
            pass
        # ---- 预算护栏：超限后按策略处置，避免无人值守的告警流烧穿预算 ----
        exceeded, action = usage.check_budget()
        if exceeded and action == "reject":
            raise RuntimeError(
                "[LLMClient] 今日 LLM 预算已用尽，已按策略拒绝调用。"
                "可在「设置 → 用量与预算」中调整上限或改用 fallback 策略。")
        if exceeded and action == "fallback" and self.mode != "mock":
            if not self._budget_noticed:
                print("  [LLMClient] 今日 LLM 预算已用尽，自动降级为 Mock（不再产生费用）")
                self._budget_noticed = True
            self.mode = "mock"
            self._budget_fallback_active = True
            return self._mock(prompt)

        if self.mode == "mock" or self._client is None:
            return self._mock(prompt)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            model = self._cfg.get("model", "")
            if self._cfg.get("provider") == "local":
                model = self._cfg.get("local_model", "qwen2.5:7b")
            model = model or "deepseek-chat"
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            self._record_usage(resp, model, prompt)
            return resp.choices[0].message.content or ""
        except Exception as e:
            # 真实调用失败（限流/网络）时回退 Mock，保证演示不崩
            print(f"  [LLMClient] 真实调用失败，已回退 Mock：{e}")
            self.mode = "mock"
            return self._mock(prompt)

    def _record_usage(self, resp, model: str, prompt: str = ""):
        """解析响应里的 token 用量并计入统计（失败不影响主流程）。"""
        try:
            u = getattr(resp, "usage", None)
            if not u:
                return
            prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(u, "completion_tokens", 0) or 0)
            cached = 0
            details = getattr(u, "prompt_tokens_details", None)
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
            elif isinstance(u, dict):
                cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
            usage.record(
                provider=self._cfg.get("provider", ""),
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached,
                task=self._task_tag(prompt),
            )
        except Exception as e:
            print(f"  [LLMClient] 用量统计失败（不影响调用）：{e}")

    # ---- 离线确定性 Mock：按 [TASK:xxx] 路由 ----
    def _mock(self, prompt: str) -> str:
        p = prompt or ""
        if p.startswith("[TASK:ROOTCAUSE]"):
            return self._mock_rootcause(p)
        if p.startswith("[TASK:CODEGEN]"):
            return self._mock_codegen(p)
        if p.startswith("[TASK:SOP]"):
            return self._mock_sop(p)
        if p.startswith("[TASK:CHANGEORDER]"):
            return self._mock_changeorder(p)
        if p.startswith("[TASK:KBQA]"):
            return self._mock_kbqa(p)
        if p.startswith("[TASK:TRIAGE]"):
            return self._mock_triage(p)
        # 无标记：通用文本
        return "（离线 Mock 模式下未触发具体任务，返回占位文本）"

    @staticmethod
    def _alert_part_of(prompt: str) -> str:
        """从 ROOTCAUSE prompt 中截出「告警」正文，剔除上下文段落。

        上下文（知识库命中 / 拓扑依赖）里可能混入其它场景的关键词——例如用
        「核心温度过热」检索时命中了光模块 SOP，prompt 里就会出现 "optical"。
        若把上下文一并参与场景匹配，诊断结果会随知识库加载顺序漂移：
        Path.glob() 在 Windows 与 Linux 上返回顺序不同，导致同一份代码
        本地返回 temperature_probe、CI 却返回 optical_power_probe。
        """
        m = re.search(r"告警:\s*(.*?)\s*\n上下文:", prompt, re.S)
        return m.group(1) if m else prompt

    def _mock_triage(self, p: str) -> str:
        """离线 TRIAGE：复用与线上相同的规则给出确定性结论。

        走到这里说明规则层已判为「无结论」（真实场景会由 LLM 语义判定）。
        Mock 无法真正理解语义，因此保守按「非噪声」处理——漏判比错杀安全，
        且与 OpsAgent._llm_triage 解析失败时的兜底行为保持一致。
        """
        alert = alert_from_prompt(p)
        verdict = rule_triage(alert)
        if verdict is None:
            verdict = False
            reason = "规则无结论，离线模式保守判定为非噪声"
        else:
            reason = "规则命中"
        return json.dumps({"is_noise": bool(verdict), "reason": reason},
                          ensure_ascii=False)

    def _mock_rootcause(self, p: str) -> str:
        # 场景化：光模块/optical/onu 触发“缺工具”闭环；否则给通用拓扑根因
        # 只认告警本体（见 _alert_part_of），保证离线 Mock 结果跨平台确定。
        scope = self._alert_part_of(p)
        if any(k in scope for k in ["光模块", "optical", "onu", "optical_power"]):
            return json.dumps({
                "hypotheses": [
                    {
                        "cause": "ONU 光模块发光功率异常偏低，疑似光路衰减或模块故障",
                        "confidence": 0.82,
                        "evidence": "onu-1 上联 olt-1；实测发光功率 -23.5dBm 低于阈值 -20dBm",
                        "recommended_tool": "optical_power_probe",
                        "recommended_action": "探测对端光功率并对比阈值，必要时更换 ONU 模块"
                    }
                ],
                "conclusion": "建议优先排查光链路，并补充光功率探测工具以定位"
            }, ensure_ascii=False)
        if any(k in scope for k in ["温度", "过热", "temperature", "散热"]):
            return json.dumps({
                "hypotheses": [
                    {
                        "cause": "核心温度过热，疑似散热风扇故障或负载过高引发降频",
                        "confidence": 0.78,
                        "evidence": "host-1 实测温度 88C 高于阈值 75C",
                        "recommended_tool": "temperature_probe",
                        "recommended_action": "探测实时温度并对比阈值，检查风扇与散热风道"
                    }
                ],
                "conclusion": "建议优先排查散热系统，并补充温度探测工具以定位"
            }, ensure_ascii=False)
        return json.dumps({
            "hypotheses": [
                {"cause": "下游依赖节点异常引发级联", "confidence": 0.7,
                 "evidence": "CMDB 显示存在级联依赖", "recommended_tool": "",
                 "recommended_action": "核查上下游节点状态"}
            ],
            "conclusion": "建议从拓扑上下游定位"
        }, ensure_ascii=False)

    def _mock_codegen(self, p: str) -> str:
        # 优先从反馈摘要里抽取需要的工具名（如"需要工具 temperature_probe 解决告警"）
        m = re.search(r"工具\s+([A-Za-z_][A-Za-z0-9_]*)", p)
        if m:
            name = m.group(1)
        elif any(k in p for k in ["光模块", "optical", "光功率", "optical_power"]):
            name = "optical_power_probe"
        else:
            name = "generic_probe"
        code = (
            f'"""研发 Agent 自动生成：{name} 探测工具（Mock）。"""\n'
            f'def run(params: dict):\n'
            f'    host = params.get("host", "unknown")\n'
            f'    return {{"status": "ok", "tool": "{name}", "host": host,\n'
            f'            "message": "已执行 {name} 探测（Mock 返回）"}}\n'
        )
        return json.dumps({
            "name": name,
            "description": f"研发 Agent 自动生成的 {name} 探测工具",
            "risk": "low",
            "params": {"host": {"type": "string"}},
            "code": code,
        }, ensure_ascii=False)

    def _mock_sop(self, p: str) -> str:
        return (
            "# 故障处置 SOP（自动沉淀）\n\n"
            "## 现象\n光模块发光功率异常偏低告警。\n\n"
            "## 排查步骤\n1. 使用 optical_power_probe 探测对端光功率；\n"
            "2. 对比阈值 -20dBm；\n3. 若持续低于阈值，更换 ONU 模块并复核光路衰减。\n\n"
            "## 升级条件\n更换后 5 分钟内仍异常，升级至传输专业。\n"
        )

    def _mock_kbqa(self, p: str) -> str:
        idx = p.find("知识库:")
        kb_text = p[idx + 4:].strip() if idx >= 0 else ""
        snippet = kb_text[:400]
        return (
            "（离线 Mock 模式）基于知识库检索结果回答：\n"
            + (snippet if snippet else "未检索到相关片段，建议补充知识库或人工核查。")
            + "\n\n说明：配置 DEEPSEEK_API_KEY 后，将由大模型基于以上知识库内容生成自然语言回答。"
        )

    def _mock_changeorder(self, p: str) -> str:
        return (
            "# 变更单（自动生成）\n\n"
            "**变更内容**：对 onu-1 执行光模块更换。\n"
            "**风险等级**：中（影响单用户接入）。\n"
            "**回滚方案**：保留原模块，更换后异常可即时回退。\n"
            "**审批**：需值班长确认。\n"
        )


if __name__ == "__main__":
    c = LLMClient()
    print("mode =", c.mode)
    print(c.complete("[TASK:ROOTCAUSE]\n请对告警做根因推理：onu-1 光模块发光功率异常"))
