"""TeleOps 最小可运行演示（无需 API Key）。

演示数据链路与能力层三件套：造数据 -> CMDB 查询 -> 工具库调用 -> 知识库检索。
真正的 LLM 调用在 W2 接入（见 src/llm_client.py）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.core.cmdb_graph import CMDBGraph
from src.core.tool_registry import ToolRegistry
from src.core.kb_store import KBStore


def main():
    print("=" * 50)
    print("TeleOps W1 演示：能力层三件套")
    print("=" * 50)

    # 1) CMDB 拓扑
    g = CMDBGraph()
    print("\n[1] CMDB 拓扑")
    print("  节点:", g.all_nodes())
    print("  svc-order 依赖 ->", g.dependencies("svc-order"))
    print("  db-order 被依赖 ->", g.dependents("db-order"))

    # 2) 工具库
    r = ToolRegistry()
    print("\n[2] 工具库 registry")
    print("  已注册:", r.list_tools())
    print("  ping_host 调用 ->", r.call("ping_host", {"host": "sw-core"}))
    print("  restart_service 是否需要人工确认 ->", r.requires_approval("restart_service"))

    # 3) 知识库
    kb = KBStore()
    print("\n[3] 知识库检索")
    for hit in kb.retrieve("核心交换机 端口 拥塞 应急"):
        print(f"  - [{hit['source']}] {hit['text'][:50]}...")

    print("\n能力层三件套跑通（无需 API Key）。")
    print("  下一步 W2：接入 LLM，写研发 / 运维双 Agent。")


if __name__ == "__main__":
    main()
