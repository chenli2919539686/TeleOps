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
import urllib.request
import uuid
import base64
from pathlib import Path
from datetime import datetime
import threading
from typing import Optional, Dict, Any, List

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

import src.config as _config_module
from src.config import TOPOLOGY_FILE, ALERTS_FILE, TRACE_DIR, DATA_DIR, load_llm_config, save_llm_config
from src.core.data_files import load_alerts, load_topology, load_eval_results
from src.core.cmdb_graph import CMDBGraph
from src.core.kb_store import KBStore
from src.core.tool_registry import ToolRegistry
from src.core.agent_registry import AgentRegistry
from src.core.workspace_store import WorkspaceStore
from src.core.requirement_board import RequirementBoard
from src.core import db
from src.core import auth
from src.core import metrics
from src.core import approvals
from src.core import settings
from src.core import rate_limit as rl
from src.core import usage
from src.core.alert_stream import AlertStream, build_playlist
from src.llm_client import LLMClient
from src.agents.ops_agent import OpsAgent
from src.agents.dev_agent import DevAgent
from src.core.agent_runtime import AgentRuntime
from src.core.state_store import get_job_store
from src.core.semaphore import get_semaphore_store
from src.core.stream_state import get_stream_state_store
from src.core.stream_executor import get_stream_executor
from src.workers import stream_tasks
from src.orchestration.graphs import build_ops_graph, build_dev_graph
from src.orchestration import dispatch as dispatch_mod
from src.adapters.registry import AdapterRegistry

app = FastAPI(title="TeleOps 智能体平台", version="0.8.7")

VERSION = "0.8.44"
_START_TS = time.time()   # 进程启动时刻（/health uptime_s、metrics 已含 uptime）

# 注册邀请码：环境变量 TELEOPS_INVITE_CODE 非空时启用注册校验。
# 设了码=只有拿到码的人能注册；不设=保持原先无门槛注册（向后兼容）。
INVITE_CODE = os.environ.get("TELEOPS_INVITE_CODE", "").strip()

# 人工审批闸（HITL）：TELEOPS_REQUIRE_APPROVAL=1 时，研发造工具等高风险动作
# 不再直接执行，而是落 pending 审批单，由管理员批准后才真正执行（企业级安全护栏）。
# 运行时取值：settings.get_require_approval() 文件优先 + env 兜底（admin 可经
# POST /settings/require-approval 运行时切换并持久化，无需重启）。
REQUIRE_APPROVAL = settings.get_require_approval()

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

# ---------------- 安全：写接口鉴权 ----------------
# 所有 POST/PUT/DELETE/PATCH 必须携带登录身份，两种方式：
#   1) Authorization: Bearer <JWT>   （界面右上角登录，或 /auth/login 获取）
#   2) X-API-Token / Bearer: <TELEOPS_API_TOKEN>  （服务级令牌，
#      供 Alertmanager webhook_configs.bearer_token、告警生成器等外部系统使用）
# GET 及 _PUBLIC_PATHS 中的路径不校验。
API_TOKEN = os.environ.get("TELEOPS_API_TOKEN", "").strip()
AUTH_REQUIRED = bool(API_TOKEN)
_PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc",
                 "/auth/status", "/auth/register", "/auth/login", "/auth/logout"}


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


def _rl_key(request: Request, bucket: str) -> str:
    """限流分桶 key：已登录用户按账号(sub)隔离；否则取真实客户端 IP（兼容 Caddy
    反代 X-Forwarded-For，避免反代后全员共用 127.0.0.1 一个桶导致 2-3 人即全员 429）。
    解析 Authorization 仅取 sub 做分桶、不验签，无安全决策依赖。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            seg = auth.split(".", 2)[1]
            payload = json.loads(base64.urlsafe_b64decode(seg + "=="))
            sub = payload.get("sub")
            if sub:
                return f"{bucket}:u:{sub}"
        except Exception:
            pass
    xff = request.headers.get("X-Forwarded-For")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    return f"{bucket}:{ip}"


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """滑动窗口限流（按账号 sub / 真实客户端 IP；分读/写/登录三档），防误用与口令爆破。"""

    async def dispatch(self, request, call_next):
        if not rl.ENABLED:
            return await call_next(request)
        path = request.url.path
        # 静态资源与低频内部端点放行，不占 API 额度
        if path in _LOW_FREQ_PATHS or path.startswith("/static") or \
                path.split("?")[0].lower().endswith(_STATIC_SUFFIXES):
            return await call_next(request)
        if path in ("/auth/login", "/auth/register"):
            bucket = "login"
        elif request.method in ("POST", "PUT", "DELETE", "PATCH"):
            bucket = "write"
        else:
            bucket = "read"
        key = _rl_key(request, bucket)
        limit = {"login": rl.LOGIN_LIMIT, "write": rl.WRITE_LIMIT,
                 "read": rl.READ_LIMIT}[bucket]
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


# ---------------- 审计辅助 ----------------
def _client_ip(request: Request) -> Optional[str]:
    """取客户端真实 IP（兼容 Caddy 反代 X-Forwarded-For）。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _actor_of(request: Request, fallback_username: str = None):
    """返回 (actor_name, actor_id) 用于审计；未登录且未给 fallback 时记 anonymous。"""
    user = getattr(request.state, "user", None)
    if user:
        return (user.get("sub") or "unknown", user.get("uid"))
    if fallback_username:
        return (fallback_username, None)
    return ("anonymous", None)


