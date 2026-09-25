"""把真实 5G 数据集回放到 TeleOps 告警流（真实电信数据接入的端到端验证）。

流程：数据集 CSV -> fiveg_dataset loader 算出超阈 Alert payload（含 severity）
      -> POST /adapters/alert/ingest?adapter_id=alert-5g
      -> 服务端 parse_webhook 转统一 Alert -> 运维 Agent 根因分析。

前置：
  1) 已运行 python scripts/fetch_5g_dataset.py 取数到 data/5g_dataset
  2) TeleOps 后端已启动（默认 http://127.0.0.1:8000）
  3) data/adapters.json 含 "alert-5g": {"dataset_path": "data/5g_dataset"}（回放脚本会自己 loader，
     服务端也会按 adapter 配置再解析一次；两侧阈值一致）

用法：
  python scripts/replay_5g_dataset.py                       # 全量回放
  python scripts/replay_5g_dataset.py --limit 500 --batch 20
  python scripts/replay_5g_dataset.py --base-url http://192.168.1.10:8000
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None

from src.adapters import fiveg_dataset as _ds  # noqa: E402


def _post(base_url, adapter_id, payloads, workspace_id, ops_agent_id):
    url = f"{base_url.rstrip('/')}/adapters/alert/ingest"
    params = {"adapter_id": adapter_id}
    body = {"payload": payloads}
    if workspace_id:
        body["workspace_id"] = workspace_id
    if ops_agent_id:
        body["ops_agent_id"] = ops_agent_id
    if requests is None:
        import urllib.request
        import json
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    else:
        r = requests.post(url, params=params, json=body, timeout=30)
        r.raise_for_status()
        return r.json()


def main():
    ap = argparse.ArgumentParser(description="回放 5G 数据集到 TeleOps 告警流")
    ap.add_argument("--dataset-path", default=os.path.join(ROOT, "data", "5g_dataset"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--adapter-id", default="alert-5g")
    ap.add_argument("--limit", type=int, default=None, help="最多处理的 CSV 行数")
    ap.add_argument("--min-severity", default="major",
                   help="严重度下限（默认 major，过滤掉 warning 以降低噪声）")
    ap.add_argument("--batch", type=int, default=50, help="每批 POST 的告警条数")
    ap.add_argument("--workspace-id", default=None)
    ap.add_argument("--ops-agent-id", default=None)
    args = ap.parse_args()

    if not (os.path.isdir(args.dataset_path) or os.path.isfile(args.dataset_path)):
        print(f"[replay] 数据集不存在：{args.dataset_path}\n"
              f"         先运行：python scripts/fetch_5g_dataset.py", file=sys.stderr)
        return 1

    payloads = _ds.load_payloads(args.dataset_path, limit=args.limit,
                                  min_severity=args.min_severity)
    if not payloads:
        print("[replay] 数据集未解析出任何超阈告警（可能阈值偏松或全部健康样本）。")
        return 0
    print(f"[replay] 解析出 {len(payloads)} 条超阈告警，回放到 {args.base_url} ...")

    sent = 0
    for i in range(0, len(payloads), args.batch):
        batch = payloads[i:i + args.batch]
        try:
            res = _post(args.base_url, args.adapter_id, batch,
                        args.workspace_id, args.ops_agent_id)
            job = res.get("job_id", "?")
            print(f"[replay] 批次 {i // args.batch + 1}: 发送 {len(batch)} 条 -> job_id={job}")
            sent += len(batch)
        except Exception as e:  # noqa: BLE001
            print(f"[replay] 批次 {i // args.batch + 1} 失败：{e}\n"
                  f"         确认后端已启动且可达 {args.base_url}", file=sys.stderr)
            return 1
    print(f"[replay] 完成：共回放 {sent} 条真实 5G 告警。可在前端「运维 Agent」查看根因分析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
