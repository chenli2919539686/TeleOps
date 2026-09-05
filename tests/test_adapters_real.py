"""集成适配器真实落地的单元测试（Zabbix 告警 / ELK 日志）。

覆盖两条路径：
  - demo 模式（未配置 base_url/凭据）：parse/fetch 返回结构正确的样例数据
  - 真实连接映射（monkeypatch 网络调用）：外部 API 响应 -> 统一 Schema 的转换
"""
import sys
from unittest.mock import MagicMock

import pytest

from src.adapters.real_adapters import (
    ZabbixAlertAdapter, ELKLogAdapter, _norm_severity,
)
from src.adapters import real_adapters


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