def _audit_write(request: Request, action: str, ws_id, detail, result="ok"):
    """写类操作的审计便捷封装：从 request 取操作人、IP，异步落到 audit_log。

    入队 O(1) 立即返回，db.audit 由后台线程消费，请求路径零阻塞。
    """
    actor, actor_id = _actor_of(request)
    ip = _client_ip(request)  # 请求线程取 IP，避免后台线程访问已回收的 request
    from src.core.audit_queue import get_writer
    get_writer().enqueue(
        lambda: db.audit(actor, action, workspace_id=ws_id, detail=detail,
                         result=result, actor_id=actor_id, ip=ip)
    )


# ---------------- LLM 运行时配置（前端设置面板可热更新） ----------------
class LLMConfig(BaseModel):
    provider: str = "deepseek"      # deepseek | openai | siliconflow | local | custom
    api_key: str = ""               # 前端保存时不传此字段表示保留原值
    base_url: str = ""
    model: str = ""
    local_endpoint: str = ""
    local_model: str = ""
    llm_triage: bool = True         # 规则无结论时是否再走 LLM 语义降噪
    budget_daily_cny: float = 0.0   # 每日预算上限（元），0 表示不限制
    budget_action: str = "fallback" # warn | fallback | reject
    # 自定义单价（¥/百万 token）：{"provider.model": [输入, 缓存命中, 输出]}
    pricing: dict = {}


LLM_PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/deepseek-chat"},
    "local": {"base_url": "", "model": ""},
    "custom": {"base_url": "", "model": ""},
}


def _mask_llm_cfg(cfg: dict) -> dict:
    out = cfg.copy()
    out["api_key"] = "已设置" if cfg.get("api_key") else ""
    out["api_key_set"] = bool(cfg.get("api_key"))
    return out



def _provider_console_url(provider: str) -> str:
    return {
        "openai": "https://platform.openai.com/usage",
        "siliconflow": "https://cloud.siliconflow.cn/expensebill",
    }.get(provider, "")


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
# D5：Agent 运行时工厂 —— 让「执行」落到该租户/业务域自己的 Agent 实例上，
# 而不是固定的全局 ops/dev 单例（多租户此前只在路由与状态灯层面隔离，
# 实际推理仍是共享的一份）。注册表解析不到时回退全局单例 → 旧行为不回归。
# 注：工厂不负责重载工具/知识，依赖 _reload_all 把「所有实例」一并重绑
#     （否则会出现：A 域实例持有旧 tools 对象，看不到刚被研发造出的新工具）
runtime = AgentRuntime(registry, ops, dev, build_ops_graph)
board = RequirementBoard()
dispatch_mode = {"value": "auto"}   # 自动 / 手动，可经 /dispatch/mode 切换
adapters = AdapterRegistry()        # 外部系统适配器注册表（含预留接口）
# 模拟告警流水线（持续监控演示），处置回调在 start 时注入。
# v0.8.18 起按业务域隔离：每个域一条独立流水线，A 域启动不影响 B 域，
# 解决「一台机器启动、所有人（含 admin）界面都跟着跑且停不下来」的全局单例问题。
_streams: Dict[str, AlertStream] = {}
_streams_lock = threading.Lock()
_stream_op_lock = threading.Lock()   # 流水线启动/停止临界区，防并发重复派发（SSE 幂等）
_STREAM_GLOBAL_KEY = "__global__"   # 兼容旧调用（不传 workspace_id）的全局槽位


_LLM_MAX = int(os.environ.get("TELEOPS_LLM_CONCURRENCY", "4"))
_AGENT_MAX = int(os.environ.get("TELEOPS_AGENT_CONCURRENCY", "2"))
# D3 收官：并发闸门改走 semaphore 状态层（默认本地 threading.Semaphore，行为不变；
# 设 TELEOPS_STATE_STORE=redis 后多副本共享同一份许可池 —— 否则 N 副本各限 4，
# 实际对模型的并发是 4N，配额保护形同虚设）。
semaphores = get_semaphore_store()


def _llm_sem():
    """全局 LLM 并发闸门：防多域/多流把模型配额打穿。"""
    return semaphores.semaphore("llm", _LLM_MAX)


