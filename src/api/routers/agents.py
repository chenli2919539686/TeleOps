"""多 Agent 矩阵 + 消息栏（人工 / 自动派发闭环）端点 — D2 演进式拆分 R2，从 server.py 抽出。

handler 体逐字搬移，零行为变更；全局单例 / helper 经 ctx 读取，模型从 ctx 别名。
"""
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Request

from src.api.context import ctx as s

ModeReq = s.ModeReq
RaiseReq = s.RaiseReq
DispatchReq = s.DispatchReq
AlertReq = s.AlertReq
FeedbackReq = s.FeedbackReq
GapRegisterReq = s.GapRegisterReq

router = APIRouter()


@router.get("/agents")
def agents(workspace_id: Optional[str] = None, request: Request = None):
    """返回 Agent 列表（含实时 status：idle/busy/error）。传 workspace_id 可按业务域过滤。

    多租户隔离：仅返回当前登录用户「可见业务域」下的 Agent（公共域 + 本人私有域）；
    未登录（匿名）时只返回公共域，避免暴露所有用户的个人域。
    """
    user = getattr(request.state, "user", None) if request else None
    visible = set(s.ws_store.visible_workspace_ids(user=user))
    items = s.registry.list(workspace_id=workspace_id)
    items = [a for a in items if a.get("workspace_id") in visible]
    return {
        "agents": items,
        "primary_ops": s.registry.primary("ops", workspace_id) if workspace_id else None,
        "primary_dev": s.registry.primary("dev", workspace_id) if workspace_id else None,
    }


@router.get("/dispatch/mode")
def get_mode():
    return {"mode": s.dispatch_mode["value"]}


@router.post("/dispatch/mode")
def set_mode(req: ModeReq):
    if req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    s.dispatch_mode["value"] = req.mode
    return {"mode": req.mode}


@router.get("/requirements")
def get_requirements(status: Optional[str] = None, workspace_id: Optional[str] = None):
    return {"requirements": s.board.list(status, workspace_id)}


@router.post("/requirements/raise")
def post_requirement(req: RaiseReq):
    """运维 Agent 诊断后把"工具缺口"登记进消息栏；自动模式下直接跑完闭环。"""
    ws_id = req.workspace_id
    ops_id = req.ops_agent_id or s._ws_primary_ops(ws_id)
    ops_inst = s.registry.get_instance(ops_id)
    if not ops_inst:
        raise HTTPException(status_code=400, detail=f"ops agent {ops_id} 不存在")
    alert_obj = s._resolve_alert_obj(req.alert, req.alert_id)
    # 业务域存在则用域内模式；否则退回全局模式（避免 None["mode"] 抛 500）
    mode = s.ws_store.get(ws_id)["mode"] if (ws_id and s.ws_store.get(ws_id)) else s.dispatch_mode["value"]
    job_id = s._start_job(lambda: s._raise_flow(ops_id, alert_obj, mode, ws_id))
    return {"job_id": job_id, "status": "running"}


@router.post("/requirements/{req_id}/dispatch-dev")
def post_dispatch_dev(req_id: str, req: DispatchReq):
    """手动模式：把需求派发给指定（或路由选中）的研发 Agent 造工具。"""
    def _flow():
        res = s.dispatch_mod.dispatch_to_dev(
            s.board, s.registry, req_id, dev_agent_id=req.agent_id, mode=req.mode)
        # 造完工具刷新工具库/知识库视图，运维侧立即可复用（含 SOP 检索）
        s._reload_all()
        return res
    job_id = s._start_job(_flow)
    return {"job_id": job_id, "status": "running"}


@router.post("/requirements/{req_id}/dispatch-ops")
def post_dispatch_ops(req_id: str, req: DispatchReq):
    """手动模式：工具造好后，派回指定（或发起方）运维 Agent 重新处置。"""
    job_id = s._start_job(lambda: s.dispatch_mod.dispatch_to_ops(
        s.board, s.registry, req_id, ops_agent_id=req.agent_id, mode=req.mode))
    return {"job_id": job_id, "status": "running"}


@router.post("/agents/{agent_id}/diagnose")
def agent_diagnose(agent_id: str, req: AlertReq):
    """运维 Agent 工作台：运行该 Agent 的告警根因分析（job 化，状态灯实时联动）。"""
    a = s.registry.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    if a["kind"] != "ops":
        raise HTTPException(status_code=400, detail=f"{agent_id} 不是运维 Agent")
    inst = s.registry.get_instance(agent_id)

    def flow():
        alert_obj = s._resolve_alert_obj(req.alert, req.alert_id)
        s.registry.set_status(agent_id, "busy")
        try:
            return inst.handle_alert(alert_obj)
        finally:
            s.registry.set_status(agent_id, "idle")

    job_id = s._start_job(flow)
    return {"job_id": job_id, "status": "running"}


@router.post("/agents/{agent_id}/build")
def build_agent(agent_id: str, feedback: Dict[str, Any], request: Request):
    """研发 Agent 工作台：运行该 Agent 的造工具流程（job 化，状态灯实时联动）。

    企业级人工闸（HITL）：开启 require_approval 时，高风险动作不直接执行，而是落
    pending 审批单，由管理员批准后才真正执行（与 stream.start 同一套审批管线）。
    实际执行逻辑在 server._run_agent_build（与审批批准共用）。
    """
    if s.get_require_approval():
        actor, _ = s._actor_of(request)
        apr_id = s.approvals.create(
            subject="tool.build", requested_by=actor,
            payload={"agent_id": agent_id, "feedback": feedback},
            detail={"agent": agent_id, "feedback": feedback.get("feedback_id"),
                    "summary": (feedback.get("summary") or "")[:120]})
        s._audit_write(request, "tool.build", None,
                       {"agent": agent_id, "pending": apr_id}, result="pending")
        return {"job_id": None, "status": "pending_approval", "approval_id": apr_id,
                "note": "已提交人工审批，管理员批准后才会真正造工具"}
    return s._run_agent_build(agent_id, feedback)


@router.post("/agents/{agent_id}/register-gap")
def agent_register_gap(agent_id: str, req: GapRegisterReq):
    """工作台诊断出工具缺口后，把缺口登记进当前业务域消息栏并按模式派发（打通工作台→闭环）。

    前端在 /agents/{id}/diagnose 跑出 missing_tool 后，带诊断结果回传本接口，
    避免重复推理；登记的需求归属该 Agent 所在业务域，自动模式即跑完研发→回传闭环。
    """
    a = s.registry.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    if a["kind"] != "ops":
        raise HTTPException(status_code=400, detail=f"{agent_id} 不是运维 Agent")
    if not req.missing_tool:
        raise HTTPException(status_code=400, detail="无工具缺口，无需登记")
    ws_id = a.get("workspace_id")
    alert_obj = s._resolve_alert_obj(req.alert, req.alert_id)
    mode = req.mode or (s.ws_store.get(ws_id)["mode"] if (ws_id and s.ws_store.get(ws_id))
                        else s.dispatch_mode["value"])
    out = {"missing_tool": req.missing_tool, "diagnosis": req.diagnosis or {}}
    job_id = s._start_job(lambda: s._raise_flow(agent_id, alert_obj, mode, ws_id, out=out))
    return {"job_id": job_id, "status": "running"}
