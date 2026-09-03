# -*- coding: utf-8 -*-
"""核心闭环「工具复用」回归：同一工具只应被造一次，重复缺口直接复用。

背景缺陷（v0.7.2 修复）：ToolRegistry 曾把工具列表缓存在内存，
研发 Agent 造出新工具后，已存在的 OpsAgent 实例与 AgentRegistry.tools
持有的旧引用都看不到 → 同一缺口被反复登记成重复需求（REQ）。

修复要点：
1. ToolRegistry 改为 SQLite 活视图（list_tools/get 实时查库）；
2. _raise_flow 登记需求前查工具库兜底（防工作台回传过期 missing_tool）。
"""
import uuid

from tests.conftest import wait_job

TEMP_ALERT = {
    "alert_id": "A-REUSE", "ts": "", "source": "zabbix", "metric": "temperature",
    "host": "host-9", "severity": "critical", "value": "88C",
    "message": "核心温度过热", "tags": ["compute", "temperature"], "is_noise": False,
}


def test_registry_live_view(client, auth_headers):
    """任何 ToolRegistry 实例都能实时看到新增/删除的工具（SQLite 活视图）。"""
    from src.api.server import tools as tool_registry
    from src.core.tool_registry import ToolRegistry

    name = "reuse_probe_" + uuid.uuid4().hex[:6]
    tool_registry.add({"name": name, "executor": f"tools/{name}.py",
                       "risk": "low", "description": "复用回归临时工具"})
    try:
        stale = ToolRegistry()   # 修复前：实例只看得到创建时刻的内存快照
        assert name in tool_registry.list_tools()
        assert name in stale.list_tools(), "新实例应实时看到新工具"
        assert stale.get(name)["name"] == name
    finally:
        assert tool_registry.remove(name) is True
    assert name not in stale.list_tools(), "删除后应实时不可见"


def test_raise_requirement_reuses_existing_tool(client, auth_headers, ws_id):
    """同一缺口二次发起：工具已造出 → 不再新增需求，返回可复用。"""
    from src.api.server import board, tools as tool_registry

    tool = "temperature_probe"
    tool_registry.remove(tool)   # 确保缺口真实存在，闭环被真实触发
    # 第一次发起：auto 模式跑完「登记 → 研发造工具 → 派回运维」闭环
    r = client.post("/requirements/raise", headers=auth_headers,
                    json={"workspace_id": ws_id, "alert": dict(TEMP_ALERT)})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"], timeout=120)
    assert ok, f"第一次发起失败：{res}"
    first = [x for x in board.list(workspace_id=ws_id) if x.get("needed_tool") == tool]
    assert len(first) == 1, "第一次发起应恰好产生 1 条需求"
    assert tool in tool_registry.list_tools(), "闭环后工具应已入库"

    # 第二次发起同一告警：诊断发现工具已存在 → 不创建新需求
    r2 = client.post("/requirements/raise", headers=auth_headers,
                     json={"workspace_id": ws_id, "alert": dict(TEMP_ALERT)})
    assert r2.status_code == 200, r2.text
    ok2, res2 = wait_job(client, r2.json()["job_id"], timeout=120)
    assert ok2, f"第二次发起失败：{res2}"
    second = [x for x in board.list(workspace_id=ws_id) if x.get("needed_tool") == tool]
    assert len(second) == 1, "第二次发起不应新增需求（工具可复用）"


def test_register_gap_stale_missing_tool_rejected(client, auth_headers, ws_id):
    """工作台回传过期缺口（工具其实已造好）：登记前查库兜底，不建重复 REQ。"""
    from src.api.server import board, registry, tools as tool_registry

    tool = "optical_power_probe"
    if tool not in tool_registry.list_tools():
        tool_registry.add({"name": tool, "executor": f"tools/{tool}.py", "risk": "low",
                           "description": "光功率探针（回归预置）"})
    ops_agents = registry.list(kind="ops", workspace_id=ws_id)
    assert ops_agents, "测试域应含运维 Agent"
    ops_id = ops_agents[0]["id"]

    r = client.post(f"/agents/{ops_id}/register-gap", headers=auth_headers,
                    json={"missing_tool": tool, "alert": dict(TEMP_ALERT),
                          "diagnosis": {"conclusion": "回传的过期诊断"}})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"], timeout=60)
    assert ok, f"登记过期缺口任务失败：{res}"
    assert res.get("reusable") is True, f"应识别为可复用而非新建需求：{res}"
    dup = [x for x in board.list(workspace_id=ws_id) if x.get("needed_tool") == tool]
    assert not dup, "不应为已存在工具创建重复需求"
