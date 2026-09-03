# -*- coding: utf-8 -*-
"""只读端点冒烟：确认服务可启动、基线数据正确、LLM 走离线 Mock。"""
import urllib.parse


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ok"
    assert d["nodes"] > 0
    # 测试环境强制离线 Mock，不应联网
    assert d["llm_mode"] == "mock", f"测试环境应走 Mock，实际 {d['llm_mode']}"


def test_api_info(client):
    """根路径已让位静态页面，元信息改由 /api/info 提供。"""
    r = client.get("/api/info")
    assert r.status_code == 200, r.text
    assert "service" in r.json()

    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", ""), "根路径应为静态前端页面"


def test_topology(client):
    d = client.get("/topology").json()
    assert len(d["nodes"]) > 0 and len(d["edges"]) > 0


def test_tools_baseline(client):
    names = [t["name"] for t in client.get("/tools").json()["tools"]]
    assert set(names) >= {"ping_host", "restart_service"}


def test_adapters(client):
    d = client.get("/adapters").json()
    assert d["count"] > 0


def test_knowledge_chinese_query(client):
    """中文查询需 URL 编码（历史 bug：未编码导致 500）。"""
    q = urllib.parse.quote("光模块告警")
    r = client.get(f"/knowledge?q={q}&top_k=2")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["hits"], list)


def test_workspaces_list(client):
    d = client.get("/workspaces").json()
    assert isinstance(d["workspaces"], list)
    # 悬浮卡统计字段必须存在
    for w in d["workspaces"]:
        assert "pending" in w
