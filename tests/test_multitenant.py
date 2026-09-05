# -*- coding: utf-8 -*-
"""多租户隔离（A 档·单机多用户）：注册自动建个人域 + 业务域/Agent 按用户可见性过滤。

核心断言：
1. 注册成功 → 自动创建 owner_id=本人 的个人业务域（含完整默认 Agent 矩阵）；
2. /workspaces 每个用户只看到「公共域(core-net) + 本人私有域」，看不到别人的；
3. /agents 同样按可见域过滤，别人的 Agent 不会泄漏；
4. 公共域 owner_id=None，对所有登录用户可见。
"""
import base64
import json
import uuid


def _register(client, username):
    """注册一个新用户，返回 (请求头, uid)。uid 从 JWT payload 解出（/auth/me 不透出）。"""
    r = client.post("/auth/register", json={"username": username, "password": "Pytest123456"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # base64url 补齐
    uid = json.loads(base64.urlsafe_b64decode(payload))["uid"]
    return headers, uid


def test_register_creates_personal_workspace(client):
    """注册后自动建个人域：恰好一个私有域、owner 归属本人、自带完整 Agent 矩阵。"""
    username = "mt_" + uuid.uuid4().hex[:8]
    headers, uid = _register(client, username)
    r = client.get("/workspaces", headers=headers)
    assert r.status_code == 200, r.text
    wss = r.json()["workspaces"]
    # 公共域始终可见
    assert "core-net" in [w["id"] for w in wss]
    # 恰好一个私有域，owner_id == 当前用户
    private = [w for w in wss if w["owner_id"] is not None]
    assert len(private) == 1, f"应只看到 1 个个人域，实际 {[w['id'] for w in wss]}"
    assert private[0]["owner_id"] == uid, "个人域 owner 应为注册用户本人"
    # 个人域含默认 Agent 矩阵（运维 + 研发）
    pa = client.get(f"/agents?workspace_id={private[0]['id']}", headers=headers).json()["agents"]
    assert {a["kind"] for a in pa} >= {"ops", "dev"}, f"个人域应自带运维+研发 Agent，实际 {pa}"
    assert all(a["id"].startswith(private[0]["id"]) for a in pa), "Agent id 应绑定个人域"


def test_two_users_do_not_see_each_others_workspaces(client):
    """用户 A、B 互相看不到对方的私有域，但都能看到公共域。"""
    ha, _ = _register(client, "mtA_" + uuid.uuid4().hex[:8])
    hb, _ = _register(client, "mtB_" + uuid.uuid4().hex[:8])
    wa = {w["id"] for w in client.get("/workspaces", headers=ha).json()["workspaces"]}
    wb = {w["id"] for w in client.get("/workspaces", headers=hb).json()["workspaces"]}
    assert "core-net" in wa and "core-net" in wb
    a_private, b_private = wa - {"core-net"}, wb - {"core-net"}
    assert a_private and b_private, "双方都应各有个人域"
    assert a_private.isdisjoint(b_private), f"域泄漏：A={a_private} B={b_private}"


def test_agents_isolated_per_user(client):
    """/agents 仅返回可见业务域（公共+本人）下的 Agent，别人的 Agent 不泄漏。"""
    ha, _ = _register(client, "mtC_" + uuid.uuid4().hex[:8])
    hb, _ = _register(client, "mtD_" + uuid.uuid4().hex[:8])
    agents_a = {a["id"] for a in client.get("/agents", headers=ha).json()["agents"]}
    agents_b = {a["id"] for a in client.get("/agents", headers=hb).json()["agents"]}
    # 公共域 Agent 双方都可见
    assert any(a.startswith("core-net") for a in agents_a)
    assert any(a.startswith("core-net") for a in agents_b)
    # A 的个人域 Agent 不应出现在 B 的列表里
    a_private = {a for a in agents_a if not a.startswith("core-net")}
    assert a_private, "A 应看到自己个人域的 Agent"
    assert a_private.isdisjoint(agents_b), f"Agent 泄漏：{a_private & agents_b}"


def test_public_domain_visible_to_all_logged_in(client):
    """公共域 owner_id=None，对所有已登录用户可见（作为共享 demo 域）。"""
    hx, _ = _register(client, "mtX_" + uuid.uuid4().hex[:8])
    wss = client.get("/workspaces", headers=hx).json()["workspaces"]
    core = next(w for w in wss if w["id"] == "core-net")
    assert core["owner_id"] is None
    assert core["agent_count"] >= 2


def test_anonymous_only_sees_public_workspace(client):
    """未登录（匿名）访问时，/workspaces 与 /agents 只能看到公共域，不能看到任何个人域。"""
    # 先注册一个用户制造个人域，确保库里有私有域
    _register(client, "mtAnon_" + uuid.uuid4().hex[:8])

    wss = client.get("/workspaces").json()["workspaces"]
    assert len(wss) == 1, f"匿名应只看到 1 个公共域，实际 {wss}"
    assert wss[0]["id"] == "core-net"
    assert wss[0]["owner_id"] is None

    agents = client.get("/agents").json()["agents"]
    assert all(a["workspace_id"] == "core-net" for a in agents), \
        f"匿名不应看到个人域 Agent，实际 {agents}"


def test_non_admin_cannot_mutate_public_workspace(client):
    """普通用户（非 admin）只能读公共域 core-net，不能增删改其 Agent 或删域。"""
    headers, _ = _register(client, "mtPub_" + uuid.uuid4().hex[:8])
    # 不是第一个用户，所以不是 admin
    me = client.get("/auth/me", headers=headers).json()
    assert me.get("is_admin") is False

    # 创建 Agent 应 403
    r = client.post("/workspaces/core-net/agents", headers=headers,
                    json={"kind": "ops", "name": "公共域新 Agent", "scope": ["compute"]})
    assert r.status_code == 403, f"普通用户不应能在 core-net 创建 Agent: {r.text}"

    # 获取一个现有 agent id 尝试修改/删除
    agents = client.get("/workspaces/core-net", headers=headers).json()["agents"]
    assert agents
    aid = agents[0]["id"]
    r = client.put(f"/workspaces/core-net/agents/{aid}", headers=headers,
                   json={"name": "改名"})
    assert r.status_code == 403, "普通用户不应能修改 core-net Agent"
    r = client.delete(f"/workspaces/core-net/agents/{aid}", headers=headers)
    assert r.status_code == 403, "普通用户不应能删除 core-net Agent"
    r = client.put("/workspaces/core-net/mode", headers=headers, json={"mode": "manual"})
    assert r.status_code == 403, "普通用户不应能修改 core-net mode"
    r = client.delete("/workspaces/core-net", headers=headers)
    assert r.status_code == 403, "普通用户不应能删除 core-net"


def test_user_cannot_mutate_others_workspace(client):
    """用户 A 不能操作用户 B 的私有业务域（包括看详情、增删改 Agent、删域）。"""
    ha, _ = _register(client, "mtOwnA_" + uuid.uuid4().hex[:8])
    hb, _ = _register(client, "mtOwnB_" + uuid.uuid4().hex[:8])
    wb = [w for w in client.get("/workspaces", headers=hb).json()["workspaces"]
          if w["owner_id"] is not None][0]

    # A 看不到 B 的私有域详情
    r = client.get(f"/workspaces/{wb['id']}", headers=ha)
    assert r.status_code == 404, "不应看到他人私有域详情"

    # A 不能在 B 的域里创建 Agent
    r = client.post(f"/workspaces/{wb['id']}/agents", headers=ha,
                    json={"kind": "ops", "name": "越权 Agent", "scope": ["compute"]})
    assert r.status_code == 403, "不应能在他人域创建 Agent"

    # A 不能删除 B 的域
    r = client.delete(f"/workspaces/{wb['id']}", headers=ha)
    assert r.status_code == 403, "不应能删除他人域"


def test_user_can_mutate_own_workspace(client):
    """用户在自己的私有域里可以正常增删改 Agent。"""
    headers, uid = _register(client, "mtOwn_" + uuid.uuid4().hex[:8])
    ws = [w for w in client.get("/workspaces", headers=headers).json()["workspaces"]
          if w["owner_id"] == uid][0]

    r = client.post(f"/workspaces/{ws['id']}/agents", headers=headers,
                    json={"kind": "ops", "name": "我的运维 Agent", "scope": ["compute"]})
    assert r.status_code in (200, 201), r.text
    aid = r.json()["id"]

    r = client.put(f"/workspaces/{ws['id']}/agents/{aid}", headers=headers,
                   json={"name": "改名后"})
    assert r.status_code == 200, r.text

    r = client.delete(f"/workspaces/{ws['id']}/agents/{aid}", headers=headers)
    assert r.status_code == 200, r.text