def _agent_sem(aid):
    """每 Agent 有界并发闸门，防止单 Agent 被并发打爆。"""
    return semaphores.semaphore(f"agent:{aid}", _AGENT_MAX)


def _stream_of(ws_id: Optional[str]) -> AlertStream:
    """取（或惰性建）某业务域专属的告警流水线实例。"""
    key = ws_id or _STREAM_GLOBAL_KEY
    with _streams_lock:
        if key not in _streams:
            _streams[key] = AlertStream()
        return _streams[key]


def _stream_key_visible(ws_id: Optional[str], request: Request) -> bool:
    """流水线槽位对当前用户是否可见（不传 ws_id 的全局槽位对所有人可见，
    与旧版行为一致；具体域沿用业务域可见性规则，防止越权窥探他人域的处置流）。"""
    if not ws_id:
        return True
    user = getattr(request.state, "user", None)
    return ws_store.is_visible_to(ws_id, user)


# ---------------- 异步任务（让 Agent 运行时 busy 态可被前端实时轮询看到） ----------------
# 异步任务表：D3 起改走状态层（默认进程内 LocalJobStore，行为不变；
# 设 TELEOPS_STATE_STORE=redis 后多副本共享任务状态 —— 否则 A 副本发起的任务
# 轮询到 B 副本会查无此任务，状态灯永远转圈）。
_jobs = get_job_store()
# 注意：下面这把锁与"任务表"无关，是用来串行化 _reload/_reload_all 的互斥量
# （沿用历史命名），不要因为看到 _jobs 前缀就当成任务表的锁。
_jobs_lock = threading.Lock()
_JOBS_MAX = 200          # 防止长时间运行内存泄漏：保留最近 200 个任务
_JOBS_TTL = 3600         # 已完成任务 1 小时后清理


def _start_job(fn):
    """把重操作放到后台线程执行；期间对应 Agent 在 registry 中标记 busy，
    前端可经 /jobs/{job_id} 轮询进度，作战室状态灯随之实时刷新。"""
    job_id = uuid.uuid4().hex[:8]
    metrics.inc("teleops_jobs_total")
    _jobs.set(job_id, {"status": "running", "result": None, "error": None,
                       "ts": time.time()})
    _jobs.trim(_JOBS_MAX)          # 超过容量上限时清理最旧的

    def _run():
        # 先取再改再写回：外部存储不像进程内字典支持原地改字段
        job = _jobs.get(job_id) or {"status": "running", "result": None, "error": None}
        try:
            job["result"] = fn()
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            job["ts"] = time.time()
            _jobs.set(job_id, job)

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _gc_jobs():
    """清理已结束且超过 TTL 的任务，避免内存无限增长。"""
    _jobs.prune_expired(_JOBS_TTL)


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
    """工具/知识被改后重新加载，并让「所有」运维 Agent 实例指向最新对象。

    D5 前这里只重绑全局 ops 单例，但多条执行链路（_run_agent_build、
    _stream_make_processor、团队台 handle_alert 等）实际用的是注册表里
    各业务域自己的实例 —— 那些实例仍持有旧 tools/kb 对象引用，于是
    「研发刚造出的新工具」在紧接着的处置轮次里看不见，闭环像没生效
    （典型的“实例级多 Agent”与“单例级 reload”不一致）。

    改为统一按多 Agent 语义重绑全部实例（与原 _reload_all 等价）。
    """
    _reload_all()


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




class AuthReq(BaseModel):
    username: str
    password: str
    invite_code: str = ""  # 当服务端启用 TELEOPS_INVITE_CODE 时必填


@app.get("/auth/status")
def auth_status():
    """前端据此判断是否需要弹出登录提示（公开端点，不受鉴权中间件限制）。"""
    return {"auth_required": AUTH_REQUIRED, "jwt_enabled": True,
            "users_exist": auth.user_count() > 0,
            "invite_required": bool(INVITE_CODE)}  # 注册邀请码开关


