"""工具库 registry：以 SQLite tools 表为唯一事实源，实时查询（活视图）。

工具脚本（executor）仍是磁盘上的 tools/<name>.py；本类只管「注册表元数据」持久化，
落盘到 data/teleops.db 的 tools 表。

v0.7.2 修复：历史上 __init__ 把工具列表缓存在内存，研发 Agent 造出新工具后，
已存在的 OpsAgent 实例（以及 AgentRegistry.tools 持有的旧引用）看不到新工具，
导致同一缺口被反复登记成重复需求。现在 list_tools()/get() 每次直查数据库，
任何实例、任何时刻都能看到最新工具库——「Agent 造的工具全队复用」。
"""
import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import db
from src.config import TOOLS_DIR


class ToolRegistry:
    # 查询列统一口径：与 tools 表元数据字段一一对应
    _COLS = "name,executor,risk,require_human_approval,description"

    @staticmethod
    def _row_to_tool(r) -> dict:
        return {
            "name": r["name"], "executor": r["executor"], "risk": r["risk"],
            "require_human_approval": bool(r["require_human_approval"]),
            "description": r["description"],
        }

    # ---------------- 查询（实时直查 SQLite，无内存缓存） ----------------
    def list_tools(self):
        rows = db.query(f"SELECT name FROM tools ORDER BY name")
        return [r["name"] for r in rows]

    def get(self, name):
        rows = db.query(f"SELECT {self._COLS} FROM tools WHERE name=?", (name,))
        return self._row_to_tool(rows[0]) if rows else None

    def requires_approval(self, name):
        t = self.get(name)
        if not t:
            return True
        return t.get("risk") == "high" or t.get("require_human_approval", False)

    # ---------------- 变更（写库即全局可见） ----------------
    def remove(self, name: str) -> bool:
        """删除一个工具（落库即生效）。工具生命周期管理用。"""
        if not self.get(name):
            return False
        db.execute("DELETE FROM tools WHERE name=?", (name,))
        return True

    def add(self, tool: dict):
        """新增 / 更新一个工具元数据（研发 Agent 造工具后调用）。"""
        db.execute(
            "INSERT OR REPLACE INTO tools "
            "(name,executor,risk,require_human_approval,description,workspace_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tool["name"], tool.get("executor"), tool.get("risk", "low"),
             1 if tool.get("require_human_approval") else 0,
             tool.get("description", ""), tool.get("workspace_id"), db._now()))
        return tool

    # ---------------- 调用 ----------------
    def _executor_path(self, executor: str) -> Path:
        """解析工具脚本路径：相对路径按 TOOLS_DIR 取文件名，兼容 tools/xxx.py 写法。"""
        p = Path(executor)
        if p.is_absolute():
            return p
        return TOOLS_DIR / p.name

    def call(self, name, params: dict):
        """调用工具：动态 import tools/<executor> 的 run(params)。

        W1 阶段：工具脚本为桩，返回模拟结果；真实执行在 W2 接入。
        """
        t = self.get(name)
        if not t:
            raise KeyError(f"工具不存在: {name}")
        if self.requires_approval(name):
            return {"status": "blocked", "reason": "高风险工具需人工确认", "tool": name}
        exec_path = self._executor_path(t["executor"])
        if not exec_path.exists():
            return {"status": "error", "reason": f"executor 缺失: {t['executor']}"}
        spec = importlib.util.spec_from_file_location(f"_tool_{name}", exec_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run(params or {})


if __name__ == "__main__":
    r = ToolRegistry()
    print("已注册工具:", r.list_tools())
    print("ping_host 调用:", r.call("ping_host", {"host": "sw-core"}))
    print("restart_service 需确认:", r.requires_approval("restart_service"))
