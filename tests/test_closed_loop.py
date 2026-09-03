# -*- coding: utf-8 -*-
"""闭环编排：需求登记 → 派发路由落在本域 → 研发造工具 → 缺口登记闭环。

全部走离线 Mock 推理，秒级完成，不联网。
"""
from tests.conftest import wait_job

ALERT = {
    "alert_id": "A-TEMP", "metric": "temperature", "host": "host-1",
    "severity": "critical", "value": "88C", "message": "温度过热",
    "tags": ["compute", "temperature"], "is_noise": False,
}


def test_raise_requirement_routed_in_domain(client, auth_headers, ws_id):
    """A1: 需求与派发的研发 Agent 都必须落在本业务域内。"""
    r = client.post("/requirements/raise", headers=auth_headers,
                    json={"workspace_id": ws_id, "alert": dict(ALERT)})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    ok, res = wait_job(client, job_id)
    assert ok, f"任务未成功：{res}"
    req = (res or {}).get("requirement")
    if req:
        assert req["workspace_id"] == ws_id, f"需求跨域串了：{req['workspace_id']}"
        dev = req.get("assigned_dev_agent_id") or ""
        assert dev.startswith(ws_id), f"研发 Agent 跨域串了：{dev}"


def test_noise_alert_not_raised(client, auth_headers, ws_id):
    """降噪：is_noise 告警不应产生需求。"""
    r = client.post("/requirements/raise", headers=auth_headers, json={
        "workspace_id": ws_id,
        "alert": {"alert_id": "A-NOISE", "metric": "cpu", "host": "host-x", "severity": "info",
                  "value": "62%", "message": "轻微抖动", "tags": ["compute"], "is_noise": True}})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"])
    assert ok, f"任务异常：{res}"


def test_register_gap_closed_loop(client, auth_headers):
    """A2: 工作台登记缺口 → 自动派发研发 → 闭环完成。

    注：同会话内 test_raise_requirement_routed_in_domain 可能已造出
    temperature_probe。v0.7.2 起登记缺口前会查工具库兜底（已存在则复用、
    不再建重复需求），因此这里先移除该工具，确保缺口真实、闭环被真实触发。
    """
    from src.api.server import tools as tool_registry  # 测试用的临时环境，可安全清理
    tool_registry.remove("temperature_probe")

    r = client.post("/agents/core-net-ops-main/register-gap", headers=auth_headers, json={
        "alert": dict(ALERT),
        "diagnosis": {"conclusion": "散热故障，需 temperature_probe 工具确认"},
        "missing_tool": "temperature_probe"})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"], timeout=120)
    assert ok, f"闭环失败：{res}"
    req = (res or {}).get("requirement")
    assert req, f"未产生需求：{res}"
    assert req["workspace_id"] == "core-net"
    assert req["status"] == "done", f"闭环未完成：{req['status']}"


def test_manual_mode_no_auto_dispatch(client, auth_headers, ws_id):
    """手动模式下，需求留在待派发状态，不自动派给研发。"""
    client.put(f"/workspaces/{ws_id}/mode", headers=auth_headers, json={"mode": "manual"})
    r = client.post("/requirements/raise", headers=auth_headers,
                    json={"workspace_id": ws_id, "alert": dict(ALERT)})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"])
    assert ok, f"任务异常：{res}"
    req = (res or {}).get("requirement")
    if req:
        assert req["mode"] == "manual"
        assert not req.get("assigned_dev_agent_id"), "手动模式不应自动派发研发"