@app.post("/auth/register")
def auth_register(req: AuthReq, request: Request):
    """注册用户：第一个注册者自动成为管理员。密码至少 6 位。

    注册成功后自动为其创建一套个人业务域（复制默认 Agent 矩阵），
    多租户隔离下该用户登录后只看到公共域 + 自己的域。
    """
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="用户名与密码必填")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if INVITE_CODE and req.invite_code != INVITE_CODE:
        # 邀请码开启但错配：拒绝注册。错误信息统一为「邀请码错误」，
        # 不区分「未填」与「填错」，避免旁路探测（已知标准实践）。
        db.audit(req.username, "auth.register", result="denied",
                 detail={"reason": "invite_code"}, ip=_client_ip(request))
        raise HTTPException(status_code=403, detail="邀请码错误，请联系管理员获取")
    if auth.get_user(req.username):
        db.audit(req.username, "auth.register", result="denied",
                 detail={"reason": "exists"}, ip=_client_ip(request))
        raise HTTPException(status_code=409, detail="用户名已存在")
    u = auth.create_user(req.username, req.password)
    # 为新用户建个人域（多租户隔离：owner_id + org_id 绑定，仅本人可见）
    ws_id = None
    try:
        ws = ws_store.create_personal(u["id"], u["username"], org_id=u.get("org_id"))
        ws_id = ws.get("id")
    except Exception as e:  # 建域失败不应阻断注册，仅记录
        metrics.inc("teleops_register_personal_ws_failed")
        print(f"[warn] 为 {u['username']} 建个人域失败: {e}")
    db.audit(u["username"], "auth.register", workspace_id=ws_id,
             detail={"ws": ws_id, "is_admin": bool(u["is_admin"])},
             result="ok", actor_id=u["id"], ip=_client_ip(request))
    token = auth.issue_token(u["username"])
    return {"token": token, "user": {
        "username": u["username"], "uid": u["id"], "is_admin": u["is_admin"],
        "org_id": u.get("org_id"), "roles": u.get("roles"), "perms": u.get("perms")}}


@app.post("/auth/login")
def auth_login(req: AuthReq, request: Request):
    u = auth.authenticate(req.username, req.password)
    if not u:
        db.audit(req.username or "unknown", "auth.login", result="denied",
                 detail={"reason": "bad_credentials"}, ip=_client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    db.audit(u["username"], "auth.login", result="ok", actor_id=u["id"],
             ip=_client_ip(request))
    token = auth.issue_token(u["username"])
    return {"token": token, "user": {
        "username": u["username"], "uid": u["id"], "is_admin": u["is_admin"],
        "org_id": u.get("org_id"), "roles": u.get("roles"), "perms": u.get("perms")}}


@app.get("/auth/me")
def auth_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": user.get("sub"), "uid": user.get("uid"),
            "is_admin": user.get("is_admin", False),
            "org_id": user.get("org_id"), "roles": user.get("roles"),
            "perms": user.get("perms")}


