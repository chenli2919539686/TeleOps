"""TeleOps W3：FastAPI 后端，把底层能力 + 双 Agent 暴露为 HTTP 接口。

这一层让项目从"命令行玩具"变成"可被前端/外部系统调用的服务（台子成型）"。

端点：
  GET  /                服务信息（含 LLM 模式、可用端点）
  GET  /health          健康检查（节点数、工具数、LLM 模式）
  GET  /topology       CMDB 拓扑（节点 + 依赖边）
  GET  /tools          工具库列表
  POST /tools/call     调用指定工具（带风险拦截）
  GET  /knowledge      知识库检索
  POST /alert          运维 Agent 处理单条告警（降噪+根因+工具+处置，LangGraph 编排）
  POST /chat           RAG 知识问答
  POST /feedback       提交反馈工单 -> 自动触发研发 Agent 造工具+沉淀 SOP（闭环自动化）
  POST /closed-loop/run 跑完整"运维缺工具 -> 研发造工具 -> 复用"闭环
  GET  /traces         列出可观测 trace

启动：python -m uvicorn src.api.server:app --reload --port 8000
文档：浏览器打开 http://localhost:8000/docs （Swagger 自动生成）
"""
import sys
import os
import json
import time
import threading
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

# 让项目根（含 src）进入导入路径
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from src.config import TOPOLOGY_FILE, ALERTS_FILE, TRACE_DIR, DATA_DIR
from src.core.cmdb_graph import CMDBGraph
from src.core.kb_store import KBStore
from src.core.tool_registry import ToolRegistry
from src.core.agent_registry import AgentRegistry
from src.core.workspace_store import WorkspaceStore
from src.core.requirement_board import RequirementBoard
from src.core import db
from src.core import auth
from src.core import metrics
from src.core import rate_limit as rl
from src.core.alert_stream import AlertStream, build_playlist
from src.llm_client import LLMClient
from src.agents.ops_agent import OpsAgent
from src.agents.dev_agent import DevAgent
from src.orchestration.graphs import build_ops_graph, build_dev_graph
from src.orchestration import dispatch as dispatch_mod
from src.adapters.registry import AdapterRegistry

app = FastAPI(title="TeleOps 智能体平台", version="0.8.1")

VERSION = "0.8.1"
_START_TS = time.time()   # 进程启动时刻（/health uptime_s、metrics 已含 uptime）

# ---------------- 安全：CORS 白名单（取代原先的 allow_origins=["*"]） ----------------
# 默认仅放行本地前端（8001）；生产/Spaces 部署请通过 TELEOPS_CORS_ORIGINS 显式放行域名，
# 例如：TELEOPS_CORS_ORIGINS="https://xxx.hf.space,https://your.domain"
CORS_ORIGINS = [o.strip() for o in os.environ.get("TELEOPS_CORS_ORIGINS", "").split(",") if o.strip()]
if not CORS_ORIGINS:
    CORS_ORIGINS = ["http://localhost:8001", "http://127.0.0.1:8001"]
# 含通配符时不携带凭证，避免浏览器拒绝凭证 + 通配符的组合
ALLOW_CREDENTIALS = "*" not in CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 安全：写接口 Token 鉴权（取代原先的零鉴权） ----------------
# 设置环境变量 TELEOPS_API_TOKEN 后，所有 POST/PUT/DELETE/PATCH 必须携带
#   Authorization: Bearer <token>   或   X-API-Token: <token>
# 未设置该变量时（本地开发默认）完全开放，行为与旧版一致（向后兼容）。
API_TOKEN = os.environ.get("TELEOPS_API_TOKEN", "").strip()
AUTH_REQUIRED = bool(API_TOKEN)
_PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc",
                 "/auth/status", "/auth/register", "/auth/login"}


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 所有请求都尝试解析 JWT（供 /auth/me 等读取身份）；写接口才强制校验
        path = request.url.path
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") \
            else request.headers.get("X-API-Token", "").strip()
        user = auth.decode_token(token) if token else None
        if user:
            request.state.user = user
        elif AUTH_REQUIRED and token and token == API_TOKEN:
            request.state.user = {"sub": "service", "is_admin": True}

        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if path not in _PUBLIC_PATHS and not path.startswith("/static"):
                if not getattr(request.state, "user", None):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "未授权：请先登录（界面右上角）获取 Token，或在「设置」中填入 API Token"},
                    )
        return await call_next(request)


