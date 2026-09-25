"""根因评估基准核心（P0-2 / 量化指标看板）。

没有根因标签的真实数据 → 自己造带标签的小样本：在 5G KPI 上「合成注入故障」，
每条标注 known root cause（true_root），再让运维 Agent 跑真实 rootcause，把它的
Top-1 假设与标签比对，算准确率；同时用健康/例行类样本算噪声抑制率（真正走
OpsAgent.normalize 的 rule_triage 降噪层，不再是机械计数）。

两条口径：
  - root_cause_top1_accuracy（主）：置信度最高假设的 cause 归约后是否命中 true_root
  - root_cause_top1_accuracy_position：位置首条假设（前端默认展示的 hypotheses[0]）
噪声抑制率：噪声样本中被 normalize 判为 is_noise 的比例（真实降噪层）。

predictor 两种：
  - "stub"  （默认，离线免费）：用确定性桩预测器，验证 taxonomy + matcher + 管线接线，
             明确标注 offline-stub（不代表 LLM 推理质量）。
  - "agent" （--live，耗 API）：调真实 OpsAgent.rootcause（经 LLMClient，有 DeepSeek Key 即真实推理）。
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from src.agents.ops_agent import OpsAgent
from src.llm_client import LLMClient
from src import config


# ---------------------------------------------------------------------------
# 1. 根因规范标签 + 归约器
#    自由文本的 cause 需归约到规范标签才能比对 true_root。
#    keywords 以「语义词」为主（覆盖/干扰/丢包/拥塞），避免裸指标名造成跨类误匹配。
# ---------------------------------------------------------------------------
ROOT_CAUSE_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "weak_coverage": {
        "label": "weak_coverage",
        "description": "小区弱覆盖/覆盖不足：RSRP/RSRQ/SINR 等无线链路质量整体劣化，用户处于信号边缘",
        "keywords": ["覆盖不足", "弱覆盖", "覆盖差", "信号弱", "边缘覆盖", "coverage"],
    },
    "interference": {
        "label": "interference",
        "description": "同频干扰/互调干扰：RSRQ/SINR/CQI 劣化但 RSRP 正常，存在外部干扰源",
        "keywords": ["同频干扰", "互调干扰", "干扰源", "质差", "干扰", "interference"],
    },
    "backhaul_packet_loss": {
        "label": "backhaul_packet_loss",
        "description": "传输/回传链路丢包：PING 丢包率与时延飙升，但无线侧 KPI（RSRP/RSRQ/SINR）正常",
        "keywords": ["回传丢包", "传输丢包", "骨干丢包", "packet loss", "ping 丢包", "丢包率", "丢包"],
    },
    "congestion": {
        "label": "congestion",
        "description": "小区拥塞/容量不足：上下行吞吐骤降，但无线链路质量（RSRP/RSRQ/SINR）正常，PRB 高负荷",
        "keywords": ["小区拥塞", "容量不足", "高负荷", "prb 高负荷", "吞吐骤降", "带宽受限", "拥塞", "congestion"],
    },
}


def match_label(text: str) -> str:
    """把自由文本根因归约到规范标签；取「最长命中关键词」的标签（最具体优先），无命中返回 unknown。"""
    if not text:
        return "unknown"
    low = str(text).lower()
    best_label = None
    best_len = 0
    for label, spec in ROOT_CAUSE_TAXONOMY.items():
        for kw in spec["keywords"]:
            kl = len(kw)
            if kl <= best_len:
                # 已有一个更长的命中，跳过更短的（除非同标签尚未记录）
                pass
            if kw.lower() in low:
                if kl > best_len or best_label is None:
                    best_len = kl
                    best_label = label
    return best_label or "unknown"


# ---------------------------------------------------------------------------
# 2. 5G 合成故障注入器
#    健康基线 + 四类根因退化模式（值落在 critical 区间，且彼此「特征可区分」），
#    每条产出一个 composite 统一 Alert（消息汇总多指标退化，贴近现网一 Incident 一工单）。
# ---------------------------------------------------------------------------
_HEALTHY_BASE = {
    "rsrp_dbm": -85.0, "rsrq_db": -8.0, "sinr_db": 12.0, "cqi": 14.0,
    "dl_throughput_mbps": 120.0, "ul_throughput_mbps": 30.0, "rssi_dbm": -70.0,
    "ping_loss_pct": 0.0,
}

# 每类故障：退化字段 + 退化值 + 主指标 + 严重度 + 汇总消息模板
_FAULT_PATTERNS: Dict[str, Dict[str, Any]] = {
    "weak_coverage": {
        "fields": {"rsrp_dbm": -118.0, "rsrq_db": -20.0, "sinr_db": -5.0, "cqi": 4.0},
        "primary": "rsrp_dbm", "severity": "critical",
        "summary": "RSRP/RSRQ/SINR 等无线链路质量整体劣化（RSRP={rsrp}, RSRQ={rsrq}, SINR={sinr}, CQI={cqi}），"
                   "下行吞吐与 Ping 正常 → 弱覆盖/覆盖不足",
    },
    "interference": {
        "fields": {"rsrq_db": -20.0, "sinr_db": -5.0, "cqi": 4.0},
        "primary": "rsrq_db", "severity": "major",
        "summary": "RSRQ/SINR/CQI 劣化但 RSRP 正常（RSRP={rsrp}, RSRQ={rsrq}, SINR={sinr}）→ 疑似同频干扰/外部干扰源",
    },
    "backhaul_packet_loss": {
        "fields": {"ping_loss_pct": 6.0},
        "primary": "ping_loss_pct", "severity": "critical",
        "summary": "Ping 丢包率与时延飙升（PINGLOSS={ping}%）但无线侧 KPI 全部正常 → 传输/回传链路丢包",
    },
    "congestion": {
        "fields": {"dl_throughput_mbps": 8.0, "ul_throughput_mbps": 1.5},
        "primary": "dl_throughput_mbps", "severity": "major",
        "summary": "上下行吞吐骤降（DL={dl}, UL={ul}）但无线链路质量正常 → 小区拥塞/容量不足",
    },
}

# 噪声样本：消息命中 triage_rules 的 INFO/RECOVERED 模式（巡检完成/已恢复/心跳正常/备份完成），
# 且 is_noise 不预标（靠规则层真实判定），用于验证降噪层而非预标。
_NOISE_MESSAGES = [
    "小区 Cell-N1 例行巡检完成，KPI 全部正常",
    "小区 Cell-N2 配置已自动恢复，指标已恢复正常",
    "小区 Cell-N3 心跳正常，无异常告警",
    "小区 Cell-N4 夜间备份完成",
    "小区 Cell-N5 单板自检 initialized，状态正常",
    "小区 Cell-N6 例行健康检查通过，指标平稳",
]


def _jit(base: float, spread: float, rng: random.Random) -> float:
    return round(base + rng.uniform(-spread, spread), 2)


def build_incidents(samples_per_fault: int = 6, noise_samples: int = 6,
                    seed: int = 20260925) -> List[Dict[str, Any]]:
    """构造带 true_root 标注的故障样本集（统一 Alert 形状，与现网同构）。"""
    rng = random.Random(seed)
    incidents: List[Dict[str, Any]] = []
    idx = 0
    for fault_type, pat in _FAULT_PATTERNS.items():
        for s in range(samples_per_fault):
            base = dict(_HEALTHY_BASE)
            for f, v in pat["fields"].items():
                spread = abs(v) * 0.04 if v != 0 else 0.2
                base[f] = _jit(v, spread, rng)
            # 汇总消息（用实际注入值，保留可读性）
            msg = pat["summary"].format(
                rsrp=base["rsrp_dbm"], rsrq=base["rsrq_db"], sinr=base["sinr_db"],
                cqi=base["cqi"], ping=base["ping_loss_pct"],
                dl=base["dl_throughput_mbps"], ul=base["ul_throughput_mbps"])
            cell = f"Cell-Bench-{fault_type[:3].upper()}-{s+1}"
            alert = _make_alert(
                alert_id=f"bench-{fault_type}-{s+1}", host=cell,
                metric=pat["primary"], value=base[pat["primary"]],
                severity=pat["severity"], message=msg,
                tags=["5g", "kpi", "bench", fault_type], is_noise=False)
            incidents.append({"id": alert["alert_id"], "true_root": fault_type,
                             "fault_type": fault_type, "is_noise": False, "alert": alert})
            idx += 1
    for n in range(noise_samples):
        msg = _NOISE_MESSAGES[n % len(_NOISE_MESSAGES)]
        cell = f"Cell-Noise-{n+1}"
        alert = _make_alert(
            alert_id=f"bench-noise-{n+1}", host=cell, metric="composite_kpi",
            value=0.0, severity="info", message=msg,
            tags=["5g", "kpi", "bench", "noise"], is_noise=False)
        incidents.append({"id": alert["alert_id"], "true_root": "(noise)",
                         "fault_type": "noise", "is_noise": True, "alert": alert})
    return incidents


def _make_alert(alert_id: str, host: str, metric: str, value: Any, severity: str,
               message: str, tags: List[str], is_noise: bool) -> Dict[str, Any]:
    """构造与 base.AlertAdapter.to_unified 完全同构的统一 Alert。"""
    return {
        "alert_id": alert_id, "ts": "2026-09-25T00:00:00Z", "source": "5g-kpi",
        "metric": metric, "host": host, "severity": severity, "value": value,
        "message": message, "tags": tags, "is_noise": bool(is_noise),
    }


# ---------------------------------------------------------------------------
# 3. 预测器
# ---------------------------------------------------------------------------
class _StubCMDB:
    def node_info(self, host): return {}
    def dependencies(self, host): return []
    def dependents(self, host): return []


class _StubKB:
    def retrieve(self, q, top_k=2): return []


class _StubTools:
    def list_tools(self): return []
    def requires_approval(self, name): return False
    def call(self, name, args): return {}
    def find_similar_tool(self, name, action): return None


def _predict_stub(incident: Dict[str, Any], error_rate: float,
                  rng: random.Random) -> Tuple[str, str]:
    """确定性桩：返回 true_root 对应描述的 cause，经 matcher 归约应与 true_root 一致；
    error_rate>0 时按概率注入一次错误标签，模拟不完美推理。"""
    true_root = incident["true_root"]
    label = true_root
    if error_rate > 0 and rng.random() < error_rate and true_root in ROOT_CAUSE_TAXONOMY:
        others = [l for l in ROOT_CAUSE_TAXONOMY if l != true_root]
        if others:
            label = rng.choice(others)
    cause_text = ROOT_CAUSE_TAXONOMY[label]["description"]
    predicted = match_label(cause_text)
    return predicted, predicted


def _predict_agent(agent: OpsAgent, incident: Dict[str, Any]) -> Tuple[str, str]:
    """真实 Agent：handle_alert → diagnosis.hypotheses，取位置首条与置信度最高条分别归约。"""
    out = agent.handle_alert(incident["alert"])
    hyps = (out.get("diagnosis") or {}).get("hypotheses") or []
    if not hyps:
        return "unknown", "unknown"
    pos = match_label(hyps[0].get("cause", ""))
    best = max(hyps, key=lambda h: float(h.get("confidence", 0) or 0))
    conf = match_label(best.get("cause", ""))
    return pos, conf


# ---------------------------------------------------------------------------
# 4. 基准主入口
# ---------------------------------------------------------------------------
def run_benchmark(predictor: str = "stub", error_rate: float = 0.0,
                  samples_per_fault: int = 6, noise_samples: int = 6,
                  seed: int = 20260925) -> Dict[str, Any]:
    """跑基准，返回指标字典。

    predictor="stub"  → 离线确定性，验证 taxonomy+matcher+管线（verify_mode 诚实标注）。
    predictor="agent" → 真实 OpsAgent.rootcause（需 DEEPSEEK_API_KEY，--live）。
    """
    # 离线确定性降噪：规则层即可判定，避免每条噪声都烧 LLM；也不污染准确率口径。
    # 进入时保存、退出时还原，避免把全局 config.LLM_TRIAGE 改成 False 泄漏到其它测试。
    _prev_llm_triage = config.LLM_TRIAGE
    config.LLM_TRIAGE = False
    rng = random.Random(seed)
    llm = LLMClient()
    agent = OpsAgent(_StubCMDB(), _StubKB(), _StubTools(), llm)

    incidents = build_incidents(samples_per_fault=samples_per_fault,
                                noise_samples=noise_samples, seed=seed)
    actionable = [i for i in incidents if not i["is_noise"]]
    noise = [i for i in incidents if i["is_noise"]]

    top1_pos_hits = 0
    top1_conf_hits = 0
    rootcause_details: List[Dict[str, Any]] = []

    for inc in actionable:
        if predictor == "agent":
            pos, conf = _predict_agent(agent, inc)
        else:
            pos, conf = _predict_stub(inc, error_rate, rng)
        hit_pos = (pos == inc["true_root"])
        hit_conf = (conf == inc["true_root"])
        top1_pos_hits += int(hit_pos)
        top1_conf_hits += int(hit_conf)
        rootcause_details.append({
            "id": inc["id"], "true_root": inc["true_root"], "predicted_position": pos,
            "predicted_confidence": conf, "top1_position": hit_pos,
            "top1_confidence": hit_conf,
        })

    # 噪声抑制：真正走 OpsAgent.normalize 的 rule_triage 降噪层
    noise_suppressed = 0
    noise_details: List[Dict[str, Any]] = []
    for inc in noise:
        norm = agent.normalize(inc["alert"])
        suppressed = bool(norm.get("is_noise"))
        noise_suppressed += int(suppressed)
        noise_details.append({"id": inc["id"], "suppressed": suppressed,
                             "triage_by": norm.get("triage_by")})

    # agent 接线冒烟：确保真实 rootcause 返回结构化 hypotheses（两种模式都跑一次，Mock 也不烧额度）
    smoke_ok = False
    try:
        sample = actionable[0]["alert"]
        out = agent.handle_alert(sample)
        hyps = (out.get("diagnosis") or {}).get("hypotheses") or []
        smoke_ok = isinstance(hyps, list) and all(
            isinstance(h, dict) and "cause" in h and "confidence" in h for h in hyps)
    except Exception:
        smoke_ok = False

    # 还原全局开关，避免把 config.LLM_TRIAGE=False 泄漏到其它测试/调用方
    config.LLM_TRIAGE = _prev_llm_triage

    n_act = max(1, len(actionable))
    n_noise = max(1, len(noise))
    verify_mode = (
        "offline-stub · matcher+taxonomy+pipeline 验证（非 LLM 推理，不代表真实根因准确率）"
        if predictor != "agent" else
        "live-agent · 真实 OpsAgent.rootcause（经 DeepSeek LLM 推理）"
    )

    return {
        "generated_at": None,  # 由调用方填
        "predictor": predictor,
        "verify_mode": {"diagnosis": verify_mode,
                        "remediation": "simulated · 修复动作作用于仿真靶机验证（见 eval_closed_loop）"},
        "root_cause_top1_accuracy": round(top1_conf_hits / n_act, 3),
        "root_cause_top1_accuracy_position": round(top1_pos_hits / n_act, 3),
        "noise_suppression_rate": round(noise_suppressed / n_noise, 3),
        "agent_smoke_ok": smoke_ok,
        "samples": {"actionable": len(actionable), "noise": len(noise),
                    "fault_types": sorted(_FAULT_PATTERNS.keys())},
        "rootcause_details": rootcause_details,
        "noise_details": noise_details,
    }
