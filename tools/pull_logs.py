"""Agent 诊断工具：用 LogQL 拉取 Loki 日志（兼容查 ELK）。

配置驱动 + demo 兜底：data/adapters.json 配了 logs-loki 的 base_url 即直连真实 Loki；
未配回退仿真日志行。运维 Agent 在根因推理阶段可主动推荐本工具，并经由 tool_args
携带 LogQL 流选择器（如 '{level="error"}'），把近期日志拉回来定位故障。

参数：
  query   : LogQL 流选择器（如 '{job="nginx"}' 或 '{level="error"}'），默认 '{job=~".+"}'
  limit   : 返回条数上限（默认 50）
  adapter : 可选，指定适配器 id（"logs-loki" | "log-elk"），默认优先 Loki
返回：{status, mode, adapter, query, count, logs:[{ts,level,message,source}]}
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
    # 默认优先 Loki，其次 ELK
    for aid in ("logs-loki", "log-elk"):
        adp = reg.get(aid)
        if adp is not None:
            return adp
    return None


def _normalize(raw, adapter_id):
    """把不同日志适配器的原始切片归一化成统一形状。"""
    out = []
    for r in raw or []:
        ts = r.get("ts", "")
        level = r.get("level")
        if not level and isinstance(r.get("raw"), dict):
            level = (r.get("raw", {}).get("labels", {}) or {}).get("level")
        level = str(level or "info").lower()
        message = r.get("message") or r.get("value") or ""
        source = r.get("source") or adapter_id
        out.append({"ts": ts, "level": level, "message": message, "source": source})
    return out


def run(params: dict) -> dict:
    params = params or {}
    query = params.get("query") or '{job=~".+"}'
    try:
        limit = int(params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    adp = _pick_adapter(params.get("adapter"))
    if adp is None:
        return {"status": "error", "reason": "未找到可用的日志监控适配器"}
    try:
        if not hasattr(adp, "fetch_recent"):
            return {"status": "error", "reason": f"适配器 {adp.id} 不支持 fetch_recent"}
        raw = adp.fetch_recent(query, limit=limit) or []
        logs = _normalize(raw, adp.id)
        mode = "live" if getattr(adp, "base_url", "") else "demo"
        return {
            "status": "ok",
            "mode": mode,
            "adapter": adp.id,
            "query": query,
            "count": len(logs),
            "logs": logs[:limit],
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"查询日志失败: {e}"}
