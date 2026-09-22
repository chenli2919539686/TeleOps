"""Agent 路由单测（v0.8.24）：registry.route 按 scope 交集把告警匹配给最专长 Agent。

覆盖：
1. 同域内按 tags/metric 命中 scope → 选专长 Agent（光功率→接入网 Agent，过热→核心网 Agent）
2. 同域无匹配 → 放宽全局同 kind Agent 再算（个人域也能用到公共域专长 Agent）
3. 全域无匹配 → 同域轮询兜底（不返回 None，保证流水线不中断）
"""
import sys
sys.path.insert(0, ".")

from src.core.agent_registry import AgentRegistry


def _reg():
    """构造一个不依赖 cmdb/kb/tools/llm 的注册表，直接注入 Agent 元数据。"""
    reg = AgentRegistry(None, None, None, None)
    reg.agents = {
        "core-net-ops-main": {"id": "core-net-ops-main", "kind": "ops",
                              "scope": ["core", "compute"], "workspace_id": "core-net",
                              "primary": True, "status": "idle"},
        "core-net-ops-2": {"id": "core-net-ops-2", "kind": "ops",
                           "scope": ["access", "optical", "onu"], "workspace_id": "core-net",
                           "primary": False, "status": "idle"},
    }
    return reg


def test_route_picks_specialist_by_scope():
    reg = _reg()
    # ONU 光功率告警：tags 含 optical/access → 命中 core-net-ops-2
    r = reg.route("ops", {"workspace_id": "core-net",
                          "tags": ["access", "optical"], "description": "光模块接收光功率低"})
    assert r == "core-net-ops-2"
    # CPU 过热：tags 含 compute → 命中 core-net-ops-main
    r = reg.route("ops", {"workspace_id": "core-net",
                          "tags": ["compute", "temperature"], "description": "核心温度过热"})
    assert r == "core-net-ops-main"


def test_route_falls_back_globally():
    """个人域只有核心网 Agent（scope core/compute），但收到光告警时应跨域兜底到公共域专长 Agent。"""
    reg = AgentRegistry(None, None, None, None)
    reg.agents = {
        "ws-1-ops-main": {"id": "ws-1-ops-main", "kind": "ops",
                          "scope": ["core", "compute"], "workspace_id": "ws-1",
                          "primary": True, "status": "idle"},
        "core-net-ops-2": {"id": "core-net-ops-2", "kind": "ops",
                           "scope": ["access", "optical", "onu"], "workspace_id": "core-net",
                           "primary": False, "status": "idle"},
    }
    r = reg.route("ops", {"workspace_id": "ws-1",
                          "tags": ["optical"], "description": "光路劣化"}, cross_domain=True)
    assert r == "core-net-ops-2"  # 跨域兜底到公共域专长


def test_route_poll_fallback_when_no_match():
    """全域都匹配不上 → 返回同域某 Agent（轮询兜底），绝不返回 None 导致流水线中断。"""
    reg = _reg()
    r = reg.route("ops", {"workspace_id": "core-net",
                          "tags": ["switch"], "description": "上联端口错包激增"})
    assert r in ("core-net-ops-main", "core-net-ops-2")


def test_route_respects_kind():
    """dev 与 ops 互不串域路由。"""
    reg = _reg()
    reg.agents["core-net-dev-main"] = {"id": "core-net-dev-main", "kind": "dev",
                                       "scope": ["net", "optical"], "workspace_id": "core-net",
                                       "primary": True, "status": "idle"}
    # 光告警路由 ops → 仍是 ops Agent，不会跑到 dev
    r = reg.route("ops", {"workspace_id": "core-net",
                          "tags": ["optical"], "description": "光"})
    assert r == "core-net-ops-2"
    # 研发需求路由 dev → 命中 dev
    r = reg.route("dev", {"workspace_id": "core-net",
                          "tags": ["net"], "description": "研发探测工具"})
    assert r == "core-net-dev-main"
