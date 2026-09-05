# -*- coding: utf-8 -*-
"""JWT 鉴权：注册 / 登录 / 身份读取 / 写接口保护 / 读接口开放。"""
import uuid


def _uname():
    return "u_" + uuid.uuid4().hex[:8]


def test_auth_status(client):
    d = client.get("/auth/status").json()
    assert d["jwt_enabled"] is True
    assert d["auth_required"] is False   # 测试环境未设共享 Token


def test_register_and_login(client):
    u, p = _uname(), "Secret123456"
    r = client.post("/auth/register", json={"username": u, "password": p})
    assert r.status_code in (200, 201), r.text
    token = r.json()["token"]

    r = client.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    assert r.json()["token"]

    # jwt 可换取身份
    r = client.get("/auth/me", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == u


def test_duplicate_register_rejected(client):
    u, p = _uname(), "Secret123456"
    assert client.post("/auth/register", json={"username": u, "password": p}).status_code in (200, 201)
    assert client.post("/auth/register", json={"username": u, "password": p}).status_code in (400, 409)


def test_wrong_password_rejected(client):
    u = _uname()
    client.post("/auth/register", json={"username": u, "password": "Secret123456"})
    r = client.post("/auth/login", json={"username": u, "password": "wrong-password"})
    assert r.status_code in (400, 401)


def test_me_without_token(client):
    assert client.get("/auth/me").status_code == 401


def test_tampered_token_rejected(client, auth_headers):
    bad = {"Authorization": "Bearer " + auth_headers["Authorization"].split()[1] + "x",
           "Content-Type": "application/json"}
    r = client.post("/workspaces", json={"name": "tamper-test"}, headers=bad)
    assert r.status_code == 401


def test_write_requires_auth(client):
    """写接口无凭据必须 401。"""
    r = client.post("/workspaces", json={"name": "no-auth-should-fail"})
    assert r.status_code == 401, r.text


def test_read_is_public(client):
    """读接口对匿名开放（鉴权仅覆盖写操作）。"""
    for path in ("/health", "/topology", "/tools", "/adapters", "/workspaces", "/requirements"):
        assert client.get(path).status_code == 200, path


def test_write_with_jwt_ok(client, auth_headers):
    name = "jwt-ws-" + uuid.uuid4().hex[:6]
    r = client.post("/workspaces", json={"name": name}, headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    wid = r.json()["id"]
    assert client.delete(f"/workspaces/{wid}", headers=auth_headers).status_code in (200, 204)


def test_logout_revokes_token(client):
    """登出后旧 JWT 立即失效（进黑名单），解决共享机器不登出残留问题。

    用独立用户，绝不触碰 session 级 auth_headers，避免污染其它用例。
    """
    u, p = "logout_" + uuid.uuid4().hex[:8], "Secret123456"
    r = client.post("/auth/register", json={"username": u, "password": p})
    assert r.status_code in (200, 201), r.text
    token = r.json()["token"]
    hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    # 登出前可正常读取身份
    assert client.get("/auth/me", headers=hdr).status_code == 200
    # 调登出
    r = client.post("/auth/logout", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True
    # 登出后旧 token 立即失效
    assert client.get("/auth/me", headers=hdr).status_code == 401


def test_revoke_token_makes_decode_return_none():
    """auth.revoke_token 后 decode_token 直接返回 None（黑名单命中）。"""
    from src.core import auth as auth_mod
    token = auth_mod.encode_token({"sub": "k", "uid": 1, "is_admin": True})
    assert auth_mod.decode_token(token) is not None
    assert auth_mod.revoke_token(token) is True
    assert auth_mod.decode_token(token) is None  # 命中黑名单


def test_logout_requires_authorization(client):
    """/auth/logout 必须带 Authorization 头，否则 400。"""
    assert client.post("/auth/logout").status_code == 400
