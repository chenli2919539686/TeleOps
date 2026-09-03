# -*- coding: utf-8 -*-
"""JWT 端到端测试: register -> login -> me -> create ws (无/有 token) -> write/read messages"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
RESULTS = []


def call(method, path, body=None, token=None, headers=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f"  | {detail}" if detail and not cond else ""))


# 0. auth status
st, body = call("GET", "/auth/status")
check("auth/status 返回 200 且 jwt_enabled", st == 200 and body.get("jwt_enabled") is True, str(body))

# 1. register（若已存在则忽略 409/400）
st, body = call("POST", "/auth/register", {"username": "e2e_tester", "password": "Test123456"})
token_reg = (body or {}).get("token") if isinstance(body, dict) else None
check("register 成功或用户已存在", st in (200, 201, 400, 409), f"status={st}")

# 2. login
st, body = call("POST", "/auth/login", {"username": "e2e_tester", "password": "Test123456"})
token = (body or {}).get("token") if isinstance(body, dict) else None
check("login 返回 token", st == 200 and token, f"status={st} body={body}")

# 3. 错误口令登录被拒
st, body = call("POST", "/auth/login", {"username": "e2e_tester", "password": "wrong"})
check("错误口令登录被拒", st in (401, 400), f"status={st}")

# 4. /auth/me
st, body = call("GET", "/auth/me", token=token)
check("/auth/me 返回用户名", st == 200 and isinstance(body, dict) and body.get("username") == "e2e_tester", f"status={st} body={body}")

# 5. 无 token 创建 ws -> 401
st, body = call("POST", "/workspaces", {"name": "jwt-e2e-ws", "description": "JWT e2e"})
check("无 token 创建业务域被拒(401)", st == 401, f"status={st} body={body}")

# 6. 篡改 token -> 401
st, body = call("POST", "/workspaces", {"name": "jwt-e2e-ws"}, token=(token or "x") + "x")
check("篡改 token 被拒(401)", st == 401, f"status={st}")

# 7. 有 token 创建 ws -> 200, 记录 owner
st, body = call("POST", "/workspaces", {"name": "jwt-e2e-ws", "description": "JWT e2e"}, token=token)
ws_id = (body or {}).get("id") if isinstance(body, dict) else None
check("带 token 创建业务域成功", st in (200, 201) and ws_id, f"status={st} body={body}")

# 8. owner 校验：list workspaces 看新建域
st, body = call("GET", "/workspaces")
names = [w.get("name") for w in (body or {}).get("workspaces", [])] if isinstance(body, dict) else []
check("新业务域出现在列表中", "jwt-e2e-ws" in names, str(names))

# 9. 写消息（带 token，字段与 MessageReq 一致）
if ws_id:
    st, body = call("POST", f"/workspaces/{ws_id}/messages",
                    {"agent_id": "a-demo", "kind": "diagnose", "summary": "JWT e2e 测试消息",
                     "detail": "端到端验证写入"}, token=token)
    check("带 token 写消息成功", st in (200, 201), f"status={st} body={body}")

    # 10. 读消息
    st, body = call("GET", f"/workspaces/{ws_id}/messages")
    msgs = (body or {}).get("messages", []) if isinstance(body, dict) else []
    check("读消息含刚写入内容", st == 200 and any("JWT e2e 测试消息" in (m.get("summary") or "") for m in msgs), f"status={st} n={len(msgs)}")

    # 11. 清理：删除测试域
    st, body = call("DELETE", f"/workspaces/{ws_id}", token=token)
    check("清理测试业务域", st in (200, 204), f"status={st}")

ok = sum(1 for _, c, _ in RESULTS if c)
print(f"\n=== {ok}/{len(RESULTS)} 通过 ===")
exit(0 if ok == len(RESULTS) else 1)
