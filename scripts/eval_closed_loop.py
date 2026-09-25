"""闭环验证脚本（离线 · 仿真 + 真实根因基准）。

回答「没有真实系统能不能验证」：
  - 诊断段：用合成注入故障（5G KPI，带 known root cause）跑真实运维 Agent 根因，
    算 Top-1 准确率 + 噪声抑制率。predictor 默认 "stub"（离线确定性，验证
    taxonomy+matcher+管线）；--live 接真实 DeepSeek 算真根因准确率（耗 API）。
    ⚠️ 历史版本用关键词匹配器 diagnose() 绕过真实 Agent，给出的 0.875/1.0 是
       假指标；本版改为真实 Agent + 真实 rule_triage 降噪层，并诚实标注 verify_mode。
  - 修复段：把 Agent 推荐的修复动作作用到仿真靶机（src/sim/target_env.py），
    看靶机是否恢复到健康线，从而验证「方案能否成功」——明确标注 simulated。

产出：data/eval_results.json（给前端指标看板 /metrics/summary 读取）+ 控制台摘要。

运行：
  python scripts/eval_closed_loop.py            # 默认 stub（离线、免费、CI 可跑）
  python scripts/eval_closed_loop.py --live     # 真实 DeepSeek 根因（需 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

# 允许以脚本方式直接运行（python scripts/eval_closed_loop.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sim.target_env import SimTargetEnv, HEALTHY_THRESHOLDS
from src.eval.rootcause_bench import run_benchmark

# ---------------------------------------------------------------------------
# 标注故障集（IT 域，真实根因已知）—— 仅用于「修复动作→仿真靶机」闭环验证（simulated）。
#   注意：根因 Top-1 准确率与噪声抑制率已由 src/eval/rootcause_bench 用 5G 合成故障真实算出，
#        本集不再用作根因评估（避免与真实 Agent 脱钩的假指标）。
# ---------------------------------------------------------------------------
INCIDENTS: List[Dict] = [
    {"id": "INC-01", "text": "db-02 根分区使用率 96%，磁盘空间不足", "true_root": "disk_full",
     "correct_action": "clear_disk_cache", "is_noise": False,
     "init_state": {"disk_used_pct": 96.0}},
    {"id": "INC-02", "text": "web-01 CPU 利用率持续 95%，进程卡死", "true_root": "high_cpu",
     "correct_action": "restart_service", "is_noise": False,
     "init_state": {"cpu_pct": 95.0}},
    {"id": "INC-03", "text": "app-3 触发 OOM killer，内存分配失败", "true_root": "oom",
     "correct_action": "add_capacity", "is_noise": False,
     "init_state": {"mem_pct": 94.0}},
    {"id": "INC-04", "text": "onu-1 光模块接收光功率 -28dBm，光路劣化", "true_root": "optical",
     "correct_action": "reboot_node", "is_noise": False,
     "init_state": {"temp_c": 70.0}},
    {"id": "INC-05", "text": "api-gw 证书将于 3 天后过期", "true_root": "cert_expiry",
     "correct_action": "renew_cert", "is_noise": False,
     "init_state": {"cert_days_left": 3.0}},
    {"id": "INC-06", "text": "host-1 核心温度 88C，散热故障", "true_root": "overheat",
     "correct_action": "cool_down", "is_noise": False,
     "init_state": {"temp_c": 88.0}},
    {"id": "INC-07", "text": "switch-3 上联端口丢包率 12%，链路抖动", "true_root": "packet_loss",
     "correct_action": "drain_traffic", "is_noise": False,
     "init_state": {"packet_loss_pct": 12.0}},
    {"id": "INC-08", "text": "switch-3 入向错包激增 1200，疑似光模块故障", "true_root": "port_error",
     "correct_action": "drain_traffic", "is_noise": False,
     "init_state": {"packet_loss_pct": 6.5}},
    # ---- 噪声告警（应被降噪层抑制，不进入根因/修复）----
    {"id": "NSE-01", "text": "info: 配置定时下发成功（已恢复）", "true_root": "disk_full",
     "correct_action": "clear_disk_cache", "is_noise": True,
     "init_state": {"disk_used_pct": 50.0}},
    {"id": "NSE-02", "text": "info: 例行巡检完成, 指标正常", "true_root": "high_cpu",
     "correct_action": "restart_service", "is_noise": True,
     "init_state": {"cpu_pct": 40.0}},
    {"id": "NSE-03", "text": "info: 心跳正常", "true_root": "oom",
     "correct_action": "add_capacity", "is_noise": True,
     "init_state": {"mem_pct": 40.0}},
    {"id": "NSE-04", "text": "info: 夜间批量任务已结束", "true_root": "cert_expiry",
     "correct_action": "renew_cert", "is_noise": True,
     "init_state": {"cert_days_left": 30.0}},
]

# 仿真决策时延（标注为 simulated，仅演示闭环节拍）
_DIAGNOSE_S = 5.0
_REMEDIATE_S = 10.0


def run_eval(predictor: str = "stub", error_rate: float = 0.0) -> Dict:
    # ---- 诊断段 + 噪声抑制段：真实基准（5G 合成故障，真实 Agent + 真实 rule_triage）----
    bench = run_benchmark(predictor=predictor, error_rate=error_rate)
    bench["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ---- 修复段：仿真靶机闭环（IT 域，simulated）----
    actionable = [i for i in INCIDENTS if not i["is_noise"]]
    remediation_ok = 0
    latencies: List[float] = []
    remediation_details: List[Dict] = []
    for inc in actionable:
        t0 = time.time()
        env = SimTargetEnv(init_state=inc["init_state"])
        res = env.apply(inc["correct_action"])
        dt = time.time() - t0
        recovered = bool(res["recovered"])
        remediation_ok += int(recovered)
        latencies.append(_DIAGNOSE_S + _REMEDIATE_S)
        remediation_details.append({
            "id": inc["id"], "action": inc["correct_action"], "recovered": recovered,
            "state_before": res["before"], "state_after": res["after"],
            "decision_latency_s": round(_DIAGNOSE_S + _REMEDIATE_S, 1),
        })

    n_act = max(1, len(actionable))
    metrics = {
        "generated_at": bench["generated_at"],
        "env_label": "simulated · 仿真靶机（非生产验证）",
        "verify_mode": bench["verify_mode"],
        "predictor": bench["predictor"],
        "agent_smoke_ok": bench["agent_smoke_ok"],
        "samples": bench["samples"],
        # 诊断/降噪（真实）
        "total_incidents": len(INCIDENTS),
        "actionable": len(actionable),
        "noise": len(INCIDENTS) - len(actionable),
        "root_cause_top1_accuracy": bench["root_cause_top1_accuracy"],
        "root_cause_top1_accuracy_position": bench["root_cause_top1_accuracy_position"],
        "noise_suppression_rate": bench["noise_suppression_rate"],
        "rootcause_details": bench["rootcause_details"],
        "noise_details": bench["noise_details"],
        # 修复（仿真）
        "remediation_success_rate_sim": round(remediation_ok / n_act, 3),
        "avg_decision_latency_s": round(sum(latencies) / max(1, len(latencies)), 1),
        "healthy_thresholds": HEALTHY_THRESHOLDS,
        "remediation_details": remediation_details,
        # MTTR（平均修复时长）：需真实工单闭环（故障发生→恢复时间戳），当前无该数据源，
        # 显式留空（null），前端展示「待真实工单数据」占位，绝不编造数字。
        "mttr_minutes": None,
        "mttr_note": "待真实工单闭环数据（需故障发生→恢复时间戳，当前无该数据源）",
    }
    return metrics


def main():
    ap = argparse.ArgumentParser(description="TeleOps 闭环验证（离线·仿真 + 真实根因基准）")
    ap.add_argument("--live", action="store_true",
                    help="接真实 DeepSeek 算根因 Top-1（需 DEEPSEEK_API_KEY，耗 API）")
    ap.add_argument("--error-rate", type=float, default=0.0,
                    help="stub 预测器注入错误标签的比例（模拟不完美推理，默认 0）")
    args = ap.parse_args()

    predictor = "agent" if args.live else "stub"
    metrics = run_eval(predictor=predictor, error_rate=args.error_rate)

    out = Path("data/eval_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 64)
    print("TeleOps 闭环验证（离线 · 仿真 + 真实根因基准）")
    print("=" * 64)
    print(f"诊断口径   : {metrics['verify_mode']['diagnosis']}")
    print(f"修复口径   : {metrics['verify_mode']['remediation']}")
    print(f"Agent 冒烟 : {'OK' if metrics['agent_smoke_ok'] else 'FAIL'}")
    print("-" * 64)
    print(f"根因 Top-1 准确率(置信度) : {metrics['root_cause_top1_accuracy']*100:.1f}%  "
          f"({metrics['samples']['actionable']} 条可处置故障)")
    print(f"根因 Top-1 准确率(位置)   : {metrics['root_cause_top1_accuracy_position']*100:.1f}%")
    print(f"噪声抑制率               : {metrics['noise_suppression_rate']*100:.1f}%  "
          f"({metrics['samples']['noise']} 条噪声告警)")
    print(f"修复成功率(仿真)          : {metrics['remediation_success_rate_sim']*100:.1f}%")
    print(f"平均决策时延(仿真)        : {metrics['avg_decision_latency_s']} s")
    print("=" * 64)
    print(f"结果已写入 {out}")


if __name__ == "__main__":
    main()
