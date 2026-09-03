"""TeleOps W2 演示：策略 B 一+二纵向闭环（运维 ↔ 研发）。

故事线：
  第一轮：运维 Agent 收到 onu-1 光模块告警 → 根因推理 → 发现需要 optical_power_probe
          工具，但工具库没有 → 触发闭环反馈
  → 研发 Agent 读反馈 → 自动生成工具脚本并注册 → 沉淀 SOP 进知识库
  第二轮：运维 Agent 复用新工具重新处置 → 这次能真的探测光功率 → 给出确诊建议

运行：python demo_w2.py  （无需任何 API Key，离线 Mock 即端到端跑通）
有 Key 时：复制 .env.example 为 .env 填入 DEEPSEEK_API_KEY，即走真实大模型。
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 0) 确保数据存在（自造 + 真实公开）
from scripts import gen_data, ingest_public

print("=" * 64)
print("TeleOps W2 演示：运维↔研发 纵向闭环")
print("=" * 64)
print("\n[准备数据] 自造(拓扑/工具/反馈) + 真实公开(告警/知识库)...")
gen_data.main()
raw = ROOT / "data/raw/bgl_sample.log"
if raw.exists():
    ingest_public.convert("bgl", raw)
    ingest_public.make_kb()
else:
    print("提示: 未找到 data/raw/bgl_sample.log，请先运行 ingest_public 生成真实告警/知识库")

# 1) 装配能力层 + Agent + 编排图
from src.core.cmdb_graph import CMDBGraph
from src.core.kb_store import KBStore
from src.core.tool_registry import ToolRegistry
from src.llm_client import LLMClient
from src.agents.ops_agent import OpsAgent
from src.agents.dev_agent import DevAgent
from src.orchestration.graphs import build_ops_graph, build_dev_graph
from src.config import TRACE_DIR


def dump_trace(name, obj):
    p = TRACE_DIR / f"{name}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def main():
    cmdb, kb, llm = CMDBGraph(), KBStore(), LLMClient()
    llm._ensure_client()  # 让 mock/live 判定生效
    print(f"\n[LLM 模式] {llm.mode}  "
          f"(有 DEEPSEEK_API_KEY 走真实大模型；无 Key 走离线 Mock)")

    tools = ToolRegistry()
    ops = OpsAgent(cmdb, kb, tools, llm)
    ops_graph = build_ops_graph(ops)

    # 目标告警：onu-1 光模块发光功率异常
    alert = {
        "alert_id": "A-ONU-1", "ts": "2026-08-30T03:12:00", "source": "zabbix",
        "metric": "optical_power", "host": "onu-1", "severity": "critical",
        "value": "-23.5dBm", "message": "ONU光模块发光功率异常偏低",
        "tags": ["access", "optical"], "is_noise": False,
    }

    print("\n" + "=" * 64)
    print("第一轮：运维 Agent 处置（工具库尚无 optical_power_probe）")
    print("=" * 64)
    s0 = {"alert": alert, "normalized": {}, "diagnosis": {},
          "tool_results": [], "plan": {}, "missing_tool": "", "is_noise": False}
    r1 = ops_graph.invoke(s0)
    print("告警类型:", "噪声(已抑制)" if r1["is_noise"] else "真实")
    print("根因假设:")
    for h in r1["diagnosis"].get("hypotheses", []):
        print(f"  - {h.get('cause')} (置信 {h.get('confidence')})")
    print("处置建议:")
    for a in r1["plan"].get("actions", []):
        print("  *", a)
    dump_trace("w2_round1", r1)

    missing = r1.get("missing_tool")
    if missing:
        print("\n" + "=" * 64)
        print(f"→ 触发闭环：运维需要 [{missing}] 但工具库缺失，派单给研发 Agent")
        print("=" * 64)
        dev = DevAgent(cmdb, kb, llm)
        dev_graph = build_dev_graph(dev)
        d0 = {"feedback": {"feedback_id": "F-001",
                           "summary": f"出现新型告警:光模块发光功率异常(onu-1)，"
                                      f"现有工具库无对应探测工具，需研发补充 {missing} 探测工具"},
              "tool": {}, "sop": ""}
        dr = dev_graph.invoke(d0)
        print("研发生成工具:", dr["tool"]["name"], "->", dr["tool"]["executor"])
        print("SOP 已沉淀:", dr["sop"])
        dump_trace("w2_dev", dr)

        # 重新加载工具库 + 运维 Agent（闭环②：研发成果回流运维）
        tools = ToolRegistry()
        ops = OpsAgent(cmdb, kb, tools, llm)
        ops_graph = build_ops_graph(ops)
        print("\n" + "=" * 64)
        print("第二轮：运维 Agent 复用新工具重新处置")
        print("=" * 64)
        r2 = ops_graph.invoke(s0)
        print("根因假设:")
        for h in r2["diagnosis"].get("hypotheses", []):
            print(f"  - {h.get('cause')} (置信 {h.get('confidence')})")
        print("工具执行结果:")
        for t in r2["tool_results"]:
            print("  *", t)
        print("处置建议:")
        for a in r2["plan"].get("actions", []):
            print("  *", a)
        dump_trace("w2_round2", r2)
        print("\n✅ 闭环完成：研发造的工具已接入运维工作流，下次同类告警可直接探测确诊。")
    else:
        print("\n本轮无需研发介入。")

    print(f"\n[可观测] traces/ 下已生成 w2_round1 / w2_dev / w2_round2.json")


if __name__ == "__main__":
    main()