class _MetricsMiddleware(BaseHTTPMiddleware):
    """采集 HTTP 请求数与耗时（按路由模板聚合，避免路径参数造成标签爆炸）。"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/metrics":
            return await call_next(request)
        t0 = time.time()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            tmpl = getattr(route, "path", None) or path
            metrics.inc("teleops_http_requests_total",
                        method=request.method, path=tmpl, status=str(status))
            metrics.observe_seconds("teleops_http_request_duration_seconds",
                                    time.time() - t0, path=tmpl)


app.add_middleware(_AuthMiddleware)
app.add_middleware(_MetricsMiddleware)

# 限流放最外层：连鉴权失败/登录爆破也先被限流（登录路径在 _PUBLIC_PATHS 不鉴权，
# 必须由本层独立拦截）。429 由本层直接返回，不穿过 Metrics/Auth，故自记独立计数器。
# 配置项见 src/core/rate_limit.py：TELEOPS_RATE_LIMIT=on|off（默认 on），
# TELEOPS_RATE_LIMIT_READ/WRITE/LOGIN 调整读/写/登录每分钟限额。
_STATIC_SUFFIXES = (".css", ".js", ".ico", ".png", ".svg", ".jpg", ".jpeg",
                    ".gif", ".woff", ".woff2", ".ttf", ".map", ".html")
_LOW_FREQ_PATHS = {"/", "/metrics", "/health", "/health/ready", "/api/info",
                   "/docs", "/openapi.json", "/redoc",
                   "/stream/status", "/stream/feed"}  # 演示流状态轮询不计读额度


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """滑动窗口限流（按客户端 IP；分读/写/登录三档），防误用与口令爆破。"""

    async def dispatch(self, request, call_next):
        if not rl.ENABLED:
            return await call_next(request)
        path = request.url.path
        # 静态资源与低频内部端点放行，不占 API 额度
        if path in _LOW_FREQ_PATHS or path.startswith("/static") or \
                path.split("?")[0].lower().endswith(_STATIC_SUFFIXES):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        if path in ("/auth/login", "/auth/register"):
            limit, key = rl.LOGIN_LIMIT, f"login:{client}"
        elif request.method in ("POST", "PUT", "DELETE", "PATCH"):
            limit, key = rl.WRITE_LIMIT, f"write:{client}"
        else:
            limit, key = rl.READ_LIMIT, f"read:{client}"
        ok, retry = rl.allow(key, limit)
        if not ok:
            # 顶层路径段做标签，基数有限（auth/workspaces/agents/...），防标签爆炸
            top = path.strip("/").split("/")[0] or "root"
            metrics.inc("teleops_rate_limited_total", method=request.method, path=top)
            return JSONResponse(
                status_code=429,
                content={"detail": f"请求过于频繁，请 {retry} 秒后重试"},
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)


metrics.set_help("teleops_rate_limited_total", "被限流拒绝的请求数（按方法/路径段）")
app.add_middleware(_RateLimitMiddleware)


# ---------------- 全局单例：启动时构建一次 ----------------
cmdb = CMDBGraph()
kb = KBStore()
llm = LLMClient()
tools = ToolRegistry()
ops = OpsAgent(cmdb, kb, tools, llm)
dev = DevAgent(cmdb, kb, llm)
ops_graph = build_ops_graph(ops)

# ---------------- 多 Agent 注册表 + 业务域 + 消息栏需求看板 ----------------
# 业务域从 data/workspaces.json 加载；首次运行初始化「核心网运维域」含 4 个 Agent。
# 每个域独立配一套运维 + 研发 Agent，可经前端作战室创建 / 命名 / 持久化。
registry = AgentRegistry(cmdb, kb, tools, llm)
ws_store = WorkspaceStore(registry=registry)
registry.ws_store = ws_store   # 让 registry.set_status 能持久化到 SQLite
board = RequirementBoard()
dispatch_mode = {"value": "auto"}   # 自动 / 手动，可经 /dispatch/mode 切换
adapters = AdapterRegistry()        # 外部系统适配器注册表（含预留接口）
stream = AlertStream()              # 模拟告警流水线（持续监控演示），处置回调在 start 时注入


# ---------------- 异步任务（让 Agent 运行时 busy 态可被前端实时轮询看到） ----------------
_jobs: Dict[str, Any] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 200          # 防止长时间运行内存泄漏：保留最近 200 个任务
_JOBS_TTL = 3600         # 已完成任务 1 小时后清理


def _start_job(fn):
    """把重操作放到后台线程执行；期间对应 Agent 在 registry 中标记 busy，
    前端可经 /jobs/{job_id} 轮询进度，作战室状态灯随之实时刷新。"""
    job_id = uuid.uuid4().hex[:8]
    metrics.inc("teleops_jobs_total")
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "result": None, "error": None, "ts": time.time()}
        # 超过上限时清理最旧的任务
        if len(_jobs) > _JOBS_MAX:
            oldest = sorted(_jobs.keys(), key=lambda k: _jobs[k].get("ts", 0))[:len(_jobs) - _JOBS_MAX]
            for k in oldest:
                del _jobs[k]

    def _run():
        try:
            _jobs[job_id]["result"] = fn()
            _jobs[job_id]["status"] = "done"
        except Exception as e:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
        finally:
            _jobs[job_id]["ts"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _gc_jobs():
    """清理已结束且超过 TTL 的任务，避免内存无限增长。"""
    now = time.time()
    with _jobs_lock:
        expired = [k for k, v in _jobs.items()
                   if v["status"] in ("done", "error") and now - v.get("ts", 0) > _JOBS_TTL]
        for k in expired:
            del _jobs[k]


def _ws_primary_ops(ws_id):
    """取某业务域内主运维 Agent；无则回退全局主运维。"""
    agents = registry.list(kind="ops", workspace_id=ws_id)
    if agents:
        for a in agents:
            if a.get("primary"):
                return a["id"]
        return agents[0]["id"]
    return registry.primary("ops")


def _reload():
    """工具/知识被改后重新加载，并让运维 Agent 指向最新实例（闭环后再读最新状态）。"""
    global tools, kb
    with _jobs_lock:
        tools = ToolRegistry()
        kb = KBStore()
        ops.tools = tools
        ops.kb = kb


def _reload_all():
    """重载工具/知识，并让注册表里所有运维 Agent 实例都指向最新对象（多 Agent 一致性）。"""
    global tools, kb
    with _jobs_lock:
        tools = ToolRegistry()
        kb = KBStore()
        for a in registry.list("ops"):
            inst = registry.get_instance(a["id"])
            inst.tools = tools
            inst.kb = kb
        ops.tools = tools
        ops.kb = kb


def _save_trace(name: str, payload: Any):
    TRACE_DIR.mkdir(exist_ok=True)
    (TRACE_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------- 业务域操作记录（工作台产出写回消息栏「操作记录」） ----------------
def _save_message(entry: dict):
    db.execute(
        "INSERT INTO messages (id,workspace_id,agent_id,agent_name,kind,summary,detail,ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (entry["id"], entry["workspace_id"], entry.get("agent_id"), entry.get("agent_name"),
         entry.get("kind"), entry.get("summary"), entry.get("detail"), entry.get("ts")))


# ---------------- 请求模型 ----------------
class ToolCallReq(BaseModel):
    name: str
    params: Dict[str, Any] = {}

class AlertReq(BaseModel):
    alert: Optional[Dict[str, Any]] = None
    alert_id: Optional[str] = None
    workspace_id: Optional[str] = None   # 绑定业务域，状态灯按域联动
    ops_agent_id: Optional[str] = None   # 指定运维 Agent（不填则取该域主 Agent）

class ChatReq(BaseModel):
    question: str
    top_k: int = 3

class FeedbackReq(BaseModel):
    feedback_id: str
    summary: str

class ClosedLoopReq(BaseModel):
    alert: Optional[Dict[str, Any]] = None
    alert_id: Optional[str] = None
    workspace_id: Optional[str] = None   # 绑定业务域，状态灯按域联动


# ---------------- 多 Agent / 消息栏 请求模型 ----------------
class RaiseReq(BaseModel):
    alert: Optional[Dict[str, Any]] = None
    alert_id: Optional[str] = None
    ops_agent_id: Optional[str] = None
    workspace_id: Optional[str] = None   # 绑定到业务域，消息栏按域隔离


class DispatchReq(BaseModel):
    agent_id: Optional[str] = None
    mode: Optional[str] = None


class GapRegisterReq(BaseModel):
    """工作台诊断缺口后回流消息栏：复用已跑过的诊断结果，登记并(自动模式)派发。"""
    alert: Optional[Dict[str, Any]] = None
    alert_id: Optional[str] = None
    diagnosis: Optional[Dict[str, Any]] = None
    missing_tool: Optional[str] = None
    mode: Optional[str] = None


class ModeReq(BaseModel):
    mode: str   # 'auto' | 'manual'


class MessageReq(BaseModel):
    """工作台产出写回消息栏「操作记录」：运维诊断 / 研发造工具 / 缺口登记。"""
    agent_id: str
    kind: str                       # 'diagnose' | 'build' | 'gap' | 'info'
    summary: str
    detail: Optional[str] = None


# ---------------- 业务域 / 工作空间 + Agent 管理（持久化） ----------------
class CreateWorkspaceReq(BaseModel):
    name: str
    adapter_id: Optional[str] = None
    mode: str = "auto"
    custom_id: Optional[str] = None


class CreateAgentReq(BaseModel):
    kind: str                       # 'ops' | 'dev'
    name: str
    scope: list[str] = []
    description: str = ""
    primary: bool = False


class UpdateAgentReq(BaseModel):
    name: Optional[str] = None
    scope: Optional[list[str]] = None
    description: Optional[str] = None


class AdapterAlertIngestReq(BaseModel):
    """外部告警 webhook 接入：把原始报文转成统一 Alert 后喂给运维 Agent。"""
    adapter_id: Optional[str] = None   # 不填则用第一个 alert 类适配器
    payload: Dict[str, Any] = {}
    ops_agent_id: Optional[str] = None
    workspace_id: Optional[str] = None   # 绑定业务域，状态灯按域联动


class StreamStartReq(BaseModel):
    """模拟告警流水线启动参数（持续监控演示，处置链路与 webhook 接入一致）。"""
    profile: str = "mixed"             # mixed=样本为主穿插故障 | story=故障剧本短循环
    interval_ms: int = 1200            # 播放节拍（毫秒，实际受单条处置耗时上浮）
    loop: bool = True                  # 播完自动循环（工具沉淀后同类故障直接复用）
    workspace_id: Optional[str] = None # 绑定业务域：状态灯联动该域运维 Agent
    ops_agent_id: Optional[str] = None # 指定运维 Agent（不填取该域主 Agent）
    mode: Optional[str] = None         # auto|manual，覆盖业务域派发模式（不填跟随域）


# ---------------- 端点 ----------------
@app.get("/api/info")
def root():
    return {
        "service": "TeleOps 智能体平台",
        "version": VERSION,
        "llm_mode": llm.mode,
        "dispatch_mode": dispatch_mode["value"],
        "rate_limit": "on" if rl.ENABLED else "off",
        "uptime_s": int(time.time() - _START_TS),
        "agents": [a["id"] for a in registry.list()],
        "adapters": [a["id"] for a in adapters.list()],
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


@app.get("/health")
def health():
    """liveness 探活：进程存活 + 依赖概况。字段向后兼容（docker healthcheck 仍看 status==ok）。"""
    db_ok = False
    try:
        db.query_one("SELECT 1")
        db_ok = True
    except Exception:
        pass
    with _jobs_lock:
        jobs_running = sum(1 for v in _jobs.values() if v["status"] == "running")
    return {
        "status": "ok",
        "version": VERSION,
        "llm_mode": llm.mode,
        "nodes": len(cmdb.all_nodes()),
        "tools": len(tools.list_tools()),
        "db": "ok" if db_ok else "error",
        "uptime_s": int(time.time() - _START_TS),
        "jobs_running": jobs_running,
        "rate_limit": "on" if rl.ENABLED else "off",
    }


@app.get("/health/ready")
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
        "version": VERSION,
        "db": "ok" if db_ok else "error",
        "data_dir_writable": dir_ok,
    }


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


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本格式指标（公开端点，供抓取）。"""
    if not _METRICS_GAUGES_REGISTERED:
        _register_metrics_gauges()
    return Response(content=metrics.render(),
                    media_type="text/plain; version=0.0.4")


