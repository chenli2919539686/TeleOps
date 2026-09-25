"""人工审批（HITL）存储层。

高风险动作（如研发造工具、启动告警流）在开启 require_approval 时，不直接执行，
而是落一条 pending 审批单，由管理员/被授权人批准后才真正执行——这是企业级
「人工闸」的核心（对标 AgentOS 的安全自愈护栏）。

存储后端（v0.8.44，跨副本外部化）
--------------------------------
- ``LocalApprovalStore``（**默认**）：进程内内存 + ``data/approvals.json`` 落盘，
  语义与改造前逐字节一致 → 单机部署零变化、零依赖。
- ``RedisApprovalStore``：每个审批单一个 JSON key（``teleops:apr:*``），多副本共享
  同一份审批态 → 配合 ``TELEOPS_STATE_STORE=redis`` 即「副本无状态」。

模块级函数 ``create / get / list_items / decide`` 全部委托给 ``get_approval_store()``
返回的存储后端，调用方（server / routers / tests）无需感知后端差异，与 D3 状态外部化
（``src/core/state_store.py``）保持同一抽象与同一开关（``TELEOPS_STATE_STORE``）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_FILE = Path("data/approvals.json")
_lock = threading.Lock()
_state: Dict[str, Any] = {"items": []}

# 审批单 / 设置 的 Redis key 前缀（与 D3 的 teleops:rl: 区分开，避免混用）
KEY_PREFIX = os.environ.get("TELEOPS_APPROVALS_PREFIX", "teleops:apr:")
# 审批单 TTL（秒）。0 = 不过期（默认，审批单需长期可追溯）
DEFAULT_TTL = int(os.environ.get("TELEOPS_APPROVAL_TTL", "0") or 0)


def _load():
    global _state
    if DATA_FILE.exists():
        try:
            _state = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            _state.setdefault("items", [])
        except Exception:
            _state = {"items": []}


def _save():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 存储抽象
# ---------------------------------------------------------------------------
class ApprovalStore(ABC):
    """审批单存储接口（dict 语义：按 id 存/取/列举/决定）。"""

    @abstractmethod
    def create(self, subject: str, requested_by: str, payload: Dict[str, Any],
               detail: str = "") -> str:
        ...

    @abstractmethod
    def get(self, aid: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def list_items(self, status: Optional[str] = None,
                   uid: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def decide(self, aid: str, decision: str,
               approver: str) -> Optional[Dict[str, Any]]:
        ...


class LocalApprovalStore(ApprovalStore):
    """进程内内存 + data/approvals.json 落盘（默认，等价改造前）。

    与改造前完全一致的语义：直接读写模块级 ``_state`` / ``DATA_FILE`` 全局，
    因此现有测试对 ``approvals._state`` / ``approvals.DATA_FILE`` 的 monkeypatch
    依然生效，无需改动。
    """

    def create(self, subject, requested_by, payload, detail=""):
        with _lock:
            _load()
            aid = f"apr-{uuid.uuid4().hex[:8]}"
            _state["items"].append({
                "id": aid, "subject": subject, "requested_by": requested_by,
                "detail": detail, "payload": payload, "status": "pending",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "decided_at": None, "decided_by": None,
            })
            _save()
            return aid

    def get(self, aid):
        with _lock:
            _load()
            return next((i for i in _state["items"] if i["id"] == aid), None)

    def list_items(self, status=None, uid=None):
        with _lock:
            _load()
            items = _state["items"]
            if status:
                items = [i for i in items if i["status"] == status]
            if uid:
                items = [i for i in items
                         if i["requested_by"] == uid or i.get("decided_by") == uid]
            return list(reversed(items))

    def decide(self, aid, decision, approver):
        with _lock:
            _load()
            item = next((i for i in _state["items"] if i["id"] == aid), None)
            if not item:
                return None
            if item["status"] != "pending":
                return item
            item["status"] = decision
            item["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item["decided_by"] = approver
            _save()
            return item


# Lua：原子地读取→若 pending 则改写状态，避免多副本并发审批时相互覆盖
# 返回："null"（无此单）/ 原 JSON（已决定，原样返回）/ 新 JSON（已更新）
_DECISION_LUA = """
local key = KEYS[1]
local decision = ARGV[1]
local approver = ARGV[2]
local now = ARGV[3]
local ttl = ARGV[4]
local raw = redis.call('GET', key)
if not raw then return 'null' end
local item = cjson.decode(raw)
if item.status ~= 'pending' then return raw end
item.status = decision
item.decided_at = now
item.decided_by = approver
local out = cjson.encode(item)
if tonumber(ttl) and tonumber(ttl) > 0 then
  redis.call('SET', key, out, 'EX', tonumber(ttl))
