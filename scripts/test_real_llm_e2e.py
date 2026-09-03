"""真实 DeepSeek LLM 端到端闭环验证。

用法：确保后端运行在 127.0.0.1:8000 且 .env 中 DEEPSEEK_API_KEY 有效。
"""
import json
import time
import requests

BASE = "http://127.0.0.1:8000"


def wait_job(job_id: str, timeout: int = 120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE}/jobs/{job_id}")
        d = r.json()
        print(f"  job {job_id[:8]}... status={d.get('status')}")
        if d.get("status") in ("done", "error", "not_found"):
            return d
        time.sleep(1)
    raise TimeoutError("job timeout")


def main():
    # 1. 注册
    user = {"username": f"realtest{int(time.time())}", "password": "RealTest123456"}
    r = requests.post(f"{BASE}/auth/register", json=user)
    print("register:", r.status_code, r.text[:120])
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 2. 创建业务域（自动模式）
    ws = {
        "name": f"真实LLM验证-{int(time.time())}",
        "adapter_id": "alert-prometheus",
        "mode": "auto",
    }
    r = requests.post(f"{BASE}/workspaces", json=ws, headers=headers)
    print("create ws:", r.status_code, r.text[:200])
    ws_id = r.json()["id"]

    # 3. 构造一个工具库中没有的告警：磁盘空间不足
    alert = {
        "alert_id": "A-DISK-FULL-001",
        "source": "zabbix",
        "metric": "disk_usage",
        "host": "host-1",
        "severity": "critical",
        "value": "98%",
        "message": "物理机 host-1 根分区磁盘使用率 98%，疑似日志膨胀或临时文件堆积",
        "tags": ["compute", "disk"],
        "is_noise": False,
    }

    # 4. 先诊断，看真实 LLM 的根因推理
    r = requests.post(
        f"{BASE}/agents/core-net-ops-main/diagnose",
        json={"alert": alert, "workspace_id": ws_id},
        headers=headers,
    )
    print("diagnose:", r.status_code, r.text[:300])
    diag_job = wait_job(r.json()["job_id"])
    diagnosis = diag_job.get("result", {})
    print("\n=== 真实 LLM 根因推理 ===")
    print(json.dumps(diagnosis.get("diagnosis", {}), ensure_ascii=False, indent=2))
    print("missing_tool:", diagnosis.get("missing_tool"))

    # 5. 登记缺口并走完整闭环（自动模式：研发造工具 → 派回运维执行）
    r = requests.post(
        f"{BASE}/requirements/raise",
        json={"alert": alert, "ops_agent_id": "core-net-ops-main", "workspace_id": ws_id},
        headers=headers,
    )
    print("\nraise:", r.status_code, r.text[:200])
    raise_job = wait_job(r.json()["job_id"])
    print("\n=== 闭环结果 ===")
    print(json.dumps(raise_job.get("result", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
