"""集成适配器真实落地的单元测试（Zabbix 告警 / ELK 日志 / 5G 数据集）。

覆盖两条路径：
  - demo 模式（未配置 base_url/凭据）：parse/fetch 返回结构正确的样例数据
  - 真实连接映射（monkeypatch 网络调用）：外部 API 响应 -> 统一 Schema 的转换
"""
import csv
import sys
from unittest.mock import MagicMock

import pytest

from src.adapters.real_adapters import (
    ZabbixAlertAdapter, ELKLogAdapter, FiveGKpiAdapter, _norm_severity,
)
from src.adapters import real_adapters
from src.adapters import fiveg_dataset as _ds


# ---------------- Zabbix：severity 归一化 ----------------
def test_norm_severity_numeric_and_label():
    assert _norm_severity(4) == "high"
    assert _norm_severity("5") == "disaster"
    assert _norm_severity("Warning") == "warning"
    assert _norm_severity("灾难") == "disaster"
    assert _norm_severity(None) == "warning"


# ---------------- Zabbix：webhook 解析（纯映射） ----------------
def test_zabbix_parse_webhook_basic():
    z = ZabbixAlertAdapter()
    alerts = z.parse_webhook({
        "event_id": "1", "host_name": "web-01", "host_ip": "10.0.0.5",
        "trigger_name": "CPU utilization is high", "trigger_severity": "High",
        "event_status": "PROBLEM", "item_value": "95 %",
        "event_date": "2026-09-05", "event_time": "10:30:00",
        "tags": [{"tag": "env"}, {"tag": "prod"}],
    })
    assert len(alerts) == 1
    a = alerts[0]
    assert a["alert_id"] == "zbx-1"
    assert a["host"] == "web-01"
    assert a["metric"] == "CPU utilization is high"
    assert a["severity"] == "high"
    assert a["value"] == "95 %"
    assert a["is_noise"] is False
    assert "env" in a["tags"] and "prod" in a["tags"]


def test_zabbix_parse_webhook_ok_is_noise():
    z = ZabbixAlertAdapter()
    a = z.parse_webhook({"event_id": "2", "trigger_name": "x",
                         "trigger_severity": 2, "event_status": "OK"})[0]
    assert a["is_noise"] is True


def test_zabbix_parse_webhook_batch_and_nested():
    z = ZabbixAlertAdapter()
    out = z.parse_webhook({
        "alerts": [
            {"event_id": "10", "trigger_name": "a", "trigger_severity": 3},
            {"event_id": "11", "trigger_name": "b", "trigger_severity": 4},
        ]
    })
    assert len(out) == 2
    assert out[0]["alert_id"] == "zbx-10" and out[1]["alert_id"] == "zbx-11"


# ---------------- Zabbix：fetch_problems demo + 真实映射 ----------------
def test_zabbix_fetch_problems_demo():
    z = ZabbixAlertAdapter()  # 未配置 -> demo
    probs = z.fetch_problems()
    assert len(probs) == 2
    assert all(k in probs[0] for k in ("alert_id", "host", "severity", "message"))
    assert probs[0]["alert_id"].startswith("zbx-")


def test_zabbix_fetch_problems_live_mapping(monkeypatch):
    z = ZabbixAlertAdapter({"base_url": "http://zbx/api", "api_token": "tok"})

    def fake_api(method, params=None):
        if method == "problem.get":
            return [{
                "eventid": "200", "clock": 1700000000,
                "name": "Disk full on srv-1", "severity": 5,
                "hosts": [{"host": "srv-1", "name": "srv-1"}],
                "tags": [{"tag": "disk"}],
            }]
        return "6.0"

    monkeypatch.setattr(z, "_api_call", fake_api)
    probs = z.fetch_problems()
    assert len(probs) == 1
    p = probs[0]
    assert p["alert_id"] == "zbx-200"
    assert p["host"] == "srv-1"
    assert p["severity"] == "disaster"
    assert p["is_noise"] is False


def test_zabbix_healthcheck_demo():
    z = ZabbixAlertAdapter()
    h = z.healthcheck()
    assert h["reachable"] is True and h["mode"] == "demo"


# ---------------- ELK：fetch_recent demo + 真实映射 ----------------
def test_elk_fetch_recent_demo():
    e = ELKLogAdapter()  # 未配置 -> demo
    logs = e.fetch_recent("error")
    assert len(logs) == 2
    assert all(k in logs[0] for k in ("ts", "host", "level", "message", "source"))


