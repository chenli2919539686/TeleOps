"""LLM 端点级熔断器测试（故障域隔离）。

覆盖三态迁移（closed → open → half_open → closed）与 **fail-fast 实效**：
熔断打开后 LLMClient 不再打已死端点（真实调用次数不再增长），直接降级 Mock。

每个用例都先 ``configure_breaker`` 重置全局单例，避免用例间互相污染状态。
"""
import time
from types import SimpleNamespace

from src.core import circuit_breaker as cb
from src.llm_client import LLMClient


def _breaker(threshold=3, reset_seconds=0.05, enabled=True):
    """重置全局熔断器为可预测配置（reset 很短，便于测半开探测）。"""
    return cb.configure_breaker(threshold=threshold,
                                reset_seconds=reset_seconds,
                                enabled=enabled)


# ---------------- 三态迁移 ----------------

def test_initial_state_is_closed_and_allows():
    b = _breaker()
    assert b.state == cb.CLOSED
    assert b.allow() is True
    assert b.snapshot()["tripped"] is False


def test_trips_open_after_threshold_consecutive_failures():
    b = _breaker(threshold=3)
    for _ in range(2):
        b.record_failure()
        assert b.state == cb.CLOSED, "未达阈值前不应打开"
    b.record_failure()
    assert b.state == cb.OPEN
    assert b.allow() is False, "打开后应拒绝真实调用（fail-fast）"
    assert b.snapshot()["tripped"] is True


def test_success_resets_failure_count_before_tripping():
    b = _breaker(threshold=3)
    b.record_failure()
    b.record_failure()
    b.record_success()          # 中途成功 → 连续失败清零
    b.record_failure()
    b.record_failure()
    assert b.state == cb.CLOSED, "成功清零后不应累计到阈值"
    b.record_failure()
    assert b.state == cb.OPEN


def test_half_open_releases_exactly_one_probe_after_window():
    b = _breaker(threshold=1, reset_seconds=0.05)
    b.record_failure()
    assert b.state == cb.OPEN
    assert b.allow() is False

    time.sleep(0.08)            # 越过恢复窗口
    assert b.state == cb.HALF_OPEN
    assert b.allow() is True, "半开应放行一次探测"
    assert b.allow() is False, "探测在途时不应重复放行"


def test_half_open_success_closes_circuit():
    b = _breaker(threshold=1, reset_seconds=0.05)
    b.record_failure()
    time.sleep(0.08)
    assert b.allow() is True
    b.record_success()
    assert b.state == cb.CLOSED
    assert b.snapshot()["failures"] == 0


def test_half_open_failure_reopens_circuit():
    b = _breaker(threshold=1, reset_seconds=0.05)
    b.record_failure()
    time.sleep(0.08)
    assert b.allow() is True
    b.record_failure()          # 探测失败 → 重新打开
    assert b.state == cb.OPEN


def test_disabled_breaker_always_allows():
    b = _breaker(threshold=1, enabled=False)
    for _ in range(10):
        b.record_failure()
    assert b.state == cb.CLOSED
    assert b.allow() is True, "关闭熔断时行为应与改造前完全一致"


def test_snapshot_exposes_ops_fields():
    b = _breaker(threshold=2, reset_seconds=60)
    s = b.snapshot()
    for k in ("name", "enabled", "state", "tripped",
              "failures", "threshold", "reset_seconds", "recover_in_seconds"):
        assert k in s
    assert s["threshold"] == 2 and s["state"] == cb.CLOSED


# ---------------- LLMClient 集成：fail-fast 是否真的生效 ----------------

class _Completions:
    """记录真实调用次数；fail=True 时每次都抛异常（模拟端点不可达）。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def create(self, model=None, messages=None, temperature=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("endpoint unreachable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=None,
        )


class _FakeLLMClient(LLMClient):
    """绕过 _ensure_client 的真实建连逻辑，直接注入假客户端。"""

    def __init__(self, fail=False):
        super().__init__()
        self._c = _Completions(fail=fail)
        self._fake = SimpleNamespace(chat=SimpleNamespace(completions=self._c))

    def _ensure_client(self):
        self.mode = "live"
        self._client = self._fake


def test_llmclient_stops_hitting_endpoint_once_open(monkeypatch):
    """核心实效：熔断打开后不再打端点，真实调用次数停止增长。"""
    b = _breaker(threshold=2, reset_seconds=60)

    def _no_budget(*a, **k):
        return (False, "fallback")

    monkeypatch.setattr("src.core.usage.check_budget", _no_budget)
    c = _FakeLLMClient(fail=True)

    c.complete("[TASK:ROOTCAUSE] 告警: ONU 光模块发光功率异常")
    c.complete("[TASK:ROOTCAUSE] 告警: ONU 光模块发光功率异常")
    assert b.state == cb.OPEN
    assert c._c.calls == 2, "达到阈值前每条都真实调用过一次"

    # 第三次起应 fail-fast，不再打端点
    out = c.complete("[TASK:ROOTCAUSE] 告警: ONU 光模块发光功率异常")
    assert c._c.calls == 2, "熔断打开后仍打了端点，fail-fast 未生效"
    assert c.mode == "mock"
    assert isinstance(out, str) and out


def test_llmclient_success_keeps_circuit_closed(monkeypatch):
    """端点正常时不应误熔断。"""
    b = _breaker(threshold=2, reset_seconds=60)
    monkeypatch.setattr("src.core.usage.check_budget", lambda *a, **k: (False, "fallback"))
    c = _FakeLLMClient(fail=False)
    for _ in range(5):
        c.complete("[TASK:ROOTCAUSE] 告警: 测试")
    assert c._c.calls == 5
    assert b.state == cb.CLOSED
    assert b.snapshot()["failures"] == 0