class AuthReq(BaseModel):
    username: str
    password: str


@app.get("/auth/status")
def auth_status():
    """前端据此判断是否需要弹出登录提示（公开端点，不受鉴权中间件限制）。"""
    return {"auth_required": AUTH_REQUIRED, "jwt_enabled": True,
            "users_exist": auth.user_count() > 0}


@app.post("/auth/register")
def auth_register(req: AuthReq):
    """注册用户：第一个注册者自动成为管理员。密码至少 6 位。"""
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="用户名与密码必填")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if auth.get_user(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    u = auth.create_user(req.username, req.password)
    token = auth.encode_token({"sub": u["username"], "uid": u["id"], "is_admin": u["is_admin"]})
    return {"token": token, "user": {"username": u["username"], "is_admin": u["is_admin"]}}


@app.post("/auth/login")
def auth_login(req: AuthReq):
    u = auth.authenticate(req.username, req.password)
    if not u:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.encode_token({"sub": u["username"], "uid": u["id"], "is_admin": u["is_admin"]})
    return {"token": token, "user": {"username": u["username"], "is_admin": u["is_admin"]}}


@app.get("/auth/me")
def auth_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": user.get("sub"), "is_admin": user.get("is_admin", False)}


@app.get("/topology")
def topology():
    return json.loads(Path(TOPOLOGY_FILE).read_text(encoding="utf-8"))


@app.get("/tools")
def list_tools():
    return {"tools": [tools.get(t) for t in tools.list_tools()]}


@app.post("/tools/call")
def call_tool(req: ToolCallReq):
    try:
        return {"result": tools.call(req.name, req.params)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/knowledge")
def knowledge(q: str, top_k: int = 3):
    hits = kb.retrieve(q, top_k=top_k)
    return {"query": q, "hits": hits}


@app.get("/alerts")
def list_alerts(severity: str = "", noise: str = "", q: str = "", limit: int = 200):
    """列出「接入业务预设」的真实告警样本（data/alerts.json，BlueGene/L 机群事件）。

    支持过滤：severity=info|critical、noise=true|false、q=关键字，供前端告警浏览器与调试使用。
    """
    data = json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))
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
        "source": "data/alerts.json · BlueGene/L 机群日志样本",
        "summary": {"all": len(data.get("alerts", [])), "severity": sev_cnt, "noise": noise_cnt},
        "alerts": alerts[: max(1, min(limit, 500))],
    }


