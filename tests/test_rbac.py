# -*- coding: utf-8 -*-
"""RBAC 接线回归测试（v0.8.49）。

验证「RBAC 引擎从死代码变成真能力」：
1. 无 tool.exec 的 viewer / dev 被 stream_start 拦（403），有 tool.exec 的 sre / admin 放行；
2. 无 agent.manage 的 viewer 被 build_agent / register-gap 拦（403），有 agent.manage 的
   dev / sre / admin 放行；
3. /admin/roles 受 org.manage 闸保护：admin 可列可分配，无 org.manage 的 sre 被拦；
4. 默认 sre（既有角色）在接线后无任何回归——原本能做的操作仍然能做。

不依赖是否开启 HITL 审批：agent.manage / tool.exec 闸在审批分支之前，无论审批开关如何都先过。
"""
import uuid

from src.core import auth as _auth
from src.core import db as _db


def _make_user(client, role: str):
    """造一个指定内置角色的用户并登录，返回 (请求头, 用户名)。

    测试构造：先按默认（sre）建用户，再直接改写 user_roles 表把角色精确设为目标角色，
    绕开生产级「锁死闸」（revoke_role 在校内无管理员时会回滚），纯粹为了可测性。
    """
    username = f"rbac_{role}_" + uuid.uuid4().hex[:8]
    password = "Rbac123456"
    _auth.create_user(username, password)  # 默认 sre；DB 为空时首个用户会被自动提为管理员
    u = _auth.get_user(username)
    if role == "super_admin":
        _db.execute("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
    else:
        # 强制非管理员 + 精确角色，避免「首个用户自动成 admin」污染 RBAC 判定
        _db.execute("UPDATE users SET is_admin=0 WHERE id=?", (u["id"],))
        _db.execute("DELETE FROM user_roles WHERE user_id=?", (u["id"],))
        _db.execute("INSERT INTO user_roles (user_id,role_id) VALUES (?,?)",
                    (u["id"], role))
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    return headers, username


def _make_ws(client, headers):
    name = "rbac-ws-" + uuid.uuid4().hex[:6]
    r = client.post("/workspaces",
                    json={"name": name, "adapter_id": "alert-prometheus", "mode": "auto"},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_ops_agent(client, headers, ws_id):
    r = client.post(f"/workspaces/{ws_id}/agents",
                    json={"kind": "ops", "name": "rbac-ops", "scope": ["domain"],
                          "description": "rbac test", "primary": True},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_dev_agent(client, headers, ws_id):
    r = client.post(f"/workspaces/{ws_id}/agents",
                    json={"kind": "dev", "name": "rbac-dev", "scope": ["domain"],
                          "description": "rbac test", "primary": True},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ---------------- stream_start（tool.exec 闸） ----------------
def test_stream_start_denied_without_tool_exec(client):
    (viewer, _) = _make_user(client, "viewer")
    ws = _make_ws(client, viewer)
    r = client.post("/stream/start",
                    json={"workspace_id": ws, "profile": "mixed", "mode": "auto"},
                    headers=viewer)
    assert r.status_code == 403, r.text  # viewer 无 tool.exec → 拦


def test_stream_start_allowed_for_sre(client, auth_headers):
    ws = _make_ws(client, auth_headers)
    r = client.post("/stream/start",
                    json={"workspace_id": ws, "profile": "mixed", "mode": "auto"},
                    headers=auth_headers)
    assert r.status_code != 403, r.text  # sre 有 tool.exec → 放行（无回归）
    client.post("/stream/stop", params={"workspace_id": ws}, headers=auth_headers)


def test_stream_start_allowed_for_admin(client, admin_headers):
    ws = _make_ws(client, admin_headers)
    r = client.post("/stream/start",
                    json={"workspace_id": ws, "profile": "mixed", "mode": "auto"},
                    headers=admin_headers)
    assert r.status_code != 403, r.text
    client.post("/stream/stop", params={"workspace_id": ws}, headers=admin_headers)


# ---------------- build_agent / register-gap（agent.manage 闸） ----------------
def test_build_agent_denied_without_agent_manage(client):
    (viewer, _) = _make_user(client, "viewer")
    ws = _make_ws(client, viewer)
    agent = _make_dev_agent(client, viewer, ws)
    r = client.post(f"/agents/{agent}/build", json={"feedback_id": "x", "summary": "t"},
                    headers=viewer)
    assert r.status_code == 403, r.text  # viewer 无 agent.manage → 拦


def test_build_agent_allowed_for_dev(client):
    (dev, _) = _make_user(client, "dev")
    ws = _make_ws(client, dev)
    agent = _make_dev_agent(client, dev, ws)
    r = client.post(f"/agents/{agent}/build", json={"feedback_id": "x", "summary": "t"},
                    headers=dev)
    assert r.status_code != 403, r.text  # dev 有 agent.manage → 放行


def test_register_gap_denied_without_agent_manage(client):
    (viewer, _) = _make_user(client, "viewer")
    ws = _make_ws(client, viewer)
    agent = _make_ops_agent(client, viewer, ws)
    r = client.post(f"/agents/{agent}/register-gap",
                    json={"missing_tool": "dummy_tool", "alert": {"summary": "t"}},
                    headers=viewer)
    assert r.status_code == 403, r.text


def test_register_gap_allowed_for_dev(client):
    (dev, _) = _make_user(client, "dev")
    ws = _make_ws(client, dev)
    agent = _make_ops_agent(client, dev, ws)
    r = client.post(f"/agents/{agent}/register-gap",
                    json={"missing_tool": "dummy_tool", "alert": {"summary": "t"}},
                    headers=dev)
    assert r.status_code != 403, r.text


# ---------------- /admin/roles（org.manage 闸） ----------------
def test_admin_roles_list_denied_without_org_manage(client):
    # 用强制非管理员的 sre 用户，避免依赖 auth_headers 在共享 DB 中是否恰好为首个用户（会自动提管理员）
    (sre, _) = _make_user(client, "sre")
    r = client.get("/admin/roles", headers=sre)
    assert r.status_code == 403, r.text  # sre 无 org.manage → 拦


def test_admin_roles_manage_roundtrip(client, admin_headers):
    r = client.get("/admin/roles", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert "users" in r.json() and "roles" in r.json()

    # admin 把一个 dev 用户升级为 org_admin，再回收，验证角色运营闭环
    (dev, dev_username) = _make_user(client, "dev")
    up = client.post("/admin/roles",
                     json={"username": dev_username, "role_id": "org_admin",
                           "action": "assign"}, headers=admin_headers)
    assert up.status_code == 200, up.text
    down = client.post("/admin/roles",
                       json={"username": dev_username, "role_id": "org_admin",
                             "action": "revoke"}, headers=admin_headers)
    assert down.status_code == 200, down.text
