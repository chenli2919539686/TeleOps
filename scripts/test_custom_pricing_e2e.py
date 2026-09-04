# -*- coding: utf-8 -*-
"""v0.8.6 自定义单价端到端验证：注册→登录→保存单价→验证生效→清除→验证回退。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method, path, body=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(r, data) as resp:
        return json.loads(resp.read().decode())


# 1. 注册（已存在则忽略报错）并登录
try:
    req("POST", "/auth/register", {"username": "price_test", "password": "test12345"})
except Exception:
    pass
token = req("POST", "/auth/login",
            {"username": "price_test", "password": "test12345"})["token"]

# 2. 保存自定义单价
req("POST", "/llm/config",
    {"provider": "deepseek",
     "pricing": {"deepseek.deepseek-chat": [1.5, 0.2, 6.0]}}, token)
p = req("GET", "/llm/usage")["pricing"]
print("保存后:", p)
assert p["source"] == "custom" and p["price"] == [1.5, 0.2, 6.0], "自定义单价未生效"

# 3. 清除自定义单价（整包提交空 pricing）
req("POST", "/llm/config", {"provider": "deepseek", "pricing": {}}, token)
p = req("GET", "/llm/usage")["pricing"]
print("清除后:", p)
assert p["source"] == "builtin" and p["price"] == [2.0, 0.5, 8.0], "清除后未回退内置表"

print("\n✅ 端到端验证通过：自定义单价 保存 → 生效 → 清除 → 回退内置表")
