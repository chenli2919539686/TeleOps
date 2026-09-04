"""运维 Agent：告警降噪 + 根因推理 + 工具调用 + 处置建议。

职责（对应岗位 A 第 2 条：把 AI 融入运维工作流）：
  - AlertNormalizer：告警去重、噪声标注（规则为主，LLM 增强）
  - RootCause：结合 CMDB 拓扑 + 知识库检索，用 LLM 产出 Top-k 根因假设
  - 工具调用：对根因建议的可用工具，自动执行探测
  - 闭环触发：若根因需要、但工具库缺失的工具，反馈给研发 Agent
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from src.llm_client import extract_json
from src import config
from src.triage_rules import rule_triage


class OpsAgent:
    def __init__(self, cmdb, kb, tools, llm):
        self.cmdb = cmdb
        self.kb = kb
        self.tools = tools
        self.llm = llm

    # ---------- 1. 降噪 ----------
    def _rule_triage(self, alert: dict):
        """一次判定：快速挡掉明显噪声。判不出来返回 None，交给 LLM。

        规则本体在 src/triage_rules.py，与离线 Mock 共用同一套判据，
        保证 Mock 结果与真实行为一致（避免跨平台/跨模式结果漂移）。
        """
        return rule_triage(alert)

    def _llm_triage(self, alert: dict) -> bool:
        """二次降噪：规则无结论时，让 LLM 做一次语义判定。

        仅在 _rule_triage 返回 None 时调用，避免每条告警都消耗 token。
        解析失败时保守按「非噪声」处理——漏判比错杀安全。
        """
        prompt = (
            "[TASK:TRIAGE]\n"
            "你是运维告警分级专家。判断下列告警是否值得人工处理："
            "已被系统自动纠正/恢复、例行备份心跳、纯信息输出等属于噪声；"
            "真实服务异常属于非噪声。\n"
            f"告警: {json.dumps(alert, ensure_ascii=False)}\n"
            '输出 JSON: {"is_noise": true 或 false, "reason": "一句中文理由"}'
        )
        try:
            data = extract_json(self.llm.complete(prompt))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        return bool(data.get("is_noise", False))

    def normalize(self, alert: dict) -> dict:
        """分层降噪：规则快判 → 无结论才交给 LLM 语义判定。"""
        verdict = self._rule_triage(alert)
        reason = "rule"
        if verdict is None:
            if config.LLM_TRIAGE:
                verdict = self._llm_triage(alert)
                reason = "llm"
            else:
                verdict = False
                reason = "rule_inconclusive_no_llm"
        return {**alert, "is_noise": bool(verdict),
                "normalized": not verdict, "triage_by": reason}

    # ---------- 2. 根因推理 ----------
    def _build_context(self, alert: dict) -> str:
        host = alert.get("host", "")
        parts = [f"告警主机: {host}"]
        info = self.cmdb.node_info(host)
        if info:
            parts.append(f"节点类型: {info.get('type')} 名称: {info.get('name')}")
            deps = self.cmdb.dependencies(host)
            depby = self.cmdb.dependents(host)
            if deps:
                parts.append("直接依赖(供应方): " + ", ".join(deps))
            if depby:
                parts.append("被依赖(影响面): " + ", ".join(depby))
        hits = self.kb.retrieve(alert.get("message", ""), top_k=2)
        if hits:
            parts.append("知识库命中:")
            for h in hits:
                parts.append(f"  [{h['source']}] {h['text'][:120]}")
        return "\n".join(parts)

    def rootcause(self, alert: dict) -> dict:
        ctx = self._build_context(alert)
        prompt = (
            "[TASK:ROOTCAUSE]\n"
            f"你是电信云网运维专家。基于以下上下文，对告警做根因推理，"
            f"输出 JSON：{{hypotheses:[{{cause,confidence,evidence,recommended_tool,recommended_action}}],conclusion}}。\n\n"
            f"告警: {json.dumps(alert, ensure_ascii=False)}\n上下文:\n{ctx}"
        )
        raw = self.llm.complete(prompt)
        data = extract_json(raw)
        if not data:
            # 兜底：哪怕解析失败也保证有结构
            data = {"hypotheses": [{"cause": "解析失败，按拓扑默认假设", "confidence": 0.5,
                                     "evidence": "", "recommended_tool": "", "recommended_action": "人工核查"}],
                    "conclusion": "需人工复核"}
        return data

    # ---------- 3. 执行根因建议的可用工具 ----------
    def run_recommended_tools(self, diagnosis: dict) -> list:
        results = []
        for h in diagnosis.get("hypotheses", []):
            tool_name = h.get("recommended_tool")
            if not tool_name:
                continue
            if tool_name not in self.tools.list_tools():
                results.append({"tool": tool_name, "status": "missing"})
                continue
            if self.tools.requires_approval(tool_name):
                results.append({"tool": tool_name, "status": "blocked",
                                "reason": "高风险工具需人工确认"})
                continue
            try:
                out = self.tools.call(tool_name, {"host": ""})
                results.append({"tool": tool_name, "status": "ok", "result": out})
            except Exception as e:
                results.append({"tool": tool_name, "status": "error", "reason": str(e)})
        return results

    def detect_missing_tool(self, diagnosis: dict):
        for h in diagnosis.get("hypotheses", []):
            t = h.get("recommended_tool")
            if t and t not in self.tools.list_tools():
                return t
        return ""

    # ---------- 4. 汇总处置建议 ----------
    def build_plan(self, diagnosis: dict, tool_results: list) -> dict:
        actions = []
        for h in diagnosis.get("hypotheses", []):
            actions.append(f"[{h.get('confidence', 0)}] {h.get('cause')} "
                           f"→ 建议: {h.get('recommended_action')}")
        for r in tool_results:
            if r["status"] == "ok":
                actions.append(f"工具 {r['tool']} 执行: {r.get('result')}")
            elif r["status"] == "missing":
                actions.append(f"⚠️ 工具 {r['tool']} 缺失，已触发研发闭环")
        return {"conclusion": diagnosis.get("conclusion", ""), "actions": actions}

    # ---------- 对外：单次告警处置（供 LangGraph 节点调用） ----------
    def handle_alert(self, alert: dict) -> dict:
        norm = self.normalize(alert)
        if norm.get("is_noise"):
            return {"alert": alert, "normalized": norm, "is_noise": True,
                    "diagnosis": {}, "tool_results": [], "plan": {"actions": ["噪声告警，已抑制"]}}
        diag = self.rootcause(alert)
        tr = self.run_recommended_tools(diag)
        missing = self.detect_missing_tool(diag)
        plan = self.build_plan(diag, tr)
        return {"alert": alert, "normalized": norm, "is_noise": False,
                "diagnosis": diag, "tool_results": tr, "missing_tool": missing, "plan": plan}
