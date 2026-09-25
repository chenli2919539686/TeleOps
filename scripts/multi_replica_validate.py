#!/usr/bin/env python3
"""TeleOps 多副本实战验证（D3 收官 / Phase 2→3 桥梁）。

起一个真实 Redis + 两个独立 uvicorn 副本（端口 8001/8002），所有共享状态
（限流/任务/流/信号量）切到 Redis 后端，实证：

  1. 两个副本 /health 暴露不同 replica_id（确为两个独立进程）；
  2. 副本 A 启动告警流 → 副本 B 的 /stream/status 立即可见 running
     （StreamStateStore 经 Redis 跨进程共享，验证 D3 多副本无状态核心承诺）；
  3. 副本 B 能停止该流（写操作跨副本生效）。

注意（D3 设计分野）：默认执行器是 ThreadStreamExecutor，把 running 状态放在
**进程内** AlertStream 对象，天然不跨副本（"与现状一致"，供单机/演示零依赖）。
要验证"多副本共享流状态"，必须切到 QueueStreamExecutor
（`TELEOPS_STREAM_EXECUTOR=queue`）：流状态/控制面落 Redis，任何副本都能看/停。
Windows 无 os.fork，RQ worker 跑不起来 → 本脚本只验证"状态/控制面跨副本共享"
（start 把 running 写 Redis、stop 改 Redis），真正的逐条播放由 Unix 部署态的
worker 消费 —— 那属于 playback 平面，不在此验证范围。

全程 mock LLM（临时配置，不触碰线上 data/llm_config.json），无外部调用、无费用。

用法：
    python scripts/multi_replica_validate.py
环境变量（可选）：
    MR_REDIS_PORT    Redis 端口（默认 6391）
    MR_HTTP_PORTS    两副本端口，逗号分隔（默认 8001,8002）
清理：脚本结束（含异常）会 terminate 两个 uvicorn 与 Redis。
"""
import os
import sys
import time
import json
import socket
import subprocess
import urllib.request
import urllib.error
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDIS_EXE = os.path.join(REPO, "tools", "redis", "redis-server.exe")
APP = "src.api.server:app"
PY = os.path.join(
    os.path.dirname(sys.executable), "python.exe"
) if sys.platform.startswith("win") else sys.executable

REDIS_PORT = int(os.environ.get("MR_REDIS_PORT", "6391"))
HTTP_PORTS = [int(p) for p in os.environ.get("MR_HTTP_PORTS", "8001,8002").split(",") if p]
TOKEN = "mr-demo-token"
WS = "core-net"  # 公共域：admin（X-API-Token）可写，匿名仅可读


