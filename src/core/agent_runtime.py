"""Agent 运行时工厂（D5）。

背景（为什么要这个模块）
--------------------------
重构前，server.py 里有三个"写死"的全局运行时对象::

    ops  = OpsAgent(cmdb, kb, tools, llm)
    dev  = DevAgent(cmdb, kb, llm)
    ops_graph = build_ops_graph(ops)

虽然 :class:`src.core.agent_registry.AgentRegistry` 已经能按业务域注册各自的
OpsAgent / DevAgent 实例（``register()`` 里逐实例 new），但**真正执行**时走的还是
全局那一份：``ops_graph.invoke(state)`` / ``dev.fulfill_feedback(fb)``。

结果是多租户只体现在"路由与状态灯"（``set_status(ops_id, ...)``）上，
实际推理、工具可见性、KB 缓存仍是所有租户共享的同一个 Agent 实例 ——
Phase 1 的组织化隔离推不动，per-tenant 配额/审计也没法挂。

本模块做什么
------------
把"这次该由哪个 Agent 实例执行"收敛到一个工厂，成为 Agent 实例的统一出口：

- :meth:`ops_instance` / :meth:`dev_instance`：按 ``agent_id``（或 ``workspace_id``
  下该 kind 的 primary）从注册表解析实例；查不到才回退全局单例 → **保证旧行为不回归**。
- :meth:`ops_graph`：为该实例编译 LangGraph 并缓存（键=实例身份）。
  图是围绕 agent 对象建的闭包（见 ``build_ops_graph``），运行期读 ``agent.tools``，
  所以实例后续被 reload 重新绑定 tools/kb 时，缓存的图照样生效，无需重建。

不在本模块范围内
----------------
- 实例本身的构造（仍由 AgentRegistry 负责）
- 工具/知识的重载（仍由 server 的 ``_reload_all`` 负责，需保证_ALL_实例一并重绑，
  否则出现"A 域实例持有旧 tools，看不到刚造出的新工具"）
"""
from typing import Any, Optional, Tuple
import threading


class AgentRuntime:
    """按租户/业务域解析 Agent 实例与执行图的工厂。

    :param registry: AgentRegistry，实例来源
    :param ops: 兜底的全局 OpsAgent 单例（agent_id 解析不到时使用）
    :param dev: 兜底的全局 DevAgent 单例
    :param graph_builder: 由 OpsAgent 实例编译 LangGraph 的函数，默认 build_ops_graph
    """

    def __init__(self, registry, ops=None, dev=None, graph_builder=None):
        self.registry = registry
        self._fallback_ops = ops
        self._fallback_dev = dev
        self._graph_builder = graph_builder
        self._graphs = {}          # (agent_id, id(instance)) -> compiled graph
        self._lock = threading.Lock()

    # ---------------- 实例解析 ----------------
    def _resolve(self, kind: str, agent_id: Optional[str],
                 workspace_id: Optional[str], fallback):
        """解析 (agent_id, instance)；解析不到返回 (None, fallback)。"""
        aid = agent_id
        if not aid and workspace_id:
            aid = self.registry.primary(kind, workspace_id)
        if aid:
            rec = self.registry.get(aid)
            # kind 不匹配（例如把 ops id 喂给 dev）视为解析失败，走兜底
            if rec and rec.get("kind") == kind:
                inst = rec.get("instance")
                if inst is not None:
                    return aid, inst
        return None, fallback

    def ops_instance(self, agent_id: Optional[str] = None,
                     workspace_id: Optional[str] = None) -> Tuple[Optional[str], Any]:
        return self._resolve("ops", agent_id, workspace_id, self._fallback_ops)

    def dev_instance(self, agent_id: Optional[str] = None,
                     workspace_id: Optional[str] = None) -> Tuple[Optional[str], Any]:
        return self._resolve("dev", agent_id, workspace_id, self._fallback_dev)

    # ---------------- 执行图 ----------------
    def _graph_builder_or_default(self):
        if self._graph_builder is not None:
            return self._graph_builder
        # 延迟导入：避免 core 层与 orchestration 层形成固定耦合（也防循环导入）
        from src.orchestration.graphs import build_ops_graph
        self._graph_builder = build_ops_graph
        return build_ops_graph

    def ops_graph(self, agent_id: Optional[str] = None,
                  workspace_id: Optional[str] = None):
        """返回绑定到该业务域 ops Agent 实例的 LangGraph。

        解析不到实例时用兜底的全局 ops 编译，语义等价于旧的全局 ``ops_graph``，
        只是改为惰性编译；图持有 agent 对象，实例被重绑 tools/kb 后仍然有效。
        """
        aid, inst = self.ops_instance(agent_id, workspace_id)
        if inst is None:                     # 连兜底单例都没有
            return None
        key = (aid, id(inst))
        with self._lock:
            g = self._graphs.get(key)
            if g is None:
                g = self._graph_builder_or_default()(inst)
                self._graphs[key] = g
            return g

    def invalidate(self, agent_id: Optional[str] = None) -> None:
        """丢弃图缓存（重建 / 删除 Agent 后调用；不传则全清）。"""
        with self._lock:
            if agent_id is None:
                self._graphs.clear()
                return
            for key in [k for k in self._graphs if k[0] == agent_id]:
                del self._graphs[key]
