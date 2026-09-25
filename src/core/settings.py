"""运行时设置（admin 可配置项）。

当前承载：人工审批闸 require_approval 的开关。

存储后端（v0.8.44，跨副本外部化）
--------------------------------
- ``LocalSettingsStore``（**默认**）：``data/settings.json`` 落盘，等价改造前。
- ``RedisSettingsStore``：单 key（``teleops:settings``），多副本共享同一份开关，
  配合 ``TELEOPS_STATE_STORE=redis`` 即「副本无状态」。

``get_require_approval`` / ``set_require_approval`` 委托给 ``get_settings_store()``
返回的后端；取值语义与改造前一致：**后端优先、env（TELEOPS_REQUIRE_APPROVAL）兜底**
（Redis 模式下后端即 Redis，无后端值则回退 env）。
"""
from __future__ import annotations

import json
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path

SETTINGS_FILE = Path("data/settings.json")
_lock = threading.Lock()
_ENV_KEY = "TELEOPS_REQUIRE_APPROVAL"
# 设置 key（与审批单前缀区分开）
SETTINGS_KEY = os.environ.get("TELEOPS_SETTINGS_KEY", "teleops:settings")


def _env_require_approval() -> bool:
    return os.environ.get(_ENV_KEY, "").strip() in ("1", "true", "True")


# ---------------------------------------------------------------------------
# 存储抽象
# ---------------------------------------------------------------------------
class SettingsStore(ABC):
    """运行时设置的存储接口（单个布尔开关：require_approval）。"""

    @abstractmethod
    def get(self) -> bool:
        ...

    @abstractmethod
    def set(self, enabled: bool) -> bool:
        ...


class LocalSettingsStore(SettingsStore):
    """data/settings.json 落盘（默认，等价改造前）。"""

    def get(self) -> bool:
        with _lock:
            if SETTINGS_FILE.exists():
                try:
                    d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                    if "require_approval" in d:
                        return bool(d["require_approval"])
                except Exception:
                    pass
        return _env_require_approval()

    def set(self, enabled: bool) -> bool:
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


class RedisSettingsStore(SettingsStore):
    """Redis 版设置（单 key），供多副本共享审批闸开关。

    安全姿态：fail-closed-lite。读时若 Redis 不可用，降级到 env 兜底（与本地默认一致），
    避免因旁路故障把「审批闸」静默关掉；写时依赖 Redis 可用（admin 配置操作，弱一致可接受）。
    """

    def __init__(self, redis_url: str = None, client=None, key: str = SETTINGS_KEY):
        self._key = key
        self._client = client
        if self._client is None:
            from .redis_factory import from_url  # 延迟导入
            self._client = from_url(
                redis_url or os.environ.get("TELEOPS_REDIS_URL",
                                            "redis://127.0.0.1:6379/0"),
                decode_responses=True)

    def get(self) -> bool:
        try:
            raw = self._client.get(self._key)
        except Exception:
            # 旁路故障：降级到 env（与本地默认一致），不静默禁用审批闸
            return _env_require_approval()
        if raw is None:
            return _env_require_approval()
        try:
            d = json.loads(raw)
            if "require_approval" in d:
                return bool(d["require_approval"])
        except Exception:
            pass
        return _env_require_approval()

    def set(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        self._client.set(self._key,
                         json.dumps({"require_approval": enabled}, ensure_ascii=False))
        return enabled


_settings_store = None
_settings_lock = threading.Lock()


def get_settings_store() -> SettingsStore:
    """按 ``TELEOPS_STATE_STORE`` 返回设置后端（进程内缓存单例）。"""
    global _settings_store
    if _settings_store is not None:
        return _settings_store
    with _settings_lock:
        if _settings_store is not None:
            return _settings_store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        _settings_store = (RedisSettingsStore()
                           if backend == "redis" else LocalSettingsStore())
        return _settings_store


def configure_settings_store(store: SettingsStore = None) -> None:
    """显式指定后端（测试用）；None 则按环境变量重建。"""
    global _settings_store
    with _settings_lock:
        _settings_store = store


# ---------------------------------------------------------------------------
# 模块级公开 API（委托给当前后端）
# ---------------------------------------------------------------------------
def get_require_approval() -> bool:
    """文件/Redis 优先，env 兜底。"""
    return get_settings_store().get()


def set_require_approval(enabled: bool) -> bool:
    """写入后端（data/settings.json 或 Redis），返回新值。"""
    return get_settings_store().set(enabled)