@app.post("/auth/logout")
def auth_logout(request: Request):
    """注销当前 JWT：加入服务端黑名单，重启后端后仍失效（黑名单持久化）。

    前端应在清空 localStorage 之前调用本端点，使残留 token 立即作废——
    修复「共享机器关浏览器不登出 → 下次访问仍以前次账号身份进入」的问题。
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=400, detail="缺少 Authorization 头")
    user = getattr(request.state, "user", None)
    actor, actor_id = (user.get("sub"), user.get("uid")) if user else ("anonymous", None)
    revoked = auth.revoke_token(token)
    db.audit(actor, "auth.logout", result="ok", actor_id=actor_id,
             ip=_client_ip(request))
    return {"detail": "已注销", "revoked": revoked}




def _alert_flow(req: AlertReq):
    alert_obj = req.alert
    if alert_obj is None and req.alert_id:
        data = load_alerts()
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
        out = runtime.ops_graph(ops_id).invoke(state)
        _save_trace("api_alert", out)
        return out
    finally:
        registry.set_status(ops_id, "idle")


def _closed_loop_flow(req: ClosedLoopReq):
    alert_obj = req.alert
    if alert_obj is None and req.alert_id:
        data = load_alerts()
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
        out1 = runtime.ops_graph(ops_id).invoke(s1)
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
                dev_res = runtime.dev_instance(dev_id)[1].fulfill_feedback(fb)
            finally:
                registry.set_status(dev_id, "idle")
            _reload()
            loop_log["dev"] = dev_res
            # 第二轮：复用新工具重新处置
            s2 = {
                "alert": alert_obj, "normalized": {}, "diagnosis": {},
                "tool_results": [], "plan": {}, "missing_tool": "", "is_noise": False,
            }
            out2 = runtime.ops_graph(ops_id).invoke(s2)
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




# ---------------- 模拟告警流水线（持续监控演示，处置链路与 webhook 接入一致） ----------------
def _stream_resolve_ctx(ws_id, ops_agent_id, mode):
    """解析流水线处置上下文：选运维 Agent + 定派发模式（跟随域 / 显式覆盖）。"""
    ops_id = ops_agent_id or (_ws_primary_ops(ws_id) if ws_id else registry.primary("ops"))
    if not mode:
        ws_meta = ws_store.get(ws_id) if ws_id else None
        mode = ws_meta["mode"] if ws_meta else dispatch_mode["value"]
    return ops_id, mode


def _stream_make_processor(ws_id, ops_id, mode, route_by_alert=True):
    """单条告警处置回调：降噪 → 根因 → 工具 →（缺工具自动登记并走研发闭环）。

    与 webhook 接入（_ingest_flow）唯一的差别是输入来源：这里来自流水线剧本，
    后续接真实告警推送时可直接复用本函数。
    """
    def process(alert: dict) -> dict:
        # 路由：按告警特征（tags/metric/source）在该域/全局 ops Agent 中选最专长者，
        # 取代"所有告警都喂同一个主运维 Agent"——这就是多 Agent 匹配分流（改造 A）。
        aid = ops_id
        if route_by_alert:
            _req = {"workspace_id": ws_id,
                    "tags": list(alert.get("tags") or [])
                            + [str(alert.get("metric") or ""), str(alert.get("source") or "")],
                    "description": alert.get("message") or "", "needed_tool": ""}
            aid = registry.route("ops", _req, cross_domain=True) or ops_id
        inst = registry.get_instance(aid)
        entry = {"noise": False, "summary": "", "missing_tool": "",
                 "loop": "none", "tool_name": "", "error": ""}
        if not inst:
            entry["error"] = f"ops agent {aid} 不存在"
            return entry
        registry.set_status(aid, "busy")
        try:
            # 并发隔离（改造 B）：全局 LLM 信号量防多域/多流踩 DeepSeek 配额，
            # 每 Agent 信号量防单 Agent 被并发打爆。任一 Agent 过载时在此排队而非阻塞进程。
            with _llm_sem(), _agent_sem(aid):
                out = inst.handle_alert(alert)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["summary"] = "处置异常，已跳过（不打断流水线）"
            return entry
        finally:
            registry.set_status(aid, "idle")
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


# ---------------- 多 Agent 矩阵 + 消息栏（人工 / 自动派发闭环） ----------------
def _resolve_alert_obj(req_alert, req_alert_id):
    if req_alert:
        return req_alert
    if req_alert_id:
        data = load_alerts()
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


# ---------------- 单个 Agent 工作台（作战室卡片点击进入） ----------------
def _run_agent_build(agent_id: str, feedback: Dict[str, Any]) -> Dict[str, Any]:
    """实际执行研发造工具流程（被 job 与审批批准共用）。"""
    a = registry.get(agent_id)
    inst = registry.get_instance(agent_id)
    registry.set_status(agent_id, "busy")
    try:
        res = inst.fulfill_feedback(feedback)
        _reload()
        _save_trace("agent_build", {"agent": agent_id, "feedback": feedback, "result": res})
        _audit_write_dummy("tool.build", a.get("workspace_id"),
                           {"agent": agent_id, "tool": res.get("tool"),
                            "feedback": feedback.get("feedback_id")})
        return {"created_tool": res["tool"], "sop": res["sop"],
                "note": "已自动注册工具并沉淀 SOP，运维 Agent 下一轮即可直接调用"}
    finally:
        registry.set_status(agent_id, "idle")


def _run_stream_start(p: Dict[str, Any]) -> bool:
    """审批通过后真正启动告警流（与 stream_start 端点逻辑一致，去除权限/网关）。"""
    ws_id = p.get("workspace_id")
    data = load_alerts()
    playlist = build_playlist(data.get("alerts", []), profile=p.get("profile", "mixed"))
    if not playlist:
        raise HTTPException(status_code=400, detail="剧本为空，请检查 data/alerts.json")
    ops_id, mode = _stream_resolve_ctx(ws_id, p.get("ops_agent_id"), p.get("mode"))
    with _stream_op_lock:
        if stream_executor.is_running(ws_id):
            raise HTTPException(status_code=409,
                                detail="该业务域的告警流已在运行，请先停止再启动")
        stream_executor.start(
            ws_id, playlist,
            process=_stream_make_processor(ws_id, ops_id, mode,
                                           route_by_alert=p.get("ops_agent_id") is None),
            profile=p.get("profile", "mixed"), interval_ms=p.get("interval_ms"),
            loop=p.get("loop"), ops_agent_id=ops_id, mode=mode,
            started_by=p.get("started_by") or "审批后启动")
    return True



def _audit_write_dummy(action, ws_id, detail):
    """无 request 上下文时写审计（审批异步执行用），异步化。"""
    from src.core.audit_queue import get_writer
    get_writer().enqueue(
        lambda: db.audit("system(hitl)", action, workspace_id=ws_id, detail=detail,
                         result="ok", actor_id=None, ip="internal")
    )



# ---------------- 外部系统适配器（接入层 / 北向感知 + 南向执行） ----------------
# ---------------- 业务域 / 工作空间（持久化） ----------------
# ---------------- 业务域操作记录（工作台产出写回消息栏） ----------------
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


# ---------------- 人工审批（HITL）端点（P1③） ----------------
class ApprovalDecisionReq(BaseModel):
    note: str = ""


@app.get("/approvals")
def list_approvals(request: Request, status: Optional[str] = None):
    """列出审批单。管理员看全部；普通用户只看自己发起/处置的。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")
    uid = user.get("uid")
    actor, _ = _actor_of(request)
    items = approvals.list_items(status=status, uid=(None if user.get("is_admin") else actor))
    return {"items": items, "total": len(items),
            "require_approval": settings.get_require_approval()}


