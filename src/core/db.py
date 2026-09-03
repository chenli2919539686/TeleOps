"""TeleOps SQLite 持久化层（取代原先的 JSON 文件存储）。

单文件数据库 data/teleops.db；首次运行自动建表，并从遗留的 JSON 文件
（workspaces.json / requirements.json / tools.json / messages.json）一次性迁移，
保证既有「核心网运维域 + 4 Agent + 2 工具」基线不丢。

设计要点：
- 用标准库 sqlite3，零额外依赖；连接开启 foreign_keys 并加锁保证线程安全。
- 所有写接口走 execute()，读接口走 query()/query_one()，均在锁内完成。
- 表结构：users / workspaces / agents / requirements / tools / messages。
"""
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR

# 数据库文件：默认 data/teleops.db；可用环境变量 TELEOPS_DB_FILE 覆盖（测试隔离用）
DB_PATH = Path(os.environ.get("TELEOPS_DB_FILE") or str(DATA_DIR / "teleops.db"))
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  adapter_id TEXT,
  mode TEXT DEFAULT 'auto',
  owner_id INTEGER,
  created_at TEXT,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '[]',
  description TEXT DEFAULT '',
  is_primary INTEGER DEFAULT 0,
  status TEXT DEFAULT 'idle',
  FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS requirements (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  status TEXT DEFAULT 'pending',
  data TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tools (
  name TEXT PRIMARY KEY,
  executor TEXT,
  risk TEXT DEFAULT 'low',
  require_human_approval INTEGER DEFAULT 0,
  description TEXT DEFAULT '',
  workspace_id TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  agent_id TEXT,
  agent_name TEXT,
  kind TEXT,
  summary TEXT,
  detail TEXT,
  ts TEXT
);
"""

_conn = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """惰性创建并缓存全局连接（线程安全）。

    高可用加固（Phase 3）：
    - journal_mode=WAL：读写不互斥（读不阻塞写、写不阻塞读），进程崩溃可恢复，
      适合"HTTP 读请求 + 后台 Agent 写任务"并发的运行形态；
    - synchronous=NORMAL：WAL 下兼顾持久性与吞吐（崩溃最多丢最近一次提交，不损坏库）；
    - busy_timeout=5000：锁冲突时最多等 5s 而非立刻抛 database is locked（配合外层锁兜底）。
    """
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute("PRAGMA synchronous = NORMAL")
        _conn.execute("PRAGMA busy_timeout = 5000")
        _init_schema()
        _migrate_from_json()
    return _conn


def _init_schema():
    _conn.executescript(_SCHEMA)
    _conn.commit()


def _migrate_from_json():
    """仅在 workspaces 表为空且遗留 JSON 存在时迁移一次。"""
    if get_conn().execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] > 0:
        return
    # 业务域 + Agent
    wf = DATA_DIR / "workspaces.json"
    if wf.exists():
        try:
            data = json.loads(wf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for w in data.get("workspaces", []):
            get_conn().execute(
                "INSERT OR IGNORE INTO workspaces (id,name,adapter_id,mode,owner_id,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (w["id"], w["name"], w.get("adapter_id"), w.get("mode", "auto"),
                 None, _now()))
            for a in w.get("agents", []):
                get_conn().execute(
                    "INSERT OR IGNORE INTO agents "
                    "(id,workspace_id,name,kind,scope,description,is_primary,status) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (a["id"], w["id"], a["name"], a["kind"],
                     json.dumps(a.get("scope", []), ensure_ascii=False),
                     a.get("description", ""), 1 if a.get("primary") else 0,
                     a.get("status", "idle")))
    # 需求
    rf = DATA_DIR / "requirements.json"
    if rf.exists():
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for r in data.get("requirements", []):
            get_conn().execute(
                "INSERT OR IGNORE INTO requirements "
                "(id,workspace_id,status,data,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (r.get("id"), r.get("workspace_id"), r.get("status", "pending"),
                 json.dumps(r, ensure_ascii=False), r.get("created_at"), r.get("updated_at")))
    # 工具
    tf = DATA_DIR / "tools.json"
    if tf.exists():
        try:
            data = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for t in data.get("tools", []):
            get_conn().execute(
                "INSERT OR IGNORE INTO tools "
                "(name,executor,risk,require_human_approval,description,workspace_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (t.get("name"), t.get("executor"), t.get("risk", "low"),
                 1 if t.get("require_human_approval") else 0,
                 t.get("description", ""), t.get("workspace_id"), _now()))
    # 操作记录
    mf = DATA_DIR / "messages.json"
    if mf.exists():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for ws_id, msgs in data.get("messages", {}).items():
            for m in msgs:
                get_conn().execute(
                    "INSERT OR IGNORE INTO messages "
                    "(id,workspace_id,agent_id,agent_name,kind,summary,detail,ts) VALUES (?,?,?,?,?,?,?,?)",
                    (m.get("id"), ws_id, m.get("agent_id"), m.get("agent_name"),
                     m.get("kind"), m.get("summary"), m.get("detail"), m.get("ts")))
    get_conn().commit()


# ---------------- 通用封装（线程安全） ----------------
def execute(sql: str, params=()):
    """执行写操作并提交，返回 cursor（调用方可用 rowcount 判断影响行数）。"""
    with _LOCK:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
    return cur


def query(sql: str, params=()):
    with _LOCK:
        rows = get_conn().execute(sql, params).fetchall()
    return rows


def query_one(sql: str, params=()):
    with _LOCK:
        row = get_conn().execute(sql, params).fetchone()
    return row
