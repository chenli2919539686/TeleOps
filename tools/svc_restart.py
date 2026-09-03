"""示例工具：服务重启（高危，需人工确认）。

工具脚本统一约定：定义 run(params: dict) -> dict。
W1 为桩实现，返回模拟结果；W2 接入真实执行（需沙箱 + 审计）。
"""


def run(params: dict) -> dict:
    service = params.get("service", "unknown")
    return {"status": "ok", "service": service, "action": "restarted",
            "note": "（模拟）真实执行需沙箱 + 审计"}
