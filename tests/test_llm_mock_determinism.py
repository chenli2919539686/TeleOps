# -*- coding: utf-8 -*-
"""离线 Mock 推理的确定性回归：场景判断只认告警本体，不受知识库上下文干扰。

背景缺陷（GitHub Actions 首次实跑暴露）：
    _mock_rootcause 曾对**整个 prompt** 做关键词匹配，而 prompt 的「上下文」
    段落会塞入知识库检索命中。用「核心温度过热」检索时，Linux 上 top-2 命中了
    sop_optical_power_probe.md（Windows 上没命中——Path.glob() 返回顺序在
    两个平台上不同），prompt 里混进 "optical" 后抢先命中光模块分支：
    本地返回 temperature_probe、CI 却返回 optical_power_probe，
    导致 test_tool_reuse.py 的两个闭环用例在 CI 上失败、本地却全绿。

修复：场景关键词只在「告警」正文内匹配（LLMClient._alert_part_of），
     诊断结果不再随知识库加载顺序漂移，跨平台确定。
"""
import json

TEMP_ALERT_JSON = '{"metric": "temperature", "message": "核心温度过热", "host": "host-9"}'
OPTICAL_SOP_CTX = "\n知识库命中:\n  [sop_optical_power_probe] 使用 optical_power_probe 探测对端光功率"


def _mock_tool_for(prompt: str) -> str:
    """取离线 Mock 对给定 prompt 推荐的工具名。"""
    from src.llm_client import LLMClient
    return json.loads(LLMClient()._mock_rootcause(prompt))["hypotheses"][0]["recommended_tool"]


def test_temperature_alert_not_hijacked_by_optical_context():
    """上下文混入光模块 SOP 时，温度告警仍应诊断出温度探针。"""
    base = f"[TASK:ROOTCAUSE]\n告警: {TEMP_ALERT_JSON}\n上下文:\n告警主机: host-9"
    assert _mock_tool_for(base) == "temperature_probe"
    # 这一行正是 CI 上失败、本地通过的分水岭
    assert _mock_tool_for(base + OPTICAL_SOP_CTX) == "temperature_probe"


def test_optical_alert_still_routes_to_optical_tool():
    """真·光模块告警仍要走光功率探针（修复不能把场景判断一刀切打死）。"""
    p = ('[TASK:ROOTCAUSE]\n告警: {"metric": "optical_power", '
         '"message": "光模块发光功率异常偏低"}\n上下文:\n告警主机: onu-1')
    assert _mock_tool_for(p) == "optical_power_probe"


def test_alert_part_extraction():
    """_alert_part_of 能正确截出告警正文；无「上下文」段落时退回整个 prompt。"""
    from src.llm_client import LLMClient
    p = f"[TASK:ROOTCAUSE]\n告警: {TEMP_ALERT_JSON}\n上下文:\n告警主机: host-9" + OPTICAL_SOP_CTX
    assert LLMClient._alert_part_of(p) == TEMP_ALERT_JSON
    plain = "no-marker-prompt"
    assert LLMClient._alert_part_of(plain) == plain
