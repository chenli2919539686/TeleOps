# -*- coding: utf-8 -*-
"""LLM 用量统计与预算护栏。

覆盖两件事：
1. 用量能正确累计并估算费用（本地/Mock 供应商计 0 元）；
2. 日预算用尽后按策略处置：fallback 自动降级 Mock 不再烧钱，
   reject 直接拒绝调用，未配置预算时不限制。

conftest 已把 TELEOPS_USAGE_FILE / TELEOPS_LLM_CONFIG_FILE 重定向到临时目录，
所以这里可以安全地读写预算配置，不会污染开发者的真实配置与用量统计。
"""
import pytest

from src.config import load_llm_config, save_llm_config
from src.core import usage
from src.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _clean_usage():
    """每个用例前后清空用量与预算配置，避免相互干扰。"""
    usage.reset()
    yield
    usage.reset()


def _set_budget(daily_cny: float, action: str = "fallback"):
    cfg = load_llm_config()
    cfg["budget_daily_cny"] = daily_cny
    cfg["budget_action"] = action
    save_llm_config(cfg)


# ---------------- 用量统计 ----------------

def test_record_accumulates_tokens_and_calls():
    usage.record("deepseek", "deepseek-chat", prompt_tokens=1000,
                 completion_tokens=500, cached_tokens=200, task="TRIAGE")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=1000,
                 completion_tokens=500, cached_tokens=200, task="ROOTCAUSE")

    s = usage.summary()
    assert s["today"]["calls"] == 2
    assert s["today"]["prompt_tokens"] == 2000
    assert s["today"]["completion_tokens"] == 1000
    assert s["today"]["cached_tokens"] == 400
    assert s["today"]["cost_cny"] > 0
    # 按任务类型统计
    assert s["today"]["by_task"]["TRIAGE"] == 1
    assert s["today"]["by_task"]["ROOTCAUSE"] == 1


def test_estimate_cost_respects_cache_discount():
    """命中缓存的输入应按折扣价计费，费用显著低于全量未命中。"""
    full = usage.estimate_cost("deepseek", "deepseek-chat",
                               prompt_tokens=1_000_000, completion_tokens=0)
    cached = usage.estimate_cost("deepseek", "deepseek-chat",
                                 prompt_tokens=1_000_000, completion_tokens=0,
                                 cached_tokens=1_000_000)
    assert full == pytest.approx(2.0)      # 2 元/百万 input
    assert cached == pytest.approx(0.5)    # 0.5 元/百万 cached input
    assert cached < full


def test_local_provider_is_free():
    """本地 Ollama / Mock 属于自部署，不产生费用。"""
    assert usage.estimate_cost("local", "qwen2.5:7b", 1_000_000, 1_000_000) == 0.0
    usage.record("local", "qwen2.5:7b", prompt_tokens=999, completion_tokens=999)
    assert usage.summary()["today"]["cost_cny"] == 0.0


def test_summary_budget_percent():
    _set_budget(1.0, "fallback")
    # 花掉 0.5 元（50 万 output token × 8 元/百万 = 4 元…… 这里用 62500 token ≈ 0.5 元）
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0, completion_tokens=62_500)
    s = usage.summary()
    assert s["budget"]["daily_cny"] == 1.0
    assert s["budget"]["exceeded"] is False
    assert s["budget"]["percent"] == pytest.approx(50.0, abs=1.0)
    assert s["budget"]["remaining_cny"] == pytest.approx(0.5, abs=0.01)


# ---------------- 预算护栏 ----------------

def test_no_budget_means_unlimited():
    _set_budget(0, "fallback")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=10_000_000,
                 completion_tokens=10_000_000)  # 远超任何默认预算
    exceeded, action = usage.check_budget()
    assert exceeded is False


def test_fallback_strategy_downgrades_to_mock():
    """日预算用尽后，LLMClient 应自动降级为 Mock，不再产生真实调用与费用。"""
    _set_budget(0.01, "fallback")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0,
                 completion_tokens=100_000)  # 0.8 元，远超 0.01 元预算

    exceeded, action = usage.check_budget()
    assert exceeded is True
    assert action == "fallback"

    client = LLMClient()
    calls_before = usage.today()["calls"]
    out = client.complete("[TASK:TRIAGE]\n告警: host-1 cpu 99%\n请输出 JSON")
    # 降级后不再记录用量（Mock 不计费），且仍有可用输出
    assert usage.today()["calls"] == calls_before
    assert out


def test_reject_strategy_raises():
    _set_budget(0.01, "reject")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0, completion_tokens=100_000)

    client = LLMClient()
    with pytest.raises(RuntimeError, match="预算"):
        client.complete("[TASK:TRIAGE]\n告警: host-1 cpu 99%")


def test_warn_strategy_still_calls():
    """warn 只标记超限，不阻断调用（用于只想看数字的场景）。"""
    _set_budget(0.01, "warn")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0, completion_tokens=100_000)

    exceeded, action = usage.check_budget()
    assert exceeded is True
    assert action == "warn"

    client = LLMClient()
    # 测试环境本就是 Mock，此处只验证「没有被预算逻辑拦截」
    assert client.complete("[TASK:TRIAGE]\n测试告警")


def test_budget_fallback_recovers_when_limit_raised():
    """预算调高后应自动脱离熔断状态。

    早期实现把 mode 永久改成 mock，而 _ensure_client 只在 provider/api_key
    变化时才重建客户端——预算解除后会一直卡在 Mock，必须重启服务。这里锁定
    「预算恢复 → 自动恢复真实调用」的行为。
    """
    _set_budget(0.01, "fallback")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0, completion_tokens=100_000)

    client = LLMClient()
    # 测试环境本就是 Mock 模式（无 Key），走不到熔断分支，这里直接模拟已熔断状态
    client._budget_fallback_active = True
    client.complete("[TASK:TRIAGE]\n测试告警")
    assert client._budget_fallback_active is True, "仍超限，应保持熔断"

    _set_budget(100.0, "fallback")          # 调高预算
    client.complete("[TASK:TRIAGE]\n测试告警")
    assert client._budget_fallback_active is False, "预算解除后应自动恢复"


def test_budget_event_recorded_once():
    """首次触发熔断时记一条事件，便于前端提示；重复调用不应刷屏。"""
    _set_budget(0.01, "fallback")
    usage.record("deepseek", "deepseek-chat", prompt_tokens=0, completion_tokens=100_000)

    for _ in range(3):
        usage.check_budget()
    s = usage.summary()
    assert len([e for e in s["events"] if e["action"] == "fallback"]) == 1
