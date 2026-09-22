"""仿真靶机（数字孪生 / digital twin）。

为什么需要它（对应「没有真实系统能否验证」的疑问）：
  Agent 的工作分两段——
    1) 诊断：读日志 -> 找根因。这一段可用「已知真实根因」的标注数据离线验证，
       不需要任何活系统（见 scripts/eval_closed_loop.py）。
    2) 修复：执行方案 -> 确认系统恢复。这一段必须作用在某个「系统」上。
       真实生产系统咱没有，于是造一个**会按物理直觉响应 Agent 动作的仿真靶机**：
       Agent 执行「清磁盘缓存」，靶机里 disk_used_pct 就真掉下来；
       掉到健康线以下，靶机回报「recovered=true」。

  => 修复成功率（remediation success）在本环境验证，文档中**明确标注为 simulated**，
     不冒充生产验证。这正是生产级 AIOps 的 shadow-mode / 仿真先行做法。

靶机只维护一组标量健康指标，apply(action) 改变指标并回报是否恢复。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 健康阈值（超过即视为异常）
HEALTHY_THRESHOLDS = {
    "disk_used_pct": 85.0,
    "cpu_pct": 90.0,
    "mem_pct": 90.0,
    "packet_loss_pct": 5.0,
    "cert_days_left": 7.0,   # 剩余天数 > 7 才健康
    "temp_c": 80.0,
}

# 各动作对指标的影响（确定性、可解释）
_ACTION_EFFECTS = {
    "clear_disk_cache":   {"disk_used_pct": -45.0},
    "restart_service":    {"cpu_pct": -60.0, "mem_pct": -55.0},
    "drain_traffic":      {"packet_loss_pct": -4.5, "cpu_pct": -20.0},
    "renew_cert":         {"cert_days_left": 358.0},
    "cool_down":          {"temp_c": -35.0},
    "adjust_fan":         {"temp_c": -30.0},
    "add_capacity":       {"mem_pct": -40.0, "cpu_pct": -15.0},
    "reboot_node":        {"disk_used_pct": -10.0, "cpu_pct": -70.0,
                           "mem_pct": -70.0, "temp_c": -25.0},
}


class SimTargetEnv:
    """一个可作用、可观测的仿真目标环境。"""

    def __init__(self, state: Optional[Dict[str, float]] = None, init_state: Optional[Dict[str, float]] = None):
        self.state: Dict[str, float] = dict(state or {
            "disk_used_pct": 40.0, "cpu_pct": 30.0, "mem_pct": 35.0,
            "packet_loss_pct": 0.2, "cert_days_left": 30.0, "temp_c": 45.0,
        })
        if init_state:
            self.state.update({k: float(v) for k, v in init_state.items()})

    def snapshot(self) -> Dict[str, float]:
        return dict(self.state)

    def is_healthy(self) -> bool:
        return (
            self.state["disk_used_pct"] < HEALTHY_THRESHOLDS["disk_used_pct"]
            and self.state["cpu_pct"] < HEALTHY_THRESHOLDS["cpu_pct"]
            and self.state["mem_pct"] < HEALTHY_THRESHOLDS["mem_pct"]
            and self.state["packet_loss_pct"] < HEALTHY_THRESHOLDS["packet_loss_pct"]
            and self.state["cert_days_left"] > HEALTHY_THRESHOLDS["cert_days_left"]
            and self.state["temp_c"] < HEALTHY_THRESHOLDS["temp_c"]
        )

    def apply(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行一个修复动作，返回作用结果。

        returned: {applied, action, recovered, note, before, after}
        """
        if action not in _ACTION_EFFECTS:
            return {"applied": False, "action": action, "recovered": False,
                    "note": f"未知动作 {action}，靶机未变化",
                    "before": self.snapshot(), "after": self.snapshot()}
        before = self.snapshot()
        for k, delta in _ACTION_EFFECTS[action].items():
            self.state[k] = max(0.0, round(self.state[k] + delta, 2))
        after = self.snapshot()
        recovered = self.is_healthy()
        return {"applied": True, "action": action, "recovered": recovered,
                "note": "已恢复" if recovered else "仍异常，需进一步处置",
                "before": before, "after": after}

    def reset(self, state: Optional[Dict[str, float]] = None):
        self.state = dict(state or {
            "disk_used_pct": 40.0, "cpu_pct": 30.0, "mem_pct": 35.0,
            "packet_loss_pct": 0.2, "cert_days_left": 30.0, "temp_c": 45.0,
        })
