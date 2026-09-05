# -*- coding: utf-8 -*-
"""pytest 全局夹具：测试隔离（临时 SQLite）+ 强制离线 Mock LLM + 已登录客户端。

要点：
1. 环境变量必须在 import src.api.server **之前** 设置，否则 db.py 会连到真实 data/teleops.db；
2. DEEPSEEK_API_KEY 置空 → LLMClient 走离线 Mock，测试不联网、不消耗额度、结果确定；
3. 所有写接口需要 JWT，提供 auth_headers 夹具统一注入。
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 测试隔离：临时数据库 / 工具目录 / 知识库目录 + 固定密钥 ----
_TMP_DIR = Path(tempfile.mkdtemp(prefix="teleops_pytest_"))
os.environ["TELEOPS_DB_FILE"] = str(_TMP_DIR / "teleops.db")
os.environ["TELEOPS_JWT_SECRET"] = "pytest-secret-do-not-use-in-prod"
os.environ["TELEOPS_API_TOKEN"] = ""      # 不启用共享 Token，只走 JWT
os.environ["DEEPSEEK_API_KEY"] = ""       # 强制离线 Mock 推理
os.environ["TELEOPS_RATE_LIMIT"] = "off"  # 全局关限流：既有用例不受窗口干扰；test_ratelimit 单独运行时打开

# LLM 配置与用量统计同样必须隔离：DATA_DIR 不能被整体重定向（拓扑/告警种子
# 数据仍来自真实 data/），但 data/llm_config.json 里存着开发者本机的真实 Key。
# 不隔离的话 pytest 会读到它去联网调用——既烧额度，又让用例结果不确定。
os.environ["TELEOPS_LLM_CONFIG_FILE"] = str(_TMP_DIR / "llm_config.json")
os.environ["TELEOPS_USAGE_FILE"] = str(_TMP_DIR / "llm_usage.json")

# 研发 Agent 造工具会落盘 .py 与 SOP 文档，同样重定向到临时目录，避免污染仓库
_TMP_TOOLS = _TMP_DIR / "tools"
_TMP_KB = _TMP_DIR / "kb"
_TMP_TOOLS.mkdir(parents=True, exist_ok=True)
_TMP_KB.mkdir(parents=True, exist_ok=True)
import shutil  # noqa: E402
for _f in Path(ROOT / "tools").glob("*.py"):        # 基线工具脚本
    shutil.copy(_f, _TMP_TOOLS / _f.name)
for _f in Path(ROOT / "kb").glob("*.md"):           # 知识库文档
    shutil.copy(_f, _TMP_KB / _f.name)
os.environ["TELEOPS_TOOLS_DIR"] = str(_TMP_TOOLS)
os.environ["TELEOPS_KB_DIR"] = str(_TMP_KB)

from fastapi.testclient import TestClient  # noqa: E402
from src.api.server import app             # noqa: E402
from src.core import rate_limit as _rl     # noqa: E402
from src.core.tool_registry import ToolRegistry  # noqa: E402
from src.core import db                    # noqa: E402


# 基线工具种子：conftest 已把磁盘 tools/*.py 拷到临时目录，这里同步把元数据落进 DB。
# ping_host/restart_service 是 v0.3.x 引入的核心示例工具，test_tools_baseline、
# test_agents_dispatch、test_real_loop 等用例都依赖它们。
def _seed_baseline_tools():
    registry = ToolRegistry()
    seed = [
        {"name": "ping_host", "executor": "net_ping.py",
         "risk": "low", "require_human_approval": False,
         "description": "网络连通性探测（ping 主机）"},
        {"name": "restart_service", "executor": "svc_restart.py",
         "risk": "high", "require_human_approval": True,
         "description": "服务重启（高危，需人工确认）"},
    ]
    for t in seed:
        if not registry.get(t["name"]):
            registry.add(t)


_seed_baseline_tools()


@pytest.fixture(autouse=True)
def _rate_limit_reset():
    """每个测试前把限流配置复位为关闭，防止 test_ratelimit 的运行时开关泄漏到其它用例。"""
    _rl.configure_rate_limit(enabled=False, read=1000, write=1000, login=1000)
    yield
    _rl.configure_rate_limit(enabled=False, read=1000, write=1000, login=1000)


@pytest.fixture(scope="session")
def client():
    """会话级 TestClient（触发 startup，复用全局单例）。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers(client):
    """注册一个测试用户并返回带 Bearer JWT 的请求头。"""
    username = "pytest_" + uuid.uuid4().hex[:8]
    r = client.post("/auth/register", json={"username": username, "password": "Pytest123456"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["token"]
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


@pytest.fixture
def ws_id(client, auth_headers):
    """创建一个临时业务域，测试结束自动删除。"""
    name = "测试域-" + uuid.uuid4().hex[:6]
    r = client.post("/workspaces", json={"name": name, "adapter_id": "alert-prometheus", "mode": "auto"},
                    headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    wid = r.json()["id"]
    yield wid
    client.delete(f"/workspaces/{wid}", headers=auth_headers)


def wait_job(client, job_id, timeout=60, interval=0.3):
    """轮询异步任务直到 done/error/not_found，返回 (ok, result)。"""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/jobs/{job_id}")
        if r.status_code != 200:
            return False, {"error": f"http {r.status_code}"}
        d = r.json()
        if d.get("status") == "done":
            return True, d.get("result")
        if d.get("status") in ("error", "not_found"):
            return False, d.get("error") or d.get("result")
        time.sleep(interval)
    return False, "timeout"