else
  redis.call('SET', key, out)
end
return out
"""


class RedisApprovalStore(ApprovalStore):
    """Redis 版审批单（每个审批单一个 JSON key），供多副本共享审批态。

    安全姿态：fail-closed。Redis 不可用时直接抛异常，而不是「静默放行 / 静默丢单」——
    审批闸是安全护栏，旁路故障应让高风险动作**卡住报错**而非绕过人工确认。
    多副本并发审批同一单时，靠上面的 Lua 脚本原子裁决，最后写入者生效（人工审批是
    一次性动作，不存在并发竞争的实际场景）。
    """

    def __init__(self, redis_url: Optional[str] = None, client=None,
                 key_prefix: str = KEY_PREFIX, ttl: int = DEFAULT_TTL):
        self._prefix = key_prefix
        self._ttl = ttl
        self._client = client
        if self._client is None:
            from .redis_factory import from_url  # 延迟导入
            self._client = from_url(
                redis_url or os.environ.get("TELEOPS_REDIS_URL",
                                            "redis://127.0.0.1:6379/0"),
                decode_responses=True)
        self._script = self._client.register_script(_DECISION_LUA)

    def _k(self, aid: str) -> str:
        return f"{self._prefix}{aid}"

    def create(self, subject, requested_by, payload, detail=""):
        aid = f"apr-{uuid.uuid4().hex[:8]}"
        item = {
            "id": aid, "subject": subject, "requested_by": requested_by,
            "detail": detail, "payload": payload, "status": "pending",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "decided_at": None, "decided_by": None,
        }
        payload_json = json.dumps(item, ensure_ascii=False)
        if self._ttl and self._ttl > 0:
            self._client.set(self._k(aid), payload_json, ex=self._ttl)
        else:
            self._client.set(self._k(aid), payload_json)
        return aid

    def get(self, aid):
        raw = self._client.get(self._k(aid))
        return json.loads(raw) if raw is not None else None

    def list_items(self, status=None, uid=None):
        out: List[Dict[str, Any]] = []
        for k in self._client.scan_iter(match=f"{self._prefix}*", count=500):
            raw = self._client.get(k)
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if status and item.get("status") != status:
                continue
            if uid and not (item.get("requested_by") == uid
                            or item.get("decided_by") == uid):
                continue
            out.append(item)
        # 按创建时间倒序（近似原 list_items 的 list(reversed(...)) 行为）
        out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return out

    def decide(self, aid, decision, approver):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        res = self._script(keys=[self._k(aid)],
                           args=[decision, approver, now, str(self._ttl or 0)])
        if res is None or res == "null":
            return None
        try:
            return json.loads(res)
        except Exception:
            return None


_approval_store: Optional[ApprovalStore] = None
_approval_store_lock = threading.Lock()


def get_approval_store() -> ApprovalStore:
    """按 ``TELEOPS_STATE_STORE`` 返回审批单存储后端（进程内缓存单例）。"""
    global _approval_store
    if _approval_store is not None:
        return _approval_store
    with _approval_store_lock:
        if _approval_store is not None:
            return _approval_store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        _approval_store = (RedisApprovalStore()
                           if backend == "redis" else LocalApprovalStore())
        return _approval_store


def configure_approval_store(store: Optional[ApprovalStore]) -> None:
    """显式指定后端（测试用）；None 则按环境变量重建。"""
    global _approval_store
    with _approval_store_lock:
        _approval_store = store


# ---------------------------------------------------------------------------
# 模块级公开 API（委托给当前后端，调用方无需感知 local/redis 差异）
# ---------------------------------------------------------------------------
def create(subject: str, requested_by: str, payload: Dict[str, Any],
           detail: str = "") -> str:
    """创建一条待审批单，返回 id。payload 在批准时由调用方执行。"""
    return get_approval_store().create(subject, requested_by, payload, detail)


def get(aid: str) -> Optional[Dict[str, Any]]:
    return get_approval_store().get(aid)


def list_items(status: Optional[str] = None,
               uid: Optional[str] = None) -> List[Dict[str, Any]]:
    return get_approval_store().list_items(status=status, uid=uid)


def decide(aid: str, decision: str, approver: str) -> Optional[Dict[str, Any]]:
    """decision: approved | rejected。返回更新后的审批单。"""
    return get_approval_store().decide(aid, decision, approver)
