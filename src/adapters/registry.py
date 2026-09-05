"""适配器注册表：集中登记所有（含预留）适配器，供后端/前端统一查询与调用。

设计要点：
  - 启动即注册全部适配器（样板 + 预留），内核只通过注册表拿适配器，不直接 import 具体类。
  - 真实接入某系统时，只需把它从 reserved 实现为 sample/active 并注册进来，其余不动。
"""
from typing import Dict, List, Optional

from src.adapters.base import BaseAdapter
from src.adapters.sample_adapters import (
    PrometheusAlertAdapter, LocalCMDBAdapter,
    LocalKnowledgeAdapter, LocalExecAdapter,
)
from src.adapters.real_adapters import ZabbixAlertAdapter, ELKLogAdapter
from src.adapters.reserved_adapters import (
    IMasterAlertAdapter,
    BlueKingCMDBAdapter, ITSMTicketAdapter, SSHExtExecAdapter,
    ConfluenceKnowledgeAdapter,
)
from src.config import load_adapter_configs
from src.core.tool_registry import ToolRegistry


class AdapterRegistry:
    def __init__(self):
        self._by_id: Dict[str, BaseAdapter] = {}
        self._by_type: Dict[str, List[str]] = {}
        self._register_all()

    def _register_all(self):
        # 样板（可运行）
        self.register(PrometheusAlertAdapter())
        self.register(LocalCMDBAdapter())
        self.register(LocalKnowledgeAdapter())
        self.register(LocalExecAdapter(ToolRegistry()))
        # 可真实对接的适配器（配置驱动 + demo 兜底）
        cfgs = load_adapter_configs()
        self.register(ZabbixAlertAdapter(cfgs.get("alert-zabbix")))
        self.register(ELKLogAdapter(cfgs.get("log-elk")))
        # 预留（接口已设计、待接入）
        self.register(IMasterAlertAdapter())
        self.register(BlueKingCMDBAdapter())
        self.register(ITSMTicketAdapter())
        self.register(SSHExtExecAdapter())
        self.register(ConfluenceKnowledgeAdapter())

    def register(self, adapter: BaseAdapter):
        if not adapter.id:
            raise ValueError("适配器必须设置 id")
        self._by_id[adapter.id] = adapter
        self._by_type.setdefault(adapter.adapter_type, []).append(adapter.id)

    def get(self, adapter_id: str) -> Optional[BaseAdapter]:
        return self._by_id.get(adapter_id)

    def list(self, adapter_type: Optional[str] = None) -> List[dict]:
        ids = self._by_type.get(adapter_type, []) if adapter_type else list(self._by_id.keys())
        return [self._by_id[i].metadata() for i in ids]

    def first_of_type(self, adapter_type: str) -> Optional[BaseAdapter]:
        ids = self._by_type.get(adapter_type, [])
        return self._by_id[ids[0]] if ids else None

    def health(self) -> Dict[str, dict]:
        return {aid: self._by_id[aid].healthcheck() for aid in self._by_id}