@app.post("/alert")
def alert(req: AlertReq):
    job_id = _start_job(lambda: _alert_flow(req))
    return {"job_id": job_id, "status": "running"}


def _alert_flow(req: AlertReq):
    alert_obj = req.alert
    if alert_obj is None and req.alert_id:
        data = json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))
        matched = [a for a in data.get("alerts", []) if a.get("alert_id") == req.alert_id]
        if not matched:
            raise HTTPException(status_code=404, detail=f"alert_id {req.alert_id} 未找到")
        alert_obj = matched[0]
    if alert_obj is None:
        raise HTTPException(status_code=400, detail="需提供 alert 或 alert_id")
    # 绑定业务域：状态灯实时联动落在对应域的 Agent，而非 core-net 全局主
    ops_id = req.ops_agent_id or _ws_primary_ops(req.workspace_id)
    registry.set_status(ops_id, "busy")
    try:
        state = {
            "alert": alert_obj, "normalized": {}, "diagnosis": {},
            "tool_results": [], "plan": {}, "missing_tool": "", "is_noise": False,
        }
        out = ops_graph.invoke(state)
        _save_trace("api_alert", out)
        return out
    finally:
        registry.set_status(ops_id, "idle")


@app.post("/chat")
def chat(req: ChatReq):
    hits = kb.retrieve(req.question, top_k=req.top_k)
    context = "\n".join(f"[{h['source']}] {h['text']}" for h in hits)
    prompt = (
        "[TASK:KBQA]\n"
        f"你是电信云网运维知识助手。仅基于知识库内容回答问题，不要编造。\n"
        f"问题: {req.question}\n知识库:\n{context}"
    )
    answer = llm.complete(prompt)
    return {"question": req.question, "answer": answer, "retrieved": hits}


@app.post("/feedback")
def feedback(req: FeedbackReq):
    fb = {"feedback_id": req.feedback_id, "summary": req.summary}
    # 自动触发研发 Agent：造工具 + 注册 + 沉淀 SOP（闭环自动化）
    res = dev.fulfill_feedback(fb)
    _reload()
    _save_trace("api_feedback", {"feedback": fb, "result": res})
    return {
        "feedback": fb,
        "created_tool": res["tool"],
        "sop": res["sop"],
        "note": "已自动注册工具并沉淀 SOP，可在 /tools 与 /knowledge 查看",
    }


@app.post("/closed-loop/run")
def closed_loop(req: ClosedLoopReq):
    job_id = _start_job(lambda: _closed_loop_flow(req))
    return {"job_id": job_id, "status": "running"}


def _closed_loop_flow(req: ClosedLoopReq):
    alert_obj = req.alert
    if alert_obj is None and req.alert_id:
        data = json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))
        matched = [a for a in data.get("alerts", []) if a.get("alert_id") == req.alert_id]
        alert_obj = matched[0] if matched else None
    if alert_obj is None:
        # 默认用一条"温度过热"告警触发闭环（推荐工具 temperature_probe，初始不在库内）
        alert_obj = {
            "alert_id": "A-TEMP-DEMO", "ts": "", "source": "zabbix",
            "metric": "temperature", "host": "host-1", "severity": "critical",
            "value": "88C", "message": "物理机 host-1 核心温度过热告警，疑似散热故障",
            "tags": ["compute", "temperature"], "is_noise": False,
        }
    # 绑定业务域：状态灯联动落在对应域的 ops/dev Agent
    ops_id = _ws_primary_ops(req.workspace_id)
    dev_id = registry.primary("dev", req.workspace_id)
    registry.set_status(ops_id, "busy")
    try:
        # 第一轮：运维处理
        s1 = {
            "alert": alert_obj, "normalized": {}, "diagnosis": {},
            "tool_results": [], "plan": {}, "missing_tool": "", "is_noise": False,
        }
        out1 = ops_graph.invoke(s1)
        missing = out1.get("missing_tool", "")
        loop_log = {"round1": out1, "dev": None, "round2": None}
        if missing:
            # 生成反馈工单并触发研发 Agent 造工具 + 沉淀 SOP
            registry.set_status(dev_id, "busy")
            try:
                fb = {
                    "feedback_id": "F-AUTO",
                    "summary": f"运维根因推理需要工具 {missing}，但工具库缺失，请研发生成",
                }
                dev_res = dev.fulfill_feedback(fb)
            finally:
                registry.set_status(dev_id, "idle")
            _reload()
            loop_log["dev"] = dev_res
            # 第二轮：复用新工具重新处置
            s2 = {
                "alert": alert_obj, "normalized": {}, "diagnosis": {},
                "tool_results": [], "plan": {}, "missing_tool": "", "is_noise": False,
            }
            out2 = ops_graph.invoke(s2)
            loop_log["round2"] = out2
        _save_trace("api_closed_loop", loop_log)
        return {
            "alert": alert_obj,
            "missing_tool": missing,
            "loop_closed": bool(missing),
            "dev_result": loop_log["dev"],
            "round1": out1,
            "round2": loop_log["round2"],
        }
    finally:
        registry.set_status(ops_id, "idle")


