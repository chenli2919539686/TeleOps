"""TeleOps 适配器层：把外部运维系统（告警/日志/CMDB/工单/执行/知识）接入内核统一 Schema。

对外只暴露 AdapterRegistry，其余基类与具体适配器在子模块内。
"""
from src.adapters.base import (
    BaseAdapter, AlertAdapter, LogAdapter, CMDBAdapter,
    TicketAdapter, ExecAdapter, KnowledgeAdapter, NORTH, SOUTH, ADAPTER_TYPES,
)
from src.adapters.registry import AdapterRegistry

__all__ = [
    "BaseAdapter", "AlertAdapter", "LogAdapter", "CMDBAdapter",
    "TicketAdapter", "ExecAdapter", "KnowledgeAdapter",
    "NORTH", "SOUTH", "ADAPTER_TYPES", "AdapterRegistry",
]
