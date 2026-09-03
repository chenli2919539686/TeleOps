"""TeleOps 适配器抽象层（北向感知 / 南向执行）。

设计目标：TeleOps 内核只认「统一 Schema」（Alert / Tool / CMDB / Feedback），
不关心外部到底是 Prometheus、Zabbix、iMaster 还是 ELK。所有外部系统都通过
「适配器」做协议翻译后再进内核——这就是「叠加在现有运维系统之上、而非替换」的核心。

方向约定：
  - NORTH（北向 / 感知）：外部系统 -> 内核（把外面的数据搬进来）
      AlertAdapter / LogAdapter / CMDBAdapter / TicketAdapter / KnowledgeAdapter
  - SOUTH（南向 / 执行）：内核 -> 外部系统（把 Agent 的决策真正落下去）
      ExecAdapter

状态约定（status 字段）：
  - reserved : 接口已设计、待接入真实系统（占位，调用返回 reserved 标记）
  - sample   : 样板实现（可跑，用于演示或本地文件对接）
  - active   : 已配置真实连接（未来接生产时置位）
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

# 方向
NORTH = "north"
SOUTH = "south"

# 适配器类型 -> 中文说明（用于前端展示与文档）
ADAPTER_TYPES: Dict[str, str] = {
    "alert": "告警接入（北向·感知）",
    "log": "日志接入（北向·感知）",
    "cmdb": "CMDB/拓扑接入（北向·感知）",
    "ticket": "工单接入（北向·感知）",
    "exec": "执行接入（南向·执行）",
    "knowledge": "知识接入（北向·感知）",
}


class BaseAdapter(ABC):
    """所有外部系统适配器的抽象基类。

    子类必须填充 6 个元数据字段，并实现 :meth:`healthcheck`。
    各类型基类（AlertAdapter 等）再追加类型特有的抽象方法。
    """

    # ---- 子类必须覆盖的元数据 ----
    id: str = ""
    name: str = ""
    adapter_type: str = ""
    system: str = ""           # 对接的外部系统，如 Prometheus / 华为 iMaster
    direction: str = ""        # NORTH / SOUTH
    status: str = "reserved"   # reserved / sample / active
    description: str = ""

    @abstractmethod
    def healthcheck(self) -> Dict[str, Any]:
        """探测外部系统是否可达，返回连通性信息。"""
        ...

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "adapter_type": self.adapter_type,
            "adapter_type_label": ADAPTER_TYPES.get(self.adapter_type, self.adapter_type),
            "system": self.system,
            "direction": self.direction,
            "direction_label": "北向·感知" if self.direction == NORTH else "南向·执行",
            "status": self.status,
            "status_label": {"reserved": "预留", "sample": "样板", "active": "已接入"}[self.status],
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# 北向：告警接入
# ---------------------------------------------------------------------------
class AlertAdapter(BaseAdapter):
    adapter_type = "alert"
    direction = NORTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def parse_webhook(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析外部 webhook 原始报文 -> 0..n 条内核 Alert。子类必须实现。"""
        raise NotImplementedError("AlertAdapter.parse_webhook 需由具体适配器实现")

    def to_unified(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """单条外部告警 -> 内核统一 Alert（默认映射，子类可重写）。"""
        return {
            "alert_id": str(raw.get("alert_id") or raw.get("id") or ""),
            "ts": raw.get("ts") or raw.get("startsAt") or "",
            "source": raw.get("source") or self.system,
            "metric": raw.get("metric") or raw.get("name") or "",
            "host": raw.get("host") or raw.get("instance") or "",
            "severity": str(raw.get("severity") or "warning").lower(),
            "value": raw.get("value", ""),
            "message": raw.get("message") or raw.get("description") or "",
            "tags": raw.get("tags") or [],
            "is_noise": bool(raw.get("is_noise", False)),
        }


# ---------------------------------------------------------------------------
# 北向：日志接入
# ---------------------------------------------------------------------------
class LogAdapter(BaseAdapter):
    adapter_type = "log"
    direction = NORTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def fetch_recent(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """拉取近期日志（用于 RAG 入库或故障定位）。子类必须实现。"""
        raise NotImplementedError("LogAdapter.fetch_recent 需由具体适配器实现")


# ---------------------------------------------------------------------------
# 北向：CMDB / 拓扑接入
# ---------------------------------------------------------------------------
class CMDBAdapter(BaseAdapter):
    adapter_type = "cmdb"
    direction = NORTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def pull_topology(self) -> Dict[str, Any]:
        """拉取资产/依赖拓扑 -> 内核统一 CMDB（{nodes, edges}）。子类必须实现。"""
        raise NotImplementedError("CMDBAdapter.pull_topology 需由具体适配器实现")


# ---------------------------------------------------------------------------
# 北向：工单接入
# ---------------------------------------------------------------------------
class TicketAdapter(BaseAdapter):
    adapter_type = "ticket"
    direction = NORTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def to_feedback(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """外部工单 -> 内核统一 Feedback（进消息栏）。子类必须实现。"""
        raise NotImplementedError("TicketAdapter.to_feedback 需由具体适配器实现")


# ---------------------------------------------------------------------------
# 南向：执行接入
# ---------------------------------------------------------------------------
class ExecAdapter(BaseAdapter):
    adapter_type = "exec"
    direction = SOUTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行内核 Agent 派发的工具调用（真正落到外部系统）。子类必须实现。"""
        raise NotImplementedError("ExecAdapter.execute 需由具体适配器实现")


# ---------------------------------------------------------------------------
# 北向：知识接入
# ---------------------------------------------------------------------------
class KnowledgeAdapter(BaseAdapter):
    adapter_type = "knowledge"
    direction = NORTH

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": None, "note": "未实现"}

    def ingest(self, source: str) -> int:
        """把外部 Runbook/SOP/Confluence 入库到知识库，返回新增条目数。子类必须实现。"""
        raise NotImplementedError("KnowledgeAdapter.ingest 需由具体适配器实现")
