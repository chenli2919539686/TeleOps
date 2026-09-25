"""管理员运营端点 — 角色分配（让内置 RBAC 引擎真正可运营）。

说明：
- RBAC 引擎（src.core.auth.enforce）此前已完整，但路由层零处调用。本路由把
  「查看/分配/回收角色」做成可执行接口，使角色分化从死代码变成真能力。
- 所有端点统一经 deps.assert_perm 的 org.manage 权限闸（super_admin / org_admin 放行），
  与既有的 admin-only 闸（reset-demo / settings / approvals）保持正交、互不放松。
- 角色分配落 user_roles 表，立即影响该用户后续所有请求的 enforce 判定（JWT 重新签发后生效）。
"""
from typing import Optional
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request

from src.api.context import ctx as s
from src.api.deps import assert_perm
from src.core import auth

router = APIRouter()


class RoleAssignReq(BaseModel):
    username: str
    role_id: str
    action: str  # "assign" | "revoke"


@router.get("/admin/roles")
def list_roles(request: Request):
    """列出全部用户及其角色 + 内置角色权限矩阵（org.manage 权限闸）。"""
    assert_perm(request, "org.manage", action="admin.roles.list")
    return {"users": auth.list_users_roles(),
            "roles": auth.list_builtin_roles()}


@router.post("/admin/roles")
def manage_role(req: RoleAssignReq, request: Request):
    """分配或回收用户角色（org.manage 权限闸）。

    - action=assign：绑定一个内置角色（如把某 sre 升级为 org_admin）。
    - action=revoke：回收角色；内置两道安全闸——禁止直接吊销 super_admin、
      且不会让系统内最后一个管理员失去权限（防锁死）。
    """
    assert_perm(request, "org.manage", action="admin.roles.manage", target=req.username)
    if req.action == "assign":
        ok = auth.assign_role(req.username, req.role_id)
        if not ok:
            raise HTTPException(status_code=400,
                                detail="用户或角色不存在（角色须为内置角色）")
        s._audit_write(request, "admin.roles.assign", req.username,
                     {"role_id": req.role_id})
        return {"status": "assigned", "username": req.username, "role_id": req.role_id}
    if req.action == "revoke":
        ok = auth.revoke_role(req.username, req.role_id)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="回收失败：用户不存在，或禁止吊销 super_admin，"
                       "或会导致系统内无剩余管理员（已自动回滚）")
        s._audit_write(request, "admin.roles.revoke", req.username,
                     {"role_id": req.role_id})
        return {"status": "revoked", "username": req.username, "role_id": req.role_id}
    raise HTTPException(status_code=400, detail="action 必须为 assign 或 revoke")
