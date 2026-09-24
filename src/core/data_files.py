"""统一 JSON 文件数据访问层（demo 种子数据的唯一出口）。

设计意图：
- 把 server.py 内联的 ``Path(X).read_text()`` 收敛到这里，作为告警 / 拓扑 /
  闭环评估等「文件型 demo 数据」的唯一读取入口。
- 当前实现读 JSON 文件，返回与各 handler 内联直读**完全一致**，属纯结构收敛，
  不改变任何运行行为（想验证请看 tests/ 全绿）。
- 后续若要把这些 demo 数据迁进数据库（换信创库 / Postgres），只需替换本模块
  实现，所有 handler 无需改动——这正是「持久化出口统一、为换库铺路」的关键。

注意：users / workspaces / agents / requirements / tools / messages / audit 已在
``src.core.db``（SQLite）统一，本模块只承接「尚未入 DB 的文件型数据」。
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import ALERTS_FILE, TOPOLOGY_FILE


def load_alerts() -> Dict[str, Any]:
    """读取 data/alerts.json（BlueGene/L 机群告警样本），返回原始 dict。"""
    return json.loads(Path(ALERTS_FILE).read_text(encoding="utf-8"))


def load_topology() -> Dict[str, Any]:
    """读取拓扑文件（CMDB / 依赖图种子数据），返回原始 dict。"""
    return json.loads(Path(TOPOLOGY_FILE).read_text(encoding="utf-8"))


def load_eval_results(path) -> Optional[Dict[str, Any]]:
    """读取闭环评估结果 JSON（如 data/eval_results.json）。

    文件不存在或解析失败时返回 None（调用方已对 None 做兜底），与原来的
    ``if exists: try read except None`` 行为一致。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