def test_elk_fetch_recent_live_mapping(monkeypatch):
    if real_adapters.requests is None:
        pytest.skip("requests 未安装，跳过真实映射测试")
    e = ELKLogAdapter({"base_url": "http://es:9200", "index": "logs-*"})

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"hits": {"hits": [
            {"_index": "logs-1", "_source": {
                "@timestamp": "2026-09-05T10:00:00Z", "host": {"name": "h1"},
                "level": "error", "message": "boom"}},
        ]}}
        return resp

    monkeypatch.setattr(real_adapters.requests, "post", fake_post)
    logs = e.fetch_recent("boom")
    assert len(logs) == 1
    lg = logs[0]
    assert lg["host"] == "h1"
    assert lg["level"] == "error"
    assert lg["message"] == "boom"
    assert lg["source"] == "logs-1"


def test_elk_healthcheck_demo():
    e = ELKLogAdapter()
    h = e.healthcheck()
    assert h["reachable"] is True and h["mode"] == "demo"


# ---------------- 注册表：状态已升级为 sample ----------------
def test_registry_marks_real_adapters_as_sample():
    from src.adapters.registry import AdapterRegistry
    r = AdapterRegistry()
    assert r.get("alert-zabbix").status == "sample"
    assert r.get("log-elk").status == "sample"
    # 旧的本地/预留适配器不受影响
    assert r.get("alert-prometheus").status == "sample"
    assert r.get("alert-imaster").status == "reserved"


