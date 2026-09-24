"""系统类端点：服务信息 / 健康检查 / Prometheus 指标。

从 server.py 抽出（D2 演进式拆分 R1）。依赖的全局单例与 helper 经
`from src.api import server as s` 引用，保证始终取到当前实例（含热重载后的 tools/kb）。
"""
import os
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from src.config import DATA_DIR, load_llm_config
from src.core import db, metrics
from src.core import rate_limit as rl
from src.api.context import ctx as s

router = APIRouter()


# ---------------- Prometheus gauge 注册（幂等，随 DB 实时状态变化） ----------------
_METRICS_GAUGES_REGISTERED = False


def _register_metrics_gauges():
    """把数据库实时状态注册为 gauge（幂等：同名重复注册即覆盖）。"""
    global _METRICS_GAUGES_REGISTERED
    _METRICS_GAUGES_REGISTERED = True

    def ws_items():
        try:
            rows = db.query("SELECT id, mode FROM workspaces")
            return [({}, len(rows)), ] + [({"domain": r["id"], "mode": r["mode"]}, 1) for r in rows]
        except Exception:
            return [({}, 0)]

    def agent_items():
        try:
            rows = db.query("SELECT workspace_id, status FROM agents")
            return [({"domain": r["workspace_id"], "status": r["status"]}, 1) for r in rows]
        except Exception:
            return []

    def req_items():
        try:
            rows = db.query("SELECT status, workspace_id FROM requirements")
            out = [({"domain": r["workspace_id"], "status": r["status"]}, 1) for r in rows]
            if not out:
                out = [({}, 0)]
            return out
        except Exception:
            return [({}, 0)]

    metrics.gauge("teleops_workspaces_total", "业务域总数", ws_items)
    metrics.gauge("teleops_agents_total", "Agent 数（按域/状态）", agent_items)
    metrics.gauge("teleops_requirements_total", "需求数（按域/状态）", req_items)
    metrics.set_help("teleops_workspaces_total", "业务域总数")
    metrics.set_help("teleops_agents_total", "Agent 数（按域/状态）")
    metrics.set_help("teleops_requirements_total", "需求数（按域/状态）")


@router.get("/api/info")
def root():
    return {
        "service": "TeleOps 智能体平台",
        "version": s.VERSION,
        "llm_mode": s.llm.mode,
        "dispatch_mode": s.dispatch_mode["value"],
        "rate_limit": "on" if rl.ENABLED else "off",
        "uptime_s": int(time.time() - s._START_TS),
        "agents": [a["id"] for a in s.registry.list()],
        "adapters": [a["id"] for a in s.adapters.list()],
        "endpoints": [
            "/health", "/health/ready", "/metrics", "/api/info",
            "/workspaces",             "/workspaces/{id}", "/workspaces/{id}/mode",
            "/workspaces/{id}/agents", "/workspaces/{id} (DELETE)", "/jobs/{job_id}",
            "/agents", "/agents/{id}/diagnose", "/agents/{id}/build",
            "/dispatch/mode", "/requirements",
            "/adapters", "/adapters/alert/ingest",
            "/topology", "/tools", "/knowledge",
            "/alert", "/chat", "/feedback", "/closed-loop/run", "/traces",
            "/auth/status", "/workspaces/{id}/messages",
        ],
    }


@router.get("/health")
def health():
    """liveness 探活：进程存活 + 依赖概况。字段向后兼容（docker healthcheck 仍看 status==ok）。"""
    db_ok = False
    try:
        db.query_one("SELECT 1")
        db_ok = True
    except Exception:
        pass
    # D3：任务表已改成 JobStore（可能落在 Redis），遍历/加锁由存储自己负责，
    # 这里只问"有几个在跑"，不要再直接碰底层字典与它的锁。
    jobs_running = s._jobs.count_running()
    return {
        "status": "ok",
        "version": s.VERSION,
        "llm_provider": load_llm_config().get("provider", "mock"),
        "llm_mode": s.llm.mode,
        "nodes": len(s.cmdb.all_nodes()),
        "tools": len(s.tools.list_tools()),
        "db": "ok" if db_ok else "error",
        "uptime_s": int(time.time() - s._START_TS),
        "jobs_running": jobs_running,
        "rate_limit": "on" if rl.ENABLED else "off",
    }


@router.get("/health/ready")
def health_ready():
    """readiness 就绪：DB 可查询 + 数据目录可写才上报 ready（编排器据此决定是否引流）。"""
    db_ok = False
    try:
        db.query_one("SELECT 1")
        db_ok = True
    except Exception:
        pass
    dir_ok = os.access(DATA_DIR, os.W_OK)
    ready = db_ok and dir_ok
    return {
        "status": "ready" if ready else "not_ready",
        "version": s.VERSION,
        "db": "ok" if db_ok else "error",
        "data_dir_writable": dir_ok,
    }


@router.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本格式指标（公开端点，供抓取）。"""
    if not _METRICS_GAUGES_REGISTERED:
        _register_metrics_gauges()
    return Response(content=metrics.render(),
                    media_type="text/plain; version=0.0.4")
