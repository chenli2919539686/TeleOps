# -*- coding: utf-8 -*-
"""极简 Prometheus 指标收集器（零第三方依赖）。

只依赖标准库，输出 Prometheus 文本暴露格式（text/plain; version=0.0.4）。
支持三类指标：
  - counter：单调递增计数（HTTP 请求数、Agent 任务数、LLM 调用数…）
  - histogram：观测值分布（HTTP 延迟，含 le 分桶）
  - gauge：抓取时实时计算（业务域数、Agent 数、需求数…通过回调采集）

用法：
    from src.core import metrics
    metrics.inc("teleops_jobs_total", status="done")
    metrics.observe_seconds("teleops_http_request_duration_seconds", 0.12, path="/health")
    metrics.gauge("teleops_workspaces_total", "业务域总数", lambda: [({}, 3)])
    text = metrics.render()
"""
import threading
import time
from collections import defaultdict

_LOCK = threading.Lock()

# 延迟分桶（秒）
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, float("inf"))

_counters = defaultdict(float)          # (name, labels_tuple) -> value
_histograms = defaultdict(lambda: {     # (name, labels_tuple) -> 统计
    "count": 0, "sum": 0.0, "buckets": [0] * len(BUCKETS)})
_gauges = {}                            # name -> {help, fn}（fn 返回 [(labels_dict, value)]）
_help = {}                              # name -> 说明文字
_start = time.time()


def _key(labels):
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _fmt_labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())) + "}"


def _escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_num(v):
    if v == float("inf"):
        return "+Inf"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(round(v, 6))


def set_help(name, text):
    _help[name] = text


def inc(name, value=1.0, **labels):
    """计数器 +value（默认 +1）。"""
    with _LOCK:
        _counters[(name, _key(labels))] += value


def observe_seconds(name, seconds, **labels):
    """直方图观测（单位秒）。桶内存储「恰好落入该桶」的计数（非累积），
    渲染时再按 Prometheus 语义做 le 累积，避免重复计数。"""
    with _LOCK:
        h = _histograms[(name, _key(labels))]
        h["count"] += 1
        h["sum"] += seconds
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                h["buckets"][i] += 1
                break   # 只命中最小桶；渲染时累积成 Prometheus 的 le 分布


def gauge(name, help_text, fn):
    """注册一个 gauge 采集回调：fn() -> [(labels_dict, value), ...]。

    每次 render() 时调用，用于暴露数据库的实时状态。
    """
    _gauges[name] = (help_text, fn)


def render() -> str:
    """渲染为 Prometheus 文本暴露格式。"""
    with _LOCK:
        lines = []

        # ---- counters ----
        by_name = defaultdict(list)
        for (name, labels), value in _counters.items():
            by_name[name].append((labels, value))
        for name in sorted(by_name):
            lines.append(f"# HELP {name} {_help.get(name, name)}")
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(by_name[name]):
                lines.append(f"{name}{_fmt_labels(dict(labels))} {_fmt_num(value)}")

        # ---- histograms ----
        h_by_name = defaultdict(list)
        for (name, labels), h in _histograms.items():
            h_by_name[name].append((labels, h))
        for name in sorted(h_by_name):
            lines.append(f"# HELP {name} {_help.get(name, name)}")
            lines.append(f"# TYPE {name} histogram")
            for labels, h in sorted(h_by_name[name]):
                base = _fmt_labels(dict(labels))
                acc = 0
                for i, b in enumerate(BUCKETS):
                    acc += h["buckets"][i]
                    le = "+Inf" if b == float("inf") else _fmt_num(b)
                    inner = (base[:-1] + f',le="{le}"}}') if base else '{le="' + le + '"}'
                    lines.append(f"{name}_bucket{inner} {acc}")
                lines.append(f"{name}_sum{base} {_fmt_num(h['sum'])}")
                lines.append(f"{name}_count{base} {h['count']}")

        # ---- gauges（抓取时计算）----
        for name in sorted(_gauges):
            help_text, fn = _gauges[name]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            try:
                items = fn() or []
            except Exception:
                items = []
            for labels, value in items:
                lines.append(f"{name}{_fmt_labels(labels or {})} {_fmt_num(value)}")

        # ---- 进程指标 ----
        lines.append("# HELP teleops_process_uptime_seconds 服务已运行秒数")
        lines.append("# TYPE teleops_process_uptime_seconds gauge")
        lines.append(f"teleops_process_uptime_seconds {_fmt_num(time.time() - _start)}")

        return "\n".join(lines) + "\n"


def reset():
    """清空所有指标（测试用）。"""
    with _LOCK:
        _counters.clear()
        _histograms.clear()


# 预置说明文字
set_help("teleops_http_requests_total", "HTTP 请求总数")
set_help("teleops_http_request_duration_seconds", "HTTP 请求耗时（秒）")
set_help("teleops_jobs_total", "异步任务总数")
set_help("teleops_llm_calls_total", "LLM 调用次数")
set_help("teleops_requirements_raised_total", "登记需求总数")
