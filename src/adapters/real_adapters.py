"""可真实对接生产系统的适配器（real）。

与 sample_adapters.py 的「本地文件/内核」样板不同，这里的适配器面向**外部真实
系统**，并采用「配置驱动 + demo 兜底」策略：

  - 在 data/adapters.json 里配置了 base_url / 凭据后，即自动切换为「真实连接」
    模式（status 仍是 sample，但 healthcheck 会真正探活、fetch 会真正调用 API）。
  - 未配置时回退为「demo 模式」：parse/fetch 仍返回结构正确的样例数据，保证
    前端演示与单测不依赖外网/外部系统，且映射逻辑与真实路径完全一致。

本文件落地两个此前为 reserved 的适配器：
  - Zabbix AlertAdapter  ：Zabbix webhook 报文 / problem.get API -> 统一 Alert
  - ELK    LogAdapter    ：Elasticsearch _search -> 日志切片
"""
import json
import time
import datetime
from typing import Any, Dict, List, Optional

try:
    import requests  # 网络调用统一走 requests（venv 已预装）
except Exception:  # noqa: BLE001
    requests = None

from src.adapters.base import AlertAdapter, LogAdapter, NORTH, SOUTH
from src.adapters import fiveg_dataset as _ds  # 5G 公开数据集加载器（P0① 真实电信数据接入）

# Zabbix severity 数值 -> 内核统一标签
ZABBIX_SEVERITY = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}
_ZABBIX_SEVERITY_LABELS = {v: v for v in ZABBIX_SEVERITY.values()}


def _norm_severity(val: Any) -> str:
    """把 Zabbix 的 severity（可能是数字/数字字符串/英文标签）归一化为小写标签。"""
    if val is None:
        return "warning"
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        return ZABBIX_SEVERITY.get(int(val), "warning")
    s = str(val).strip().lower()
    if s in _ZABBIX_SEVERITY_LABELS:
        return s
    # 容错：中文/大小写差异
    mapping = {"未分类": "not_classified", "信息": "information", "警告": "warning",
               "一般严重": "average", "严重": "high", "灾难": "disaster"}
    return mapping.get(s, "warning")


