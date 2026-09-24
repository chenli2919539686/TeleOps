"""业务域（Workspace）持久化：每个北向接入/接口对应一个独立业务域，
域内自带一套运维 + 研发 Agent。元数据落盘 SQLite（data/teleops.db），
重启不丢，并发安全，为后续常驻服务做准备。

- Workspace: { id, name, adapter_id, mode('auto'|'manual'), owner_id, agents[] }
- Agent(域内): { id, name, kind('ops'|'dev'), scope[], description, primary, status }
"""
import json
import re
from typing import Optional, List, Dict, Any

from src.core import db, auth


def _slug(name: str) -> str:
    """把业务域名字转成可读 id 片段（中文/符号 -> ws- 前缀 + 序号由调用方补）。"""
    s = re.sub(r"[^a-zA-Z0-9]", "-", name).strip("-").lower()
    return s or "ws"


DEFAULT_WORKSPACE = {
    "id": "core-net",
    "name": "核心网运维域",
    "adapter_id": "alert-prometheus",
    "mode": "auto",
    "agents": [
        {"id": "core-net-ops-main", "name": "核心网运维 Agent", "kind": "ops",
         "scope": ["core", "compute"], "description": "负责核心网 / 算力设备告警处置", "primary": True},
        {"id": "core-net-ops-2", "name": "接入网运维 Agent", "kind": "ops",
         "scope": ["access", "optical", "onu"], "description": "负责接入网 / 光模块 / ONU 告警处置", "primary": False},
        {"id": "core-net-dev-main", "name": "网络工具研发 Agent", "kind": "dev",
         "scope": ["net", "optical"], "description": "负责网络 / 光层探测工具研发", "primary": True},
        {"id": "core-net-dev-2", "name": "通用工具研发 Agent", "kind": "dev",
         "scope": ["compute", "generic"], "description": "负责算力 / 通用运维工具研发", "primary": False},
    ],
}


