# -*- coding: utf-8 -*-
"""W3 经典端点回归（原 test_api.py 的 pytest 化版本）：告警处置 / RAG 问答 / 反馈造工具 / 完整闭环 / 追溯。

注：/alert 与 /closed-loop/run 已改为异步任务（返回 job_id），需轮询 /jobs/{id} 取结果。
"""

import pytest

from tests.conftest import wait_job

ALERT = {
    "alert_id": "A-1", "ts": "", "source": "zabbix", "metric": "optical_power",
    "host": "onu-1", "severity": "critical", "value": "-23.5dBm",
    "message": "ONU 光模块发光功率异常偏低", "tags": ["optical"], "is_noise": False,
}

# 闭环必须用「工具确实缺失」的告警：Mock 对 光模块/temperature 两类关键词才推荐新工具
LOOP_ALERTS = {
    "optical_power_probe": {
        "alert_id": "A-OPT", "ts": "", "source": "zabbix", "metric": "optical_power",
        "host": "onu-9", "severity": "critical", "value": "-24.1dBm",
        "message": "ONU 光模块发光功率异常偏低", "tags": ["optical"], "is_noise": False,
    },
    "temperature_probe": {
        "alert_id": "A-TEMP", "ts": "", "source": "zabbix", "metric": "temperature",
        "host": "host-9", "severity": "critical", "value": "88C",
        "message": "核心温度过热", "tags": ["compute", "temperature"], "is_noise": False,
    },
}


def test_alert_diagnosis(client, auth_headers):
    r = client.post("/alert", headers=auth_headers, json={"alert": dict(ALERT)})
    assert r.status_code == 200, r.text
    ok, res = wait_job(client, r.json()["job_id"])
    assert ok, f"告警处置失败：{res}"
    assert "diagnosis" in res, f"返回缺少 diagnosis：{list(res or {})}"


def test_chat_rag(client, auth_headers):
    r = client.post("/chat", headers=auth_headers,
                    json={"question": "光模块发光功率异常怎么处理", "top_k": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["answer"]) > 0
    assert isinstance(d["retrieved"], list)


def test_feedback_creates_tool(client, auth_headers):
    r = client.post("/feedback", headers=auth_headers,
                    json={"feedback_id": "F-PYTEST", "summary": "需要探测光模块温度"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["created_tool"]["name"]


def test_closed_loop_run(client, auth_headers):
    """完整纵向闭环：运维缺工具 → 研发造工具 → 第二轮复用成功。

    注意：闭环要求「缺失的工具确实不存在」。同会话内其它测试可能已造出同名工具，
    因此先删除目标探针工具，确保闭环被真实触发（而非因工具已存在而直接跳过）。
    """
    from src.api.server import tools as tool_registry  # 测试用的临时环境，可安全清理

    tool, chosen = next(iter(LOOP_ALERTS.items()))
    tool_registry.remove(tool)   # 走注册表删除（同时刷内存缓存）
    assert tool not in {t["name"] for t in client.get("/tools").json()["tools"]}, "工具清理未生效"

    r = client.post("/closed-loop/run", headers=auth_headers, json={"alert": chosen})
    assert r.status_code == 200, r.text
    ok, d = wait_job(client, r.json()["job_id"], timeout=120)
    assert ok, f"闭环任务失败：{d}"
    assert d.get("missing_tool"), f"闭环应触发缺失工具：{list(d or {})}"
    assert d.get("loop_closed") is True, f"闭环未闭合：{d}"
    if d.get("dev_result"):
        assert d["dev_result"]["tool"]["name"] == d["missing_tool"]
    assert any(tr.get("status") == "ok"
               for tr in ((d.get("round2") or {}).get("tool_results") or [])), "第二轮应调用新工具成功"


def test_traces(client):
    d = client.get("/traces").json()
    assert len(d["traces"]) > 0


def test_alert_requires_auth(client):
    assert client.post("/alert", json={"alert": dict(ALERT)}).status_code == 401
