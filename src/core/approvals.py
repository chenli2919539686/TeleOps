"""人工审批（HITL）存储层。

高风险动作（如研发造工具、启动告警流）在开启 require_approval 时，不直接执行，
而是落一条 pending 审批单，由管理员/被授权人批准后才真正执行——这是企业级
「人工闸」的核心（对标 AgentOS 的安全自愈护栏）。

存储：data/approvals.json（与 adapters.json 同约定，进程级内存 + 落盘）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_FILE = Path("data/approvals.json")
_lock = __import__("threading").Lock()

_state: Dict[str, Any] = {"items": []}


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


def create(subject: str, requested_by: str, payload: Dict[str, Any],
           detail: str = "") -> str:
    """创建一条待审批单，返回 id。payload 在批准时由调用方执行。"""
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


def get(aid: str) -> Optional[Dict[str, Any]]:
    with _lock:
        _load()
        return next((i for i in _state["items"] if i["id"] == aid), None)


def list_items(status: Optional[str] = None, uid: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        _load()
        items = _state["items"]
        if status:
            items = [i for i in items if i["status"] == status]
        if uid:
            items = [i for i in items if i["requested_by"] == uid or i.get("decided_by") == uid]
        return list(reversed(items))


def decide(aid: str, decision: str, approver: str) -> Optional[Dict[str, Any]]:
    """decision: approved | rejected。返回更新后的审批单。"""
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
