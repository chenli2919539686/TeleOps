"""API 层鉴权依赖：RBAC 权限闸。

这是路由层**唯一**的权限判定入口，所有「这个动作用户有没有资格做」都应经此，
不要再在 handler 里手写 is_admin / perms 判断（那是鉴权中间件的职责，这里是授权）。

设计：
- 底层判定全部复用 ``src.core.auth.enforce``（super_admin 全放行 + 权限集合校验 +
  组织树前缀可见性），本文件只负责「无权即 403 + 留痕」。
- 越权访问会写一条 audit（result=denied），与多租户租户闸保持同一套可观测口径，
  便于事后回溯「谁在什么时候撞了哪个权限」。
- 与多租户租户闸（``ws_store.is_writable_by`` / ``is_visible_to``）是**正交**的两层：
  租户闸管「这个资源是不是你的」，RBAC 闸管「你的角色有没有这个能力」。
  例如 viewer 对自己域可见（租户闸过），但没有 tool.exec（RBAC 闸拦），因此看不到
  「启动告警流」按钮的能力——这正是方法论要求的角色分化。
"""
from fastapi import HTTPException
from src.core import auth
from src.api.context import ctx as s


def assert_perm(request, perm: str, org_id: str = None,
                action: str = "rbac.deny", target=None):
    """权限闸：无 ``perm`` 权限即抛 403，并留审计痕。

    :param request: FastAPI Request（从中取 ``request.state.user``，由鉴权中间件填充）。
    :param perm: 权限点，如 ``tool.exec`` / ``agent.manage`` / ``org.manage``。
    :param org_id: 资源绑定的组织 id；None 表示只校验权限集合（不限定组织树）。
                  注意：业务域 workspace 不是 org_units 节点，执行类端点传 None 即可，
                  组织树维度由多租户租户闸单独负责。
    :param action: 审计动作名，便于日志区分是哪个端点触发的越权。
    :param target: 审计目标（域 id / 用户名等）。
    :raises HTTPException: 403 权限不足。
    """
    user = getattr(request.state, "user", None) if request else None
    if not auth.enforce(user, perm, org_id):
        try:
            s._audit_write(request, action, target,
                         {"perm": perm, "org_id": org_id}, result="denied")
        except Exception:
            # 审计是旁路，绝不能因为审计写失败而阻断正常鉴权判定
            pass
        raise HTTPException(
            status_code=403,
            detail=f"权限不足：需要 {perm}" + (f"（组织 {org_id}）" if org_id else ""))
    return True
