"""OSS 零代码切源 —— 端到端验证脚本（离线，不烧 API）。

验证目标：证明一份「华为风格 OSS 导出 CSV」经 column_aliases 配置，
无需改代码即可接入现有 5G 管线，产出规范统一 Alert，并验证噪声抑制与 Agent 接线。

分层：
  1) 列名映射：厂商列名（Time/eNodeB ID/LTE_RSRP/...）经 aliases 归约到规范列名
     → host 正确（非 unknown-cell）、ts 正确（ISO+Z）。
  2) 噪声抑制：min_severity=major 过滤掉 warning 级健康波动（真实数据噪声）。
  3) Agent 接线：取一条 critical 的统一 Alert 喂 OpsAgent.handle_alert（mock LLM），
     证明 OSS 形状的数据能走通根因分析调用链（真实 5G 根因需 --live + DeepSeek）。

用法：
  python scripts/validate_oss_sample.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

# 强制 mock LLM，离线不烧额度（必须在 import config/LLMClient 之前）
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("TELEOPS_DB_FILE", ":memory:")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.real_adapters import FiveGKpiAdapter  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src import config  # noqa: E402


class ForceMockLLM(LLMClient):
    """强制 mock 模式，离线验证 Agent 接线用（绕过 .env 里的真实 Key）。"""

    def _ensure_client(self):
        self._client = None
        self.mode = "mock"

SAMPLE = ROOT / "samples" / "oss_sample_huawei.csv"

# 厂商列名 → 规范列名（只配「内置别名表里没有」的列；SINR/CQI/Packet_Loss 已自动识别）
COLUMN_ALIASES = {
    "time": "Timestamp",
    "enodeb id": "CellID",
    "lte_rsrp": "RSRP",
    "lte_rsrq": "RSRQ",
    "dl_throughput": "DL_bitrate",
    "ul_throughput": "UL_bitrate",
}

CFG = {
    "dataset_path": str(SAMPLE),
    "min_severity": "major",
    "column_aliases": COLUMN_ALIASES,
}


def main() -> int:
    print("=" * 70)
    print("OSS 零代码切源 —— 端到端验证（华为风格样例 CSV）")
    print("=" * 70)
    print(f"样例文件: {SAMPLE}")
    print(f"列名映射: {COLUMN_ALIASES}")
    print()

    adapter = FiveGKpiAdapter(CFG)

    # ---- 层 1：列名映射 + 统一 Alert ----
    alerts_major = adapter.load_dataset_alerts(min_severity="major")
    alerts_info = adapter.load_dataset_alerts(min_severity="info")

    hosts = Counter(a["host"] for a in alerts_major)
    sev = Counter(a["severity"] for a in alerts_major)
    metrics = Counter(a["metric"] for a in alerts_major)
    ts_sample = sorted({a["ts"] for a in alerts_major})[:3]

    print("[层1] 列名映射 → 统一 Alert（min_severity=major）")
    print(f"  告警条数        : {len(alerts_major)}")
    print(f"  小区(host)分布  : {dict(hosts)}")
    print(f"  严重度分布      : {dict(sev)}")
    print(f"  指标(metric)分布: {dict(metrics)}")
    print(f"  时间戳样例      : {ts_sample}")
    unknown = [a for a in alerts_major if a["host"] == "unknown-cell"]
    print(f"  host=unknown-cell 的告警（应为 0，证明别名生效）: {len(unknown)}")
    print()

    # ---- 层 2：噪声抑制 ----
    print("[层2] 噪声抑制（warning 级健康波动被 major 下限过滤）")
    print(f"  info 下限告警数  : {len(alerts_info)}")
    print(f"  major 下限告警数 : {len(alerts_major)}")
    print(f"  被抑制的噪声条数 : {len(alerts_info) - len(alerts_major)}")
    print()

    # ---- 层 3：Agent 接线冒烟（强制 mock LLM，离线不烧额度）----
    print("[层3] Agent 接线冒烟（取一条 critical 统一 Alert 喂 OpsAgent.handle_alert）")
    llm = ForceMockLLM()
    from src.eval.rootcause_bench import _StubCMDB, _StubKB, _StubTools  # noqa: E402
    from src.agents.ops_agent import OpsAgent  # noqa: E402

    prev = config.LLM_TRIAGE
    config.LLM_TRIAGE = False  # 关闭 LLM triage，仅验证 rootcause 接线
    crit = next(a for a in alerts_major if a["severity"] == "critical")
    agent = OpsAgent(_StubCMDB(), _StubKB(), _StubTools(), llm)
    try:
        result = agent.handle_alert(crit)
        hyps = result.get("diagnosis", {}).get("hypotheses", []) if isinstance(result, dict) else []
        print(f"  输入告警: host={crit['host']} metric={crit['metric']} "
              f"value={crit['value']}{crit.get('unit','')} sev={crit['severity']}")
        print(f"  返回 hypotheses 条数: {len(hyps)}（mock 模式为通用假设，证明调用链通）")
        if hyps:
            h0 = hyps[0]
            print(f"  首条假设: cause={h0.get('cause')} conf={h0.get('confidence')}")
        print("  ✅ OSS 形状的 Alert 成功喂入 OpsAgent.handle_alert，未报错")
    except Exception as e:  # pragma: no cover
        print(f"  ❌ Agent 接线异常: {e}")
        return 1
    finally:
        config.LLM_TRIAGE = prev

    print()
    print("=" * 70)
    # ---- 断言：把“验证”落成硬校验 ----
    hosts_info = Counter(a["host"] for a in alerts_info)
    ok = (
        len(alerts_major) == 14          # 4 类退化的 major+ 告警总数
        and len(hosts) == 3              # major 下限下仅 101/102/103 出告警
        and len(hosts_info) == 5         # info 下限下噪声小区 104/105 也被正确识别
        and len(unknown) == 0            # 别名生效，无 unknown-cell
        and (len(alerts_info) - len(alerts_major)) == 4  # 噪声抑制 4 条 warning
        and all(a["ts"].endswith("Z") for a in alerts_major)  # 时间戳规整
    )
    if ok:
        print("✅ 全部断言通过：OSS 零代码切源脚手架端到端可用")
        return 0
    print("❌ 断言未通过，请检查列映射或样例数据")
    return 1


if __name__ == "__main__":
    sys.exit(main())
