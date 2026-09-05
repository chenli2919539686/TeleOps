"""轻量告警生成器 —— 给 TeleOps 喂「真实可达的告警」，验证集成适配器全链路。

为什么需要它：
  TeleOps 的告警类适配器（alert-prometheus / alert-zabbix ...）只负责把外部告警
  翻译成内核统一 Alert；但告警本身得由某个「被监控系统」产生。如果你手头没有
  Zabbix / Prometheus / ELK，本脚本就是那个「最小可用告警源」：

    - 按 Alertmanager 标准 webhook 格式，定时 POST 到
        POST /adapters/alert/ingest?adapter_id=alert-prometheus
    - 走的是**真实端点 + 真实 HTTP**，服务端会真的跑
        parse_webhook -> 统一 Alert -> 运维 Agent 根因分析 -> 生成工具
      所以整条闭环是真实的，只有「告警内容」是合成的。

用法：
  # 一次性发 3 条不同告警（默认）
  python scripts/alert_simulator.py --count 3

  # 每 20 秒自动发一批，持续运行（Ctrl+C 停止）
  python scripts/alert_simulator.py --loop --interval 20

  # 只打印将要发送的报文，不真正发出（调试用）
  python scripts/alert_simulator.py --dry-run

  # 指向别的地址 / 别的适配器
  python scripts/alert_simulator.py --base http://127.0.0.1:8000 --adapter alert-zabbix

鉴权（写接口需要登录用户，二选一）：
  # 方式一：界面账号登录换 JWT
  python scripts/alert_simulator.py --user admin --password ******
  # 方式二：服务端设置了 TELEOPS_API_TOKEN 时直接携带（Alertmanager 同款模式）
  python scripts/alert_simulator.py --token <TELEOPS_API_TOKEN>
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None


BASE_URL = "http://127.0.0.1:8000"
ENDPOINT = "/adapters/alert/ingest"

HOSTS = [
    "web-01", "web-02", "db-02", "app-03", "cache-01", "gw-01", "oss-05", "k8s-node-7",
]
JOBS = ["web", "database", "cache", "gateway", "storage"]

# 合成告警模板：贴近真实运维场景，severity 用 Prometheus 约定（warning/critical/info）
TEMPLATES = [
    {
        "alertname": "HostHighCpuLoad",
        "severity": "critical",
        "summary": "CPU 使用率持续超过 90%",
        "description": "主机 {host} 近 5 分钟 CPU 使用率均值 94%，疑似计算密集型进程或死循环。",
        "value": "94%",
    },
    {
        "alertname": "DiskWillFillIn4Hours",
        "severity": "warning",
        "summary": "磁盘分区预计 4 小时内写满",
        "description": "主机 {host} 的 /var 分区剩余空间低于 8%，按当前增速约 4 小时写满。",
        "value": "剩余 7.6%",
    },
    {
        "alertname": "HostMemoryUnderPressure",
        "severity": "warning",
        "summary": "内存压力升高，开始频繁 swap",
        "description": "主机 {host} 内存使用率 96%，swap 命中率上升，可能影响服务响应。",
        "value": "96% mem",
    },
    {
        "alertname": "TargetDown",
        "severity": "critical",
        "summary": "采集目标失联",
        "description": "主机 {host} 的 exporter 连续 2 分钟无响应，可能进程挂掉或网络中断。",
        "value": "up=0",
    },
    {
        "alertname": "NetworkPacketLoss",
        "severity": "warning",
        "summary": "网络丢包率异常",
        "description": "主机 {host} 到上游网关丢包率 3.2%，已影响部分请求成功率。",
        "value": "loss=3.2%",
    },
    {
        "alertname": "DbSlowQueries",
        "severity": "warning",
        "summary": "数据库出现慢查询堆积",
        "description": "主机 {host} 的慢查询数突增，平均耗时 2.3s，疑似缺失索引或锁等待。",
        "value": "2.3s avg",
    },
    {
        "alertname": "CertificateExpiringSoon",
        "severity": "info",
        "summary": "TLS 证书将在 7 天内过期",
        "description": "主机 {host} 的站点证书将于 7 天后到期，需提前轮换避免中断。",
        "value": "7d left",
    },
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def login(base: str, username: str, password: str) -> str:
    """用界面账号登录换 JWT（写接口需要登录用户）。"""
    if requests is None:
        raise RuntimeError("未安装 requests")
    r = requests.post(f"{base}/auth/login",
                      json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def build_alert(template: Dict[str, Any], host: str) -> Dict[str, Any]:
    """构造一条 Alertmanager 标准 webhook alert 条目。"""
    return {
        "labels": {
            "alertname": template["alertname"],
            "instance": f"{host}:9100",
            "job": random.choice(JOBS),
            "severity": template["severity"],
            "host": host,
        },
        "annotations": {
            "summary": template["summary"],
            "description": template["description"].format(host=host),
            "value": template["value"],
        },
        "startsAt": _now_iso(),
        "endsAt": "0001-01-01T00:00:00Z",
        "status": "firing",
    }


def build_webhook(n: int) -> Dict[str, Any]:
    """随机挑 n 条不同告警，组成一次 webhook 报文（可含多条）。"""
    chosen = random.sample(TEMPLATES, k=min(n, len(TEMPLATES)))
    alerts = [build_alert(t, random.choice(HOSTS)) for t in chosen]
    return {
        "receiver": "teleops",
        "status": "firing",
        "alerts": alerts,
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]},
        "commonLabels": {"source": "alert-simulator"},
        "externalURL": "http://alert-simulator.local",
    }


def send_once(base: str, adapter: str, webhook: Dict[str, Any], poll: bool,
              headers: Dict[str, str] | None = None) -> None:
    url = f"{base}{ENDPOINT}"
    if requests is None:
        print("  [错误] 未安装 requests，无法发送。请先 pip install requests。", file=sys.stderr)
        return
    headers = headers or {}
    try:
        r = requests.post(url, params={"adapter_id": adapter},
                          json={"adapter_id": adapter, "payload": webhook},
                          headers=headers, timeout=10)
        r.raise_for_status()
        resp = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  [发送失败] {e}")
        return

    names = [a["labels"]["alertname"] for a in webhook["alerts"]]
    print(f"  -> 已发送 {len(names)} 条告警：{', '.join(names)}  | job_id={resp.get('job_id')}")

    if not poll:
        return
    job_id = resp.get("job_id")
    if not job_id:
        return
    # 轮询 job 结果（最多 ~25s）
    for _ in range(25):
        try:
            jr = requests.get(f"{base}/jobs/{job_id}", timeout=5)
            if jr.status_code == 200:
                j = jr.json()
                if j.get("status") in ("done", "error"):
                    if j.get("status") == "error":
                        print(f"     [任务失败] {j.get('error')}")
                    else:
                        _print_job(j)
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    print("  -> job 仍在运行（根因分析耗时较长，可稍后在作战室查看）。")


def _print_job(j: Dict[str, Any]) -> None:
    res = j.get("result") or {}
    print(f"     [闭环结果] 接入 {res.get('ingested')} 条 | 适配器={res.get('adapter', {}).get('id')} | "
          f"运维 Agent={res.get('ops_agent_id')}")
    for item in (res.get("results") or [])[:3]:
        alert = item.get("alert") or {}
        name = alert.get("alert_id") or alert.get("metric") or "(未命名)"
        plan = item.get("plan") or {}
        concl = plan.get("conclusion") if isinstance(plan, dict) else None
        print(f"     - {name}{'（噪音已过滤）' if item.get('is_noise') else ''}:")
        if concl:
            print(f"       根因: {concl[:120]}{'...' if len(str(concl)) > 120 else ''}")
        tools = [t.get("tool") or t.get("name") for t in (item.get("tool_results") or [])]
        if tools:
            uniq = list(dict.fromkeys(t for t in tools if t))
            print(f"       已探测工具: {', '.join(uniq)}")
        if item.get("missing_tool"):
            print(f"       缺失工具(可触发工具生成): {item['missing_tool']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TeleOps 轻量告警生成器（合成告警，真实链路）")
    ap.add_argument("--base", default=BASE_URL, help="TeleOps 后端地址")
    ap.add_argument("--adapter", default="alert-prometheus", help="适配器 id")
    ap.add_argument("--count", type=int, default=3, help="每次发送的告警条数")
    ap.add_argument("--loop", action="store_true", help="持续循环发送")
    ap.add_argument("--interval", type=int, default=20, help="循环间隔秒数")
    ap.add_argument("--dry-run", action="store_true", help="只打印报文，不发送")
    ap.add_argument("--no-poll", action="store_true", help="发送后不轮询 job 结果")
    ap.add_argument("--token", default="", help="API Token（服务端设置了 TELEOPS_API_TOKEN 时用 X-API-Token 头）")
    ap.add_argument("--user", default="", help="界面登录用户名（自动 /auth/login 换 JWT）")
    ap.add_argument("--password", default="", help="界面登录密码")
    args = ap.parse_args()

    # 鉴权：写接口需要登录用户。两种方式二选一：
    #   1) --token  : 服务端设置了 TELEOPS_API_TOKEN 时直接携带（Alertmanager 也是这个模式）
    #   2) --user/--password : 普通界面账号登录换 JWT
    headers: Dict[str, str] = {}
    if args.token:
        headers["X-API-Token"] = args.token
    elif args.user and args.password:
        try:
            jwt = login(args.base, args.user, args.password)
            headers["Authorization"] = f"Bearer {jwt}"
            print(f"[鉴权] 已登录 {args.user}，携带 JWT")
        except Exception as e:  # noqa: BLE001
            print(f"[鉴权失败] {e}", file=sys.stderr)
            sys.exit(1)

    print(f"[告警生成器] 目标 {args.base}{ENDPOINT}?adapter_id={args.adapter}")
    print(f"            模式: {'DRY-RUN' if args.dry_run else ('LOOP 每'+str(args.interval)+'s' if args.loop else '单次')} | 每次 {args.count} 条\n")

    try:
        while True:
            wh = build_webhook(args.count)
            if args.dry_run:
                print(json.dumps(wh, ensure_ascii=False, indent=2))
            else:
                send_once(args.base, args.adapter, wh, poll=not args.no_poll, headers=headers)
            if not args.loop:
                break
            print(f"  ... 等待 {args.interval}s ...\n")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[已停止]")


if __name__ == "__main__":
    main()
