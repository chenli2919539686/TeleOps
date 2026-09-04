"""告警降噪规则（一次判定层）。

单独成模块的理由：ops_agent 用它做真实判定，llm_client 的离线 Mock 也要用
同一套判据给出确定性结果。若两边各存一份词表，将来改一处漏一处就会出现
「Mock 与真实行为不一致」——这类不一致正是本项目此前踩过的坑（CI 与本地
结果漂移）。因此抽出来共用。

判定返回三态：
    True  -> 噪声，无需人工处理
    False -> 真故障，需要处置
    None  -> 规则无结论，交给上层（LLM）做语义判定
"""
import re

# 1) 已自动恢复类：优先级最高。
#    "instruction cache parity error corrected" 这类含 error 但已自愈，
#    必须优先于故障词命中，否则会被误判成真故障。
RECOVERED_PATTERNS = [
    "corrected", "recovered", "resolved", "restored", "cleared",
    "已恢复", "已纠正", "已解决", "恢复正常", "自动恢复",
]

# 2) 例行/信息类：备份、心跳、启动自检等，天然不需要人处理
INFO_PATTERNS = [
    "backup", "备份完成", "备份成功", "心跳正常", "ping_ok", "heartbeat",
    "startup", "initialized", "configuration", "启动完成", "巡检完成",
]

# 3) 严重故障类：明确的工作异常信号。
#    注意 "error" 必须收录——它是最常见的故障措辞（如 "uncorrectable ECC
#    error"）。放在 RECOVERED_PATTERNS 之后检查，故 "parity error corrected"
#    这类已自愈的仍会优先命中恢复类，不会被误杀。
FAILURE_PATTERNS = [
    "failed", "failure", "fatal", "panic", "error", "timeout", "timed out",
    "refused", "denied", "unavailable", "crash", "aborted", "corrupt",
    "lost", "exceeded", "out of memory", "oom",
    "失败", "中断", "宕机", "异常退出", "无法", "不可用", "超阈", "离线",
]

NOISE_SEVERITIES = ("info", "debug", "notice")
ALERT_SEVERITIES = ("critical", "fatal", "severe", "error")


def alert_text(alert: dict) -> str:
    """取告警的判定文本（消息 + 指标名），统一小写。"""
    return (str(alert.get("message", "")) + " " + str(alert.get("metric", ""))).lower()


def rule_triage(alert: dict):
    """规则层降噪：True(噪声) / False(真故障) / None(无结论)。

    设计要点——不短路：
    早期实现把 alert["is_noise"] 放在 or 的第一项，数据一旦预标，后续分支
    因 Python 短路永不求值，规则形同虚设。这里把预标字段降级为「兜底」，
    且每个分支都能独立得出结论，保证面对未预标的真实告警时规则依然有效。
    """
    text = alert_text(alert)

    if any(p in text for p in RECOVERED_PATTERNS):
        return True
    if any(p in text for p in INFO_PATTERNS):
        return True
    if any(p in text for p in FAILURE_PATTERNS):
        return False

    sev = str(alert.get("severity", "") or "").lower()
    if sev in ALERT_SEVERITIES:
        return False
    if sev in NOISE_SEVERITIES:
        return True

    # 兜底：调用方预标的噪声标记（数据自带答案时补位，而非唯一依据）
    if "is_noise" in alert:
        return bool(alert["is_noise"])
    return None


def alert_from_prompt(prompt: str) -> dict:
    """从 TRIAGE prompt 中取出告警 JSON，供 Mock 复用同一套规则。

    与 _alert_part_of 同理：只用告警本体参与判定，避免 prompt 里的
    指令文字（含"失败""异常"等词）污染判定结果。
    """
    m = re.search(r"告警:\s*(\{.*?\})\s*\n", prompt or "", re.S)
    if not m:
        return {}
    import json
    try:
        data = json.loads(m.group(1))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
