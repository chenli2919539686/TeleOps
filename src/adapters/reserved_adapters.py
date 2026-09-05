"""预留（reserved）适配器占位。

这些不是空壳——它们已经把「接口契约 + 要对接的真实系统 + 数据映射关系」设计好了，
只是没有连真实后端（无外部地址/无凭据）。落地生产时，把对应方法的 reserved 标记
换成真实 API 调用即可，内核与前端无需改动。

覆盖的外部系统（对应你列的 8 大运维领域）：
  - Zabbix / 华为 iMaster NCE / 中兴 NetNumen  -> 告警（北向）
  - ELK / Loki                              -> 日志（北向）
  - 蓝鲸 CMDB / 优维                         -> 资产拓扑（北向）
  - ITSM / 钉钉 / 企微工单                   -> 工单（北向）
  - SSH / Netconf / 厂商 REST               -> 设备执行（南向）
  - Confluence / 内部 Runbook               -> 知识（北向）
"""
from typing import Any, Dict, List

from src.adapters.base import (
    AlertAdapter, CMDBAdapter, TicketAdapter, ExecAdapter,
    KnowledgeAdapter, NORTH, SOUTH,
)


def _reserved(system: str, what: str) -> Dict[str, Any]:
    """统一的「预留」返回结构，明确提示接口已设计、待接入。"""
    return {
        "status": "reserved",
        "system": system,
        "note": f"接口已设计，待接入真实系统：{what}",
    }


# --------------------------- 告警：华为 iMaster NCE ---------------------------
class IMasterAlertAdapter(AlertAdapter):
    id = "alert-imaster"
    name = "华为 iMaster NCE 告警接入（预留）"
    system = "华为 iMaster NCE"
    status = "reserved"
    description = "对接 iMaster NCE 告警北向接口（EMS/南向告警），适配运营商现网主流网管。"

    def healthcheck(self) -> Dict[str, Any]:
        return _reserved(self.system, "iMaster NCE 告警北向接口（需网元 IP + 账号）")

    def parse_webhook(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [_reserved(self.system, "iMaster NCE alarm -> 统一 Alert")]


# --------------------------- CMDB：蓝鲸 ---------------------------
class BlueKingCMDBAdapter(CMDBAdapter):
    id = "cmdb-blueking"
    name = "蓝鲸 CMDB 接入（预留）"
    system = "蓝鲸 CMDB"
    status = "reserved"
    description = "从蓝鲸 CMDB 拉资产与依赖关系，转成内核统一拓扑。真实接入需蓝鲸 API 网关地址与 App 凭据。"

    def healthcheck(self) -> Dict[str, Any]:
        return _reserved(self.system, "蓝鲸 CMDB API（需 BK_API_URL + APP_CODE/SECRET）")

    def pull_topology(self) -> Dict[str, Any]:
        return _reserved(self.system, "蓝鲸 CMDB topo -> 统一 {nodes, edges}")


# --------------------------- 工单：ITSM ---------------------------
class ITSMTicketAdapter(TicketAdapter):
    id = "ticket-itsm"
    name = "ITSM 工单接入（预留）"
    system = "ITSM / 钉钉 / 企微工单"
    status = "reserved"
    description = "把外部工单系统事件转成内核 Feedback 进消息栏。真实接入需 ITSM openapi 或钉钉/企微机器人 webhook。"

    def healthcheck(self) -> Dict[str, Any]:
        return _reserved(self.system, "ITSM/钉钉/企微 openapi")

    def to_feedback(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return _reserved(self.system, "外部工单 -> 统一 Feedback（进消息栏）")


# --------------------------- 执行：SSH / Netconf ---------------------------
class SSHExtExecAdapter(ExecAdapter):
    id = "exec-ssh"
    name = "SSH / Netconf 设备执行（预留）"
    system = "SSH / Netconf / 厂商 REST"
    status = "reserved"
    description = "把 Agent 造的工具真正下发到设备（重启端口、查光功率、下发配置）。真实接入需跳板机/设备账号，且高风险动作走人工闸。"

    def healthcheck(self) -> Dict[str, Any]:
        return _reserved(self.system, "跳板机/设备 SSH 或 Netconf（需账号 + 白名单）")

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return _reserved(self.system, f"工具 {tool_name}({params}) -> 设备执行（需人工闸审批）")


# --------------------------- 知识：Confluence ---------------------------
class ConfluenceKnowledgeAdapter(KnowledgeAdapter):
    id = "knowledge-confluence"
    name = "Confluence / Runbook 知识接入（预留）"
    system = "Confluence / 内部 Runbook"
    status = "reserved"
    description = "把 Confluence 上的运维手册/应急预案同步进知识库。真实接入需 Confluence REST API 与空间密钥。"

    def healthcheck(self) -> Dict[str, Any]:
        return _reserved(self.system, "Confluence REST API（需 base_url + PAT）")

    def ingest(self, source: str = "") -> int:
        r = _reserved(self.system, f"同步空间 '{source or '<default>'}' -> 知识库")
        return 0  # 预留：返回新增条目数占位
