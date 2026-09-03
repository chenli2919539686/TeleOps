"""多 Agent 注册表：管理多个运维/研发 Agent，支持按 scope 路由。

这是"业务运维一体化"的编排底座——把原来固定的"一个运维 + 一个研发"
升级为"可路由的多 Agent 矩阵"，配合消息栏(requirement_board)与派发器(dispatch)
形成人工 / 自动闭环。

- 每个 Agent 有 id / name / kind(ops|dev) / scope(擅长领域标签) / status
- 路由规则：需求 tags 与 agent.scope 有交集优先；否则轮询兜底
"""
from typing import Dict, List, Optional


class AgentRegistry:
    def __init__(self, cmdb, kb, tools, llm):
        self.cmdb = cmdb
        self.kb = kb
        self.tools = tools
        self.llm = llm
        self.agents: Dict[str, dict] = {}   # id -> {meta + instance}
        self._rr = {"ops": 0, "dev": 0}

    # ---------------- 注册 ----------------
    def register(self, kind, agent_id, name, scope, description="", agent=None,
                 primary=False, workspace_id=None):
        if agent is None:
            from src.agents.ops_agent import OpsAgent
            from src.agents.dev_agent import DevAgent
            agent = OpsAgent(self.cmdb, self.kb, self.tools, self.llm) if kind == "ops" \
                else DevAgent(self.cmdb, self.kb, self.llm)
        agent.agent_id = agent_id
        agent.agent_name = name
        self.agents[agent_id] = {
            "id": agent_id, "name": name, "kind": kind,
            "scope": scope, "description": description,
            "status": "idle", "instance": agent, "primary": primary,
            "workspace_id": workspace_id,
        }
        return agent

    # ---------------- 查询 ----------------
    def list(self, kind=None, workspace_id=None):
        out = []
        for a in self.agents.values():
            if kind and a["kind"] != kind:
                continue
            if workspace_id and a.get("workspace_id") != workspace_id:
                continue
            out.append({k: v for k, v in a.items() if k != "instance"})
        return out

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def get_instance(self, agent_id):
        a = self.agents.get(agent_id)
        return a["instance"] if a else None

    def primary(self, kind, workspace_id=None):
        # 优先返回指定业务域的 primary；找不到再取该域任一；最后全局兜底
        cands = [a for a in self.agents.values()
                 if a["kind"] == kind and (workspace_id is None or a.get("workspace_id") == workspace_id)]
        for a in cands:
            if a.get("primary"):
                return a["id"]
        if cands:
            return cands[0]["id"]
        # 全局兜底
        for a in self.agents.values():
            if a["kind"] == kind:
                return a["id"]
        return None

    # ---------------- 路由：按业务域隔离 + scope 交集，否则轮询 ----------------
    def route(self, kind, requirement: dict):
        ws = requirement.get("workspace_id")
        # 优先在同业务域内路由，遵守"每域独立一套 Agent"的设计承诺
        cands = [a for a in self.agents.values()
                 if a["kind"] == kind and (ws is None or a.get("workspace_id") == ws)]
        if not cands:
            # 同域无候选（理论上不会发生，建域时强制塞入 ops+dev），放宽到全局兜底
            cands = [a for a in self.agents.values() if a["kind"] == kind]
            if not cands:
                return None
        tags = set(requirement.get("tags", []) or [])
        hint = (requirement.get("needed_tool", "") + " " +
                requirement.get("description", ""))
        scored = []
        for a in cands:
            if tags:
                score = len(set(a["scope"]) & tags)
            else:
                score = sum(1 for s in a["scope"] if s in hint)
            scored.append((score, a["id"]))
        scored.sort(key=lambda x: -x[0])
        if scored[0][0] > 0:
            return scored[0][1]
        # 轮询兜底（限定在同域候选集合内）
        i = self._rr[kind] % len(cands)
        self._rr[kind] += 1
        return cands[i]["id"]

    def set_status(self, agent_id, status):
        if agent_id in self.agents:
            self.agents[agent_id]["status"] = status
            # 持久化到 SQLite，重启后仍可读回（避免内存态丢失）
            if getattr(self, "ws_store", None) is not None:
                self.ws_store.set_agent_status(agent_id, status)

    # ---------------- 改名 / 删除 ----------------
    def rename(self, agent_id, name):
        if agent_id in self.agents:
            self.agents[agent_id]["name"] = name
            inst = self.agents[agent_id]["instance"]
            inst.agent_name = name
            return True
        return False

    def update_scope(self, agent_id, scope, description=""):
        if agent_id in self.agents:
            self.agents[agent_id]["scope"] = scope
            if description != "":
                self.agents[agent_id]["description"] = description
            return True
        return False

    def delete(self, agent_id):
        if agent_id in self.agents:
            # 至少保留每个 kind 一个 agent，避免路由空轮询
            kind = self.agents[agent_id]["kind"]
            same = [a for a in self.agents.values() if a["kind"] == kind]
            if len(same) <= 1:
                return False, "每个业务域需至少保留一个该类型 Agent"
            del self.agents[agent_id]
            return True, ""
        return False, "Agent 不存在"

    def remove_by_workspace(self, ws_id):
        """删除指定业务域下的所有 agent 实例（用于删除业务域时级联清理）。"""
        before = len(self.agents)
        self.agents = {aid: a for aid, a in self.agents.items()
                       if a.get("workspace_id") != ws_id}
        return before - len(self.agents)
