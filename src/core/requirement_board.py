"""消息栏 / 需求看板：汇总运维 Agent 提出的"工具缺口"需求，供人工 / 自动派发。

状态机：
  pending → dev_assigned → building → tool_ready → ops_assigned → done
  （任意阶段可 rejected）

每条需求记录：来源运维 Agent、原始告警、需要的工具、派发的研发/运维 Agent、
造出的工具名、两轮处置结果、trace 时间线。持久化改为 SQLite（data/teleops.db）。
"""
import json
from datetime import datetime
from typing import Optional

from src.core import db


class RequirementBoard:
    def __init__(self, path=None):
        # path 参数保留以兼容旧调用；持久化已统一走 SQLite
        self._seq = 1

    def _next_seq(self) -> int:
        """实时计算下一个 REQ 编号（避免内存 seq 漂移导致与历史数据主键冲突）。"""
        rows = db.query("SELECT id FROM requirements WHERE id LIKE 'REQ-%'")
        nums = [int(r["id"].split("-")[-1]) for r in rows if r["id"].startswith("REQ-")]
        return (max(nums) if nums else 0) + 1

    def add(self, req: dict) -> dict:
        if not req.get("id"):
            req["id"] = f"REQ-{self._next_seq():03d}"
        now = datetime.now().isoformat(timespec="seconds")
        req.setdefault("status", "pending")
        req.setdefault("workspace_id", None)
        req.setdefault("created_at", now)
        req.setdefault("updated_at", now)
        req.setdefault("trace", [])
        db.execute(
            "INSERT INTO requirements (id,workspace_id,status,data,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (req["id"], req.get("workspace_id"), req["status"],
             json.dumps(req, ensure_ascii=False), req["created_at"], req["updated_at"]))
        return req

    def list(self, status=None, workspace_id=None) -> list:
        rows = db.query("SELECT data FROM requirements ORDER BY created_at ASC")
        rs = [json.loads(r["data"]) for r in rows]
        if status:
            rs = [r for r in rs if r["status"] == status]
        if workspace_id is not None:
            rs = [r for r in rs if r.get("workspace_id") == workspace_id]
        return rs

    def get(self, req_id) -> Optional[dict]:
        r = db.query_one("SELECT data FROM requirements WHERE id=?", (req_id,))
        return json.loads(r["data"]) if r else None

    def update(self, req_id, **fields) -> Optional[dict]:
        r = db.query_one("SELECT data FROM requirements WHERE id=?", (req_id,))
        if not r:
            return None
        req = json.loads(r["data"])
        req.update(fields)
        req["updated_at"] = datetime.now().isoformat(timespec="seconds")
        db.execute("UPDATE requirements SET data=?, status=?, updated_at=? WHERE id=?",
                   (json.dumps(req, ensure_ascii=False), req["status"], req["updated_at"], req_id))
        return req

    def reset(self):
        db.execute("DELETE FROM requirements")
        self._seq = 1
