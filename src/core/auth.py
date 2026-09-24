"""每用户鉴权 + RBAC 权限判定：自实现 HS256 JWT + pbkdf2 口令哈希，用户/组织/角色落 SQLite。

Phase 1 升级：
- 登录令牌把 ``org_id`` / ``roles`` / ``perms`` 一并写入 JWT（声明式），路由无需额外查库即可做
  组织树 + 权限判定（auth.enforce）。
- 多租户从「个人域」升级为「组织树 + 标准 RBAC」：用户归属 org_units 节点，角色绑定权限，
  can(user, perm, org_id) 按「本人 org 及其子孙 org」+ 权限集合判定。
- 仍零额外依赖（无 PyJWT / passlib）；签名密钥取 TELEOPS_JWT_SECRET，未设回退开发默认。

向后兼容：is_admin=True 的用户自动拥有 super_admin 角色（全部权限）；JWT 未带 org/perms 的
旧令牌在 enforce 时回退查库。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Optional, Dict, Any, List

from src.core import db

JWT_SECRET = (os.environ.get("TELEOPS_JWT_SECRET")
              or os.environ.get("TELEOPS_API_TOKEN")
              or "dev-insecure-secret-change-me")
JWT_EXP_SECONDS = int(os.environ.get("TELEOPS_JWT_EXP", "604800"))  # 默认 7 天

# ---------------- JWT 注销（黑名单） ----------------
_REVOKED_FILE = os.environ.get(
    "TELEOPS_REVOKED_FILE",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "jwt_revoked.json"))
_REVOKED: Dict[str, int] = {}  # jti -> exp（绝对过期时间戳，便于定期清理）


def _load_revoked() -> None:
    global _REVOKED
    try:
        with open(_REVOKED_FILE, "r", encoding="utf-8") as f:
            _REVOKED = json.load(f)
    except Exception:
        _REVOKED = {}
    now = int(time.time())
    _REVOKED = {k: v for k, v in _REVOKED.items() if v > now}


def _save_revoked() -> None:
    try:
        d = os.path.dirname(_REVOKED_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_REVOKED_FILE, "w", encoding="utf-8") as f:
            json.dump(_REVOKED, f)
    except Exception:
        pass


_load_revoked()


# ---------------- base64url 工具 ----------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


# ---------------- JWT ----------------
def encode_token(payload: Dict[str, Any], exp_seconds: int = JWT_EXP_SECONDS) -> str:
    """对任意 payload 签名（声明式：调用方负责放入 sub/uid/org_id/perms 等）。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = dict(payload)
    body["iat"] = now
    body["jti"] = secrets.token_hex(8)  # 唯一标识，供登出黑名单吊销
    body["exp"] = now + exp_seconds
    seg1 = _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    seg2 = _b64u(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(JWT_SECRET.encode("utf-8"), f"{seg1}.{seg2}".encode("utf-8"),
                   hashlib.sha256).digest()
    seg3 = _b64u(sig)
    return f"{seg1}.{seg2}.{seg3}"


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """校验签名与有效期，返回 payload；失败返回 None。仅接受 HS256。"""
    try:
        seg1, seg2, seg3 = token.split(".")
    except Exception:
        return None
    expected = hmac.new(JWT_SECRET.encode("utf-8"), f"{seg1}.{seg2}".encode("utf-8"),
                        hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _b64d(seg3)):
            return None
        body = json.loads(_b64d(seg2))
    except Exception:
        return None
    if body.get("exp", 0) < int(time.time()):
        return None
    jti = body.get("jti")
    if jti and jti in _REVOKED:
        return None  # 已登出/吊销
    return body


def revoke_token(token: str) -> bool:
    """把指定 JWT 加入黑名单（吊销）。成功返回 True，无效/无法解析返回 False。"""
    body = decode_token(token)
    if not body:
        return False
    jti = body.get("jti")
    if not jti:
        return False
    _REVOKED[jti] = int(body.get("exp", time.time() + JWT_EXP_SECONDS))
    _save_revoked()
    return True


def issue_token(username: str) -> str:
    """登录/注册成功时签发令牌：把用户身份 + 组织 + 角色权限写进声明。"""
    u = get_user(username)
    if not u:
        raise ValueError(f"用户不存在：{username}")
    claims = {
        "sub": u["username"],
        "uid": u["id"],
        "is_admin": u["is_admin"],
        "org_id": u.get("org_id"),
        "roles": u.get("roles"),
        "perms": u.get("perms"),
    }
    return encode_token(claims)


# ---------------- 口令哈希 ----------------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()
    return f"{salt}:{dk}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, dk = stored.split(":")
        return hmac.compare_digest(
            dk, hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                    bytes.fromhex(salt), 100_000).hex())
    except Exception:
        return False


