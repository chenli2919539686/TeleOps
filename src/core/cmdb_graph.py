"""CMDB 拓扑图模块：加载拓扑并提供上下游/邻居查询。

优先使用 networkx（若已安装），否则退化为内置轻量图实现，
保证脚手架在无第三方依赖时也能跑通。
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import networkx as nx
    _HAS_NX = True
except ImportError:
    _HAS_NX = False

from src.config import TOPOLOGY_FILE


class CMDBGraph:
    def __init__(self, path: Path = TOPOLOGY_FILE):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._build()

    def _build(self):
        if _HAS_NX:
            self._g = nx.DiGraph()
            for n in self.data["nodes"]:
                self._g.add_node(n["id"], **n)
            for e in self.data["edges"]:
                self._g.add_edge(e["from"], e["to"], rel=e.get("rel", ""))
        else:
            self._adj_out = {}
            self._adj_in = {}
            for n in self.data["nodes"]:
                self._adj_out.setdefault(n["id"], [])
                self._adj_in.setdefault(n["id"], [])
            for e in self.data["edges"]:
                self._adj_out.setdefault(e["from"], []).append(e["to"])
                self._adj_in.setdefault(e["to"], []).append(e["from"])

    def dependencies(self, node):
        """node 直接依赖的节点（node -> x，x 是 node 的供应方）。"""
        if _HAS_NX:
            return list(self._g.successors(node))
        return self._adj_out.get(node, [])

    def dependents(self, node):
        """直接依赖 node 的节点（x -> node，node 是 x 的依赖）。"""
        if _HAS_NX:
            return list(self._g.predecessors(node))
        return self._adj_in.get(node, [])

    def neighbors(self, node):
        s = set(self.dependencies(node)) | set(self.dependents(node))
        return list(s)

    def node_info(self, node):
        for n in self.data["nodes"]:
            if n["id"] == node:
                return n
        return None

    def all_nodes(self):
        return [n["id"] for n in self.data["nodes"]]


if __name__ == "__main__":
    g = CMDBGraph()
    print("节点:", g.all_nodes())
    print("svc-order 依赖:", g.dependencies("svc-order"))
    print("db-order 被谁依赖:", g.dependents("db-order"))
