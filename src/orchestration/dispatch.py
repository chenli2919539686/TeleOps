"""派发器：连接 消息栏(RequirementBoard) 与 多 Agent 注册表(AgentRegistry)。

支持 自动 / 手动 两种模式：
  - 自动：需求产生后自动路由到研发 Agent 造工具，再造好自动派回"发起该需求的运维 Agent"。
  - 手动：需求进入消息栏 pending，由人工在界面点"派发研发" / "派发运维"逐步推进。

这把原来写死在 graphs.py 里的"全自动闭环"升级为带人工闸、可指定 Agent 的编排层。
"""
from typing import Optional
from datetime import datetime


def _log(req, step, msg):
    req.setdefault("trace", []).append(
        {"at": datetime.now().isoformat(timespec="seconds"), "step": step, "msg": msg}
    )


def raise_requirement(board, registry, alert, out, source_ops_agent_id, mode="auto", workspace_id=None):
    """运维 Agent 诊断后发现缺工具 → 在消息栏登记一条需求（pending）。"""
    needed = out.get("missing_tool", "")
    diag = out.get("diagnosis", {}) or {}
    concl = diag.get("conclusion", "")
    req = {
        "title": f"运维缺工具：{needed or '未知'}",
        "description": f"运维 Agent({source_ops_agent_id}) 根因结论：{concl}；"
                       f"需要工具 {needed} 但工具库缺失。",
        "source_ops_agent_id": source_ops_agent_id,
        "alert": alert,
        "tags": alert.get("tags", []),
        "needed_tool": needed,
        "mode": mode,
        "workspace_id": workspace_id,
        "status": "pending",
    }
    return board.add(req)


def dispatch_to_dev(board, registry, req_id, dev_agent_id=None, mode=None):
    """把需求派发给研发 Agent 造工具 + 注册 + 沉淀 SOP。"""
    req = board.get(req_id)
    if not req:
        return {"error": "requirement not found"}
    mode = mode or req.get("mode", "auto")
    dev_id = dev_agent_id or registry.route("dev", req)
    if not dev_id:
        return {"error": "no dev agent available"}
    dev_meta = registry.get(dev_id)
    dev = registry.get_instance(dev_id)
    registry.set_status(dev_id, "busy")
    _log(req, "dispatch_dev", f"派发给研发 Agent {dev_meta['name']} 造工具 {req['needed_tool']}")
    fb = {
        "feedback_id": f"FB-{req_id}",
        "summary": (
            f"运维 Agent 需要工具 {req['needed_tool']} 解决告警 "
            f"{req['alert'].get('alert_id')}。根因: {req['description']}。"
            f"请生成对应探测工具并注册。"
        ),
    }
    res = dev.fulfill_feedback(fb)
    created = res["tool"]["name"]
    board.update(req_id,
                 status="tool_ready",
                 assigned_dev_agent_id=dev_id,
                 created_tool_name=created,
                 dev_result=res,
                 trace=req.get("trace", []))
    registry.set_status(dev_id, "idle")
    return board.get(req_id)


def dispatch_to_ops(board, registry, req_id, ops_agent_id=None, mode=None):
    """工具造好后，把"用新工具重新处置"派回运维 Agent（默认派回发起方）。"""
    req = board.get(req_id)
    if not req:
        return {"error": "requirement not found"}
    mode = mode or req.get("mode", "auto")
    # 默认派回发起该需求的运维 Agent → 闭环回到原点（兜底也锁定同业务域）
    ops_id = (ops_agent_id or req.get("source_ops_agent_id")
              or registry.primary("ops", req.get("workspace_id")))
    ops_meta = registry.get(ops_id)
    ops = registry.get_instance(ops_id)
    if not ops:
        return {"error": "no ops agent available"}
    registry.set_status(ops_id, "busy")
    _log(req, "dispatch_ops",
         f"派发运维 Agent {ops_meta['name']} 用新工具 {req.get('created_tool_name')} 重新处置")
    out = ops.handle_alert(req["alert"])
    board.update(req_id,
                 status="done",
                 target_ops_agent_id=ops_id,
                 round2=out,
                 trace=req.get("trace", []))
    registry.set_status(ops_id, "idle")
    return board.get(req_id)
