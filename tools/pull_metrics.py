"""Agent 诊断工具：拉取监控指标时序（Prometheus / Grafana）。

配置驱动 + demo 兜底：data/adapters.json 配了 metrics-prometheus / metrics-grafana
的 base_url 即直连真实 Prometheus；未配回退仿真时序（GrafanaAdapter._synthetic_series）。

这是「MCP 真连监控系统 → Agent 诊断工具化」的落点：运维 Agent 在根因推理阶段可主动
推荐本工具，并经由 tool_args 携带 PromQL 表达式，把实时指标拉回来佐证假设。

参数：
  query   : PromQL 表达式（如 "up"、"node_cpu_seconds_total"），默认 "up"
  hours   : 回看小时数（默认 1）
  adapter : 可选，指定适配器 id（"metrics-prometheus" | "metrics-grafana"），
            默认优先直连 Prometheus，其次 Grafana 数据源代理
返回：{status, mode, adapter, query, points, series:[{ts,value}]}
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.adapters.registry import AdapterRegistry


def _pick_adapter(adapter_id=None):
    reg = AdapterRegistry()
    if adapter_id:
        adp = reg.get(adapter_id)
        if adp is not None:
            return adp
    # 默认优先直连 Prometheus，其次 Grafana 数据源代理
    for aid in ("metrics-prometheus", "metrics-grafana"):
        adp = reg.get(aid)
        if adp is not None:
            return adp
    return None


def run(params: dict) -> dict:
    params = params or {}
    query = params.get("query") or "up"
    try:
        hours = int(params.get("hours", 1))
    except (TypeError, ValueError):
        hours = 1
    adp = _pick_adapter(params.get("adapter"))
    if adp is None:
        return {"status": "error", "reason": "未找到可用的指标监控适配器"}
    try:
        if not hasattr(adp, "query_metrics"):
            return {"status": "error", "reason": f"适配器 {adp.id} 不支持 query_metrics"}
        res = adp.query_metrics(query, hours=hours)
        series = res.get("series", []) or []
        return {
            "status": "ok",
            "mode": res.get("mode", "unknown"),
            "adapter": adp.id,
            "query": query,
            "points": len(series),
            "series": series[:200],
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"查询指标失败: {e}"}
