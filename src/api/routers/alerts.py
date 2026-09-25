"""告警 / 对话 / 闭环端点（D2 演进式拆分 R2，从 server.py 抽出）。

handler 体逐字搬移，零行为变更；全局单例与 helper 经 ctx 读取，
请求模型在模块加载时从 ctx 别名（与 R1 约定一致，ctx 已由 server 填充）。
"""
from fastapi import APIRouter

from src.api.context import ctx as s

# 请求模型别名（模块加载时从 ctx 取）
AlertReq = s.AlertReq
ChatReq = s.ChatReq
FeedbackReq = s.FeedbackReq
ClosedLoopReq = s.ClosedLoopReq

router = APIRouter()


@router.get("/alerts")
def list_alerts(severity: str = "", noise: str = "", q: str = "", limit: int = 200):
    """列出「接入业务预设」的真实告警样本（data/alerts.json，5G 电信运维场景）。

    支持过滤：severity=info|critical、noise=true|false、q=关键字，供前端告警浏览器与调试使用。
    """
    data = s.load_alerts()
    alerts = data.get("alerts", [])
    sev_cnt: dict = {}
    noise_cnt = 0
    for a in alerts:
        sev_cnt[a.get("severity", "?")] = sev_cnt.get(a.get("severity", "?"), 0) + 1
        if a.get("is_noise"):
            noise_cnt += 1
    if severity:
        alerts = [a for a in alerts if a.get("severity") == severity]
    if noise in ("true", "false"):
        want = noise == "true"
        alerts = [a for a in alerts if bool(a.get("is_noise")) == want]
    if q:
        ql = q.strip().lower()
        alerts = [a for a in alerts
                  if ql in " ".join(str(a.get(k, "")) for k in ("alert_id", "metric", "host", "message")).lower()]
    return {
        "total": len(alerts),
        "source": "data/alerts.json · 5G 电信运维场景样本",
        "summary": {"all": len(data.get("alerts", [])), "severity": sev_cnt, "noise": noise_cnt},
        "alerts": alerts[: max(1, min(limit, 500))],
    }


@router.post("/alert")
def alert(req: AlertReq):
    job_id = s._start_job(lambda: s._alert_flow(req))
    return {"job_id": job_id, "status": "running"}


@router.post("/chat")
def chat(req: ChatReq):
    hits = s.kb.retrieve(req.question, top_k=req.top_k)
    context = "\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    prompt = (
        "[TASK:KBQA]\n"
        f"你是电信云网运维知识助手。仅基于知识库内容回答问题，不要编造。\n"
        f"问题: {req.question}\n知识库:\n{context}"
    )
    answer = s.llm.complete(prompt)
    return {"question": req.question, "answer": answer, "retrieved": hits}


@router.post("/feedback")
def feedback(req: FeedbackReq):
    fb = {"feedback_id": req.feedback_id, "summary": req.summary}
    # 自动触发研发 Agent：造工具 + 注册 + 沉淀 SOP（闭环自动化）
    # D5：实例统一经运行时工厂取（AgentRegistry 查不到时回退全局 dev，行为不变）
    res = s.runtime.dev_instance()[1].fulfill_feedback(fb)
    s._reload()
    s._save_trace("api_feedback", {"feedback": fb, "result": res})
    return {
        "feedback": fb,
        "created_tool": res["tool"],
        "sop": res["sop"],
        "note": "已自动注册工具并沉淀 SOP，可在 /tools 与 /knowledge 查看",
    }


@router.post("/closed-loop/run")
def closed_loop(req: ClosedLoopReq):
    job_id = s._start_job(lambda: s._closed_loop_flow(req))
    return {"job_id": job_id, "status": "running"}
