# -*- coding: utf-8 -*-
"""Agent 删除回归：前端删除入口依赖的 DELETE 端点（后端已实现，补测试保护）。"""


def test_delete_extra_agent_then_protect_last(client, auth_headers, ws_id):
    # 默认域自带 1 运维 + 1 研发；先加一个同名类型 Agent
    r = client.post(f"/workspaces/{ws_id}/agents", headers=auth_headers, json={
        "kind": "ops", "name": "额外运维 Agent", "scope": ["x"], "description": "测试",
    })
    assert r.status_code == 200, r.text
    extra_id = r.json()["id"]

    # 删除额外 Agent 成功
    r = client.delete(f"/workspaces/{ws_id}/agents/{extra_id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json().get("deleted") == extra_id

    # 该域现在只剩 1 个运维 Agent，删除应被保护拒绝（400）
    ops = [a for a in client.get(f"/agents?workspace_id={ws_id}").json()["agents"]
           if a["kind"] == "ops"]
    assert len(ops) == 1, ops
    r = client.delete(f"/workspaces/{ws_id}/agents/{ops[0]['id']}", headers=auth_headers)
    assert r.status_code == 400, r.text
    assert "至少" in r.json().get("detail", "")


def test_delete_nonexistent_agent_404(client, auth_headers, ws_id):
    r = client.delete(f"/workspaces/{ws_id}/agents/no-such-agent", headers=auth_headers)
    assert r.status_code == 400
    assert "不存在" in r.json().get("detail", "")
