# -*- coding: utf-8 -*-
"""告警降噪（一次规则判定 + 二次 LLM 语义判定）的回归测试。

背景缺陷：
    早期 OpsAgent.normalize 把 alert["is_noise"] 放在 or 表达式的第一项：

        is_noise = (alert.get("is_noise", False)
                    or alert.get("severity") == "info"
                    or any(p in msg for p in noise_patterns))

    而 data/alerts.json 在离线转换阶段（scripts/ingest_public.py）就已按
    `is_noise = (level == "INFO")` 预标好了。Python 的 or 一旦遇到真值就
    短路——52 条预标噪声在第一项即返回 True，后两个分支**从未被执行**。

    更糟的是那套 noise_patterns（备份/心跳/ping_ok）是为别的场景写的，
    对 BGL 超算日志完全不命中：把这批数据的预标字段全部剥掉后，
    关键词规则只能识别出 **0 条**。也就是说降噪代码写了却从未真正生效。

修复：
    1. 规则抽到 src/triage_rules.py，按优先级真正逐条判定，
       返回 True(噪声) / False(真故障) / None(无结论) 三态；
       预标字段降级为**兜底**而非短路前提。
    2. 规则无结论时才由 LLM 做二次语义判定（OpsAgent._llm_triage），
       可用 TELEOPS_LLM_TRIAGE=0 关闭。
"""
import json

from src import config
from src.triage_rules import rule_triage


def _agent(llm):
    """构造只用于降噪的 OpsAgent（normalize 不触碰 cmdb/kb/tools）。"""
    from src.agents.ops_agent import OpsAgent
    return OpsAgent(None, None, None, llm)


class _CountingLLM:
    """记录 complete() 调用，用于验证「规则有结论时不问 LLM」。"""

    def __init__(self, reply='{"is_noise": false, "reason": "stub"}'):
        self.calls = []
        self.reply = reply

    def complete(self, prompt, system=None, temperature=0.2):
        self.calls.append(prompt)
        return self.reply


# ---------- 一次判定：规则层 ----------

def test_rule_works_without_prelabeled_fields():
    """核心回归：剥掉 is_noise / severity 后，规则仍能独立判定。

    这是原缺陷的直接反例——短路实现下剥掉预标字段后规则会全部失效。
    """
    bare = {"message": "instruction cache parity error corrected"}
    assert rule_triage(bare) is True          # 靠 corrected 判出噪声

    bare_fail = {"message": "ciod: failed to read message prefix"}
    assert rule_triage(bare_fail) is False    # 靠 failed 判出真故障

    # 无关键词、无级别、无预标 -> 明确表示「判不出来」
    assert rule_triage({"message": "something entirely unrecognizable"}) is None


def test_recovered_beats_error_keyword():
    """已自动纠正必须优先于故障关键词。

    "parity error corrected" 同时含 error 与 corrected，若故障词先命中
    就会被误判成真故障——真实语义是硬件已自愈，属噪声。
    """
    assert rule_triage({"message": "instruction cache parity error corrected"}) is True
    assert rule_triage({"message": "ECC error corrected on DIMM 3"}) is True
    # 只有 error、没有恢复信号，才是真故障
    assert rule_triage({"message": "uncorrectable ECC error on DIMM 3"}) is False


def test_routine_patterns_are_noise():
    """例行备份 / 心跳类告警判为噪声。"""
    assert rule_triage({"message": "nightly backup completed"}) is True
    assert rule_triage({"message": "node heartbeat ok"}) is True
    assert rule_triage({"message": "备份完成"}) is True


def test_severity_used_when_text_has_no_signal():
    """文本无关键词时，级别兜底判定。"""
    assert rule_triage({"message": "x", "severity": "critical"}) is False
    assert rule_triage({"message": "x", "severity": "info"}) is True


def test_prelabeled_field_is_fallback_not_shortcut():
    """预标字段只作兜底：有明确文本信号时，文本优先于预标。"""
    # 文本说已恢复，即便预标 is_noise=False 也应按噪声处理
    assert rule_triage({"message": "parity error corrected", "is_noise": False}) is True
    # 文本无信号时才用预标
    assert rule_triage({"message": "x", "is_noise": True}) is True
    assert rule_triage({"message": "x", "is_noise": False}) is False


# ---------- 二次判定：LLM 层 ----------

def test_llm_not_called_when_rule_concludes():
    """规则有结论时不调用 LLM——省 token，也是分层的意义。"""
    llm = _CountingLLM()
    out = _agent(llm).normalize({"message": "parity error corrected"})
    assert out["is_noise"] is True
    assert llm.calls == [], "规则已判为噪声，不应再问 LLM"
    assert out["triage_by"] == "rule"


def test_llm_called_when_rule_inconclusive():
    """规则无结论时才交给 LLM。"""
    llm = _CountingLLM('{"is_noise": true, "reason": "例行自检输出"}')
    out = _agent(llm).normalize({"message": "totally opaque message"})
    assert len(llm.calls) == 1
    assert "[TASK:TRIAGE]" in llm.calls[0]
    assert out["is_noise"] is True
    assert out["triage_by"] == "llm"


def test_llm_disabled_falls_back_to_non_noise(monkeypatch):
    """关闭二次降噪时，规则无结论按「非噪声」处理——漏判比错杀安全。"""
    monkeypatch.setattr(config, "LLM_TRIAGE", False)
    llm = _CountingLLM()
    out = _agent(llm).normalize({"message": "totally opaque message"})
    assert llm.calls == [], "已关闭 LLM 降噪，不应发起调用"
    assert out["is_noise"] is False
    assert out["triage_by"] == "rule_inconclusive_no_llm"


def test_llm_parse_failure_is_conservative():
    """LLM 返回无法解析的内容时，保守判定为非噪声。"""
    out = _agent(_CountingLLM("not json at all")).normalize({"message": "opaque"})
    assert out["is_noise"] is False


# ---------- 真实数据集：结论不得漂移 ----------

def test_real_dataset_verdict_unchanged():
    """对 data/alerts.json 的判定结果应保持 52 噪声 / 3 真故障。

    改造不能改变既有 demo 行为，否则演示与文档口径会对不上。
    """
    with open(config.ALERTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    alerts = data if isinstance(data, list) else data.get("alerts", [])

    noise = sum(1 for a in alerts if rule_triage(a) is True)
    real = sum(1 for a in alerts if rule_triage(a) is False)
    assert len(alerts) == 55
    assert noise == 52
    assert real == 3


def test_mock_triage_matches_rule_layer():
    """离线 Mock 的 TRIAGE 结论与规则层一致，避免 Mock 与线上行为漂移。"""
    from src.llm_client import LLMClient

    alert = {"message": "parity error corrected", "metric": "bgl_kernel_info"}
    prompt = ("[TASK:TRIAGE]\n你是运维告警分级专家。\n"
              f"告警: {json.dumps(alert, ensure_ascii=False)}\n"
              '输出 JSON: {"is_noise": true 或 false}')
    data = json.loads(LLMClient()._mock_triage(prompt))
    assert data["is_noise"] is rule_triage(alert) is True