def log(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


def wait_redis(redis_url, timeout=15):
    try:
        import redis as _redis
    except Exception:
        return False
    r = _redis.Redis.from_url(redis_url, protocol=2)  # 兼容旧版/代理 Redis
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if r.ping():
                return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_health(port, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def api(port, path, method="GET", token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if token:
        req.add_header("X-API-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def is_port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    if not os.path.exists(REDIS_EXE):
        print(f"[ERR] 找不到 redis-server：{REDIS_EXE}")
        print("      先运行：从 nuget redis-64 解压 redis-server.exe 到 tools/redis/")
        return 2

    for p in HTTP_PORTS + [REDIS_PORT]:
        if not is_port_free(p):
            print(f"[ERR] 端口 {p} 被占用，请释放后重试")
            return 2

    # 临时 mock LLM 配置：强制 provider=mock，不触碰线上 data/llm_config.json
    mock_cfg = {
        "provider": "mock", "api_key": "", "base_url": "", "model": "mock",
        "local_endpoint": "", "local_model": "", "llm_triage": False,
        "budget_daily_cny": 0, "budget_action": "fallback", "pricing": {},
    }
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(mock_cfg, tmp)
    tmp.close()
    mock_cfg_path = tmp.name

    redis_proc = None
    uvicorns = []
    results = []
    try:
        # 1) 起 Redis（纯内存、禁持久化）；若已通过 MR_REDIS_URL 提供可达实例则复用
        redis_url = os.environ.get("MR_REDIS_URL")
        if redis_url and wait_redis(redis_url, timeout=3):
            print(f"[info] 复用外部 Redis：{redis_url}")
        else:
            if redis_url:
                print(f"[warn] MR_REDIS_URL {redis_url} 不可达，改自起 Redis")
            redis_proc = subprocess.Popen(
                [REDIS_EXE, "--port", str(REDIS_PORT), "--save", "",
                 "--appendonly", "no", "--maxmemory", "256mb",
                 "--maxmemory-policy", "allkeys-lru"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            redis_url = f"redis://127.0.0.1:{REDIS_PORT}/0"
            if not wait_redis(redis_url, timeout=15):
                raise RuntimeError("Redis 未能在 15s 内就绪（检查 tools/redis/redis-server.exe 是否被沙箱拦截）")

        # 2) 起两个独立 uvicorn 副本
        for i, port in enumerate(HTTP_PORTS):
            rid = f"replica-{chr(65 + i)}"  # replica-A / replica-B
            env = dict(os.environ)
            env.update({
                "TELEOPS_STATE_STORE": "redis",
                "TELEOPS_REDIS_URL": redis_url,
                "TELEOPS_REDIS_PROTOCOL": "2",        # 兼容旧版/代理 Redis（不支持 HELLO）
                # 默认线程执行器把 running 状态放进程内，不跨副本；切队列执行器让
                # 流状态/控制面落 Redis（Windows 无 worker，只验证状态共享，不播放）
                "TELEOPS_STREAM_EXECUTOR": "queue",
                "TELEOPS_API_TOKEN": TOKEN,           # 等价于 admin
                "TELEOPS_REPLICA_ID": rid,            # 显式副本标识
                "TELEOPS_LLM_CONFIG_FILE": mock_cfg_path,  # 强制 mock
            })
            p = subprocess.Popen(
                [PY, "-m", "uvicorn", APP, "--host", "127.0.0.1", "--port", str(port),
                 "--log-level", "warning"],
                env=env, cwd=REPO,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            uvicorns.append((rid, port, p))
            print(f"[info] 启动 {rid} @ :{port} (pid {p.pid})")

        # 3) 等两个副本健康
        for rid, port, p in uvicorns:
            ok = wait_health(port)
            results.append(log(f"副本 {rid} 健康 (: {port})", ok))
            if not ok:
                raise RuntimeError(f"{rid} 未就绪")

        # 4) 断言两副本 replica_id 不同
        ids = {}
        for rid, port, p in uvicorns:
            st, d = api(port, "/health")
            ids[port] = d.get("replica_id")
            print(f"       :{port} -> replica_id={d.get('replica_id')} version={d.get('version')}")
        distinct = len(set(ids.values())) == len(ids)
        results.append(log("两副本 replica_id 互异", distinct, str(ids)))

        # 5) 核心证明：副本 A 启动流 → 副本 B 读状态可见
        a_port = uvicorns[0][1]
        b_port = uvicorns[1][1]
        st, d = api(a_port, f"/stream/start?workspace_id={WS}", method="POST",
                    token=TOKEN, body={"workspace_id": WS, "profile": "mixed"})
        started = st == 200 and d.get("status") == "running"
        results.append(log(f"副本 A 启动告警流 ({WS})", started, f"status={d.get('status')}"))

        time.sleep(1.0)  # 让 A 把 running 状态刷入 Redis
        st2, d2 = api(b_port, f"/stream/status?workspace_id={WS}")
        b_sees = st2 == 200 and d2.get("running") is True
        results.append(log("副本 B 跨进程可见流状态(running)", b_sees,
                           f"running={d2.get('running')} rounds={d2.get('rounds')}"))

        # 6) 反向：副本 B 停止该流（写操作跨副本生效）
        st3, d3 = api(b_port, f"/stream/stop?workspace_id={WS}", method="POST", token=TOKEN)
        stopped = st3 == 200
        results.append(log("副本 B 跨进程停止流", stopped, f"status={d3.get('status')}"))

        # 7) 收尾：副本 A 确认已停止
        st4, d4 = api(a_port, f"/stream/status?workspace_id={WS}")
        a_stopped = st4 == 200 and d4.get("running") is False
        results.append(log("副本 A 确认流已停止", a_stopped, f"running={d4.get('running')}"))

    except Exception as e:
        print(f"[ERR] {type(e).__name__}: {e}")
        results.append(False)
    finally:
        for rid, port, p in uvicorns:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if redis_proc is not None:
            try:
                redis_proc.terminate()
                redis_proc.wait(timeout=10)
            except Exception:
                try:
                    redis_proc.kill()
                except Exception:
                    pass
        try:
            os.unlink(mock_cfg_path)
        except Exception:
            pass

    npass = sum(1 for r in results if r)
    print(f"\n===== 多副本实战验证：{npass}/{len(results)} 通过 =====")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
