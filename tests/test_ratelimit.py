# -*- coding: utf-8 -*-
"""限流中间件回归测试（Phase 3）。

conftest 全局 TELEOPS_RATE_LIMIT=off + autouse fixture 复位，保证既有测试不受干扰；
本文件运行时打开限流并调小阈值，直接验证：
1. 登录接口超限 → 429 + Retry-After（防口令爆破档最严）
2. 写接口（POST /workspaces）超限 → 429，且已放行的请求正常完成
3. /metrics /health 等低频/资源路径不计 API 额度，始终放行
4. 被限流请求计数进 teleops_rate_limited_total 指标
"""
from src.core import rate_limit as rl


def _open(login=1000, read=1000, write=1000):
    rl.configure_rate_limit(enabled=True, read=read, write=write, login=login)


def test_login_endpoint_rate_limited(client):
    """登录档最严：超限返回 429 + Retry-After。"""
    _open(login=3)
    # 前 3 次到达业务层（ghost 用户不存在 → 401 而非 429）
    for _ in range(3):
        r = client.post("/auth/login", json={"username": "ghost", "password": "wrong"})
        assert r.status_code == 401, r.text
    # 第 4 次被限流
    r = client.post("/auth/login", json={"username": "ghost", "password": "wrong"})
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1
    # 登录与读请求是不同 key：读接口不受登录档牵连
    assert client.get("/topology").status_code == 200


def test_write_endpoints_rate_limited(client, auth_headers):
    """写档：前 2 次正常建域成功，第 3 次 429；清理已建域。"""
    _open(write=2)
    created = []
    try:
        for i in range(2):
            r = client.post("/workspaces", json={"name": f"rl-ws-{i}"}, headers=auth_headers)
            assert r.status_code in (200, 201), r.text
            created.append(r.json()["id"])
        r = client.post("/workspaces", json={"name": "rl-ws-over"}, headers=auth_headers)
        assert r.status_code == 429, r.text
    finally:
        for w in created:
            client.delete(f"/workspaces/{w}", headers=auth_headers)


def test_low_freq_paths_not_rate_limited(client):
    """/metrics /health 与静态资源放行，不占 API 额度；login 压到 1 立即触发 429 验证分层。"""
    _open(login=1)
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.post("/auth/login", json={"username": "x", "password": "y"}).status_code == 401
    # 第二次 login 触发 429
    r = client.post("/auth/login", json={"username": "x", "password": "y"})
    assert r.status_code == 429
    # 429 已进入独立计数器（/metrics 再抓一次即可见）
    assert "teleops_rate_limited_total" in client.get("/metrics").text
    # 静态资源路径同样放行
    assert client.get("/styles.css").status_code in (200, 404)  # 不被 429 拦即可


def test_default_thresholds_v0812():
    """v0.8.12：默认阈值调严到推荐档（登录 5 / 写 60 / 读 120）。"""
    # 用环境变量关闭副作用，但阈值是模块级常量，重新 import 检查
    import importlib
    import src.core.rate_limit as rl_mod
    importlib.reload(rl_mod)
    # 重载模块会重新读环境变量；此时 TELEOPS_RATE_LIMIT_* 仍是 conftest 默认值
    # 直接断言当前默认（reload 后等同模块顶层定义）
    assert rl_mod.LOGIN_LIMIT == 5, f"登录档应为 5，实际 {rl_mod.LOGIN_LIMIT}"
    assert rl_mod.WRITE_LIMIT == 60, f"写档应为 60，实际 {rl_mod.WRITE_LIMIT}"
    assert rl_mod.READ_LIMIT == 120, f"读档应为 120，实际 {rl_mod.READ_LIMIT}"


def test_alert_ingest_rate_limited(client, auth_headers):
    """/adapters/alert/ingest 走写档限额（北向告警源防刷）。"""
    _open(write=3)
    payload = {"alerts": [{"alertname": "TestAlert",
                           "labels": {"host": "h1"},
                           "annotations": {"summary": "unit"}}]}
    # 前 3 次到达业务层（adapter_id 可能无效但不会被 429 拦）
    seen = []
    for _ in range(3):
        r = client.post("/adapters/alert/ingest?adapter_id=alert-prometheus",
                        json=payload, headers=auth_headers)
        seen.append(r.status_code)
        # 不管业务层 200/4xx，关键是不要在这一步出现 429
        assert r.status_code != 429, f"前 3 次不应被限流: {r.text}"
    # 第 4 次被限流
    r = client.post("/adapters/alert/ingest?adapter_id=alert-prometheus",
                    json=payload, headers=auth_headers)
    assert r.status_code == 429, f"第 4 次应被限流，实际 {r.status_code}（前 3 次: {seen}）"
    assert int(r.headers.get("Retry-After", "0")) >= 1
