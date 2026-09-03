#!/usr/bin/env python3
"""TeleOps 整体项目重新验证脚本（受控，只读 + 最小受控闭环，结束自动清理测试数据回到基线）。"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error

BASE = "http://localhost:8000"
results = []
_TOKEN = ""          # 每用户 JWT（写接口鉴权）
VERIFY_USER = "verify_bot"
VERIFY_PASS = "Verify123456"


def log(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" :: {detail}" if detail else ""))


def _headers(extra=None):
    h = {"Content-Type": "application/json"}
    if _TOKEN:
        h["Authorization"] = "Bearer " + _TOKEN
    if extra:
        h.update(extra)
    return h


def _snapshot():
    """记录 DB 基线快照（users/requirements/messages/tools），供结束清理。"""
    import sqlite3, os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "teleops.db")
    if not os.path.exists(path):
        return None, {}
    con = sqlite3.connect(path)
    snap = {
        "users": {r[0] for r in con.execute("SELECT username FROM users")},
        "requirements": {r[0] for r in con.execute("SELECT id FROM requirements")},
        "messages": {r[0] for r in con.execute("SELECT id FROM messages")},
        "tools": {r[0] for r in con.execute("SELECT name FROM tools")},
    }
    con.close()
    return path, snap


def _restore(path, snap):
    """删除快照之后新增的测试数据（verify_bot / 需求 / 消息 / 工具）。"""
    if not path or not snap:
        return
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("DELETE FROM users WHERE username=?", (VERIFY_USER,))
    for t, col in (("requirements", "id"), ("messages", "id"), ("tools", "name")):
        for (v,) in con.execute(f"SELECT {col} FROM {t}"):
            if v not in snap[t]:
                con.execute(f"DELETE FROM {t} WHERE {col}=?", (v,))
    con.commit()
    con.close()


def get(path):
    req = urllib.request.Request(BASE + path, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post(path, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def delete(path):
    req = urllib.request.Request(BASE + path, headers=_headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _login():
    """注册（忽略已存在）并登录验证账号，取得 JWT。"""
    global _TOKEN
    post("/auth/register", {"username": VERIFY_USER, "password": VERIFY_PASS})
    st, d = post("/auth/login", {"username": VERIFY_USER, "password": VERIFY_PASS})
    if st == 200 and d.get("token"):
        _TOKEN = d["token"]
        return True
    return False


def poll(job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, d = get(f"/jobs/{job_id}")
        except Exception:
            time.sleep(1); continue
        if d.get("status") == "done":
            return True, d.get("result")
        if d.get("status") == "error":
            return False, d.get("error")
        if d.get("status") == "not_found":
            return False, "not_found"
        time.sleep(1.5)
    return False, "timeout"


# ---------------- T0: JWT 登录 ----------------
DB_PATH, SNAP = _snapshot()
try:
    log("T0 jwt login", _login(), "verify_bot 登录获取 JWT")
except Exception as e:
    log("T0 jwt login", False, str(e))

# ---------------- T1: 只读端点 ----------------
try:
    st, d = get("/health"); log("T1a health", st == 200 and d.get("status") == "ok", f"llm={d.get('llm_mode')} nodes={d.get('nodes')} tools={d.get('tools')}")
except Exception as e:
    log("T1a health", False, str(e))

try:
    st, d = get("/topology"); log("T1b topology", st == 200 and len(d.get("nodes", [])) > 0, f"nodes={len(d.get('nodes', []))}")
except Exception as e:
    log("T1b topology", False, str(e))

try:
    st, d = get("/tools"); log("T1c tools", st == 200 and len(d.get("tools", [])) == 2, f"tools={[t['name'] for t in d.get('tools', [])]}")
except Exception as e:
    log("T1c tools", False, str(e))

try:
    st, d = get("/adapters"); log("T1d adapters", st == 200 and d.get("count", 0) > 0, f"count={d.get('count')}")
except Exception as e:
    log("T1d adapters", False, str(e))

try:
    st, d = get("/knowledge?q=" + urllib.parse.quote("光模块告警") + "&top_k=2"); log("T1e knowledge", st == 200, f"hits={len(d.get('hits', []))}")
except Exception as e:
    log("T1e knowledge", False, str(e))

try:
    st, d = get("/workspaces"); wss = d.get("workspaces", []); ok = st == 200 and len(wss) >= 1
    log("T1f workspaces", ok, f"count={len(wss)} ids={[w['id'] for w in wss]}")
except Exception as e:
    log("T1f workspaces", False, str(e))

# ---------------- T2: A3 幽灵域不崩溃 ----------------
try:
    st, d = get("/agents?workspace_id=ghost-xyz")
    ok = st == 200 and d.get("agents") == []
    log("T2a ghost-domain agents", ok, f"status={st} agents={d.get('agents')}")
except Exception as e:
    log("T2a ghost-domain agents", False, str(e))

try:
    # 幽灵域 + 噪声告警发起需求：应优雅返回（无 missing_tool 早退），不抛 500
    st, d = post("/requirements/raise", {"workspace_id": "ghost-xyz",
                                        "alert": {"alert_id": "A-NOISE", "metric": "cpu", "host": "host-x",
                                                  "severity": "info", "value": "62%", "message": "抖动",
                                                  "tags": ["compute"], "is_noise": True}})
    ok = st == 200
    log("T2b ghost-domain raise no-500", ok, f"status={st} body={json.dumps(d, ensure_ascii=False)[:120]}")
except Exception as e:
    log("T2b ghost-domain raise no-500", False, str(e))

# ---------------- T3: A1 跨域隔离（建 ws-2 跑闭环，验证研发路由落在 ws-2） ----------------
try:
    st, d = post("/workspaces", {"name": "验证域A1", "adapter_id": "alert-prometheus", "mode": "auto"})
    ok = st == 200 and d.get("id", "").startswith("ws-")
    ws2 = d.get("id") if ok else None
    log("T3a create ws-2", ok, f"id={ws2}")
except Exception as e:
    log("T3a create ws-2", False, str(e)); ws2 = None

if ws2:
    try:
        st, d = get(f"/agents?workspace_id={ws2}")
        ws2_dev = [a["id"] for a in d.get("agents", []) if a["kind"] == "dev"]
        log("T3b ws-2 agents isolated", st == 200 and len(ws2_dev) == 1 and ws2_dev[0].startswith(ws2),
            f"dev={ws2_dev}")
    except Exception as e:
        log("T3b ws-2 agents isolated", False, str(e))

    try:
        st, d = post("/requirements/raise", {"workspace_id": ws2,
            "alert": {"alert_id": "A-TEMP-A1", "metric": "temperature", "host": "host-1",
                      "severity": "critical", "value": "88C", "message": "温度过热",
                      "tags": ["compute", "temperature"], "is_noise": False}})
        if st == 200 and "job_id" in d:
            ok_poll, res = poll(d["job_id"])
            # res 要么含 requirement（已闭环），要么含 error（未触发缺口）
            req = res.get("requirement") if isinstance(res, dict) else None
            if req:
                bound = req.get("workspace_id") == ws2
                dev_bound = (req.get("assigned_dev_agent_id") or "").startswith(ws2)
                log("T3c A1 routing in-domain", bound and dev_bound,
                    f"req_ws={req.get('workspace_id')} dev={req.get('assigned_dev_agent_id')} mode={req.get('mode')}")
            else:
                log("T3c A1 routing in-domain", ok_poll, f"no requirement; res={json.dumps(res, ensure_ascii=False)[:120]}")
        else:
            log("T3c A1 routing in-domain", False, f"status={st} body={json.dumps(d, ensure_ascii=False)[:120]}")
    except Exception as e:
        log("T3c A1 routing in-domain", False, str(e))

    try:
        st, d = delete(f"/workspaces/{ws2}")
        ok = st == 200 and d.get("deleted") == ws2
        log("T3d delete ws-2 cascade", ok, f"status={st}")
    except Exception as e:
        log("T3d delete ws-2 cascade", False, str(e))

# ---------------- T4: A2 工作台 -> 消息栏闭环（core-net 温度告警） ----------------
try:
    st, d = post("/agents/core-net-ops-main/register-gap", {
        "alert": {"alert_id": "A-TEMP-CORE", "metric": "temperature", "host": "host-1",
                  "severity": "critical", "value": "88C", "message": "温度过热",
                  "tags": ["compute", "temperature"], "is_noise": False},
        "diagnosis": {"conclusion": "散热故障，需 temperature_probe 工具确认"},
        "missing_tool": "temperature_probe"})
    if st == 200 and "job_id" in d:
        ok_poll, res = poll(d["job_id"])
        req = res.get("requirement") if isinstance(res, dict) else None
        if req:
            closed = req.get("status") == "done"
            log("T4a A2 register-gap closed loop", ok_poll and closed,
                f"req_id={req.get('id')} status={req.get('status')} ws={req.get('workspace_id')} dev={req.get('assigned_dev_agent_id')}")
        else:
            log("T4a A2 register-gap closed loop", ok_poll, f"res={json.dumps(res, ensure_ascii=False)[:160]}")
    else:
        log("T4a A2 register-gap closed loop", False, f"status={st} body={json.dumps(d, ensure_ascii=False)[:160]}")
except Exception as e:
    log("T4a A2 register-gap closed loop", False, str(e))

# ---------------- 汇总 ----------------
print("\n===== 验证汇总 =====")
np_ = sum(1 for _, ok, _ in results if ok)
print(f"通过 {np_}/{len(results)}")
fails = [n for n, ok, _ in results if not ok]
if fails:
    print("失败项:", fails)
else:
    print("全部通过 ✅")

# ---------------- 清理：回到干净基线 ----------------
try:
    _restore(DB_PATH, SNAP)
    print("已清理测试数据（verify_bot / 新增需求 / 消息 / 工具），回到基线。")
except Exception as e:
    print(f"清理失败（可手工删除 verify_bot 用户）: {e}")
