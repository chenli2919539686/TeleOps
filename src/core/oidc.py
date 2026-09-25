"""OIDC / 企业 SSO 单点登录（配置驱动 + mock 兜底，零额外依赖）。

设计要点（贴合项目既有「适配器式」配置驱动风格）：
- 设 ``TELEOPS_OIDC_*`` 即启用；未配真实 IdP（无 TELEOPS_OIDC_ISSUER）或显式
  TELEOPS_OIDC_DEV=1 → 走 dev mock 链路，无 IdP 也能端到端跑通登录闭环。
- dev mock：用 TELEOPS_OIDC_DEV_USERS 里配置的虚拟员工身份签发 TeleOps JWT，
  用于开发/演示，不连真实 IdP。
- 真实 IdP：标准 Authorization Code 流——/auth/oidc/login 跳授权页，回调用
  code 换 token，再用 access_token 调 userinfo 端点取身份（免 JWKS / RSA 验签依赖）。

所有配置在调用时从 os.environ 读取（非模块级常量），便于单测 monkeypatch。
"""
import base64
import json
import os
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional

from src.core import auth


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else default


def oidc_enabled() -> bool:
    """配置驱动启用判定：TELEOPS_OIDC_ENABLED=1，或任意 TELEOPS_OIDC_* 已配置。"""
    if _env("TELEOPS_OIDC_ENABLED") in ("1", "true", "True", "yes", "on"):
        return True
    keys = ("TELEOPS_OIDC_ISSUER", "TELEOPS_OIDC_CLIENT_ID",
            "TELEOPS_OIDC_DEV", "TELEOPS_OIDC_DEV_USERS")
    return any(_env(k) for k in keys)


def oidc_dev_mock() -> bool:
    """是否走 dev mock：显式 TELEOPS_OIDC_DEV=1，或启用但未配真实 IdP。"""
    if _env("TELEOPS_OIDC_DEV") in ("1", "true", "True", "yes", "on"):
        return True
    return oidc_enabled() and not _env("TELEOPS_OIDC_ISSUER")


def oidc_mode() -> str:
    if not oidc_enabled():
        return "off"
    return "dev" if oidc_dev_mock() else "live"


def oidc_config_summary() -> Dict[str, Any]:
    """给前端 /auth/status 用的精简配置摘要（不泄露 secret）。"""
    return {
        "oidc_enabled": oidc_enabled(),
        "oidc_mode": oidc_mode(),
        "issuer": _env("TELEOPS_OIDC_ISSUER") or None,
        "client_id": _env("TELEOPS_OIDC_CLIENT_ID") or None,
    }


def dev_users() -> List[Dict[str, str]]:
    """dev mock 可演示的虚拟员工身份（email:Name，逗号分隔；缺省一个 demo）。"""
    raw = _env("TELEOPS_OIDC_DEV_USERS")
    if not raw:
        return [{"sub": "dev-demo-oidc-local", "email": "demo@oidc.local", "name": "OIDCDemo"}]
    out: List[Dict[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            email, name = part.rsplit(":", 1)
            email, name = email.strip(), name.strip()
        else:
            email, name = part, part.split("@")[0]
        out.append({"sub": "dev-" + email, "email": email, "name": name})
    return out


def resolve_dev_identity(dev_user: Optional[str] = None) -> Optional[Dict[str, str]]:
    """从 dev_users 里按 email 取虚拟员工身份；未指定取第一个。"""
    users = dev_users()
    if dev_user:
        hit = next((u for u in users if u["email"] == dev_user), None)
        if hit:
            return hit
    return users[0] if users else None


# ---------------- 真实 IdP：Discovery + 令牌交换 + userinfo ----------------
def _discover(issuer: str) -> Dict[str, Any]:
    """拉取 IdP 的 .well-known/openid-configuration（兼容尾斜杠）。"""
    base = issuer.rstrip("/")
    url = base + "/.well-known/openid-configuration"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def authorize_url(state: str = "") -> str:
    """返回浏览器应跳转的授权地址。

    - dev mock：本地回调，带虚拟员工身份参数（前端直接 fetch 拿 token）。
    - live：基于 discovery 构建标准 authorize URL。
    """
    if oidc_dev_mock():
        u = resolve_dev_identity()
        if not u:
            return "/auth/oidc/callback"
        q = urllib.parse.urlencode({"dev_user": u["email"], "dev_name": u["name"]})
        return f"/auth/oidc/callback?{q}"
    issuer = _env("TELEOPS_OIDC_ISSUER").rstrip("/")
    cfg = _discover(issuer)
    authz = cfg.get("authorization_endpoint") or (issuer + "/protocol/openid-connect/auth")
    params = {
        "response_type": "code",
        "client_id": _env("TELEOPS_OIDC_CLIENT_ID"),
        "redirect_uri": _env("TELEOPS_OIDC_REDIRECT_URI"),
        "scope": _env("TELEOPS_OIDC_SCOPE") or "openid email profile",
    }
    if state:
        params["state"] = state
    return authz + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
    """用授权码换 token（标准 token endpoint）。"""
    issuer = _env("TELEOPS_OIDC_ISSUER").rstrip("/")
    cfg = _discover(issuer)
    token_ep = cfg.get("token_endpoint") or (issuer + "/protocol/openid-connect/token")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _env("TELEOPS_OIDC_CLIENT_ID"),
        "client_secret": _env("TELEOPS_OIDC_CLIENT_SECRET"),
        "redirect_uri": redirect_uri or _env("TELEOPS_OIDC_REDIRECT_URI"),
    }
    req = urllib.request.Request(
        token_ep, data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def resolve_live_identity(code: str) -> Optional[Dict[str, str]]:
    """真实 IdP 身份解析：code→token→userinfo（免 JWKS 验签，信道 TLS+Bearer 已保证安全）。"""
    tokens = exchange_code(code)
    access = tokens.get("access_token")
    issuer = _env("TELEOPS_OIDC_ISSUER").rstrip("/")
    cfg = _discover(issuer)
    userinfo_ep = cfg.get("userinfo_endpoint")
    claims: Dict[str, Any] = {}
    if userinfo_ep and access:
        req = urllib.request.Request(
            userinfo_ep, headers={"Authorization": "Bearer " + access,
                                  "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            claims = json.loads(resp.read().decode("utf-8"))
    else:
        # 兜底：解 id_token payload（仅当无 userinfo 端点；生产建议优先 userinfo）
        idt = tokens.get("id_token")
        if idt:
            part = idt.split(".")[1]
            part += "=" * (-len(part) % 4)
            claims = json.loads(base64.urlsafe_b64decode(part))
    if not claims:
        return None
    sub = claims.get("sub") or claims.get("email")
    email = claims.get("email") or (sub if "@" in str(sub) else "")
    name = claims.get("name") or (email.split("@")[0] if email else "OIDCUser")
    return {"sub": sub, "email": email, "name": name}


def upsert_oidc_user(identity: Dict[str, str]):
    """按邮箱 / sub upsert 本地用户；返回 (user_dict, created)。"""
    username = identity.get("email") or identity.get("sub")
    if not username:
        return None, False
    u = auth.get_user(username)
    if u:
        return u, False
    u = auth.create_user(username, os.urandom(12).hex())
    return u, True
