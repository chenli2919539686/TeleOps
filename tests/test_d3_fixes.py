# -*- coding: utf-8 -*-
"""D3 收尾修复回归测试。

1. LLM 真实调用必须有界超时：只配了 Key 但对端慢/不可达时，若不传 timeout
   OpenAI SDK 会用自己长达数百秒的默认值，告警流水线会逐条卡死在上面 ——
   表现为"流水线停不下来"、/stream/stop 要等 join 超时才返回。
2. 流水线启动者 started_by 应显示真实用户名而不是"匿名"：
   JWT 早期只写了标准声明 sub、没写 username，业务侧按 username 取就取不到。
"""
import json
import uuid

import pytest

from src.core import auth
from src.llm_client import LLMClient, LLM_TIMEOUT


# ---------------- 1. LLM 有界超时 ----------------
class _OpenAIRecorder:
    """替换 openai.OpenAI，只为捕获构造参数（不发任何真实请求）。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _with_llm_config(cfg: dict):
    """把指定配置写进 LLM 运行时配置（conftest 已重定向到临时目录），用完还原。

    必须走真实配置文件：``_ensure_client()`` 内部会执行
    ``self._cfg = load_llm_config()``，直接给 ``_cfg`` 赋值会被覆盖掉。
    """
    from src.config import load_llm_config, save_llm_config
    original = load_llm_config()
    save_llm_config({**original, **cfg})
    try:
        yield
    finally:
        save_llm_config(original)


@pytest.mark.parametrize("cfg", [
    {"provider": "openai", "api_key": "sk-test", "base_url": ""},
    {"provider": "deepseek", "api_key": "sk-test", "base_url": ""},
    {"provider": "local", "api_key": "", "local_endpoint": "http://localhost:11434/v1"},
])
def test_llm_client_uses_bounded_timeout(monkeypatch, cfg):
    """三种真实通道都必须显式带上界超时，杜绝依赖 SDK 默认的超长等待。"""
    monkeypatch.setattr("openai.OpenAI", _OpenAIRecorder, raising=False)
    for _ in _with_llm_config(cfg):
        c = LLMClient()
        c._ensure_client()
        assert isinstance(c._client, _OpenAIRecorder), "应创建真实通道客户端而非降级 mock"
        got = c._client.kwargs.get("timeout")
        assert got is not None, "必须显式传 timeout，不能依赖 SDK 默认（数百秒）"
        assert got == LLM_TIMEOUT
        assert 0 < got <= 600


def test_llm_timeout_default_is_conservative():
    """默认超时应是"有界且够用"，不是 SDK 那种几百秒。"""
    assert LLM_TIMEOUT > 0
    assert LLM_TIMEOUT <= 120, f"默认超时 {LLM_TIMEOUT}s 偏大，卡死体感仍会很差"


def test_no_key_still_falls_back_to_mock():
    """无 Key 仍走确定性 Mock，不给纯离线演示增加不确定性。"""
    for _ in _with_llm_config({"provider": "openai", "api_key": "", "base_url": ""}):
        c = LLMClient()
        c._ensure_client()
        assert c.mode == "mock"


# ---------------- 2. started_by 不再永远是"匿名" ----------------
def test_jwt_carries_username_claim():
    """签发的 token 必须带上 username 声明，且与标准 sub 一致。"""
    name = "fix_user_" + uuid.uuid4().hex[:8]
    auth.create_user(name, "Pytest123456")
    payload = auth.decode_token(auth.issue_token(name))
    assert payload is not None
    assert payload["username"] == name, "缺 username 声明会让业务侧取不到用户名"
    assert payload["sub"] == name, "sub 仍是权威身份声明"


def test_started_by_real_username_for_legacy_token(client):
    """旧 token（只有 sub、没有 username）也要能解析出真实用户名。

    这是线上真实会遇到的数据形态：老用户手里是修复前签发的 token。
    """
    name = "legacy_" + uuid.uuid4().hex[:8]
    r = client.post("/auth/register", json={"username": name, "password": "Pytest123456"})
    assert r.status_code in (200, 201), r.text

    u = auth.get_user(name)
    # 手工复刻 issue_token 修复前的载荷：只有 sub，没有 username
    legacy_claims = {
        "sub": u["username"], "uid": u["id"], "is_admin": u["is_admin"],
        "org_id": u.get("org_id"), "roles": u.get("roles"), "perms": u.get("perms"),
    }
    legacy_token = auth.encode_token(legacy_claims)
    assert "username" not in auth.decode_token(legacy_token), \
        "构造的旧 token 必须真的没有 username 字段，否则本用例失去意义"

    headers = {"Authorization": "Bearer " + legacy_token, "Content-Type": "application/json"}
    wss = client.get("/workspaces", headers=headers).json()["workspaces"]
    ws = next(w["id"] for w in wss if w.get("owner_id") is not None)

    started = client.post("/stream/start", headers=headers,
                          json={"profile": "mixed", "interval_ms": 1000,
                                "loop": False, "workspace_id": ws})
    try:
        assert started.status_code == 200, started.text
        got = started.json()["started_by"]
        assert got == name, f"旧 token 应回退取 sub 得到用户名，实际 {got!r}"
    finally:
        # 注意：/stream/stop 的 workspace_id 是 **query 参数**，放 body 里会被忽略
        client.post(f"/stream/stop?workspace_id={ws}", headers=headers)
