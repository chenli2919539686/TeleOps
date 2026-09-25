# -*- coding: utf-8 -*-
"""OIDC / SSO 单点登录（v0.8.46）：配置驱动 + dev mock，零外部依赖可演示。

核心断言：
1. 默认禁用；设 TELEOPS_OIDC_ENABLED=1 → 启用且走 dev mock（无 IdP 也能跑）；
2. 设 TELEOPS_OIDC_ISSUER → 启用且走 live（真实 IdP）；
3. TELEOPS_OIDC_DEV_USERS 可配置多个虚拟员工身份（dev mock 演示）；
4. /auth/oidc/login（dev）返回本地回调地址；/auth/oidc/callback（dev）签发 JWT 并建用户；
5. dev 登录幂等（重复登录不新建账号）+ 审计落 auth.oidc；
6. dev 登录后拿到 token 能拉到个人业务域（多租户隔离建域）；
7. /auth/status 暴露 oidc_enabled / oidc_mode。
"""
import pytest


_OIDC_ENV = ("TELEOPS_OIDC_ENABLED", "TELEOPS_OIDC_ISSUER", "TELEOPS_OIDC_CLIENT_ID",
             "TELEOPS_OIDC_CLIENT_SECRET", "TELEOPS_OIDC_REDIRECT_URI", "TELEOPS_OIDC_SCOPE",
             "TELEOPS_OIDC_DEV", "TELEOPS_OIDC_DEV_USERS", "TELEOPS_OIDC_FRONTEND_URL")


@pytest.fixture(autouse=True)
def _clear_oidc_env(monkeypatch):
    """每个用例前清空所有 OIDC 相关环境变量，避免跨用例污染。"""
    for k in _OIDC_ENV:
        monkeypatch.delenv(k, raising=False)
    yield


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_oidc_disabled_by_default():
    from src.core import oidc
    assert oidc.oidc_enabled() is False
    assert oidc.oidc_mode() == "off"


def test_oidc_enabled_via_flag_is_dev(monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_ENABLED="1")
    assert oidc.oidc_enabled() is True
    assert oidc.oidc_mode() == "dev"   # 无 issuer → dev mock


def test_oidc_enabled_via_issuer_is_live(monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_ISSUER="https://idp.example.com")
    assert oidc.oidc_enabled() is True
    assert oidc.oidc_mode() == "live"


def test_oidc_explicit_dev_flag(monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_DEV="1")
    assert oidc.oidc_dev_mock() is True


def test_dev_users_parsing(monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_DEV_USERS="demo@oidc.local:OIDCDemo,alice@oidc.local:Alice")
    us = oidc.dev_users()
    assert len(us) == 2
    assert us[0]["email"] == "demo@oidc.local" and us[0]["name"] == "OIDCDemo"
    assert us[1]["email"] == "alice@oidc.local" and us[1]["name"] == "Alice"


def test_login_endpoint_dev(client, monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_ENABLED="1")
    r = client.get("/auth/oidc/login")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "dev"
    assert "dev_user=" in d["redirect_url"]


def test_callback_dev_creates_user_and_token(client, monkeypatch):
    from src.core import oidc
    from src.core import db
    _set(monkeypatch, TELEOPS_OIDC_ENABLED="1")
    r = client.get("/auth/oidc/callback?dev_user=demo@oidc.local&dev_name=OIDCDemo")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["token"]
    assert d["user"]["username"] == "demo@oidc.local"
    assert d["mode"] == "dev"

    # 幂等：再登一次仍是同一用户，不新建
    r2 = client.get("/auth/oidc/callback?dev_user=demo@oidc.local")
    assert r2.status_code == 200
    assert r2.json()["user"]["username"] == "demo@oidc.local"

    # 审计落 auth.oidc
    row = db.query_one(
        "SELECT 1 FROM audit_log WHERE action='auth.oidc' AND actor=?",
        ("demo@oidc.local",))
    assert row, "OIDC 登录未写入审计"


def test_callback_dev_token_lists_personal_workspace(client, monkeypatch):
    """dev 登录后拿到的 token 能拉到个人业务域（验证建域 + token 有效）。"""
    _set(monkeypatch, TELEOPS_OIDC_ENABLED="1",
         TELEOPS_OIDC_DEV_USERS="bob@oidc.local:Bob")
    r = client.get("/auth/oidc/callback?dev_user=bob@oidc.local")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    headers = {"Authorization": "Bearer " + token}
    ws = client.get("/workspaces", headers=headers)
    assert ws.status_code == 200, ws.text
    wss = ws.json()["workspaces"]
    # 至少有一个 owner_id 非空的私人域（多租户隔离建域生效）
    assert any(w.get("owner_id") is not None for w in wss), wss


def test_callback_live_without_code_400(client, monkeypatch):
    """live 模式（配了 issuer）下，回调既无 dev_user 也无 code → 无法解析身份，返回 400。"""
    _set(monkeypatch, TELEOPS_OIDC_ISSUER="https://idp.example.com")
    r = client.get("/auth/oidc/callback")
    assert r.status_code == 400, r.text
    assert "未获取到 OIDC 身份" in r.json().get("detail", "")


def test_status_exposes_oidc(client, monkeypatch):
    from src.core import oidc
    _set(monkeypatch, TELEOPS_OIDC_ENABLED="1", TELEOPS_OIDC_ISSUER="https://idp.example.com")
    # 先触发一次 OIDC 路由确保模块配置生效（实际为调用时读取 env，无需预热）
    assert oidc.oidc_mode() == "live"
    st = client.get("/auth/status").json()
    assert st["oidc_enabled"] is True
    assert st["oidc_mode"] == "live"
    assert st["issuer"] == "https://idp.example.com"
