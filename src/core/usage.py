"""LLM 用量统计与预算护栏。

问题背景：实时告警流是无人值守的后台线程，会持续循环调用 LLM
（降噪 → 根因 → 造工具 → 写 SOP）。一次长时间演示可能触发数百次调用，
没有护栏的话几小时就能烧掉整月预算——这是成本失控最典型的场景。

本模块做三件事：
  1. **计量**：按天累计调用次数 / input / output token / 估算费用，
     落盘 data/llm_usage.json（保留最近 30 天）；
  2. **计价**：按 provider + model 查定价表估算人民币成本，
     本地 Ollama 等自部署模型计 0；未知模型按保守默认值估算；
  3. **护栏**：达到日预算上限后按策略处置——
       warn     仅标记超限，继续调用（只想看数字时用）
       fallback 自动降级为 Mock，不再产生费用（默认，最安全）
       reject   直接拒绝调用并抛错（连 Mock 兜底都不要时用）

线程安全：告警流的 DevAgent 用线程池并发调用 LLM，故全程加锁。
"""
import json
import os
import threading
import time
from pathlib import Path

from src.config import DATA_DIR, load_llm_config

# 用量文件同样支持环境变量重定向：测试环境必须隔离，否则跑一次测试
# 就会把真实调用记进开发者的用量统计里（而且测试期间还会联网烧额度）。
USAGE_FILE = Path(os.environ.get("TELEOPS_USAGE_FILE")
                  or str(DATA_DIR / "llm_usage.json"))
_LOCK = threading.Lock()
_KEEP_DAYS = 30

# 定价表：¥ / 百万 token，格式为 (输入未命中缓存, 输入命中缓存, 输出)。
# 价格随厂商调整，这里只用于「估算量级」，精确账单以平台为准。
PRICING = {
    "deepseek.deepseek-chat": (2.0, 0.5, 8.0),
    "deepseek.deepseek-reasoner": (4.0, 1.0, 16.0),
    "deepseek.deepseek-v4-pro": (3.0, 0.025, 6.0),
    "deepseek.deepseek-v4-flash": (1.0, 0.1, 4.0),
    "openai.gpt-4o-mini": (1.08, 0.54, 4.32),
    "openai.gpt-4o": (27.0, 13.5, 108.0),
    "openai.gpt-4.1-mini": (2.9, 0.72, 11.6),
    "siliconflow.Qwen/Qwen2.5-7B-Instruct": (0.7, 0.0, 0.7),
    "siliconflow.deepseek-ai/DeepSeek-V3": (2.0, 0.5, 8.0),
}
# 未知模型按 deepseek-chat 保守估算（宁可估高，不要低估）
DEFAULT_PRICING = (2.0, 0.5, 8.0)
# 自部署 / Mock 不产生费用
FREE_PROVIDERS = {"local", "mock", "offline", ""}

_EMPTY = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "cost_cny": 0.0,
}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _load() -> dict:
    if not USAGE_FILE.exists():
        return {"days": {}, "budget_events": []}
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"days": {}, "budget_events": []}
        data.setdefault("days", {})
        data.setdefault("budget_events", [])
        return data
    except Exception:
        return {"days": {}, "budget_events": []}


def _save(data: dict):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[usage] 写入 {USAGE_FILE} 失败：{e}")


def _as_price(v) -> tuple | None:
    """把用户配置的单价规整为 (输入, 缓存命中, 输出) 三元组；非法返回 None。

    支持 [in, cached, out] 三元组或 [in, out] 二元组（缓存命中按 0 折）。
    """
    if not isinstance(v, (list, tuple)):
        return None
    try:
        nums = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    if len(nums) == 2:
        nums = [nums[0], 0.0, nums[1]]
    if len(nums) != 3 or any(n < 0 for n in nums):
        return None
    return (nums[0], nums[1], nums[2])


def _price(provider: str, model: str):
    """查单价：(输入未命中缓存, 输入命中缓存, 输出)，单位 ¥/百万 token。

    优先级：免费供应商(0 元) > 用户自定义单价(llm_config.json 的 pricing 字段)
    > 内置定价表 > 保守默认价。自定义 key 支持 "provider.model" 全名或裸
    "model"（匹配当前 provider 时生效）；非法配置一律跳过回退，绝不因
    手滑填错而中断计费。
    """
    provider = (provider or "").strip()
    if provider in FREE_PROVIDERS:
        return (0.0, 0.0, 0.0)
    custom = load_llm_config().get("pricing")
    if isinstance(custom, dict):
        for key in (f"{provider}.{model}", model):
            if key in custom:
                p = _as_price(custom[key])
                if p is not None:
                    return p
    return PRICING.get(f"{provider}.{model}", DEFAULT_PRICING)


def estimate_cost(provider: str, model: str, prompt_tokens: int,
                  completion_tokens: int, cached_tokens: int = 0) -> float:
    """估算单次调用费用（元）。"""
    p_in, p_cached, p_out = _price(provider, model)
    uncached = max(prompt_tokens - cached_tokens, 0)
    return (uncached * p_in + cached_tokens * p_cached +
            completion_tokens * p_out) / 1_000_000.0


