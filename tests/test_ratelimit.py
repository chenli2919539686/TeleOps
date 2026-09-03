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
