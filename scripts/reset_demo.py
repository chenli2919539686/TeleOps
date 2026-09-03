"""重置 TeleOps 演示数据：移除研发 Agent 之前自动生成的工具，并清空消息栏需求看板，
让"运行完整闭环 / 消息栏派发"能重新触发"缺工具 → 研发造工具 → 复用"全过程。

- 工具库只保留基线 2 个（ping_host / restart_service），其余自动生成的一律移除
- 删除 tools/ 下自动生成脚本（删除被沙箱拦截时跳过，不影响演示）
- 清空 data/requirements.json 消息栏
重新跑闭环/派发会自动再次生成，可反复演示。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
TOOLS_JSON = ROOT / "data" / "tools.json"
REQ_JSON = ROOT / "data" / "requirements.json"

# 基线工具（由 scripts/gen_data.py 生成，非自动生成）
BASELINE = {"ping_host", "restart_service"}
# 历史上研发 Agent 可能自动生成的工具脚本名
AUTO_PY = ["optical_power_probe", "temperature_probe", "generic_probe"]

removed = []
for name in AUTO_PY:
    f = TOOLS_DIR / f"{name}.py"
    if f.exists():
        try:
            f.unlink()
            removed.append(f"tools/{name}.py")
        except OSError as e:
            removed.append(f"tools/{name}.py (残留，已忽略: {e})")

if TOOLS_JSON.exists():
    data = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))
    before = len(data.get("tools", []))
    data["tools"] = [t for t in data.get("tools", []) if t.get("name") in BASELINE]
    TOOLS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    removed.append(f"data/tools.json: 移除 {before - len(data['tools'])} 条自动生成工具")

if REQ_JSON.exists():
    REQ_JSON.write_text(json.dumps({"requirements": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    removed.append("data/requirements.json: 清空消息栏需求看板")

if not removed:
    print("✅ 已经是干净状态，无需重置。")
else:
    print("✅ 已重置演示数据：")
    for r in removed:
        print("  -", r)
    print("\n现在运行后端后：『闭环看板』点『运行完整闭环』，或『消息栏』发起需求即可看到研发造工具全过程。")
