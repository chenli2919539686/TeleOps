"""可真实运行的样板适配器（sample）。

这些不是占位，而是「能跑、能演示」的最小实现：
  - PrometheusAlertAdapter：解析标准 Alertmanager webhook 报文（生产最常用的告警入口）
  - LocalCMDBAdapter      ：从本地 topology.json 拉拓扑（真实对接时换成网管/蓝鲸 API）
  - LocalKnowledgeAdapter ：把 kb/ 目录作为知识源（真实对接时换成 Confluence/Runbook API）
  - LocalExecAdapter      ：把工具调用转发给内核 ToolRegistry（真实对接时换成 SSH/Netconf）

预留（reserved）的占位适配器见 reserved_adapters.py。
"""
from pathlib import Path
from typing import Any, Dict, List

from src.adapters.base import (
    AlertAdapter, CMDBAdapter, KnowledgeAdapter, ExecAdapter, NORTH, SOUTH,
)
from src.config import TOPOLOGY_FILE, KB_DIR
from src.core.tool_registry import ToolRegistry


class PrometheusAlertAdapter(AlertAdapter):
    """对接 Prometheus / Alertmanager 的 webhook。

    真实部署时，在 Alertmanager 的 receiver 里配置 webhook 指向
    POST /adapters/alert/ingest?adapter_id=alert-prometheus 即可让告警自动流进 TeleOps。
    """
    id = "alert-prometheus"
    name = "Prometheus Alertmanager 告警接入"
    system = "Prometheus / Alertmanager"
    status = "sample"
    description = "解析 Alertmanager 标准 webhook 报文，转成内核统一 Alert，是生产最常用的告警入口。"

    def parse_webhook(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for a in payload.get("alerts", []):
            labels = a.get("labels", {})
            annotations = a.get("annotations", {})
            instance = labels.get("instance", "")
            host = instance.split(":")[0] if instance else labels.get("host", "")
            out.append(self.to_unified({
                "alert_id": f"{labels.get('alertname','alert')}-{host or 'unknown'}",
                "ts": a.get("startsAt", ""),
                "source": "prometheus",
                "metric": labels.get("alertname", ""),
                "host": host,
                "severity": labels.get("severity", "warning"),
                "value": annotations.get("value", labels.get("value", "")),
                "message": annotations.get("description") or annotations.get("summary", ""),
                "tags": [v for v in [labels.get("job"), labels.get("severity")] if v],
                "is_noise": False,
            }))
        return out

    def healthcheck(self) -> Dict[str, Any]:
        # 样板：真实应 GET {prometheus_url}/-/healthy
        return {"reachable": True, "endpoint": "<prometheus-url>/-/healthy",
                "note": "样板实现，未配置真实地址；webhook 解析逻辑可真实运行"}


class LocalCMDBAdapter(CMDBAdapter):
    """从本地 topology.json 拉拓扑（演示用）。

    真实对接：把 _read_topology 换成对网管 NBI / 蓝鲸 CMDB API 的调用即可，
    内核只认返回的 {nodes, edges} 结构，下游无需改动。
    """
    id = "cmdb-local"
    name = "本地 CMDB 拓扑接入（样板）"
    system = "本地 topology.json（生产替换为网管/蓝鲸 CMDB）"
    status = "sample"
    description = "读取本地 topology.json 作为统一拓扑源，验证 CMDBAdapter 接入契约。"

    def _read_topology(self) -> Dict[str, Any]:
        return dict(__import__("json").loads(Path(TOPOLOGY_FILE).read_text(encoding="utf-8")))

    def pull_topology(self) -> Dict[str, Any]:
        return self._read_topology()

    def healthcheck(self) -> Dict[str, Any]:
        try:
            topo = self._read_topology()
            return {"reachable": True, "nodes": len(topo.get("nodes", [])),
                    "edges": len(topo.get("edges", []))}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "error": str(e)}


class LocalKnowledgeAdapter(KnowledgeAdapter):
    """把本地 kb/ 目录作为知识源（演示用）。"""
    id = "knowledge-local"
    name = "本地知识库接入（样板）"
    system = "本地 kb/ 目录（生产替换为 Confluence/Runbook API）"
    status = "sample"
    description = "扫描 kb/ 目录下的 Markdown 作为 RAG 知识源，验证 KnowledgeAdapter 接入契约。"

    def ingest(self, source: str = "") -> int:
        files = list(Path(KB_DIR).rglob("*.md"))
        return len(files)

    def healthcheck(self) -> Dict[str, Any]:
        try:
            n = self.ingest()
            return {"reachable": True, "kb_files": n}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "error": str(e)}


class LocalExecAdapter(ExecAdapter):
    """把工具调用转发给内核 ToolRegistry（演示用）。

    真实对接：把 _run 换成 SSH / Netconf / 厂商 REST 调用，
    内核 Agent 造出的工具只需描述「调哪个外部动作」，由这里真正落地。
    """
    id = "exec-local"
    name = "本地执行接入（样板）"
    system = "内核 ToolRegistry（生产替换为 SSH/Netconf/厂商 API）"
    status = "sample"
    description = "将 Agent 派发的工具调用转交本地 ToolRegistry 执行，验证 ExecAdapter 南向契约。"

    def __init__(self, tool_registry: ToolRegistry = None):
        self._tools = tool_registry or ToolRegistry()

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name not in self._tools.list_tools():
            return {"ok": False, "status": "missing",
                    "reason": f"工具 {tool_name} 不在库中"}
        try:
            result = self._tools.call(tool_name, params)
            return {"ok": True, "status": "ok", "result": result}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "error", "reason": str(e)}

    def healthcheck(self) -> Dict[str, Any]:
        return {"reachable": True, "tools": len(self._tools.list_tools())}