@app.get("/traces")
def traces():
    files = sorted(Path(TRACE_DIR).glob("*.json"))
    return {"traces": [f.name for f in files]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    """轮询异步任务进度（前端据此让作战室状态灯实时刷新）。"""
    _gc_jobs()   # 顺带清理过期任务，避免内存泄漏
    return _jobs.get(job_id, {"status": "not_found"})


# ---------------- 模拟告警流水线（持续监控演示，处置链路与 webhook 接入一致） ----------------
def _stream_resolve_ctx(ws_id, ops_agent_id, mode):
    """解析流水线处置上下文：选运维 Agent + 定派发模式（跟随域 / 显式覆盖）。"""
    ops_id = ops_agent_id or (_ws_primary_ops(ws_id) if ws_id else registry.primary("ops"))
    if not mode:
        ws_meta = ws_store.get(ws_id) if ws_id else None
        mode = ws_meta["mode"] if ws_meta else dispatch_mode["value"]
    return ops_id, mode


def _stream_make_processor(ws_id, ops_id, mode):
    """单条告警处置回调：降噪 → 根因 → 工具 →（缺工具自动登记并走研发闭环）。

    与 webhook 接入（_ingest_flow）唯一的差别是输入来源：这里来自流水线剧本，
    后续接真实告警推送时可直接复用本函数。
    """
    def process(alert: dict) -> dict:
        inst = registry.get_instance(ops_id)
        entry = {"noise": False, "summary": "", "missing_tool": "",
                 "loop": "none", "tool_name": "", "error": ""}
        if not inst:
            entry["error"] = f"ops agent {ops_id} 不存在"
            return entry
        registry.set_status(ops_id, "busy")
        try:
            out = inst.handle_alert(alert)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["summary"] = "处置异常，已跳过（不打断流水线）"
            return entry
        finally:
            registry.set_status(ops_id, "idle")
        norm = out.get("normalized") or {}
        entry["noise"] = bool(norm.get("is_noise"))
        entry["triage_by"] = norm.get("triage_by") or ""
        diag = out.get("diagnosis") or {}
        hyps = diag.get("hypotheses") or []
        cause = (hyps[0] or {}).get("cause", "") if hyps else ""
        concl = diag.get("conclusion", "") or ""
        if entry["noise"]:
            entry["summary"] = "噪声告警，已由降噪层抑制，不打扰处置队列"
            return entry
        entry["hypothesis"] = cause or concl
        missing = out.get("missing_tool") or ""
        entry["missing_tool"] = missing
        # 根因推荐的工具（含已存在库中、可直接调用的）
        rec_tools = [h.get("recommended_tool") for h in hyps if h.get("recommended_tool")]
        if not missing:
            # 若推荐工具正是本场流水线此前造出来的沉淀 → 标为「复用」，叙事直白
            rec = next((t for t in rec_tools if t in created_pool), "")
            if rec:
                entry["loop"] = "reused"
                entry["tool_name"] = rec
                entry["summary"] = (f"同类故障再现 → 复用本轮沉淀的 {rec} "
                                    "直接探测，无需研发介入")
            else:
                entry["summary"] = (f"真实故障 → 根因：{cause or concl or '已定位'}；"
                                    "已有工具处置")
            return entry
        # 工具缺口 → 复用登记/派发流程（_raise_flow 传入 out，避免重复诊断）
        try:
            rr = _raise_flow(ops_id, alert, mode, ws_id, out=out)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["summary"] = f"缺口 {missing} 派发异常"
            return entry
        if rr.get("reusable"):
            entry["loop"] = "reused"
            entry["tool_name"] = rr.get("tool") or missing
            entry["summary"] = (f"检测到缺口工具 {missing}，但工具库已有沉淀 → "
                                "直接复用，无需研发重复造")
        elif rr.get("requirement"):
            req = rr["requirement"]
            entry["tool_name"] = req.get("created_tool_name") or missing
            if req.get("status") == "done":
                entry["loop"] = "created"
                created_pool.add(entry["tool_name"])
                entry["summary"] = (f"缺口 {missing} → 研发造出 {entry['tool_name']} "
                                    "并注册 → 运维已复用重新处置（闭环达成）")
            else:
                entry["loop"] = "pending"
                entry["summary"] = (f"缺口 {missing} 已登记消息栏"
                                    f"（{req.get('status')}），等待派发研发")
        else:
            entry["summary"] = f"缺口 {missing} 登记未成功：{rr.get('error') or '未知'}"
            entry["error"] = rr.get("error") or ""
        return entry
    created_pool = set()   # 本场流水线造出的工具（用于给后续「复用」打标）
    return process


@app.post("/stream/start")
def stream_start(req: StreamStartReq):
    """启动模拟告警流水线：自动循环播放剧本，Agent 持续处置不停摆。"""
    if stream.running:
        raise HTTPException(status_code=409,
                            detail="告警流已在运行，请先停止（/stream/stop）再启动")
    if req.profile not in ("mixed", "story"):
        raise HTTPException(status_code=400, detail="profile 必须为 mixed 或 story")
    if req.mode and req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    data = json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))
    playlist = build_playlist(data.get("alerts", []), profile=req.profile)
    if not playlist:
        raise HTTPException(status_code=400, detail="剧本为空，请检查 data/alerts.json")
    ops_id, mode = _stream_resolve_ctx(req.workspace_id, req.ops_agent_id, req.mode)
    stream._process = _stream_make_processor(req.workspace_id, ops_id, mode)
    stream.start(playlist, profile=req.profile,
                 interval_ms=req.interval_ms, loop=req.loop, ops_agent_id=ops_id)
    return {"status": "running", "profile": req.profile, "ops_agent_id": ops_id,
            "mode": mode, "playlist_len": len(playlist), "detail": stream.status()}


