"""验证工具名归一化：同样光学问题不应重复造工具。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.tool_registry import ToolRegistry


def main():
    r = ToolRegistry()
    existing = r.list_tools()
    print("已有工具:", existing)

    # 模拟 LLM 对同一光功率问题推荐的不同名字（含未入库的新名字）
    aliases = [
        "display_transceiver_diagnosis",   # 精确命中
        "optical_module_diagnose",         # 精确命中
        "optical_transceiver_diagnostic",  # 精确命中
        "olt_onu_optical_probe",           # 精确命中
        "ssh_display_transceiver_diagnosis",  # 精确命中
        "transceiver_diagnosis_probe",     # 精确命中
        "transceiver_diagnosis",           # 未命中，应相似归一到 display_transceiver_diagnosis
        "optical_diagnose",                # 未命中，应相似归一到 optical_module_diagnose / optical_power_probe
        "display_optical_probe",           # 未命中，应归一
        "onu_optical_probe",               # 未命中，应归一到 olt_onu_optical_probe
    ]
    for a in aliases:
        mapped = r.find_similar_tool(a, "")
        print(f"  {a:40s} -> {mapped or '(无)'}")


if __name__ == "__main__":
    main()
