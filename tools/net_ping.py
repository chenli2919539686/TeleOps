"""示例工具：ping 探测（研发数字员工产出的小工具之一）。

工具脚本统一约定：定义 run(params: dict) -> dict。
W1 为桩实现，返回模拟结果；W2 接入真实网络探测。
"""


def run(params: dict) -> dict:
    host = params.get("host", "unknown")
    # W1 桩：返回模拟连通性结果
    return {"status": "ok", "host": host, "reachable": True, "latency_ms": 12}
