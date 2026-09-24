"""操作审计日志端点（多租户问责）：谁在何时对哪个业务域做了什么。

从 server.py 抽出（D2 演进式拆分 R1）。ws_store 经 `s.` 引用，
确保走最新的业务域可见性判定（含组织树 + RBAC）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.core import db
from src.api.context import ctx as s

router = APIRouter()


@router.get("/audit")
def list_audit(request: Request, limit: int = 50, offset: int = 0,
               workspace_id: Optional[str] = None,
               since: Optional[str] = None, until: Optional[str] = None):
    """操作审计日志（多租户问责）：谁在何时对哪个业务域做了什么。

    隔离规则（与读写隔离同口径）：
    - 管理员：默认看全量，可按 workspace_id 过滤
    - 普通用户：只看「自己可见业务域」的操作 + 自己的认证类记录
      （auth.login / auth.logout / auth.register 等 workspace_id 为空的动作）
    - 匿名：401
    - 指定 workspace_id 时先校验可见性，越权/不存在 → 404（不暴露域是否存在）

    since/until：审计回放时间范围（ISO 字符串，按 ts 字典序过滤，兼容 ISO 格式）。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录才能查看审计日志")
    uid = user.get("uid")
    is_admin = bool(user.get("is_admin"))

    if workspace_id:
        if not s.ws_store.is_visible_to(workspace_id, user):
            raise HTTPException(status_code=404, detail="业务域不存在")
        where, params = "WHERE workspace_id=?", [workspace_id]
    elif is_admin:
        where, params = "", []
    else:
        visible = s.ws_store.visible_workspace_ids(user=user)
        if not visible:
            where, params = "WHERE 1=0", []
        else:
            ph = ",".join("?" * len(visible))
            # 域内操作 + 本人认证类记录（workspace_id 为空的登录/登出/注册）
            where = (f"WHERE (workspace_id IN ({ph}) "
                     "OR (workspace_id IS NULL AND actor_id=?))")
            params = list(visible) + [uid]

    if since or until:
        clauses = []
        if since:
            clauses.append("ts >= ?")
            params = list(params) + [since]
        if until:
            clauses.append("ts <= ?")
            params = list(params) + [until]
        where = f"{where} AND {' AND '.join(clauses)}" if where else f"WHERE {' AND '.join(clauses)}"

    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    rows = db.query(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (safe_limit, safe_offset))
    total = db.query_one(f"SELECT COUNT(*) AS c FROM audit_log {where}",
                         tuple(params))["c"]
    return {"items": [dict(r) for r in rows], "total": total,
            "limit": safe_limit, "offset": safe_offset,
            "scope": "all" if is_admin else "own"}
