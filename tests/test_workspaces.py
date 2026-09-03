# -*- coding: utf-8 -*-
"""业务域（多租户隔离）：CRUD / 域内 Agent 自动创建 / 派发模式 / 操作记录 / 级联删除。"""


def test_create_workspace_with_agents(client, auth_headers, ws_id):
    r = client.get(f"/agents?workspace_id={ws_id}")
    assert r.status_code == 200, r.text
    agents = r.json()["agents"]
    kinds = {a["kind"] for a in agents}
    assert kinds >= {"ops", "dev"}, f"新域应自带运维+研发 Agent，实际 {kinds}"
    # Agent id 必须绑定本域（跨域隔离的关键）
    assert all(a["id"].startswith(ws_id) for a in agents), [a["id"] for a in agents]


def test_domain_isolation(client, auth_headers, ws_id):
    """A1: 不同业务域的 Agent 互不串扰。"""
    other = client.get("/agents?workspace_id=core-net").json()["agents"]
    mine = client.get(f"/agents?workspace_id={ws_id}").json()["agents"]
    mine_ids = {a["id"] for a in mine}
    other_ids = {a["id"] for a in other}
    assert not (mine_ids & other_ids), "两个域的 Agent id 不应重叠"


def test_ghost_domain_no_500(client, auth_headers):
    """A3: 幽灵域请求必须优雅返回，不能 500。"""
    assert client.get("/agents?workspace_id=ghost-xyz").json()["agents"] == []
    r = client.post("/requirements/raise", headers=auth_headers, json={
        "workspace_id": "ghost-xyz",
        "alert": {"alert_id": "A-NOISE", "metric": "cpu", "host": "host-x", "severity": "info",
                  "value": "62%", "message": "抖动", "tags": ["compute"], "is_noise": True}})
    assert r.status_code == 200, r.text


def test_mode_switch(client, auth_headers, ws_id):
    r = client.put(f"/workspaces/{ws_id}/mode", headers=auth_headers, json={"mode": "manual"})
    assert r.status_code == 200, r.text
    assert client.get(f"/workspaces/{ws_id}").json()["mode"] == "manual"
    client.put(f"/workspaces/{ws_id}/mode", headers=auth_headers, json={"mode": "auto"})
    assert client.get(f"/workspaces/{ws_id}").json()["mode"] == "auto"


def test_messages_roundtrip(client, auth_headers, ws_id):
    """工作台产出写回消息栏（操作记录）。"""
    r = client.post(f"/workspaces/{ws_id}/messages", headers=auth_headers, json={
        "agent_id": f"{ws_id}-ops-main", "kind": "diagnose",
        "summary": "pytest 写入的测试记录", "detail": "detail-x"})
    assert r.status_code in (200, 201), r.text

    msgs = client.get(f"/workspaces/{ws_id}/messages").json()["messages"]
    assert any("pytest 写入的测试记录" in (m.get("summary") or "") for m in msgs)


def test_messages_require_auth(client, ws_id):
    r = client.post(f"/workspaces/{ws_id}/messages", json={
        "agent_id": "x", "kind": "diagnose", "summary": "匿名写入应失败"})
    assert r.status_code == 401


def test_delete_default_domain_protected(client, auth_headers):
    r = client.delete("/workspaces/core-net", headers=auth_headers)
    assert r.status_code == 400, f"默认域应受保护，实际 {r.status_code}"


def test_delete_cascade(client, auth_headers):
    name = "待删除域-" + "x" * 6
    r = client.post("/workspaces", headers=auth_headers, json={"name": name})
    wid = r.json()["id"]
    assert client.delete(f"/workspaces/{wid}", headers=auth_headers).status_code in (200, 204)
    # 级联：域内 Agent 一并清理
    assert client.get(f"/agents?workspace_id={wid}").json()["agents"] == []
    # 域本身消失
    assert wid not in [w["id"] for w in client.get("/workspaces").json()["workspaces"]]
