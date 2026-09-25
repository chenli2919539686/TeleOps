"""根因评估基准测试（P0-2 / 量化指标看板）。

全部离线可跑：conftest 强制 DEEPSEEK_API_KEY="" → Mock LLM，不联网、不烧额度。
不依赖 DB / TestClient（与 test_adapters_real.py 同属纯单元测试）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval import rootcause_bench as bench
from src.eval.rootcause_bench import (
    ROOT_CAUSE_TAXONOMY, build_incidents, match_label, run_benchmark,
)


def test_match_label_taxonomy_self_consistent():
    """每个规范标签的描述文本经 matcher 应归约回自身（taxonomy 自洽）。"""
    for label, spec in ROOT_CAUSE_TAXONOMY.items():
        assert match_label(spec["description"]) == label, f"{label} 描述未归约回自身"


def test_match_label_realistic_phrases():
    """贴近真实 LLM 输出的中文短语应能归约到正确标签。"""
    cases = {
        "由于同频干扰导致 SINR 下降，RSRQ 也变差": "interference",
        "小区弱覆盖导致 RSRP 很低，用户处于信号边缘": "weak_coverage",
        "传输链路丢包严重，Ping 时延飙升": "backhaul_packet_loss",
        "PRB 高负荷，小区拥塞容量不足": "congestion",
    }
    for text, expected in cases.items():
        assert match_label(text) == expected, f"'{text}' 应归约到 {expected}"


def test_match_label_unknown():
    assert match_label("") == "unknown"
    assert match_label("服务器风扇转速正常") == "unknown"


def test_build_incidents_shape_and_labels():
    incidents = build_incidents(samples_per_fault=3, noise_samples=4, seed=1)
    actionable = [i for i in incidents if not i["is_noise"]]
    noise = [i for i in incidents if i["is_noise"]]
    assert len(actionable) == 3 * len(ROOT_CAUSE_TAXONOMY)
    assert len(noise) == 4

    for inc in actionable:
        a = inc["alert"]
        # 统一 Alert 形状与 base.to_unified 同构
        for key in ("alert_id", "ts", "source", "metric", "host", "severity",
                    "value", "message", "tags", "is_noise"):
            assert key in a, f"统一 Alert 缺字段 {key}"
        assert inc["true_root"] in ROOT_CAUSE_TAXONOMY
        assert a["is_noise"] is False

    # 噪声样本不预标 is_noise（靠规则层真实判定），否则测不出降噪层
    for inc in noise:
        assert inc["alert"]["is_noise"] is False
        assert inc["fault_type"] == "noise"


def test_run_benchmark_stub_all_correct():
    """默认 stub 模式：matcher+taxonomy 自洽 → Top-1=1.0，噪声抑制=1.0，Agent 冒烟通过。"""
    m = run_benchmark(predictor="stub", error_rate=0.0, samples_per_fault=4,
                      noise_samples=5, seed=7)
    assert m["predictor"] == "stub"
    assert m["root_cause_top1_accuracy"] == 1.0
    assert m["root_cause_top1_accuracy_position"] == 1.0
    assert m["noise_suppression_rate"] == 1.0
    assert m["agent_smoke_ok"] is True
    assert m["samples"]["actionable"] == 4 * len(ROOT_CAUSE_TAXONOMY)
    assert m["samples"]["noise"] == 5


def test_run_benchmark_stub_error_injection_lowers_accuracy():
    """error_rate 注入错误标签应降低 Top-1（验证指标对预测质量敏感）。"""
    clean = run_benchmark(predictor="stub", error_rate=0.0, samples_per_fault=10,
                          noise_samples=3, seed=3)
    noisy = run_benchmark(predictor="stub", error_rate=0.5, samples_per_fault=10,
                          noise_samples=3, seed=3)
    assert noisy["root_cause_top1_accuracy"] < clean["root_cause_top1_accuracy"]


def test_verify_mode_honest_label():
    m = run_benchmark(predictor="stub", seed=5)
    assert "offline-stub" in m["verify_mode"]["diagnosis"]
    assert "simulated" in m["verify_mode"]["remediation"]