# ---------------- 组织 / 角色 助手 ----------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]", "-", name).strip("-").lower()
    return s or "org"


def _ensure_personal_org(username: str) -> str:
    """为普通用户建一个个人组织叶节点（挂在根组织下），返回 org_id。幂等。"""
    root_path = db.org_path_of(db.ROOT_ORG_ID) or ("/" + db.ROOT_ORG_ID)
    org_id = f"org-{_slug(username)}"
    n = 2
    while db.query_one("SELECT 1 FROM org_units WHERE id=?", (org_id,)):
        org_id = f"org-{_slug(username)}-{n}"
        n += 1
    if not db.query_one("SELECT 1 FROM org_units WHERE id=?", (org_id,)):
        db.execute(
            "INSERT INTO org_units (id,name,parent_id,path,level,org_type) VALUES (?,?,?,?,?,?)",
            (org_id, f"{username} 的组织", db.ROOT_ORG_ID,
             root_path + "/" + org_id, 1, "team"))
    return org_id


def user_roles(user_id: int) -> List[str]:
    rows = db.query("SELECT role_id FROM user_roles WHERE user_id=?", (user_id,))
    return [r["role_id"] for r in rows]


def user_perms(user_id: int) -> List[str]:
    rows = db.query(
        "SELECT DISTINCT rp.permission FROM user_roles ur "
        "JOIN role_permissions rp ON rp.role_id=ur.role_id WHERE ur.user_id=?",
        (user_id,))
    return [r["permission"] for r in rows]


def _user_record(r: dict) -> Dict[str, Any]:
    uid = r["id"]
    return {
        "id": uid,
        "username": r["username"],
        "is_admin": bool(r["is_admin"]),
        "org_id": r.get("org_id"),
        "roles": user_roles(uid),
        "perms": user_perms(uid),
    }


# ---------------- RBAC 判定 ----------------
def enforce(user: Optional[Dict[str, Any]], perm: str, org_id: Optional[str] = None) -> bool:
    """can(user, perm, org_id)：标准 RBAC + 组织树自上而下可见性。

    - 未登录 → False。
    - super_admin（is_admin）→ 全部放行。
    - 权限不在用户权限集合 → False。
    - org_id 为 None（资源不绑定具体组织，如本人认证记录）→ 仅校验权限。
    - 否则要求用户 org 是目标 org 的祖先或自身（物化 path 前缀判定）。
    旧令牌若未带 perms/org_id，回退查库。
    """
    if not user:
        return False
    if user.get("is_admin"):
        return True
    perms = user.get("perms")
    if perms is None:
        u = get_user(user.get("sub")) if user.get("sub") else None
        perms = u["perms"] if u else []
    if perm not in perms:
        return False
    if org_id is None:
        return True
    upath = db.org_path_of(user["org_id"]) if user.get("org_id") else None
    if upath is None and user.get("sub"):
        u = get_user(user.get("sub"))
        if u:
            upath = db.org_path_of(u.get("org_id"))
    opath = db.org_path_of(org_id)
    if not upath or not opath:
        return False
    # 自上而下：用户能看到其所属 org 及所有子孙 org 的资源
    return opath.startswith(upath)


# ---------------- 用户 CRUD ----------------
def user_count() -> int:
    return db.query_one("SELECT COUNT(*) AS c FROM users")["c"]


def create_user(username: str, password: str, is_admin: bool = False,
                org_id: Optional[str] = None) -> Dict[str, Any]:
    # 第一个注册的用户自动成为管理员（归入根组织 + super_admin）
    is_admin = is_admin or (user_count() == 0)
    if org_id is None:
        org_id = db.ROOT_ORG_ID if is_admin else _ensure_personal_org(username)
    cur = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, org_id, created_at) "
        "VALUES (?,?,?,?,?)",
        (username, hash_password(password), 1 if is_admin else 0, org_id, db._now()))
    uid = cur.lastrowid
    role = "super_admin" if is_admin else "sre"
    db.execute("INSERT OR IGNORE INTO user_roles (user_id,role_id) VALUES (?,?)",
               (uid, role))
    return get_user(username)


def get_user(username: str) -> Optional[Dict[str, Any]]:
    r = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if not r:
        return None
    return _user_record(r)


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    r = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if not r:
        return None
    if not verify_password(password, r["password_hash"]):
        return None
    return _user_record(r)
