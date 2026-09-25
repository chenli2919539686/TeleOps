"""MCP 监控 → Agent 诊断工具化 的测试。

覆盖两类：
  1. 工具执行器（tools/pull_metrics.py / tools/pull_logs.py）：demo 模式返回结构化数据；
     live 模式经注入的 fake 适配器解析（不联网）。
  2. OpsAgent.run_recommended_tools：把 pull_metrics 作为 recommended_tool + tool_args
     透传到工具调用，并验证 missing 检测。
"""
import sys
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.adapters.registry as reg_mod


def _load_executor(name: str):
    p = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_t_{name}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------- 执行器：demo 模式 ----------------
def test_pull_metrics_demo_returns_series():
    mod = _load_executor("pull_metrics")
    out = mod.run({"query": "up", "hours": 1})
    assert out["status"] == "ok"
    assert out["mode"] == "demo"
    assert out["query"] == "up"
    assert isinstance(out["series"], list) and len(out["series"]) > 0


def test_pull_logs_demo_returns_logs():
    mod = _load_executor("pull_logs")
    out = mod.run({"query": '{job=~".+"}', "limit": 5})
    assert out["status"] == "ok"
    assert out["mode"] == "demo"
    assert out["count"] > 0
    assert all("message" in l and "level" in l for l in out["logs"])


def test_pull_metrics_no_adapter_errors(monkeypatch):
    class FakeReg:
        def get(self, aid):
            return None
    monkeypatch.setattr(reg_mod, "AdapterRegistry", FakeReg)
    mod = _load_executor("pull_metrics")
    out = mod.run({})
    assert out["status"] == "error"


# ---------------- 执行器：live 模式（注入 fake 适配器，不联网） ----------------
def test_pull_metrics_live_parses_adapter(monkeypatch):
    class FakeAdapter:
        id = "metrics-prometheus"
        base_url = "http://prom:9090"

        def query_metrics(self, promql, hours=1):
            return {"mode": "live", "promql": promql,
                    "series": [{"ts": 1, "value": 0.5}]}

    class FakeReg:
        def get(self, aid):
            return FakeAdapter()

    monkeypatch.setattr(reg_mod, "AdapterRegistry", FakeReg)
    mod = _load_executor("pull_metrics")
    out = mod.run({"query": "node_cpu", "hours": 2})
    assert out["status"] == "ok"
    assert out["mode"] == "live"
    assert out["adapter"] == "metrics-prometheus"
    assert out["points"] == 1
    assert out["series"] == [{"ts": 1, "value": 0.5}]


def test_pull_logs_live_normalizes(monkeypatch):
    class FakeAdapter:
        id = "logs-loki"
        base_url = "http://loki:3100"

        def fetch_recent(self, query, limit=100):
            return [{"ts": 123, "metric": query, "value": "[ERROR] boom",
                     "source": "loki", "raw": {"labels": {"level": "error"}}}]

    class FakeReg:
        def get(self, aid):
            return FakeAdapter()

    monkeypatch.setattr(reg_mod, "AdapterRegistry", FakeReg)
    mod = _load_executor("pull_logs")
    out = mod.run({"query": '{level="error"}', "limit": 10})
    assert out["status"] == "ok"
    assert out["mode"] == "live"
    assert out["count"] == 1
    assert out["logs"][0]["level"] == "error"
    assert out["logs"][0]["message"] == "[ERROR] boom"


# ---------------- OpsAgent 集成：把监控工具作为诊断动作执行 ----------------
def _make_agent():
    from src.agents.ops_agent import OpsAgent
    from src.core.tool_registry import ToolRegistry
    # cmdb/kb/llm 在 run_recommended_tools 路径上不被使用，传占位
    return OpsAgent(cmdb=None, kb=None, tools=ToolRegistry(), llm=None)


def test_ops_agent_runs_pull_metrics_with_tool_args():
    agent = _make_agent()
    diagnosis = {"hypotheses": [{
        "cause": "CPU 升高", "confidence": 0.8, "evidence": "x",
        "recommended_tool": "pull_metrics", "recommended_action": "查指标",
        "tool_args": {"query": "node_cpu", "hours": 1},
    }]}
    results = agent.run_recommended_tools(diagnosis, {"host": "web-01"})
    assert len(results) == 1
    r = results[0]
    assert r["tool"] == "pull_metrics"
    assert r["status"] == "ok"              # 测试环境无 adapters.json → demo 模式
    assert r["result"]["query"] == "node_cpu"
    assert r["result"]["mode"] == "demo"


def test_ops_agent_runs_pull_logs_with_tool_args():
    agent = _make_agent()
    diagnosis = {"hypotheses": [{
        "recommended_tool": "pull_logs",
        "tool_args": {"query": '{level="error"}', "limit": 3},
    }]}
    results = agent.run_recommended_tools(diagnosis, {})
    assert results[0]["tool"] == "pull_logs"
    assert results[0]["status"] == "ok"
    assert results[0]["result"]["count"] > 0


def test_ops_agent_missing_tool_detected():
    agent = _make_agent()
    diagnosis = {"hypotheses": [{"recommended_tool": "nonexistent_x_probe"}]}
    results = agent.run_recommended_tools(diagnosis, {})
    assert results[0]["status"] == "missing"
