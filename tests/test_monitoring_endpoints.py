"""监控接入端点测试（MCP 真连：Grafana/Prometheus/Loki）。

验证：
  - /adapters/metrics-prometheus/query  demo 路径返回仿真时序
  - /adapters/logs-loki/logs            demo 路径返回仿真日志
  - 未知 adapter -> 404；不支持该能力的 adapter -> 400
"""
import pytest


def test_metrics_query_demo(client, admin_headers):
    r = client.post("/adapters/metrics-prometheus/query",
                    json={"promql": "up", "hours": 1}, headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "demo"
    assert len(d["series"]) > 0


def test_logs_query_demo(client, admin_headers):
    r = client.post("/adapters/logs-loki/logs",
                    json={"query": '{job="x"}', "limit": 10}, headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "demo"
    assert len(d["logs"]) > 0
    assert d["adapter_id"] == "logs-loki"


def test_metrics_query_unknown_adapter_404(client, admin_headers):
    r = client.post("/adapters/does-not-exist/query",
                    json={"promql": "up"}, headers=admin_headers)
    assert r.status_code == 404


def test_logs_query_no_log_capability_400(client, admin_headers):
    # alert-prometheus 是 AlertAdapter，没有 fetch_recent -> 400
    r = client.post("/adapters/alert-prometheus/logs",
                    json={"query": "x"}, headers=admin_headers)
    assert r.status_code == 400


def test_metrics_query_no_metric_capability_400(client, admin_headers):
    # alert-prometheus 没有 query_metrics -> 400
    r = client.post("/adapters/alert-prometheus/query",
                    json={"promql": "up"}, headers=admin_headers)
    assert r.status_code == 400