class WorkspaceStore:
    def __init__(self, path=None, registry=None):
        # path 参数保留以兼容旧调用，持久化已统一走 SQLite
        self.registry = registry
        if self._count_workspaces() == 0:
            self._seed_default()
        self._ensure_default_agents()
        self._sync_registry()

    # ---------------- 启动：从 SQLite 同步进 AgentRegistry ----------------
    def _seed_default(self):
        w = dict(DEFAULT_WORKSPACE)
        db.execute(
            "INSERT OR IGNORE INTO workspaces (id,name,adapter_id,mode,owner_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (w["id"], w["name"], w.get("adapter_id"), w.get("mode", "auto"), None, db._now()))
        for a in w["agents"]:
            db.execute(
                "INSERT OR IGNORE INTO agents "
                "(id,workspace_id,name,kind,scope,description,is_primary,status) VALUES (?,?,?,?,?,?,?,?)",
                (a["id"], w["id"], a["name"], a["kind"], json.dumps(a.get("scope", []), ensure_ascii=False),
                 a.get("description", ""), 1 if a.get("primary") else 0, "idle"))

    def _ensure_default_agents(self):
        """迁移补种：保证核心网公共域具备默认的多专长 Agent 矩阵（含 2 个运维 Agent），

        使 registry.route 按 scope 交集的告警分流在公共域天然生效。幂等（INSERT OR IGNORE）。

        已存在 DB 的库不会重跑 _seed_default，故用本函数补齐核心-net 的默认 Agent。

        """
        wid = DEFAULT_WORKSPACE["id"]
        if not db.query_one("SELECT 1 FROM workspaces WHERE id=?", (wid,)):
            return
        for a in DEFAULT_WORKSPACE["agents"]:
            db.execute(
                "INSERT OR IGNORE INTO agents "
                "(id,workspace_id,name,kind,scope,description,is_primary,status) VALUES (?,?,?,?,?,?,?,?)",
                (a["id"], wid, a["name"], a["kind"], json.dumps(a.get("scope", []), ensure_ascii=False),
                 a.get("description", ""), 1 if a.get("primary") else 0, "idle"))

    def _sync_registry(self):
        if self.registry is None:
            return
        for w in db.query("SELECT * FROM workspaces"):
            for a in db.query("SELECT * FROM agents WHERE workspace_id=?", (w["id"],)):
                self.registry.register(
                    a["kind"], a["id"], a["name"], json.loads(a["scope"]),
                    a["description"], primary=bool(a["is_primary"]),
                    workspace_id=w["id"],
                )
                self.registry.set_status(a["id"], a["status"])

    # ---------------- 查询 ----------------
    def _count_workspaces(self) -> int:
        return db.query_one("SELECT COUNT(*) AS c FROM workspaces")["c"]

    def _merge_status(self, agents: List[dict]) -> List[dict]:
        if self.registry is None:
            return agents
        out = []
        for a in agents:
            a = dict(a)
            inst = self.registry.get(a["id"])
            if inst:
                a["status"] = inst["status"]
            out.append(a)
        return out

    def list(self, owner_id: Optional[int] = None, user: Optional[dict] = None) -> List[dict]:
        """列出业务域（按组织树 + RBAC 做多租户隔离）。

        - user 不传（None）→ 仅返回公共域（向后兼容系统/管理视角，配合 is_admin 全可见）。
        - 旧调用若传 owner_id（int），等效于只看该 owner 的个人域 + 公共域。
        - 标准行为：返回「公共域 + 用户组织树可见域 + 本人个人域」。
        """
        if user is None and owner_id is not None:
            user = {"uid": owner_id, "is_admin": False}  # 旧 owner_id 语义兼容
        out = []
        for w in db.query("SELECT * FROM workspaces ORDER BY id"):
            if not self._visible_to_user(w, user):
                continue
            agents = self._agents_of(w["id"])
            out.append({
                "id": w["id"], "name": w["name"], "adapter_id": w["adapter_id"],
                "mode": w["mode"], "owner_id": w["owner_id"],
                "agent_count": len(agents),
                "ops": [a for a in agents if a["kind"] == "ops"],
                "dev": [a for a in agents if a["kind"] == "dev"],
            })
        return out

    def visible_workspace_ids(self, owner_id: Optional[int] = None,
                              user: Optional[dict] = None) -> List[str]:
        """返回某用户可见的业务域 id 集合，供 /agents、/audit 等接口做隔离过滤。"""
        return [w["id"] for w in self.list(user=user)]

    def _visible_to_user(self, w: dict, user: Optional[dict]) -> bool:
        """统一可见性判定：公共域 / 本人个人域 / 组织树包含 + ws.view 权限 / admin 全可见。"""
        owner_id = w.get("owner_id")
        org_id = w.get("org_id")
        if owner_id is None and org_id is None:
            return True  # 公共域（如 core-net）对所有用户可见
        if not user:
            return False
        if user.get("is_admin"):
            return True
        if owner_id is not None and owner_id == user.get("uid"):
            return True
        if org_id and auth.enforce(user, "ws.view", org_id):
            return True
        return False

    def is_visible_to(self, ws_id: str, user: Optional[dict]) -> bool:
        """当前用户是否有权查看该业务域。"""
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (ws_id,))
        if not w:
            return False
        return self._visible_to_user(w, user)

    def is_writable_by(self, ws_id: str, user: Optional[dict]) -> bool:
        """当前用户是否有权修改该业务域（增删改 agent / 改 mode / 删域）。

        规则（组织树 + RBAC）：
        - super_admin 可写任何域；
        - 本人个人域（owner_id==uid）可写；
        - 用户在目标 org 拥有 ws.manage 权限 → 可写其组织树下的域；
        - 公共域仅 admin 可写。
        """
        if not user:
            return False
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (ws_id,))
        if not w:
            return False
        if user.get("is_admin"):
            return True
        owner_id = w["owner_id"]
        if owner_id is not None and owner_id == user.get("uid"):
            return True
        org_id = w["org_id"]
        if org_id and auth.enforce(user, "ws.manage", org_id):
            return True
        return False

    def get(self, ws_id) -> Optional[dict]:
        w = db.query_one("SELECT * FROM workspaces WHERE id=?", (ws_id,))
        if not w:
            return None
        agents = self._agents_of(ws_id)
        return {
            "id": w["id"], "name": w["name"], "adapter_id": w["adapter_id"],
            "mode": w["mode"], "owner_id": w["owner_id"], "agents": agents,
        }

    def _agents_of(self, ws_id) -> List[dict]:
        rows = db.query("SELECT * FROM agents WHERE workspace_id=? ORDER BY id", (ws_id,))
        agents = [{
            "id": r["id"], "name": r["name"], "kind": r["kind"],
            "scope": json.loads(r["scope"]), "description": r["description"],
            "primary": bool(r["is_primary"]), "status": r["status"],
        } for r in rows]
        return self._merge_status(agents)

    # ---------------- 业务域 CRUD ----------------
    def _next_ws_id(self) -> str:
        ids = [r["id"] for r in db.query("SELECT id FROM workspaces")]
        n = 1
        while f"ws-{n}" in ids:
            n += 1
        return f"ws-{n}"

    def create(self, name: str, adapter_id: Optional[str], mode: str = "auto",
               custom_id: Optional[str] = None, owner_id: Optional[int] = None,
               org_id: Optional[str] = None) -> dict:
        ws_id = custom_id or self._next_ws_id()
        if db.query_one("SELECT 1 FROM workspaces WHERE id=?", (ws_id,)):
            raise ValueError(f"业务域 {ws_id} 已存在")
        db.execute(
            "INSERT INTO workspaces (id,name,adapter_id,mode,owner_id,org_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ws_id, name, adapter_id, mode, owner_id, org_id, db._now()))
        # 每个新域初始化一套基础 Agent：1 运维 + 1 研发，可后续改名/扩展
        agents = [
            {"id": f"{ws_id}-ops-main", "name": f"{name}·运维 Agent", "kind": "ops",
             "scope": ["compute"], "description": "运维告警处置主 Agent", "primary": True},
            {"id": f"{ws_id}-dev-main", "name": f"{name}·研发 Agent", "kind": "dev",
             "scope": ["generic"], "description": "工具研发主 Agent", "primary": True},
        ]
        for a in agents:
            db.execute(
                "INSERT INTO agents (id,workspace_id,name,kind,scope,description,is_primary,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a["id"], ws_id, a["name"], a["kind"], json.dumps(a["scope"], ensure_ascii=False),
                 a["description"], 1 if a["primary"] else 0, "idle"))
            if self.registry:
                self.registry.register(a["kind"], a["id"], a["name"], a["scope"],
                                       a["description"], primary=a["primary"], workspace_id=ws_id)
        return self.get(ws_id)

    def create_personal(self, owner_id: int, username: str, org_id: Optional[str] = None) -> dict:
        """为新注册用户建一套个人业务域，复制默认域的完整 Agent 矩阵（4 个）。

        域 id 用 ws-<username> 便于辨识且天然唯一（重名用户 id 不同但用户名唯一）；
        若已存在则追加序号兜底。owner_id 绑定到注册用户，保证多租户隔离下只有本人可见。
        """
        base_id = _slug(username) or "ws"
        ws_id = f"ws-{base_id}"
        n = 2
        while db.query_one("SELECT 1 FROM workspaces WHERE id=?", (ws_id,)):
            ws_id = f"ws-{base_id}-{n}"
            n += 1
        name = f"{username} 的工作域"
        db.execute(
            "INSERT INTO workspaces (id,name,adapter_id,mode,owner_id,org_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ws_id, name, "alert-prometheus", "auto", owner_id, org_id, db._now()))
        for a in DEFAULT_WORKSPACE["agents"]:
            agent_id = f"{ws_id}-{a['id'].split('-', 1)[-1]}"  # 去掉默认域前缀，换成个人域前缀
            db.execute(
                "INSERT OR IGNORE INTO agents "
                "(id,workspace_id,name,kind,scope,description,is_primary,status) VALUES (?,?,?,?,?,?,?,?)",
                (agent_id, ws_id, a["name"], a["kind"], json.dumps(a.get("scope", []), ensure_ascii=False),
                 a.get("description", ""), 1 if a.get("primary") else 0, "idle"))
            if self.registry:
                self.registry.register(a["kind"], agent_id, a["name"], a.get("scope", []),
                                       a.get("description", ""), primary=bool(a.get("primary")),
                                       workspace_id=ws_id)
        return self.get(ws_id)

    def rename(self, ws_id, name) -> bool:
        cur = db.execute("UPDATE workspaces SET name=? WHERE id=?", (name, ws_id))
        return cur.rowcount > 0

    def update_mode(self, ws_id, mode) -> bool:
        if mode not in ("auto", "manual"):
            return False
        cur = db.execute("UPDATE workspaces SET mode=? WHERE id=?", (mode, ws_id))
        return cur.rowcount > 0

    # ---------------- 域内 Agent 管理 ----------------
    def _next_agent_id(self, ws_id, kind) -> str:
        prefix = f"{ws_id}-{kind}"
        seq = 1
        existing = [r["id"] for r in db.query("SELECT id FROM agents WHERE workspace_id=?", (ws_id,))]
        while f"{prefix}-{seq:02d}" in existing:
            seq += 1
        return f"{prefix}-{seq:02d}"

    def add_agent(self, ws_id, kind, name, scope, description="", primary=False) -> dict:
        if kind not in ("ops", "dev"):
            raise ValueError("kind 必须为 ops 或 dev")
        if not db.query_one("SELECT 1 FROM workspaces WHERE id=?", (ws_id,)):
            raise ValueError("业务域不存在")
        agent_id = self._next_agent_id(ws_id, kind)
        db.execute(
            "INSERT INTO agents (id,workspace_id,name,kind,scope,description,is_primary,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, ws_id, name, kind, json.dumps(scope or [], ensure_ascii=False),
             description, 1 if primary else 0, "idle"))
        if self.registry:
            self.registry.register(kind, agent_id, name, scope, description,
                                   primary=primary, workspace_id=ws_id)
        return {
            "id": agent_id, "name": name, "kind": kind, "scope": scope,
            "description": description, "primary": primary, "status": "idle",
        }

    def rename_agent(self, ws_id, agent_id, name) -> bool:
        cur = db.execute("UPDATE agents SET name=? WHERE id=? AND workspace_id=?", (name, agent_id, ws_id))
        if cur.rowcount > 0 and self.registry:
            self.registry.rename(agent_id, name)
        return cur.rowcount > 0

    def update_agent(self, ws_id, agent_id, scope=None, description=None) -> bool:
        if scope is not None:
            db.execute("UPDATE agents SET scope=? WHERE id=? AND workspace_id=?",
                       (json.dumps(scope, ensure_ascii=False), agent_id, ws_id))
        if description is not None:
            db.execute("UPDATE agents SET description=? WHERE id=? AND workspace_id=?",
                       (description, agent_id, ws_id))
        if self.registry:
            a = db.query_one("SELECT scope,description FROM agents WHERE id=?", (agent_id,))
            if a:
                self.registry.update_scope(agent_id, json.loads(a["scope"]), a["description"])
        return True

    def delete_agent(self, ws_id, agent_id) -> tuple:
        agents = db.query("SELECT * FROM agents WHERE workspace_id=?", (ws_id,))
        a = next((x for x in agents if x["id"] == agent_id), None)
        if not a:
            return False, "Agent 不存在"
        same = [x for x in agents if x["kind"] == a["kind"]]
        if len(same) <= 1:
            return False, "每个业务域需至少保留一个该类型 Agent"
        db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        if self.registry:
            self.registry.delete(agent_id)
        return True, ""

    def set_agent_status(self, agent_id: str, status: str):
        """持久化 Agent 实时状态（供 registry.set_status 回调调用，重启后仍可读回）。"""
        db.execute("UPDATE agents SET status=? WHERE id=?", (status, agent_id))

    def delete_workspace(self, ws_id) -> tuple:
        """删除业务域（默认域 core-net 受保护），并级联清理其下所有数据。

        级联范围：Agent 实例 + 需求 + 操作记录。
        需求与消息没有指向 workspaces 的外键级联，若不显式清理会成为孤儿行；
        业务域 id 一旦被复用（测试/重建同名域场景），新域会读到上一个域的残留。
        """
        if ws_id == "core-net":
            raise ValueError("默认业务域「核心网运维域」不可删除")
        if not db.query_one("SELECT 1 FROM workspaces WHERE id=?", (ws_id,)):
            return False, "业务域不存在"
        if self.registry:
            self.registry.remove_by_workspace(ws_id)
        # agents 随外键 ON DELETE CASCADE 自动清理；需求/消息需显式删除
        db.execute("DELETE FROM requirements WHERE workspace_id=?", (ws_id,))
        db.execute("DELETE FROM messages WHERE workspace_id=?", (ws_id,))
        db.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        return True, ""