# ---------------------------------------------------------------------------
# Zabbix 告警接入（北向·感知）
# ---------------------------------------------------------------------------
class ZabbixAlertAdapter(AlertAdapter):
    """对接 Zabbix 的告警。

    两条真实接入路径：
      1. Webhook 媒体类型：在 Zabbix 里建一个 Webhook 媒体类型，把告警 POST 到
         POST /adapters/alert/ingest?adapter_id=alert-zabbix，由 parse_webhook 解析。
         报文形状见 _ZABBIX_WEBHOOK_SAMPLE（字段均可选，做了容错）。
      2. Zabbix API：配置 base_url + api_token（或 user/password）后，fetch_problems
         直接调 problem.get 拉取活跃 problem event，转成统一 Alert。

    真实部署要点：高风险动作（如触发脚本/重启）仍应经 ExecAdapter + 人工闸，
    本适配器只负责「感知」，不做任何写操作。
    """
    id = "alert-zabbix"
    name = "Zabbix 告警接入"
    system = "Zabbix"
    direction = NORTH
    status = "sample"
    description = "解析 Zabbix problem webhook 或拉取 problem.get API，转成内核统一 Alert；配置 base_url+令牌后即为真实连接。"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.base_url: str = (cfg.get("base_url") or "").rstrip("/")
        self.api_token: str = cfg.get("api_token") or ""
        self.user: str = cfg.get("user") or ""
        self.password: str = cfg.get("password") or ""
        self.verify_ssl: bool = bool(cfg.get("verify_ssl", False))
        self._auth_cache: Optional[str] = None

    # ---------------- Webhook 解析（纯映射，无需网络） ----------------
    def parse_webhook(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析 Zabbix Webhook 媒体类型推送的报文 -> 0..n 条统一 Alert。

        payload 支持单条对象或批量列表；字段均可选，缺失时给合理默认。
        """
        items = payload if isinstance(payload, list) else [payload]
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            # 兼容「告警列表包了一层 alerts」的常见写法
            if "alerts" in it and isinstance(it["alerts"], list):
                out.extend(self.parse_webhook(it["alerts"]))
                continue
            host = it.get("host_name") or it.get("host") or it.get("host_ip") or ""
            tags = it.get("tags") or []
            if isinstance(tags, list) and tags and isinstance(tags[0], dict):
                tag_list = [t.get("tag") for t in tags if t.get("tag")]
            else:
                tag_list = [str(t) for t in tags]
            event_status = str(it.get("event_status") or it.get("status") or "PROBLEM")
            ts = it.get("event_date", "")
            if it.get("event_time"):
                ts = f"{ts} {it.get('event_time')}".strip()
            out.append(self.to_unified({
                "alert_id": f"zbx-{it.get('event_id') or it.get('eventid') or host or 'unknown'}",
                "ts": ts,
                "source": "zabbix",
                "metric": it.get("trigger_name") or it.get("trigger_name_raw") or it.get("name") or "",
                "host": host,
                "severity": _norm_severity(it.get("trigger_severity") or it.get("severity")),
                "value": it.get("item_value") or it.get("value") or "",
                "message": it.get("trigger_description") or it.get("description")
                          or it.get("trigger_name") or "",
                "tags": tag_list,
                "is_noise": event_status.upper() == "OK",
            }))
        return out

    # ---------------- Zabbix API（真实连接，可选） ----------------
    def _api_call(self, method: str, params: Optional[dict] = None) -> Any:
        if requests is None:
            raise RuntimeError("未安装 requests，无法调用 Zabbix API")
        if not self.base_url:
            raise RuntimeError("未配置 base_url")
        url = f"{self.base_url}/api_jsonrpc.php"
        headers = {"Content-Type": "application/json"}
        auth = None
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        elif self.user and self.password:
            auth = self._login()
        body = {"jsonrpc": "2.0", "method": method, "params": params or {},
                "id": 1, "auth": auth}
        resp = requests.post(url, json=body, headers=headers,
                             timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Zabbix API 错误：{data['error']}")
        return data.get("result")

    def _login(self) -> str:
        if requests is None:
            raise RuntimeError("未安装 requests")
        url = f"{self.base_url}/api_jsonrpc.php"
        body = {"jsonrpc": "2.0", "method": "user.login",
                "params": {"username": self.user, "password": self.password}, "id": 1}
        resp = requests.post(url, json=body,
                             headers={"Content-Type": "application/json"},
                             timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        return resp.json().get("result", "")

    def fetch_problems(self, recent_minutes: int = 60) -> List[Dict[str, Any]]:
        """拉取 Zabbix 活跃 problem event -> 统一 Alert 列表。

        未配置 base_url/令牌时返回 demo fixture（结构与真实路径一致）。
        """
        if not (self.base_url and (self.api_token or (self.user and self.password))):
            return self._demo_problems()
        time_from = int(time.time()) - recent_minutes * 60
        raw = self._api_call("problem.get", {
            "recent": "true",
            "time_from": time_from,
            "output": "extend",
            "selectHosts": ["host", "name"],
            "selectTags": "extend",
            "sortfield": ["eventid"],
            "sortorder": "DESC",
        }) or []
        out: List[Dict[str, Any]] = []
        for p in raw:
            hosts = p.get("hosts") or []
            host = hosts[0].get("host") if hosts else ""
            out.append(self.to_unified({
                "alert_id": f"zbx-{p.get('eventid')}",
                "ts": (datetime.datetime.fromtimestamp(int(p["clock"])).isoformat()
                       if p.get("clock") else ""),
                "source": "zabbix",
                "metric": p.get("name", ""),
                "host": host,
                "severity": _norm_severity(p.get("severity")),
                "value": "",
                "message": p.get("name", ""),
                "tags": [t.get("tag") for t in (p.get("tags") or []) if t.get("tag")],
                "is_noise": False,
            }))
        return out

    def _demo_problems(self) -> List[Dict[str, Any]]:
        now = int(time.time())
        raw = [
            {"eventid": "100123", "clock": now - 300,
             "name": "CPU utilization is high on web-01", "severity": 4,
             "hosts": [{"host": "web-01", "name": "web-01"}], "tags": [{"tag": "env"}, {"tag": "prod"}]},
            {"eventid": "100124", "clock": now - 120,
             "name": "/var partition is low on db-02", "severity": 3,
             "hosts": [{"host": "db-02", "name": "db-02"}], "tags": [{"tag": "env"}]},
        ]
        return [self.to_unified({
            "alert_id": f"zbx-{p['eventid']}",
            "ts": datetime.datetime.fromtimestamp(p["clock"]).isoformat(),
            "source": "zabbix", "metric": p["name"], "host": p["hosts"][0]["host"],
            "severity": _norm_severity(p["severity"]), "value": "",
            "message": p["name"], "tags": [t["tag"] for t in p["tags"]], "is_noise": False,
        }) for p in raw]

    def healthcheck(self) -> Dict[str, Any]:
        if not (self.base_url and (self.api_token or (self.user and self.password))):
            return {"reachable": True, "mode": "demo",
                    "endpoint": "<zabbix-url>/api_jsonrpc.php",
                    "note": "未配置真实地址，webhook 解析与 demo problem 可真实运行；配置 base_url+令牌后自动转为真实连接"}
        try:
            version = self._api_call("apiinfo.version")
            return {"reachable": True, "mode": "live", "zabbix_version": version}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "mode": "live", "error": str(e)}


# ---------------------------------------------------------------------------
# ELK / Elasticsearch 日志接入（北向·感知）
# ---------------------------------------------------------------------------
class ELKLogAdapter(LogAdapter):
    """对接 Elasticsearch 的日志索引。

    真实接入路径：配置 base_url（如 http://localhost:9200）+ index（如 logs-*）
    后，fetch_recent 直接 POST {index}/_search，把命中日志映射为统一日志切片。
    支持 api_key（Bearer）或 basic auth（user:password）。

    未配置时回退 demo fixture，保证前端演示与单测不依赖 ES 实例。
    """
    id = "log-elk"
    name = "ELK 日志接入"
    system = "Elasticsearch / Logstash / Kibana"
    direction = NORTH
    status = "sample"
    description = "从 Elasticsearch 索引拉取近期日志做 RAG 入库与故障定位；配置 base_url+index 后即为真实连接。"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.base_url: str = (cfg.get("base_url") or "").rstrip("/")
        self.index: str = cfg.get("index") or "logs-*"
        self.api_key: str = cfg.get("api_key") or ""
        self.user: str = cfg.get("user") or ""
        self.password: str = cfg.get("password") or ""
        self.verify_ssl: bool = bool(cfg.get("verify_ssl", False))

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"ApiKey {self.api_key}"
        elif self.user and self.password:
            import base64
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {token}"
        return h

    def fetch_recent(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """按 free-text 查询 ES 索引，返回统一日志切片。

        每条：{ts, host, level, message, source, raw}
        """
        if not (self.base_url and self.index):
            return self._demo_logs()
        if requests is None:
            raise RuntimeError("未安装 requests，无法查询 ES")
        url = f"{self.base_url}/{self.index}/_search"
        body = {
            "size": limit,
            "query": {"query_string": {"query": query, "default_operator": "AND"}},
            "sort": [{"@timestamp": {"order": "desc"}}],
        }
        resp = requests.post(url, json=body, headers=self._headers(),
                             timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        hits = (resp.json().get("hits", {}) or {}).get("hits", []) or []
        return [self._map_hit(h) for h in hits]

    @staticmethod
    def _map_hit(hit: Dict[str, Any]) -> Dict[str, Any]:
        src = hit.get("_source", {}) or {}
        host = src.get("host")
        if isinstance(host, dict):
            host = host.get("name") or host.get("hostname") or ""
        host = host or src.get("hostname") or ""
        level = str(src.get("level") or src.get("loglevel") or src.get("severity") or "info").lower()
        message = (src.get("message") or src.get("msg") or src.get("log")
                   or json.dumps(src, ensure_ascii=False)[:200])
        return {
            "ts": src.get("@timestamp") or src.get("timestamp") or src.get("time") or "",
            "host": host,
            "level": level,
            "message": message,
            "source": hit.get("_index", ""),
            "raw": src,
        }

    def _demo_logs(self) -> List[Dict[str, Any]]:
        now = datetime.datetime.utcnow().isoformat() + "Z"
        return [
            {"ts": now, "host": "web-01", "level": "error",
             "message": "connection refused to upstream 10.0.0.9:8080", "source": "logs-demo", "raw": {}},
            {"ts": now, "host": "db-02", "level": "warn",
             "message": "slow query took 2.3s on table orders", "source": "logs-demo", "raw": {}},
        ]

    def healthcheck(self) -> Dict[str, Any]:
        if not (self.base_url and self.index):
            return {"reachable": True, "mode": "demo",
                    "endpoint": "<es-url>/<index>/_search",
                    "note": "未配置真实地址，fetch_recent 返回 demo 日志；配置 base_url+index 后自动转为真实连接"}
        if requests is None:
            return {"reachable": None, "mode": "live", "note": "未安装 requests"}
        try:
            resp = requests.get(f"{self.base_url}", headers=self._headers(),
                                timeout=10, verify=self.verify_ssl)
            resp.raise_for_status()
            info = resp.json()
            return {"reachable": True, "mode": "live",
                    "cluster": info.get("cluster_name", ""), "version": info.get("version", {}).get("number", "")}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "mode": "live", "error": str(e)}


# ---------------------------------------------------------------------------
# 5G KPI 告警接入（北向·感知）—— 真实电信数据集落地（P0①）
# ---------------------------------------------------------------------------
class FiveGKpiAdapter(AlertAdapter):
    """对接 5G 小区 KPI / 性能指标（如 uccmisl/5Gdataset、OSS 导出的小区级指标）。

    parse_webhook 接受两种形态：
      1. 单条 KPI：{cell, metric, value, threshold, unit, ts, severity}
      2. 批量列表 / OSS 导出行：[{cell_id, kpi_name, kpi_value, ...}, ...]

    超过阈值即转成统一 Alert（severity 按越界幅度映射），可直接喂进 alert_stream
    的运维 Agent 根因分析——这就是「把公开日志换成真实电信数据」的落地点。

    未配置真实数据源时回退 demo fixture（结构一致：小区级 RRC/PRB/丢包等指标），
    保证演示与单测不依赖外网；配置 data/adapters.json 的 alert-5g 后即接真实数据。
    """
    id = "alert-5g"
    name = "5G 小区 KPI 告警接入"
    system = "5G 网管 / OSS（小区级 KPI）"
    direction = NORTH
    status = "sample"
    description = "把 5G 小区 KPI（RRC 建立成功率/PRB 利用率/丢包率等）超阈转成内核统一 Alert；配置 alert-5g 后接真实数据集。"

    def __init__(self, config: Optional[dict] = None):
        # 真实数据源配置（data/adapters.json[alert-5g]）：
        #   dataset_path : 数据集文件/目录/glob（如 data/5g_dataset）
        #   thresholds   : 可选，按指标覆盖 METRIC_SPECS 的边界（good/warn/major/critical）
        cfg = config or {}
        self._config = cfg
        self.dataset_path: str = (cfg.get("dataset_path") or "").strip()
        self._specs = dict(_ds.METRIC_SPECS)
        for metric, bounds in (cfg.get("thresholds") or {}).items():
            if metric in self._specs and isinstance(bounds, dict):
                self._specs[metric].update({k: float(v) for k, v in bounds.items()
                                            if k in ("good", "warn", "major", "critical")})

    # 指标 -> 严重度映射（越界幅度）
    _SEV_BY_RATIO = [
        (0.30, "critical"), (0.15, "major"), (0.05, "warning"), (0.0, "info"),
    ]

    def parse_webhook(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        items = payload if isinstance(payload, list) else [payload]
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cell = it.get("cell") or it.get("cell_id") or it.get("Cell_Identity") or ""
            metric = it.get("metric") or it.get("kpi_name") or it.get("Metric") or ""
            try:
                value = float(it.get("value", it.get("kpi_value", 0)) or 0)
            except (TypeError, ValueError):
                value = 0.0
            try:
                threshold = float(it.get("threshold") or it.get("Threshold") or 0)
            except (TypeError, ValueError):
                threshold = 0.0
            ts = it.get("ts") or it.get("Timestamp") or ""
            # 越界幅度 -> severity（仅当 payload 未自带显式 severity 时计算）
            # 注意：5G 数据集 loader 已按指标方向算好 severity 并随 payload 传入，
            # 这里必须优先采用，否则 RSRP/RSRQ/SNR 等「越低越差」指标会被反比。
            explicit = it.get("severity")
            if explicit:
                severity = str(explicit).lower()
            else:
                ratio = (value - threshold) / threshold if threshold else 0.0
                severity = "info"
                for r, sev in self._SEV_BY_RATIO:
                    if ratio >= r:
                        severity = sev
                        break
            out.append(self.to_unified({
                "alert_id": f"5g-{cell}-{metric}".replace(" ", "_"),
                "ts": ts,
                "source": "5g-kpi",
                "metric": metric,
                "host": str(cell),
                "severity": severity,
                "value": value,
                "message": f"小区 {cell} 的 {metric}={value} 超过阈值 {threshold}"
                           if threshold else f"小区 {cell} 的 {metric}={value}",
                "tags": ["5g", "kpi"],
                "is_noise": severity in ("info",) and threshold and value <= threshold,
            }))
        return out

    def _demo_kpis(self) -> List[Dict[str, Any]]:
        return [
            {"cell": "Cell-VLAN-01", "metric": "RRC_setup_success_rate", "value": 0.91,
             "threshold": 0.98, "ts": "2026-09-23T08:00:00Z"},
            {"cell": "Cell-VLAN-02", "metric": "PRB_utilization", "value": 0.96,
             "threshold": 0.85, "ts": "2026-09-23T08:00:00Z"},
            {"cell": "Cell-VLAN-01", "metric": "downlink_packet_loss_rate", "value": 0.07,
             "threshold": 0.02, "ts": "2026-09-23T08:00:00Z"},
            {"cell": "Cell-VLAN-03", "metric": "RRC_setup_success_rate", "value": 0.995,
             "threshold": 0.98, "ts": "2026-09-23T08:00:00Z", "is_noise": True},
        ]

    def demo_alerts(self) -> List[Dict[str, Any]]:
        """返回一组 demo 5G 告警（供前端一键试推）。"""
        return self.parse_webhook(self._demo_kpis())

    def load_dataset_alerts(self, path: Optional[str] = None, limit: Optional[int] = None,
                            min_severity: Optional[str] = None) -> List[Dict[str, Any]]:
        """读真实 5G 数据集（uccmisl/5Gdataset）→ 统一 Alert 列表。

        这是「真实电信数据接入」的主入口：数据集行经 fiveg_dataset.row_to_payloads
        算出按指标方向的 severity，再统一封装成内核 Alert，可直接喂进 alert_stream
        让运维 Agent 做根因分析。未配置 dataset_path 时返回空列表（由调用方回退 demo）。

        min_severity：严重度下限（默认取自配置 min_severity，否则 info=全部）。
        真实数据集噪声大，建议用 "major" 过滤掉 warning，避免健康波动刷屏。
        """
        ds = (path or self.dataset_path or "").strip()
        if not ds:
            return []
        floor = min_severity or self._config.get("min_severity") or "info"
        payloads = _ds.load_payloads(ds, limit=limit, specs=self._specs, min_severity=floor)
        return [self.to_unified(p) for p in payloads]

    def healthcheck(self) -> Dict[str, Any]:
        import os
        if self.dataset_path and os.path.isfile(self.dataset_path) or \
           (self.dataset_path and os.path.isdir(self.dataset_path)):
            n = 0
            try:
                n = len(_ds.load_payloads(self.dataset_path, limit=2000, specs=self._specs))
            except Exception:  # noqa: BLE001
                n = 0
            return {"reachable": True, "mode": "dataset", "dataset_path": self.dataset_path,
                    "sampled_alerts": n,
                    "note": "已接入真实 5G 数据集，loader 把小区 KPI 超阈转成统一 Alert 并喂入告警流"}
        return {"reachable": True, "mode": "demo",
                "endpoint": "data/adapters.json[alert-5g]",
                "note": "未配置 dataset_path，demo 小区 KPI 可真实解析并喂入告警流；配置 alert-5g.dataset_path 后接真实 5G/OSS 数据"}


# ---------------------------------------------------------------------------
# Grafana / Prometheus 指标查询（北向·感知）—— MCP 风格接入真实运维系统（P1④）
# ---------------------------------------------------------------------------
class GrafanaAdapter(LogAdapter):
    """对接 Grafana / Prometheus 拉取真实指标时序，让 Agent 能直接「看监控大屏」。

    这是「MCP 接真实运维系统」的落地：Agent 的诊断不再只看我们喂的日志，
    而是能主动去 Prometheus 查询实时指标（如 up、node_cpu_seconds、5G 小区 KPI）。

    真实接入路径：配置 base_url（Prometheus 或 Grafana 地址）+ api_key 后，
    query_metrics 直接调 Prometheus HTTP API（/api/v1/query_range）；
    未配置时回退仿真时序（synthetic），保证演示与单测不依赖外部监控实例。
    """
    id = "metrics-grafana"
    name = "Grafana / Prometheus 指标接入"
    system = "Grafana / Prometheus"
    direction = NORTH
    status = "sample"
    description = "从 Prometheus/Grafana 拉取指标时序供 Agent 诊断；MCP 风格标准接口，配置后接真实监控。"

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.base_url: str = (cfg.get("base_url") or "").rstrip("/")
        self.api_key: str = cfg.get("api_key") or ""
        self.datasource: str = cfg.get("datasource") or "prometheus"
        self.verify_ssl: bool = bool(cfg.get("verify_ssl", False))

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def query_metrics(self, promql: str, hours: int = 1) -> Dict[str, Any]:
        """查询指标时序，返回 {series: [{ts, value}], mode}。

        未配置 base_url 时返回仿真时序（按 promql 关键字生成合理曲线）。
        """
        if not self.base_url:
            return {"mode": "demo", "promql": promql,
                    "series": self._synthetic_series(promql, hours)}
        if requests is None:
            raise RuntimeError("未安装 requests，无法查询 Prometheus")
        end = int(time.time())
        start = end - hours * 3600
        step = max(30, (end - start) // 60)
        url = f"{self.base_url}/api/v1/query_range"
        resp = requests.get(url, params={"query": promql, "start": start,
                                         "end": end, "step": step},
                            headers=self._headers(), timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("result", []) or []
        series = []
        for d in data:
            for ts, val in d.get("values", []):
                series.append({"ts": ts, "value": float(val)})
        return {"mode": "live", "promql": promql, "series": series}

    @staticmethod
    def _synthetic_series(promql: str, hours: int) -> List[Dict[str, Any]]:
        now = int(time.time())
        n = 24
        base = 0.5
        if "cpu" in promql.lower():
            base = 0.82
        elif "mem" in promql.lower():
            base = 0.6
        elif "packet" in promql.lower() or "loss" in promql.lower():
            base = 0.04
        elif "prb" in promql.lower():
            base = 0.9
        series = []
        for i in range(n):
            t = now - (n - i) * (hours * 3600 // n)
            v = round(base + 0.1 * ((i % 5) - 2) / 5.0, 3)
            series.append({"ts": t, "value": max(0.0, v)})
        return series

    def fetch_recent(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        # 复用 query_metrics：把 free-text 当作 promql 查询，返回指标切片
        res = self.query_metrics(query, hours=1)
        return [{"ts": s["ts"], "metric": query, "value": s["value"],
                 "source": "grafana", "raw": s} for s in res.get("series", [])[:limit]]

    def healthcheck(self) -> Dict[str, Any]:
        if not self.base_url:
            return {"reachable": True, "mode": "demo",
                    "endpoint": "<prometheus-url>/api/v1/query_range",
                    "note": "未配置真实地址，query_metrics 返回仿真时序；配置 base_url+api_key 后接真实 Prometheus/Grafana"}
        if requests is None:
            return {"reachable": None, "mode": "live", "note": "未安装 requests"}
        try:
            resp = requests.get(f"{self.base_url}/api/v1/query",
                                params={"query": "up"}, headers=self._headers(),
                                timeout=10, verify=self.verify_ssl)
            resp.raise_for_status()
            return {"reachable": True, "mode": "live",
                    "status": resp.json().get("status")}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "mode": "live", "error": str(e)}
