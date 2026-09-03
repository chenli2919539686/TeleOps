"""每用户鉴权：用标准库实现 HS256 JWT + pbkdf2 口令哈希，用户表落在 SQLite。

不引入额外依赖（无 PyJWT / passlib）：JWT 用 hmac+sha256+base64url 自实现，
口令用 hashlib.pbkdf2_hmac。签名密钥取环境变量 TELEOPS_JWT_SECRET，
未设置时回退到 TELEOPS_API_TOKEN，再回退到开发默认（仅本地 Demo 用，生产务必设置）。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Dict, Any

from src.core import db

JWT_SECRET = (os.environ.get("TELEOPS_JWT_SECRET")
              or os.environ.get("TELEOPS_API_TOKEN")
              or "dev-insecure-secret-change-me")
JWT_EXP_SECONDS = int(os.environ.get("TELEOPS_JWT_EXP", "604800"))  # 默认 7 天


# ---------------- base64url 工具 ----------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


# ---------------- JWT ----------------
def encode_token(payload: Dict[str, Any], exp_seconds: int = JWT_EXP_SECONDS) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = dict(payload)
    body["iat"] = now
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
    return body


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


# ---------------- 用户 CRUD ----------------
def user_count() -> int:
    return db.query_one("SELECT COUNT(*) AS c FROM users")["c"]


def create_user(username: str, password: str, is_admin: bool = False) -> Dict[str, Any]:
    # 第一个注册的用户自动成为管理员
    is_admin = is_admin or (user_count() == 0)
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
        (username, hash_password(password), 1 if is_admin else 0, db._now()))
    return get_user(username)


def get_user(username: str) -> Optional[Dict[str, Any]]:
    r = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if not r:
        return None
    return {"id": r["id"], "username": r["username"],
            "is_admin": bool(r["is_admin"]), "created_at": r["created_at"]}


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    r = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if not r:
        return None
    if not verify_password(password, r["password_hash"]):
        return None
    return {"id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"])}