def record(provider: str, model: str, prompt_tokens: int = 0,
           completion_tokens: int = 0, cached_tokens: int = 0,
           task: str = "") -> float:
    """记录一次 LLM 调用，返回本次估算费用（元）。"""
    cost = estimate_cost(provider, model, prompt_tokens,
                         completion_tokens, cached_tokens)
    day = _today()
    with _LOCK:
        data = _load()
        days = data["days"]
        d = days.setdefault(day, dict(_EMPTY))
        d["calls"] += 1
        d["prompt_tokens"] += prompt_tokens
        d["completion_tokens"] += completion_tokens
        d["cached_tokens"] += cached_tokens
        d["cost_cny"] = round(d["cost_cny"] + cost, 6)
        if task:
            by_task = d.setdefault("by_task", {})
            by_task[task] = by_task.get(task, 0) + 1
        # 只保留最近 N 天，避免文件无限增长
        if len(days) > _KEEP_DAYS:
            for old in sorted(days.keys())[:-_KEEP_DAYS]:
                days.pop(old, None)
        _save(data)
    return cost


def price_info(provider: str, model: str) -> dict:
    """返回当前生效的单价与来源（供前端展示「这个价是哪来的」）。

    source: custom（用户自定义）/ builtin（内置定价表）/ free（本地或 Mock）
    / default（未知模型按保守默认价估算）。
    """
    provider = (provider or "").strip()
    if provider in FREE_PROVIDERS:
        return {"source": "free", "price": [0.0, 0.0, 0.0]}
    custom = load_llm_config().get("pricing")
    if isinstance(custom, dict):
        for key in (f"{provider}.{model}", model):
            if key in custom and _as_price(custom[key]) is not None:
                return {"source": "custom", "price": list(_as_price(custom[key]))}
    full = f"{provider}.{model}"
    if full in PRICING:
        return {"source": "builtin", "price": list(PRICING[full])}
    return {"source": "default", "price": list(DEFAULT_PRICING)}


def budget_settings() -> tuple:
    """读取预算配置：(daily_limit_cny, action)。limit<=0 表示不限制。"""
    cfg = load_llm_config()
    try:
        limit = float(cfg.get("budget_daily_cny", 0) or 0)
    except (TypeError, ValueError):
        limit = 0.0
    action = (cfg.get("budget_action") or "fallback").strip().lower()
    if action not in ("warn", "fallback", "reject"):
        action = "fallback"
    return limit, action


def check_budget() -> tuple:
    """检查今日是否已超预算。

    返回 (exceeded: bool, action: str)。未配置预算（limit<=0）时恒为
    (False, action)，即不限制。
    """
    limit, action = budget_settings()
    if limit <= 0:
        return False, action
    spent = today().get("cost_cny", 0.0)
    if spent < limit:
        return False, action
    # 首次触发时记一条事件，便于前端提示「已熔断」
    day = _today()
    with _LOCK:
        data = _load()
        if not any(e.get("date") == day for e in data["budget_events"]):
            data["budget_events"].append({
                "date": day,
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "spent_cny": round(spent, 4),
                "limit_cny": limit,
                "action": action,
            })
            data["budget_events"] = data["budget_events"][-50:]
            _save(data)
    return True, action


def today() -> dict:
    return _load()["days"].get(_today(), dict(_EMPTY))


def summary() -> dict:
    """汇总今日 / 累计用量 + 预算状态，供 /llm/usage 返回前端。"""
    # 先跑一次预算检查（首次超限时它会写入 events），再读取快照，
    # 否则本次刚触发的熔断事件要等下一次请求才看得到。
    exceeded, _ = check_budget()
    data = _load()
    days = data["days"]
    total = dict(_EMPTY)
    for d in days.values():
        total["calls"] += d.get("calls", 0)
        total["prompt_tokens"] += d.get("prompt_tokens", 0)
        total["completion_tokens"] += d.get("completion_tokens", 0)
        total["cached_tokens"] += d.get("cached_tokens", 0)
        total["cost_cny"] += d.get("cost_cny", 0.0)
    total["cost_cny"] = round(total["cost_cny"], 6)

    limit, action = budget_settings()
    spent = days.get(_today(), dict(_EMPTY)).get("cost_cny", 0.0)
    cfg = load_llm_config()
    return {
        "today": days.get(_today(), dict(_EMPTY)),
        "total": total,
        "days_count": len(days),
        "recent": [
            {"date": k, **{kk: v[kk] for kk in
                           ("calls", "prompt_tokens", "completion_tokens", "cost_cny")
                           if kk in v}}
            for k, v in sorted(days.items())[-14:]
        ],
        "budget": {
            "daily_cny": limit,
            "action": action,
            "spent_cny": round(spent, 6),
            "exceeded": exceeded,
            "remaining_cny": round(max(limit - spent, 0.0), 6) if limit > 0 else None,
            "percent": round(min(spent / limit * 100, 100), 1) if limit > 0 else None,
        },
        "events": data["budget_events"][-10:],
        "provider": cfg.get("provider", ""),
        "model": cfg.get("model", ""),
        "pricing": price_info(cfg.get("provider", ""), cfg.get("model", "")),
    }


def reset():
    """清空全部用量记录（演示前重置用）。"""
    with _LOCK:
        _save({"days": {}, "budget_events": []})