@app.post("/stream/stop")
def stream_stop():
    """停止流水线（幂等），返回最终统计。"""
    stream.stop()
    return {"status": "stopped", "detail": stream.status()}


@app.post("/stream/reset-demo")
def stream_reset_demo():
    """重置演示数据：清掉流内沉淀的工具，让「缺工具→造工具」可反复重演。

    只删除「全局工具库」里非内置（保留 ping_host / restart_service）的工具行，
    不动各业务域自有工具与消息栏历史。
    """
    stream.stop()
    db.execute("DELETE FROM tools WHERE name NOT IN ('ping_host','restart_service') "
               "AND (workspace_id IS NULL OR workspace_id='')")
    # 一并清空需求看板，让「缺工具→造工具」闭环可从头重演，避免历史 REQ 干扰演示
    db.execute("DELETE FROM requirements")
    return {"status": "reset",
            "tools": [r["name"] for r in db.query(
                "SELECT name FROM tools ORDER BY name")]}


@app.get("/stream/status")
def stream_status():
    """流水线运行状态 + 累计统计（前端秒级轮询）。"""
    return stream.status()


@app.get("/stream/feed")
def stream_feed(after: int = 0):
    """增量拉取处置流水（seq > after），供前端像监控大屏一样滚动渲染。"""
    return {"items": stream.feed(after=after)}


@app.get("/stream/tasks")
def stream_tasks(limit: int = 50, agent_id: Optional[str] = None):
    """作战室任务队列：把流水线告警按「分配运维 Agent → 处置 → 闭环」可视化。

    可选 agent_id 过滤只看某 Agent 的任务；limit 控制返回最近 N 条。
    """
    items = stream.tasks(limit=limit)
    if agent_id:
        items = [t for t in items if t.get("assigned_agent") == agent_id]
    return {"tasks": items, "running": stream.running}


# ---------------- 多 Agent 矩阵 + 消息栏（人工 / 自动派发闭环） ----------------
@app.get("/agents")
def agents(workspace_id: Optional[str] = None):
    """返回 Agent 列表（含实时 status：idle/busy/error）。传 workspace_id 可按业务域过滤。"""
    return {
        "agents": registry.list(workspace_id=workspace_id),
        "primary_ops": registry.primary("ops"),
        "primary_dev": registry.primary("dev"),
    }


@app.get("/dispatch/mode")
def get_mode():
    return {"mode": dispatch_mode["value"]}


@app.post("/dispatch/mode")
def set_mode(req: ModeReq):
    if req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    dispatch_mode["value"] = req.mode
    return {"mode": req.mode}


@app.get("/requirements")
def get_requirements(status: Optional[str] = None, workspace_id: Optional[str] = None):
    return {"requirements": board.list(status, workspace_id)}


def _resolve_alert_obj(req_alert, req_alert_id):
    if req_alert:
        return req_alert
    if req_alert_id:
        data = json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))
        matched = [a for a in data.get("alerts", []) if a.get("alert_id") == req_alert_id]
        if matched:
            return matched[0]
    # 默认温度过热告警（推荐工具 temperature_probe，初始不在库内）
    return {
        "alert_id": "A-TEMP-DEMO", "ts": "", "source": "zabbix",
        "metric": "temperature", "host": "host-1", "severity": "critical",
        "value": "88C", "message": "物理机 host-1 核心温度过热告警，疑似散热故障",
        "tags": ["compute", "temperature"], "is_noise": False,
    }


@app.post("/requirements/raise")
def post_requirement(req: RaiseReq):
    """运维 Agent 诊断后把"工具缺口"登记进消息栏；自动模式下直接跑完闭环。"""
    ws_id = req.workspace_id
    ops_id = req.ops_agent_id or _ws_primary_ops(ws_id)
    ops_inst = registry.get_instance(ops_id)
    if not ops_inst:
        raise HTTPException(status_code=400, detail=f"ops agent {ops_id} 不存在")
    alert_obj = _resolve_alert_obj(req.alert, req.alert_id)
    # 业务域存在则用域内模式；否则退回全局模式（避免 None["mode"] 抛 500）
    mode = ws_store.get(ws_id)["mode"] if (ws_id and ws_store.get(ws_id)) else dispatch_mode["value"]
    job_id = _start_job(lambda: _raise_flow(ops_id, alert_obj, mode, ws_id))
    return {"job_id": job_id, "status": "running"}


