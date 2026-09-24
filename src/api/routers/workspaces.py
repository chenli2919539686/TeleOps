"""业务域 / 工作空间（持久化）端点 — D2 演进式拆分 R2，从 server.py 抽出。

handler 体逐字搬移，零行为变更；全局单例 / helper 经 ctx 读取，模型从 ctx 别名。
"""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.api.context import ctx as s

MessageReq = s.MessageReq
CreateWorkspaceReq = s.CreateWorkspaceReq
UpdateAgentReq = s.UpdateAgentReq
CreateAgentReq = s.CreateAgentReq
ModeReq = s.ModeReq

router = APIRouter()


@router.get("/workspaces")
def list_workspaces(request: Request):
    """列出当前登录用户可见的业务域（公共域 + 本人私有域）。多租户隔离。
    未登录（匿名）时只返回公共域，避免暴露所有用户的个人域。"""
    user = getattr(request.state, "user", None)
    wss = s.ws_store.list(user=user)
    # 附带各业务域「待处理需求数」（未闭环、未驳回），供作战室悬浮卡展示
    for w in wss:
        w["pending"] = len([
            r for r in s.board.list(workspace_id=w["id"])
            if r.get("status") not in ("done", "rejected")
        ])
    return {"workspaces": wss}


@router.get("/workspaces/{ws_id}/messages")
def get_messages(ws_id: str, limit: int = 50):
    """按业务域拉取操作记录（最新在前），供消息栏「操作记录」tab 展示。"""
    rows = s.db.query(
        "SELECT * FROM messages WHERE workspace_id=? ORDER BY ts DESC LIMIT ?", (ws_id, limit))
    return {"messages": [dict(r) for r in rows]}


@router.post("/workspaces/{ws_id}/messages")
def post_message(ws_id: str, req: MessageReq):
    """写入一条操作记录（diagnose/build/gap）。受 Token 鉴权保护。"""
    ws = s.ws_store.get(ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"业务域 {ws_id} 不存在")
    agent_name = ""
    a = next((x for x in ws.get("agents", []) if x["id"] == req.agent_id), None)
    if a:
        agent_name = a["name"]
    entry = {
        "id": "M-" + uuid.uuid4().hex[:6],
        "workspace_id": ws_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent_id": req.agent_id,
        "agent_name": agent_name,
        "kind": req.kind,
        "summary": req.summary,
        "detail": req.detail,
    }
    s._save_message(entry)
    return entry


@router.post("/workspaces")
def create_workspace(req: CreateWorkspaceReq, request: Request):
    if req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    # 记录创建者（JWT 登录用户）与归属组织，便于后续组织树 + RBAC 隔离
    owner_id = None
    user = getattr(request.state, "user", None)
    if user and user.get("uid"):
        owner_id = user["uid"]
    org_id = user.get("org_id") if user else None
    try:
        ws = s.ws_store.create(req.name, req.adapter_id, req.mode, req.custom_id,
                            owner_id=owner_id, org_id=org_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    s._audit_write(request, "workspace.create", ws["id"],
                 {"name": req.name, "mode": req.mode, "owner_id": owner_id})
    return ws


@router.get("/workspaces/{ws_id}")
def get_workspace(ws_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_visible_to(ws_id, user):
        raise HTTPException(status_code=404, detail=f"业务域 {ws_id} 不存在")
    ws = s.ws_store.get(ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"业务域 {ws_id} 不存在")
    return ws


@router.put("/workspaces/{ws_id}/mode")
def set_workspace_mode(ws_id: str, req: ModeReq, request: Request):
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_writable_by(ws_id, user):
        s._audit_write(request, "workspace.mode", ws_id, {"mode": req.mode},
                     result="denied")
        raise HTTPException(status_code=403, detail="无权修改该业务域")
    if not s.ws_store.update_mode(ws_id, req.mode):
        raise HTTPException(status_code=400, detail="业务域不存在或 mode 非法")
    s._audit_write(request, "workspace.mode", ws_id, {"mode": req.mode})
    return {"id": ws_id, "mode": req.mode}


@router.post("/workspaces/{ws_id}/agents")
def create_agent(ws_id: str, req: CreateAgentReq, request: Request):
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_writable_by(ws_id, user):
        s._audit_write(request, "agent.create", ws_id, {"name": req.name, "kind": req.kind},
                     result="denied")
        raise HTTPException(status_code=403, detail="无权在该业务域下创建 Agent")
    try:
        agent = s.ws_store.add_agent(ws_id, req.kind, req.name, req.scope, req.description, req.primary)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    s._audit_write(request, "agent.create", ws_id,
                 {"agent": agent["id"], "name": req.name, "kind": req.kind})
    return agent


@router.put("/workspaces/{ws_id}/agents/{agent_id}")
def update_agent(ws_id: str, agent_id: str, req: UpdateAgentReq, request: Request):
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_writable_by(ws_id, user):
        s._audit_write(request, "agent.update", ws_id, {"agent": agent_id},
                     result="denied")
        raise HTTPException(status_code=403, detail="无权修改该业务域下的 Agent")
    if req.name:
        if not s.ws_store.rename_agent(ws_id, agent_id, req.name):
            raise HTTPException(status_code=404, detail="Agent 不存在")
    if req.scope is not None or req.description is not None:
        if not s.ws_store.update_agent(ws_id, agent_id, req.scope, req.description):
            raise HTTPException(status_code=404, detail="Agent 不存在")
    s._audit_write(request, "agent.update", ws_id,
                 {"agent": agent_id, "name": req.name,
                  "scope": req.scope, "description": req.description})
    return s.ws_store.get(ws_id)


@router.delete("/workspaces/{ws_id}")
def delete_workspace(ws_id: str, request: Request):
    """删除业务域（默认域受保护），并级联清理其下所有 Agent 实例。"""
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_writable_by(ws_id, user):
        s._audit_write(request, "workspace.delete", ws_id, {}, result="denied")
        raise HTTPException(status_code=403, detail="无权删除该业务域")
    try:
        ok, msg = s.ws_store.delete_workspace(ws_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    s._audit_write(request, "workspace.delete", ws_id, {"msg": msg})
    return {"deleted": ws_id}


@router.delete("/workspaces/{ws_id}/agents/{agent_id}")
def delete_agent(ws_id: str, agent_id: str, request: Request):
    user = getattr(request.state, "user", None)
    if not s.ws_store.is_writable_by(ws_id, user):
        s._audit_write(request, "agent.delete", ws_id, {"agent": agent_id},
                     result="denied")
        raise HTTPException(status_code=403, detail="无权删除该业务域下的 Agent")
    ok, msg = s.ws_store.delete_agent(ws_id, agent_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    s._audit_write(request, "agent.delete", ws_id, {"agent": agent_id})
    return {"deleted": agent_id, "workspace": ws_id}
