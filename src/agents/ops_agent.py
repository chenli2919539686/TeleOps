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


class OpsAgent:
    def __init__(self, cmdb, kb, tools, llm):
        self.cmdb = cmdb
        self.kb = kb
        self.tools = tools
        self.llm = llm

    # ---------- 1. 降噪 ----------
    def normalize(self, alert: dict) -> dict:
        """规则降噪：info 级 / 已知噪声模式 / 已标记 is_noise 视为噪声。"""
        msg = (alert.get("message", "") + alert.get("metric", "")).lower()
        noise_patterns = ["备份完成", "backup", "心跳正常", "ping_ok", "heartbeat"]
        is_noise = (
            alert.get("is_noise", False)
            or alert.get("severity", "") == "info"
            or any(p in msg for p in noise_patterns)
        )
        return {**alert, "is_noise": bool(is_noise),
                "normalized": False if is_noise else True}

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