def _raise_flow(ops_id, alert_obj, mode, ws_id, out=None):
    registry.set_status(ops_id, "busy")
    try:
        # 工作台场景：诊断已在 /diagnose 跑过，直接复用结果避免重复推理
        if out is None:
            out = registry.get_instance(ops_id).handle_alert(alert_obj)
        missing = out.get("missing_tool")
        if not missing:
            return {"error": "该告警未触发工具缺口，无需派发", "diagnosis": out.get("diagnosis")}
        # 工具复用兜底：诊断可能来自前端旧会话（register-gap 回传），
        # 或工具刚被其它 Agent 造出——登记需求前先查工具库，避免重复 REQ。
        if tools.get(missing):
            return {"error": f"工具 {missing} 已在工具库中（可复用），无需重复登记需求",
                    "reusable": True, "tool": missing,
                    "diagnosis": out.get("diagnosis")}
        req_obj = dispatch_mod.raise_requirement(
            board, registry, alert_obj, out, ops_id, mode, workspace_id=ws_id)
        # 自动模式：登记后立即走 研发造工具 → 派回运维 的完整闭环
        if mode == "auto":
            dispatch_mod.dispatch_to_dev(board, registry, req_obj["id"])
            _reload_all()
            dispatch_mod.dispatch_to_ops(board, registry, req_obj["id"])
        return {"requirement": board.get(req_obj["id"]), "mode": mode}
    finally:
        registry.set_status(ops_id, "idle")


@app.post("/requirements/{req_id}/dispatch-dev")
def post_dispatch_dev(req_id: str, req: DispatchReq):
    """手动模式：把需求派发给指定（或路由选中）的研发 Agent 造工具。"""
    def _flow():
        res = dispatch_mod.dispatch_to_dev(
            board, registry, req_id, dev_agent_id=req.agent_id, mode=req.mode)
        # 造完工具刷新工具库/知识库视图，运维侧立即可复用（含 SOP 检索）
        _reload_all()
        return res
    job_id = _start_job(_flow)
    return {"job_id": job_id, "status": "running"}


@app.post("/requirements/{req_id}/dispatch-ops")
def post_dispatch_ops(req_id: str, req: DispatchReq):
    """手动模式：工具造好后，派回指定（或发起方）运维 Agent 重新处置。"""
    job_id = _start_job(lambda: dispatch_mod.dispatch_to_ops(
        board, registry, req_id, ops_agent_id=req.agent_id, mode=req.mode))
    return {"job_id": job_id, "status": "running"}


# ---------------- 单个 Agent 工作台（作战室卡片点击进入） ----------------
@app.post("/agents/{agent_id}/diagnose")
def agent_diagnose(agent_id: str, req: AlertReq):
    """运维 Agent 工作台：运行该 Agent 的告警根因分析（job 化，状态灯实时联动）。"""
    a = registry.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    if a["kind"] != "ops":
        raise HTTPException(status_code=400, detail=f"{agent_id} 不是运维 Agent")
    inst = registry.get_instance(agent_id)

    def flow():
        alert_obj = _resolve_alert_obj(req.alert, req.alert_id)
        registry.set_status(agent_id, "busy")
        try:
            return inst.handle_alert(alert_obj)
        finally:
            registry.set_status(agent_id, "idle")

    job_id = _start_job(flow)
    return {"job_id": job_id, "status": "running"}


@app.post("/agents/{agent_id}/build")
def agent_build(agent_id: str, req: FeedbackReq):
    """研发 Agent 工作台：运行该 Agent 的造工具流程（job 化，状态灯实时联动）。"""
    a = registry.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    if a["kind"] != "dev":
        raise HTTPException(status_code=400, detail=f"{agent_id} 不是研发 Agent")
    inst = registry.get_instance(agent_id)

    def flow():
        registry.set_status(agent_id, "busy")
        try:
            fb = {"feedback_id": req.feedback_id, "summary": req.summary}
            res = inst.fulfill_feedback(fb)
            _reload()
            _save_trace("agent_build", {"agent": agent_id, "feedback": fb, "result": res})
            return {"created_tool": res["tool"], "sop": res["sop"],
                    "note": "已自动注册工具并沉淀 SOP，运维 Agent 下一轮即可直接调用"}
        finally:
            registry.set_status(agent_id, "idle")

    job_id = _start_job(flow)
    return {"job_id": job_id, "status": "running"}


@app.post("/agents/{agent_id}/register-gap")
def agent_register_gap(agent_id: str, req: GapRegisterReq):
    """工作台诊断出工具缺口后，把缺口登记进当前业务域消息栏并按模式派发（打通工作台→闭环）。

    前端在 /agents/{id}/diagnose 跑出 missing_tool 后，带诊断结果回传本接口，
    避免重复推理；登记的需求归属该 Agent 所在业务域，自动模式即跑完研发→回传闭环。
    """
    a = registry.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
    if a["kind"] != "ops":
        raise HTTPException(status_code=400, detail=f"{agent_id} 不是运维 Agent")
    if not req.missing_tool:
        raise HTTPException(status_code=400, detail="无工具缺口，无需登记")
    ws_id = a.get("workspace_id")
    alert_obj = _resolve_alert_obj(req.alert, req.alert_id)
    mode = req.mode or (ws_store.get(ws_id)["mode"] if (ws_id and ws_store.get(ws_id))
                        else dispatch_mode["value"])
    out = {"missing_tool": req.missing_tool, "diagnosis": req.diagnosis or {}}
    job_id = _start_job(lambda: _raise_flow(agent_id, alert_obj, mode, ws_id, out=out))
    return {"job_id": job_id, "status": "running"}


# ---------------- 外部系统适配器（接入层 / 北向感知 + 南向执行） ----------------
# ---------------- 业务域 / 工作空间（持久化） ----------------
@app.get("/workspaces")
def list_workspaces():
    wss = ws_store.list()
    # 附带各业务域「待处理需求数」（未闭环、未驳回），供作战室悬浮卡展示
    for w in wss:
        w["pending"] = len([
            r for r in board.list(workspace_id=w["id"])
            if r.get("status") not in ("done", "rejected")
        ])
    return {"workspaces": wss}


