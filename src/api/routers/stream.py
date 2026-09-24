"""模拟告警流水线（SSE 持续监控演示）端点 — D2 演进式拆分 R2，从 server.py 抽出。

handler 体逐字搬移，零行为变更；全局单例 / helper / 锁经 ctx 读取，模型从 ctx 别名。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.api.context import ctx as s

StreamStartReq = s.StreamStartReq

router = APIRouter()


@router.post("/stream/start")
def stream_start(req: StreamStartReq, request: Request):
    """启动模拟告警流水线：自动循环播放剧本，Agent 持续处置不停摆。

    v0.8.18：流水线按业务域隔离——req.workspace_id 决定启动哪条流，
    且要求当前用户对该域有写权限（公共域仅 admin、私有域仅 owner、匿名 403），
    与 Agent 增删改同一套多租户规则。启动者用户名记入 status 供前端展示。
    """
    if req.workspace_id and not s.ws_store.is_visible_to(
            req.workspace_id, getattr(request.state, "user", None)):
        raise HTTPException(status_code=404, detail="业务域不存在")
    user = getattr(request.state, "user", None)
    if req.workspace_id and not s.ws_store.is_writable_by(req.workspace_id, user):
        s._audit_write(request, "stream.start", req.workspace_id,
                     {"profile": req.profile, "mode": req.mode}, result="denied")
        raise HTTPException(
            status_code=403,
            detail="无权在该业务域启动告警流水线（公共域仅管理员，私有域仅所有者）")
    if req.profile not in ("mixed", "story"):
        raise HTTPException(status_code=400, detail="profile 必须为 mixed 或 story")
    if req.mode and req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    data = s.load_alerts()
    playlist = s.build_playlist(data.get("alerts", []), profile=req.profile)
    if not playlist:
        raise HTTPException(status_code=400, detail="剧本为空，请检查 data/alerts.json")
    ops_id, mode = s._stream_resolve_ctx(req.workspace_id, req.ops_agent_id, req.mode)
    # 临界区：把「已在运行检查 + 启动」做成原子，避免并发 stream_start 的 TOCTOU
    # 竞态导致同一域重复派发流水线（SSE 幂等）。
    with s._stream_op_lock:
        # D3：走执行器（默认线程执行器内部仍驱动 AlertStream，行为不变；
        # TELEOPS_STREAM_EXECUTOR=queue 时改由外部 worker 消费队列播放）
        if s.stream_executor.is_running(req.workspace_id):
            started_by = (s.stream_executor.status(req.workspace_id).get("started_by")
                          or "其他人")
            s._audit_write(request, "stream.start", req.workspace_id,
                         {"profile": req.profile, "already_by": started_by}, result="denied")
            raise HTTPException(
                status_code=409,
                detail=f"该业务域的告警流已在运行（由 {started_by} 启动），"
                       "请先停止再启动")
        # D3 修复：早期签发的 token 里只有标准声明 sub、没有 username，
        # 导致已登录用户的流水线被记成"由 匿名 启动"。按 username → sub 顺序取，
        # 兼容新旧 token（auth.issue_token 现已同时写入两个字段）。
        started_by = ((user or {}).get("username")
                      or (user or {}).get("sub") or "匿名")
        s.stream_executor.start(
            req.workspace_id, playlist,
            process=s._stream_make_processor(req.workspace_id, ops_id, mode,
                                             route_by_alert=req.ops_agent_id is None),
            profile=req.profile, interval_ms=req.interval_ms, loop=req.loop,
            ops_agent_id=ops_id, mode=mode, started_by=started_by)
    s._audit_write(request, "stream.start", req.workspace_id,
                 {"profile": req.profile, "mode": mode, "ops_agent_id": ops_id})
    return {"status": "running", "profile": req.profile, "ops_agent_id": ops_id,
            "mode": mode, "playlist_len": len(playlist),
            "workspace_id": req.workspace_id,
            "started_by": started_by,
            "detail": s.stream_executor.status(req.workspace_id)}


@router.post("/stream/stop")
def stream_stop(request: Request, workspace_id: Optional[str] = None):
    """停止流水线（幂等），返回最终统计。

    v0.8.18：按 workspace_id 停对应域的流（不传则停全局槽位）；写权限同启动，
    保证「谁的地盘谁能停」，admin 可停任何域（管理需要）。
    """
    if workspace_id and not s.ws_store.is_visible_to(
            workspace_id, getattr(request.state, "user", None)):
        raise HTTPException(status_code=404, detail="业务域不存在")
    if workspace_id and not s.ws_store.is_writable_by(
            workspace_id, getattr(request.state, "user", None)):
        s._audit_write(request, "stream.stop", workspace_id, {}, result="denied")
        raise HTTPException(status_code=403, detail="无权停止该业务域的告警流水线")
    was_running = s.stream_executor.is_running(workspace_id)
    s.stream_executor.stop(workspace_id)
    if was_running:
        s._audit_write(request, "stream.stop", workspace_id,
                     {"rounds": s.stream_executor.status(workspace_id).get("rounds", 0)})
    return {"status": "stopped",
            "detail": s.stream_executor.status(workspace_id)}


@router.post("/stream/reset-demo")
def stream_reset_demo(request: Request):
    """重置演示数据：清掉流内沉淀的工具，让「缺工具→造工具」可反复重演。

    只删除「全局工具库」里非内置（保留 ping_host / restart_service）的工具行，
    不动各业务域自有工具与消息栏历史。
    v0.8.18：清的是全局工具库 → 收紧为管理员专用，且先停掉所有域的流水线
    （避免边跑边清造成处置报错刷屏）。
    """
    user = getattr(request.state, "user", None)
    if not (user or {}).get("is_admin"):
        s._audit_write(request, "demo.reset", None, {}, result="denied")
        raise HTTPException(status_code=403, detail="重置演示数据仅管理员可用")
    # 停所有域的流水线：交给执行器（不再直接摸 _streams 这个进程内字典，
    # 队列模式下流根本不在这个字典里）
    running = s.stream_executor.stop_all()
    s.db.execute("DELETE FROM tools WHERE name NOT IN ('ping_host','restart_service') "
               "AND (workspace_id IS NULL OR workspace_id='')")
    # 一并清空需求看板，让「缺工具→造工具」闭环可从头重演，避免历史 REQ 干扰演示
    s.db.execute("DELETE FROM requirements")
    s._audit_write(request, "demo.reset", None, {"stopped_streams": running})
    return {"status": "reset", "stopped_streams": running,
            "tools": [r["name"] for r in s.db.query(
                "SELECT name FROM tools ORDER BY name")]}


@router.get("/stream/status")
def stream_status(request: Request, workspace_id: Optional[str] = None):
    """流水线运行状态 + 累计统计（前端秒级轮询）。

    v0.8.18：按 workspace_id 查对应域的流；越权查看他人域 → 404（不暴露存在）。
    """
    if not s._stream_key_visible(workspace_id, request):
        raise HTTPException(status_code=404, detail="业务域不存在")
    return s.stream_executor.status(workspace_id)


@router.get("/stream/feed")
def stream_feed(after: int = 0, request: Request = None,
                workspace_id: Optional[str] = None):
    """增量拉取处置流水（seq > after），供前端像监控大屏一样滚动渲染。"""
    if not s._stream_key_visible(workspace_id, request):
        raise HTTPException(status_code=404, detail="业务域不存在")
    return {"items": s.stream_executor.feed(workspace_id, after=after)}


@router.get("/stream/tasks")
def stream_tasks(limit: int = 50, agent_id: Optional[str] = None,
                 request: Request = None, workspace_id: Optional[str] = None):
    """作战室任务队列：把流水线告警按「分配运维 Agent → 处置 → 闭环」可视化。

    可选 agent_id 过滤只看某 Agent 的任务；limit 控制返回最近 N 条。
    """
    if not s._stream_key_visible(workspace_id, request):
        raise HTTPException(status_code=404, detail="业务域不存在")
    items = s.stream_executor.tasks(workspace_id, limit=limit, agent_id=agent_id)
    return {"tasks": items, "running": s.stream_executor.is_running(workspace_id)}
