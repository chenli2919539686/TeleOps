"""LangGraph 编排：运维闭环图 + 研发闭环图。

设计：每个 Agent 的"业务智能"写在 src/agents 里，这里只用 LangGraph 把多个
步骤串成有向工作流（可分支/可回退/可观测），体现"编排引擎"这一能力层。
"""
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END


# ---------------- 运维闭环图 ----------------
class OpsState(TypedDict):
    alert: dict
    normalized: dict
    diagnosis: dict
    tool_results: list
    plan: dict
    missing_tool: str
    is_noise: bool


def build_ops_graph(agent):
    def normalize(s: OpsState) -> dict:
        n = agent.normalize(s["alert"])
        return {"normalized": n, "is_noise": n.get("is_noise", False)}

    def diagnose(s: OpsState) -> dict:
        # 噪声告警直接跳过根因推理
        if s.get("normalized", {}).get("is_noise"):
            return {"diagnosis": {}, "tool_results": [], "missing_tool": "", "plan": {"actions": ["噪声告警，已抑制"]}}
        diag = agent.rootcause(s["alert"])
        tr = agent.run_recommended_tools(diag)
        missing = agent.detect_missing_tool(diag)
        plan = agent.build_plan(diag, tr)
        return {"diagnosis": diag, "tool_results": tr, "missing_tool": missing, "plan": plan}

    g = StateGraph(OpsState)
    g.add_node("normalize", normalize)
    g.add_node("diagnose", diagnose)
    g.add_edge("normalize", "diagnose")
    g.add_edge("diagnose", END)
    g.set_entry_point("normalize")
    return g.compile()


# ---------------- 研发闭环图 ----------------
class DevState(TypedDict):
    feedback: dict
    tool: dict
    sop: str


def build_dev_graph(agent):
    def codegen(s: DevState) -> dict:
        tool = agent.generate_tool(s["feedback"])
        return {"tool": tool}

    def register(s: DevState) -> dict:
        agent.register_tool(s["tool"])
        return {}

    def sop(s: DevState) -> dict:
        path = agent.write_sop(s["feedback"], s["tool"])
        return {"sop": path}

    g = StateGraph(DevState)
    g.add_node("codegen", codegen)
    g.add_node("register", register)
    g.add_node("sop", sop)
    g.add_edge("codegen", "register")
    g.add_edge("register", "sop")
    g.add_edge("sop", END)
    g.set_entry_point("codegen")
    return g.compile()
