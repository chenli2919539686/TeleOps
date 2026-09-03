"""研发 Agent 自动生成：temperature_probe 探测工具（Mock）。"""
def run(params: dict):
    host = params.get("host", "unknown")
    return {"status": "ok", "tool": "temperature_probe", "host": host,
            "message": "已执行 temperature_probe 探测（Mock 返回）"}
