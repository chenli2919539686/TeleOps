"""LLM 端点级熔断器（故障域隔离）。

问题背景（为什么需要它）
------------------------
LLM 端点（DeepSeek 公有云 / 本地私有化推理）不可达或持续报错时，原有逻辑是
「调用失败 → 回退 Mock」，但**下一条告警仍会再去打一次已死的端点**。后果：

1. 每条告警都被 ``LLM_TIMEOUT``（默认 30s）拖住，告警流水线看起来像"卡死"，
   且 /stream/stop 要等 join 超时才返回；
2. 无谓消耗连接、并发许可与（可能的）请求额度；
3. 端点恢复后没有探测机制，只能靠"再试一次才知道"。

熔断三态（与业界通用语义一致）
------------------------------
- **closed（闭合）**：正常调用；记录连续失败次数。
- **open（打开）**：连续失败达阈值 → 打开。窗口期内**跳过真实调用、直接降级
  Mock**（fail-fast），不再打已死端点，流水线立刻恢复流畅。
- **half_open（半开）**：窗口期满后放行**一次探测调用**；成功→闭合，失败→重新打开。

配置（环境变量，默认全开且保守）
--------------------------------
- ``TELEOPS_LLM_CB_ENABLED``：总开关，默认 ``1``（启用）。
- ``TELEOPS_LLM_CB_THRESHOLD``：连续失败多少次后打开，默认 ``5``。
- ``TELEOPS_LLM_CB_RESET``：打开后多久进入半开探测（秒），默认 ``60``。

线程安全
--------
LLM 调用发生在告警流水线线程 / Agent 并发信号量下，状态用 ``threading.Lock``
保护，避免多线程并发把计数打乱。
"""
import os
import time
import threading

# 连续失败阈值与恢复窗口（每次读取 env，便于单测与运行时调整）
def _threshold() -> int:
    return int(os.environ.get("TELEOPS_LLM_CB_THRESHOLD", "5") or 5)


def _reset_seconds() -> float:
    return float(os.environ.get("TELEOPS_LLM_CB_RESET", "60") or 60)


def _enabled() -> bool:
    return os.environ.get("TELEOPS_LLM_CB_ENABLED", "1").strip().lower() in (
        "1", "on", "true", "yes")


CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    """LLM 端点熔断器（closed → open → half_open → closed）。"""

    def __init__(self, name: str = "llm", threshold: int = None,
                 reset_seconds: float = None, enabled: bool = None):
        self.name = name
        self._threshold = threshold if threshold is not None else _threshold()
        self._reset = reset_seconds if reset_seconds is not None else _reset_seconds()
        self._enabled = enabled if enabled is not None else _enabled()
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_inflight = False  # 半开探测是否已放行且未回收

    # ---------- 状态查询 ----------
    @property
    def state(self) -> str:
        """当前状态；若处于 open 且已过恢复窗口，惰性迁移到 half_open。"""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._state == OPEN and self._reset <= 0:
            return OPEN
        if self._state == OPEN and (time.time() - self._opened_at) >= self._reset:
            self._state = HALF_OPEN
            self._probe_inflight = False
        return self._state

    def allow(self) -> bool:
        """是否允许发起真实调用。

        - 熔断器禁用 → 恒 True（行为与改造前完全一致）。
        - closed → True。
        - open 且在窗口内 → False（fail-fast，应直接走 Mock）。
        - open 且已过窗口 → 迁移 half_open 并放行一次探测。
        - half_open → 探测已在途则返回 False，避免并发下放多个探测。
        """
        if not self._enabled:
            return True
        with self._lock:
            st = self._state_locked()
            if st == CLOSED:
                return True
            if st == OPEN:
                return False
            # half_open：只放行一个探测
            if self._probe_inflight:
                return False
            self._probe_inflight = True
            return True

    # ---------- 结果上报 ----------
    def record_success(self):
        """真实调用成功：闭合熔断并清零计数。"""
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._probe_inflight = False
            self._state = CLOSED

    def record_failure(self):
        """真实调用失败：累计连续失败，达阈值即打开；半开探测失败则重新打开。

        熔断关闭（``TELEOPS_LLM_CB_ENABLED=0``）时**完全惰性**：既不计数也不跳闸，
        状态恒为 closed，保证与改造前行为逐字节一致。
        """
        if not self._enabled:
            return
        with self._lock:
            st = self._state_locked()
            self._probe_inflight = False
            if st == HALF_OPEN:
                # 探测失败 → 立刻回到打开，重新计时
                self._state = OPEN
                self._opened_at = time.time()
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = OPEN
                self._opened_at = time.time()

    def reset(self):
        """手动复位（运维/测试用）。"""
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._probe_inflight = False

    # ---------- 可观测 ----------
    def snapshot(self) -> dict:
        """给 /health、/metrics 用的只读快照。"""
        with self._lock:
            st = self._state_locked()
            remaining = 0.0
            if st == OPEN:
                remaining = max(0.0, self._reset - (time.time() - self._opened_at))
            return {
                "name": self.name,
                "enabled": self._enabled,
                "state": st,
                "tripped": st != CLOSED,
                "failures": self._failures,
                "threshold": self._threshold,
                "reset_seconds": self._reset,
                "recover_in_seconds": round(remaining, 1),
            }


# ---- 进程内单例（LLM 端点是全局共享依赖，熔断态也应全局共享）----
_BREAKER = None
_BREAKER_LOCK = threading.Lock()


def get_llm_breaker() -> CircuitBreaker:
    """获取全局 LLM 熔断器（首次调用时按 env 构造）。"""
    global _BREAKER
    if _BREAKER is None:
        with _BREAKER_LOCK:
            if _BREAKER is None:
                _BREAKER = CircuitBreaker(name="llm")
    return _BREAKER


def configure_breaker(breaker: CircuitBreaker = None, **kwargs):
    """替换/重置全局熔断器（测试隔离用，避免用例间互相污染状态）。"""
    global _BREAKER
    with _BREAKER_LOCK:
        if breaker is not None:
            _BREAKER = breaker
        else:
            _BREAKER = CircuitBreaker(name="llm", **kwargs)
    return _BREAKER