@app.post("/approvals/{apr_id}/approve")
def approve_apr(apr_id: str, req: ApprovalDecisionReq, request: Request):
    """批准审批单；若为 tool.build，批准即真正执行研发造工具（HITL 闭环）。"""
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可审批")
    item = approvals.get(apr_id)
    if not item:
        raise HTTPException(status_code=404, detail="审批单不存在")
    decided = approvals.decide(apr_id, "approved", user.get("sub"))
    _audit_write(request, "approval.approve", item.get("payload", {}).get("workspace_id"),
                 {"approval_id": apr_id, "subject": item["subject"], "note": req.note})
    # 批准即执行：tool.build 真正造工具；stream.start 真正启动告警流（HITL 闭环）。
    executed = False
    if item["subject"] == "tool.build" and item.get("payload"):
        try:
            _run_agent_build(item["payload"]["agent_id"], item["payload"]["feedback"])
            executed = True
        except Exception as e:  # noqa: BLE001
            decided = approvals.decide(apr_id, "approved_failed", user.get("sub"))
            return {"approval": decided, "executed": False, "error": str(e)}
    elif item["subject"] == "stream.start" and item.get("payload"):
        try:
            _run_stream_start(item["payload"])
            executed = True
        except Exception as e:  # noqa: BLE001
            decided = approvals.decide(apr_id, "approved_failed", user.get("sub"))
            return {"approval": decided, "executed": False, "error": str(e)}
    return {"approval": decided, "executed": executed}


@app.post("/approvals/{apr_id}/reject")
def reject_apr(apr_id: str, req: ApprovalDecisionReq, request: Request):
    """拒绝审批单。"""
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可审批")
    item = approvals.get(apr_id)
    if not item:
        raise HTTPException(status_code=404, detail="审批单不存在")
    decided = approvals.decide(apr_id, "rejected", user.get("sub"))
    _audit_write(request, "approval.reject", item.get("payload", {}).get("workspace_id"),
                 {"approval_id": apr_id, "subject": item["subject"], "note": req.note})
    return {"approval": decided}


# ---------------- 运行时设置（admin 可配置，持久化） ----------------
class RequireApprovalReq(BaseModel):
    enabled: bool


@app.post("/settings/require-approval")
def set_require_approval_endpoint(req: RequireApprovalReq, request: Request):
    """管理员运行时切换人工审批闸（HITL 总开关），写入 data/settings.json 持久化。

    与部署期 env TELEOPS_REQUIRE_APPROVAL 二选一：文件值优先；此端点即「admin 可配置」。
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可配置")
    settings.set_require_approval(req.enabled)
    _audit_write(request, "settings.require_approval", None, {"enabled": req.enabled})
    return {"require_approval": settings.get_require_approval()}


# ---------------- 量化指标看板（P0② / P2⑥） ----------------
@app.post("/metrics/run")
def metrics_run(request: Request):
    """服务端重跑闭环评估脚本，刷新 data/eval_results.json 并返回最新指标。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        import subprocess
        subprocess.run([sys.executable, "scripts/eval_closed_loop.py"],
                       cwd=str(ROOT), check=True, capture_output=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"评估运行失败：{e}")
    return metrics_summary(request)


@app.get("/metrics/summary")
def metrics_summary(request: Request):
    """闭环量化指标：根因 Top-1 / 噪声抑制率 / 修复成功率(仿真) / 平均决策时延。

    diagnosis 段用离线标注数据验证；remediation 段用仿真靶机验证（明确标注 simulated）。
    实时部分附带当前告警流与适配器健康概况。
    """
    eval_data = load_eval_results("data/eval_results.json")
    with _streams_lock:
        running = {k: s.status() for k, s in _streams.items() if s.running}
    live = {
        "active_streams": len(running),
        "streams": running,
        "adapters": len(adapters.list()),
        "tools": len(tools.list_tools()),
    }
    return {"eval": eval_data, "live": live,
            "env_note": "diagnosis=offline-labeled, remediation=simulated"}


# ---------------- OIDC 单点登录（P2⑤，含 dev mock） ----------------
OIDC_ISSUER = os.environ.get("TELEOPS_OIDC_ISSUER", "").strip()
OIDC_DEV = os.environ.get("TELEOPS_OIDC_DEV", "").strip() in ("1", "true", "True") or not OIDC_ISSUER


def _decode_id_token_unverified(id_token: str) -> Dict[str, Any]:
    """仅用于本地 dev mock：不校验签名，直接解 payload（生产须走 JWKS 验签）。"""
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        import base64
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


