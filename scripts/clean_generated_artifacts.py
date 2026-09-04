"""清理演示运行时生成的工具脚本与 SOP，只保留预设工具。

用于在修复工具名归一化后，把 working tree 恢复到干净基线，
避免把自动生成产物提交进仓库。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import db
from src.config import TOOLS_DIR, KB_DIR

# 预设工具的执行脚本，必须保留
PRESET_FILES = {"net_ping.py", "svc_restart.py"}


def clean_db():
    db.execute("DELETE FROM tools WHERE name NOT IN ('ping_host','restart_service')")
    db.execute("DELETE FROM requirements")
    print("DB: 已清理生成工具与需求看板")


def clean_files():
    for f in TOOLS_DIR.glob("*.py"):
        if f.name not in PRESET_FILES:
            f.unlink()
            print(f"  删工具脚本: {f.name}")
    for f in KB_DIR.glob("sop_*.md"):
        f.unlink()
        print(f"  删 SOP: {f.name}")


if __name__ == "__main__":
    clean_db()
    clean_files()
    print("done")
