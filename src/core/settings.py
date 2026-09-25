"""运行时设置（admin 可配置项）。

当前承载：人工审批闸 require_approval 的开关。
- 文件优先：data/settings.json 的 require_approval 字段（admin 运行时切换并持久化）。
- env 兜底：TELEOPS_REQUIRE_APPROVAL=1/true（部署期硬默认，无需重启）。
两者皆无 → 关闭（与现状一致，不破坏既有行为）。
落盘约定与 adapters.json / approvals.json 一致：进程级 + JSON 文件。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

SETTINGS_FILE = Path("data/settings.json")
_lock = threading.Lock()
_ENV_KEY = "TELEOPS_REQUIRE_APPROVAL"


def _env_require_approval() -> bool:
    return os.environ.get(_ENV_KEY, "").strip() in ("1", "true", "True")


def get_require_approval() -> bool:
    """文件优先，env 兜底。"""
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if "require_approval" in d:
                    return bool(d["require_approval"])
            except Exception:
                pass
    return _env_require_approval()


def set_require_approval(enabled: bool) -> bool:
    """写入 data/settings.json 的 require_approval 字段，返回新值。"""
    enabled = bool(enabled)
    with _lock:
        d: dict = {}
        if SETTINGS_FILE.exists():
            try:
                d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        d["require_approval"] = enabled
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return enabled