# ---------------- 业务域操作记录（工作台产出写回消息栏） ----------------
@app.get("/workspaces/{ws_id}/messages")
def get_messages(ws_id: str, limit: int = 50):
    """按业务域拉取操作记录（最新在前），供消息栏「操作记录」tab 展示。"""
    rows = db.query(
        "SELECT * FROM messages WHERE workspace_id=? ORDER BY ts DESC LIMIT ?", (ws_id, limit))
    return {"messages": [dict(r) for r in rows]}


@app.post("/workspaces/{ws_id}/messages")
def post_message(ws_id: str, req: MessageReq):
    """写入一条操作记录（diagnose/build/gap）。受 Token 鉴权保护。"""
    ws = ws_store.get(ws_id)
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
    _save_message(entry)
    return entry


@app.post("/workspaces")
def create_workspace(req: CreateWorkspaceReq, request: Request):
    if req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode 必须为 auto 或 manual")
    # 记录创建者（JWT 登录用户），便于后续多租户归属
    owner_id = None
    user = getattr(request.state, "user", None)
    if user and user.get("uid"):
        owner_id = user["uid"]
    try:
        ws = ws_store.create(req.name, req.adapter_id, req.mode, req.custom_id, owner_id=owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ws


@app.get("/workspaces/{ws_id}")
def get_workspace(ws_id: str):
    ws = ws_store.get(ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"业务域 {ws_id} 不存在")
    return ws


@app.put("/workspaces/{ws_id}/mode")
def set_workspace_mode(ws_id: str, req: ModeReq):
    if not ws_store.update_mode(ws_id, req.mode):
        raise HTTPException(status_code=400, detail="业务域不存在或 mode 非法")
    return {"id": ws_id, "mode": req.mode}


@app.post("/workspaces/{ws_id}/agents")
def create_agent(ws_id: str, req: CreateAgentReq):
    try:
        agent = ws_store.add_agent(ws_id, req.kind, req.name, req.scope, req.description, req.primary)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return agent


@app.put("/workspaces/{ws_id}/agents/{agent_id}")
def update_agent(ws_id: str, agent_id: str, req: UpdateAgentReq):
    if req.name:
        if not ws_store.rename_agent(ws_id, agent_id, req.name):
            raise HTTPException(status_code=404, detail="Agent 不存在")
    if req.scope is not None or req.description is not None:
        if not ws_store.update_agent(ws_id, agent_id, req.scope, req.description):
            raise HTTPException(status_code=404, detail="Agent 不存在")
    return ws_store.get(ws_id)


@app.delete("/workspaces/{ws_id}")
def delete_workspace(ws_id: str):
    """删除业务域（默认域受保护），并级联清理其下所有 Agent 实例。"""
    try:
        ok, msg = ws_store.delete_workspace(ws_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"deleted": ws_id}


@app.delete("/workspaces/{ws_id}/agents/{agent_id}")
def delete_agent(ws_id: str, agent_id: str):
    ok, msg = ws_store.delete_agent(ws_id, agent_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"deleted": agent_id, "workspace": ws_id}


# ---------------- 外部系统适配器（接入层 / 北向感知 + 南向执行） ----------------
@app.get("/adapters")
def list_adapters(adapter_type: Optional[str] = None):
    """列出全部适配器（含预留接口）及其北向/南向、样板/预留状态。"""
    return {"adapters": adapters.list(adapter_type), "count": len(adapters.list(adapter_type))}


@app.post("/adapters/{adapter_id}/test")
def test_adapter(adapter_id: str):
    """探测某适配器连通性（预留接口返回 reserved 标记 + 待接入说明）。"""
    adp = adapters.get(adapter_id)
    if not adp:
        raise HTTPException(status_code=404, detail=f"adapter {adapter_id} 不存在")
    return {"adapter": adp.metadata(), "health": adp.healthcheck()}


@app.post("/adapters/alert/ingest")
def ingest_alert(req: AdapterAlertIngestReq):
    """外部告警 webhook 接入：选适配器解析 -> 统一 Alert -> 喂运维 Agent 根因分析。

    真实部署示例：Alertmanager receiver 指向
    POST /adapters/alert/ingest?adapter_id=alert-prometheus
    body 为 Alertmanager 标准 webhook 报文。
    """
    job_id = _start_job(lambda: _ingest_flow(req))
    return {"job_id": job_id, "status": "running"}


def _ingest_flow(req: AdapterAlertIngestReq):
    adp = adapters.get(req.adapter_id) if req.adapter_id else adapters.first_of_type("alert")
    if not adp:
        raise HTTPException(status_code=404, detail="未找到 alert 类适配器")
    if adp.adapter_type != "alert":
        raise HTTPException(status_code=400, detail=f"{adp.id} 不是告警适配器")
    raw_alerts = adp.parse_webhook(req.payload)
    # 绑定业务域：状态灯联动落在对应域的运维 Agent
    ops_id = req.ops_agent_id or _ws_primary_ops(req.workspace_id)
    ops_inst = registry.get_instance(ops_id)
    registry.set_status(ops_id, "busy")
    results = []
    try:
        for alert_obj in raw_alerts:
            out = ops_inst.handle_alert(alert_obj)
            results.append(out)
            _save_trace("adapter_ingest", {"adapter": adp.id, "alert": alert_obj, "out": out})
    finally:
        registry.set_status(ops_id, "idle")
    return {
        "adapter": adp.metadata(),
        "ingested": len(results),
        "ops_agent_id": ops_id,
        "results": results,
    }


# ---------------- 静态前端：单端口同时提供 API 与界面 ----------------
# 让 FastAPI 直接托管 web/，访问根路径即可打开作战室界面；
# 容器 / HF Spaces 部署时无需额外静态服务器，同源调用也避免跨域。
WEB_DIR = ROOT / "web"
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="webui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