# ===========================================================================
# 5G 公开数据集接入（P0① 真实电信数据接入）
# ===========================================================================
def _write_dataset(path, rows):
    cols = ["Timestamp", "CellID", "NetworkMode", "RSRP", "RSRQ", "SNR", "CQI",
            "RSSI", "DL_bitrate", "UL_bitrate", "PINGLOSS"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# 一行严重劣化（多指标 critical），一行健康（不进告警流）
_DEGRADED = {"Timestamp": "2019.12.16_13.40.04", "CellID": "11", "NetworkMode": "5G",
             "RSRP": "-125", "RSRQ": "-20", "SNR": "-7", "CQI": "2", "RSSI": "-105",
             "DL_bitrate": "2", "UL_bitrate": "0", "PINGLOSS": "8"}
_HEALTHY = {"Timestamp": "2019.12.16_13.41.04", "CellID": "11", "NetworkMode": "5G",
            "RSRP": "-95", "RSRQ": "-10", "SNR": "20", "CQI": "14", "RSSI": "-80",
            "DL_bitrate": "120", "UL_bitrate": "30", "PINGLOSS": "0"}


def test_fiveg_row_to_payloads_direction_aware():
    """RSRP/RSRQ/SNR 等「越低越差」指标必须按方向判级，不能反比。"""
    payloads = _ds.row_to_payloads(_DEGRADED)
    by_metric = {p["metric"]: p for p in payloads}
    assert by_metric["rsrp_dbm"]["severity"] == "critical"   # -125 <= -120
    assert by_metric["rsrq_db"]["severity"] == "major"       # -20 <= -18 (>-21)
    assert by_metric["sinr_db"]["severity"] == "critical"    # -7 <= -6
    assert by_metric["dl_throughput_mbps"]["severity"] == "critical"
    assert by_metric["ping_loss_pct"]["severity"] == "critical"  # higher_worse 8>=5
    # 健康行不应产生任何告警
    assert _ds.row_to_payloads(_HEALTHY) == []


def test_fiveg_parse_webhook_honors_explicit_severity():
    """payload 自带 severity 时必须原样采用，不被比值逻辑覆盖（越低越差不被反算）。"""
    a = FiveGKpiAdapter().parse_webhook(
        {"cell": "C1", "metric": "rsrp_dbm", "value": -95, "threshold": -110,
         "severity": "major"})[0]
    assert a["severity"] == "major"
    # 无显式 severity 时，value=-95 高于阈值 -> 默认 info（不告警方向）
    a2 = FiveGKpiAdapter().parse_webhook(
        {"cell": "C1", "metric": "rsrp_dbm", "value": -95, "threshold": -110})[0]
    assert a2["severity"] == "info"


def test_fiveg_adapter_load_dataset_alerts(tmp_path):
    """端到端：数据集目录 -> 统一 Alert（host=小区、severity 正确、健康行被跳过）。"""
    d = tmp_path / "ds"
    d.mkdir()
    _write_dataset(d / "trace.csv", [_DEGRADED, _HEALTHY])
    adp = FiveGKpiAdapter({"dataset_path": str(d)})
    alerts = adp.load_dataset_alerts()
    # 仅 _DEGRADED 这一行产生告警（8 个超阈指标）
    assert len(alerts) == 8
    assert all(a["host"] == "11" for a in alerts)
    # 方向感知判级：RSRP/SINR/CQI/RSSI/吞吐/丢包=critical，RSRQ=-20 落 major 档
    by_metric = {a["metric"]: a["severity"] for a in alerts}
    assert by_metric["rsrp_dbm"] == "critical"
    assert by_metric["sinr_db"] == "critical"
    assert by_metric["ping_loss_pct"] == "critical"
    assert by_metric["rsrq_db"] == "major"  # -20 ∈ (major=-18, critical=-21]
    assert all(a["source"] == "5g-kpi" for a in alerts)
    # 配置缺失时回退空列表
    assert FiveGKpiAdapter().load_dataset_alerts() == []


def test_fiveg_adapter_healthcheck_dataset_mode(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    _write_dataset(d / "t.csv", [_DEGRADED])
    h = FiveGKpiAdapter({"dataset_path": str(d)}).healthcheck()
    assert h["mode"] == "dataset"
    assert h["dataset_path"] == str(d)
    # 未配置 -> demo
    assert FiveGKpiAdapter().healthcheck()["mode"] == "demo"


def test_fiveg_ts_normalization():
    assert _ds._parse_ts("2019.12.16_13.40.04") == "2019-12-16T13:40:04Z"
    assert _ds._parse_ts("-") == ""
    assert _ds._parse_ts("") == ""


def test_fiveg_min_severity_floor():
    """min_severity 过滤掉低严重度告警（真实数据噪声抑制）。"""
    all_p = _ds.row_to_payloads(_DEGRADED)            # 7 critical + 1 major
    crit_only = _ds.row_to_payloads(_DEGRADED, min_severity="critical")
    assert len(all_p) == 8
    assert len(crit_only) == 7                        # 去掉 major 的 RSRQ
    assert all(p["severity"] == "critical" for p in crit_only)


def test_fiveg_oss_column_aliases_override(tmp_path):
    """OSS 导出（华为/中兴/爱立信列名差异）经 column_aliases 覆盖，纯配置切源、零代码改动。

    验证核心：小区标识列(eNodeB ID)与时间列(Time)不经别名无法映射 → host=unknown-cell、
    ts 为空；配置别名后正确映射为 host=BTS-07 且 ts 规整。
    """
    oss_cols = ["Time", "eNodeB ID", "LTE_RSRP", "LTE_RSRQ", "SINR", "CQI", "RSSI",
                "DL_Throughput", "UL_Throughput", "Packet_Loss"]
    oss_row = {"Time": "2026-09-25 13:40:00", "eNodeB ID": "BTS-07", "LTE_RSRP": "-125",
               "LTE_RSRQ": "-20", "SINR": "-7", "CQI": "2", "RSSI": "-105",
               "DL_Throughput": "2", "UL_Throughput": "0", "Packet_Loss": "8"}
    d = tmp_path / "oss"
    d.mkdir()
    with open(d / "oss_export.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=oss_cols)
        w.writeheader()
        w.writerow(oss_row)

    # ① 无别名：小区标识/时间列匹配不上 → host 全是 unknown-cell、ts 为空
    no_alias = FiveGKpiAdapter({"dataset_path": str(d)}).load_dataset_alerts()
    assert no_alias, "应至少产出部分告警"
    assert all(a["host"] == "unknown-cell" for a in no_alias)
    assert all(a["ts"] == "" for a in no_alias)
    assert not any(a["host"] == "BTS-07" for a in no_alias)  # 别名才是切源关键

    # ② 配置别名：OSS 列名 → 规范列名
    aliases = {
        "time": "Timestamp", "enodeb id": "CellID", "lte_rsrp": "RSRP",
        "lte_rsrq": "RSRQ", "sinr": "SNR", "cqi": "CQI", "rssi": "RSSI",
        "dl_throughput": "DL_bitrate", "ul_throughput": "UL_bitrate",
        "packet_loss": "PINGLOSS",
    }
    adp = FiveGKpiAdapter({"dataset_path": str(d), "column_aliases": aliases})
    alerts = adp.load_dataset_alerts()
    assert len(alerts) == 8            # 8 个超阈指标（含 RSSI）
    assert all(a["host"] == "BTS-07" for a in alerts)
    by_metric = {a["metric"]: a["severity"] for a in alerts}
    assert by_metric["rsrp_dbm"] == "critical"
    assert by_metric["rsrq_db"] == "major"
    assert by_metric["ping_loss_pct"] == "critical"
    # 时间戳经别名映射后也能正常规整
    assert alerts[0]["ts"] == "2026-09-25T13:40:00Z"
    # healthcheck 抽样同样走别名（dataset 模式）
    assert adp.healthcheck()["mode"] == "dataset"