@app.get("/auth/oidc/login")
def oidc_login():
    """发起 OIDC 登录：返回跳转 URL。

    dev mock（无真实 IdP）：跳回 /auth/oidc/callback?dev_user=... 直接演示。
    真实 IdP（配了 TELEOPS_OIDC_ISSUER）：返回标准 authorize 重定向地址。
    """
    if OIDC_DEV:
        return {"redirect_url": "/auth/oidc/callback?dev_user=demo@oidc.local&name=OIDCDemo",
                "mode": "dev"}
    return {"redirect_url": (f"{OIDC_ISSUER}/authorize?response_type=code"
                             f"&client_id={os.environ.get('TELEOPS_OIDC_CLIENT_ID','')}"
                             f"&redirect_uri={os.environ.get('TELEOPS_OIDC_REDIRECT','')}"
                             f"&scope=openid%20email%20profile"),
            "mode": "live"}


@app.post("/auth/oidc/callback")
def oidc_callback(dev_user: Optional[str] = None, name: Optional[str] = None,
                  code: Optional[str] = None, id_token: Optional[str] = None):
    """OIDC 回调：校验身份后签发 TeleOps JWT。

    dev_user 模式（demo）：按邮箱 upsert 用户并直接签发。
    id_token 模式：解 payload 取 sub/email（生产应 JWKS 验签），upsert 并签发。
    """
    sub_email = None
    display = name or "OIDCUser"
    if dev_user:
        sub_email = dev_user
        display = name or dev_user.split("@")[0]
    elif id_token:
        claims = _decode_id_token_unverified(id_token)
        sub_email = claims.get("email") or claims.get("sub")
        display = claims.get("name") or (sub_email or "oidc").split("@")[0]
    if not sub_email:
        raise HTTPException(status_code=400, detail="未获取到 OIDC 身份")
    u = auth.get_user(sub_email)
    if not u:
        u = auth.create_user(sub_email, os.urandom(12).hex())
    token = auth.issue_token(u["username"])
    db.audit(u["username"], "auth.oidc", result="ok", actor_id=u["id"],
             ip="oidc")
    return {"token": token, "user": {"username": u["username"], "is_admin": u["is_admin"]},
            "mode": "dev" if OIDC_DEV else "live"}


# ---------------- Grafana / Prometheus 指标查询（P1④，MCP 风格） ----------------
class MetricsQueryReq(BaseModel):
    promql: str = "up"
    hours: int = 1


@app.post("/adapters/{adapter_id}/query")
def adapter_query(adapter_id: str, req: MetricsQueryReq):
    """让 Agent / 前端主动查询真实监控指标（Grafana/Prometheus/Loki）。

    未配置真实 base_url 时返回仿真时序；配置后接真实 Prometheus /api/v1/query_range
    或 Loki 日志量统计。
    """
    adp = adapters.get(adapter_id)
    if not adp:
        raise HTTPException(status_code=404, detail=f"adapter {adapter_id} 不存在")
    if not hasattr(adp, "query_metrics"):
        raise HTTPException(status_code=400, detail=f"{adp.id} 不支持指标查询")
    try:
        return adp.query_metrics(req.promql, hours=req.hours)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"查询失败：{e}")


# ---------------- 日志查询（Loki / ELK / Grafana logs，MCP 风格） ----------------
class LogQueryReq(BaseModel):
    query: str = '{job=~".+"}'
    limit: int = 100


