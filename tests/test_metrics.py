# -*- coding: utf-8 -*-
"""Prometheus /metrics 回归测试：格式合法 + 三类指标齐全 + 实时 gauge。"""
import re

from src.core import metrics


def test_metrics_render_prometheus_format(client, auth_headers):
    """访问 /metrics（无需鉴权），应返回合法的 Prometheus 文本格式。"""
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert "text/plain" in r.headers.get("content-type", "")
    text = r.text

    # 至少包含四类指标：请求 counter / 耗时 histogram / 进程 gauge / 业务域 gauge
    for name in ("teleops_http_requests_total",
                 "teleops_http_request_duration_seconds",
                 "teleops_process_uptime_seconds",
                 "teleops_workspaces_total"):
        assert re.search(rf"^# HELP {name} ", text, re.M), f"缺少 {name} HELP"
        assert re.search(rf"^# TYPE {name} ", text, re.M), f"缺少 {name} TYPE"

    # 每行格式：metric{labels} value（label value 允许含路径模板 {ws_id} 等）
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert re.match(r"^[a-z_][a-z0-9_]*(?:\{.*\})? [0-9.]+$", line), f"非法指标行: {line}"


def test_metrics_counters_advance(client, auth_headers):
    """触发一次 LLM 任务后 jobs/llm counter 应递增。"""
    before = client.get("/metrics").text
    m = re.search(r"^teleops_jobs_total (\d+)", before, re.M)
    b_jobs = int(m.group(1)) if m else 0

    # 发起一个后台诊断任务并等其完成
    alert = {"alert_id": "M-A1", "ts": "", "source": "zabbix", "metric": "optical_power",
             "host": "onu-1", "severity": "critical", "value": "-23.5dBm",
             "message": "ONU 光模块发光功率异常偏低", "tags": ["optical"], "is_noise": False}
    r = client.post("/alert", headers=auth_headers, json={"alert": alert})
    assert r.status_code == 200, r.text
    from tests.conftest import wait_job
    ok, _ = wait_job(client, r.json()["job_id"], timeout=60)
    assert ok

    after = client.get("/metrics").text
    a_jobs = int(re.search(r"^teleops_jobs_total (\d+)", after, re.M).group(1))
    assert a_jobs >= b_jobs + 1, f"jobs counter 未递增: {b_jobs} -> {a_jobs}"
    llm = int(re.search(r"^teleops_llm_calls_total\{[^}]*\} (\d+)", after, re.M).group(1))
    assert llm >= 1, "LLM 调用未被统计"


def test_metrics_gauge_reflects_db(client, auth_headers):
    """实时 gauge 应反映当前库里的业务域与 Agent 数量。"""
    def ws_total():
        text = client.get("/metrics").text
        return int(re.search(r"^teleops_workspaces_total (\d+)", text, re.M).group(1))

    before = ws_total()
    r = client.post("/workspaces", headers=auth_headers,
                    json={"name": "metrics-gauge-ws", "description": "metrics gauge 测试"})
    assert r.status_code == 200, r.text
    after = ws_total()
    assert after == before + 1, f"业务域 gauge 未随建域刷新: {before} -> {after}"

    # 清理测试域（保持基线干净）
    ws_id = r.json().get("id")
    if ws_id:
        client.delete(f"/workspaces/{ws_id}", headers=auth_headers)


def test_metrics_reset_unit():
    """metrics 模块内部：直方图桶为 Prometheus 累积语义，counter 可重置。"""
    metrics.reset()
    metrics.observe_seconds("t_dur", 0.01)
    metrics.observe_seconds("t_dur", 0.2)
    text = metrics.render()
    bucket = dict(re.findall(r't_dur_bucket\{le="([^"]+)"\} (\d+)', text))
    assert bucket["0.05"] == "1"     # 0.01 落在最小桶
    assert bucket["0.25"] == "2"     # 0.01+0.2 都 <= 0.25
    assert bucket["+Inf"] == "2"     # 全部观测