@app.post("/adapters/{adapter_id}/logs")
def adapter_logs(adapter_id: str, req: LogQueryReq):
    """让 Agent / 前端拉取真实日志（Loki 用 LogQL、ELK 用 Lucene/KQL、Grafana 日志）。

    未配置真实 base_url 时返回仿真日志行；配置后接真实 Loki /loki/api/v1/query_range 等。
    """
    adp = adapters.get(adapter_id)
    if not adp:
        raise HTTPException(status_code=404, detail=f"adapter {adapter_id} 不存在")
    if not hasattr(adp, "fetch_recent"):
        raise HTTPException(status_code=400, detail=f"{adp.id} 不支持日志拉取")
    try:
        return {"adapter_id": adp.id, "mode": "live" if adp.base_url else "demo",
                "logs": adp.fetch_recent(req.query, limit=req.limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"查询失败：{e}")


# ---------------- API 路由挂载（D2 演进式拆分：按域从 routers/ 装配） ----------------
# server.py 已完成全局单例与 helper 的初始化；先把跨模块共享对象挂到 ctx，
# routers 子模块只从 ctx 读取（不 import server），切断循环依赖。
from src.api.context import ctx

ctx.VERSION = VERSION
ctx._START_TS = _START_TS
ctx.registry = registry
ctx.ws_store = ws_store
ctx.cmdb = cmdb
ctx.kb = kb
ctx.llm = llm
ctx.tools = tools
ctx.adapters = adapters
ctx.dispatch_mode = dispatch_mode
ctx._jobs = _jobs
ctx._config_module = _config_module
ctx.LLM_PROVIDER_PRESETS = LLM_PROVIDER_PRESETS
ctx.LLMConfig = LLMConfig
ctx._mask_llm_cfg = _mask_llm_cfg
ctx._provider_console_url = _provider_console_url
ctx.ToolCallReq = ToolCallReq
ctx._gc_jobs = _gc_jobs
# R2：抽出 alerts/stream/agents/workspaces 4 组后，补齐它们依赖的共享对象
ctx.ops = ops
ctx.dev = dev
ctx.ops_graph = ops_graph
# D5：Agent 运行时工厂 —— 新代码请一律经 s.runtime 取实例/图，直接用上面三个
# 全局单例会绕过租户隔离（保留它们仅为兼容与兜底）。
ctx.runtime = runtime
ctx.board = board
ctx.db = db
ctx.dispatch_mod = dispatch_mod
ctx.REQUIRE_APPROVAL = REQUIRE_APPROVAL
ctx.get_require_approval = settings.get_require_approval
ctx.approvals = approvals
ctx._streams = _streams
ctx._streams_lock = _streams_lock
ctx._stream_op_lock = _stream_op_lock
ctx.semaphores = semaphores
ctx.load_alerts = load_alerts
ctx.build_playlist = build_playlist
ctx._start_job = _start_job
ctx._alert_flow = _alert_flow
ctx._closed_loop_flow = _closed_loop_flow
ctx._save_trace = _save_trace
ctx._reload = _reload
ctx._ws_primary_ops = _ws_primary_ops
ctx._stream_of = _stream_of
ctx._stream_resolve_ctx = _stream_resolve_ctx
ctx._stream_make_processor = _stream_make_processor

# D3 第三步：告警流调度可外部化（TELEOPS_STREAM_EXECUTOR=queue 时改走 RQ）。
# 默认仍是线程执行器（内部代理 _stream_of/_streams），行为与改造前完全一致。
# 队列模式下 worker 进程靠下面注册的工厂重建处置回调，所以工厂必须在这里注册好。
def _processor_for_queue_worker(ws_id):
    """worker 侧重建单条告警处置回调（参数从共享流状态里取）。"""
    st = get_stream_state_store().get(ws_id) or {}
    ops_id = st.get("ops_agent_id")
    return _stream_make_processor(ws_id, ops_id, st.get("mode") or "auto",
                                 route_by_alert=not ops_id)


stream_tasks.set_processor_factory(_processor_for_queue_worker)
stream_executor = get_stream_executor(_stream_of, streams=_streams)
ctx.stream_executor = stream_executor
ctx._stream_key_visible = _stream_key_visible
ctx._audit_write = _audit_write
ctx._actor_of = _actor_of
ctx._resolve_alert_obj = _resolve_alert_obj
ctx._raise_flow = _raise_flow
ctx._reload_all = _reload_all
ctx._actor_of = _actor_of
ctx._run_agent_build = _run_agent_build
ctx._audit_write_dummy = _audit_write_dummy
ctx._save_message = _save_message
ctx._llm_sem = _llm_sem
ctx._client_ip = _client_ip
# R2 抽出的 router 用作类型标注 / 别名的请求模型
ctx.AlertReq = AlertReq
ctx.ChatReq = ChatReq
ctx.FeedbackReq = FeedbackReq
ctx.ClosedLoopReq = ClosedLoopReq
ctx.StreamStartReq = StreamStartReq
ctx.ModeReq = ModeReq
ctx.RaiseReq = RaiseReq
ctx.DispatchReq = DispatchReq
ctx.GapRegisterReq = GapRegisterReq
ctx.MessageReq = MessageReq
ctx.CreateWorkspaceReq = CreateWorkspaceReq
ctx.CreateAgentReq = CreateAgentReq
ctx.UpdateAgentReq = UpdateAgentReq

from src.api.routers import (
    system_router,
    core_router,
    traces_router,
    llm_router,
    audit_router,
    alerts_router,
    stream_router,
    agents_router,
    workspaces_router,
)
app.include_router(system_router)
app.include_router(core_router)
app.include_router(traces_router)
app.include_router(llm_router)
app.include_router(audit_router)
app.include_router(alerts_router)
app.include_router(stream_router)
app.include_router(agents_router)
app.include_router(workspaces_router)


# ---------------- 静态前端：单端口同时提供 API 与界面 ----------------
# 让 FastAPI 直接托管 web/，访问根路径即可打开作战室界面；
# 容器 / HF Spaces 部署时无需额外静态服务器，同源调用也避免跨域。
WEB_DIR = ROOT / "web"
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="webui")


if __name__ == "__main__":
    import uvicorn
    # 默认只监听本机回环（127.0.0.1），避免本地开发时意外暴露到局域网/公网；
    # 需要局域网内其他设备访问时，设 TELEOPS_HOST=0.0.0.0 再启动。
    _host = os.environ.get("TELEOPS_HOST", "127.0.0.1")
    uvicorn.run(app, host=_host, port=int(os.environ.get("TELEOPS_PORT", "8000")))
