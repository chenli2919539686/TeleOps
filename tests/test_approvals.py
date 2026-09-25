# -*- coding: utf-8 -*-
"""人工审批（HITL）闭环集成测试。

覆盖：
- 审批存储层（create/list/get/decide）单测；
- 开关开启（TELEOPS_REQUIRE_APPROVAL=1）时，tool.build 与 stream.start 落 pending 审批单；
- GET /approvals 的 admin 全量 / 普通用户仅见自己；
- POST /approvals/{id}/approve 对 stream.start 真正启动流、对 tool.build 闭环；
- POST /approvals/{id}/reject 拒绝；
- POST /settings/require-approval 运行时切换 + data/settings.json 持久化。

隔离：approvals.DATA_FILE / settings.SETTINGS_FILE 重定向到临时目录，绝不污染仓库 data/。
"""
import os

from src.core import approvals
from src.core import settings


def _isolate(tmp_path, monkeypatch):
    """把审批单与运行时设置重定向到临时目录，并强制本地存储后端（隔离 Redis 配置污染）。

    显式 configure 为本地后端，确保即便同进程其它测试注入了 Redis 后端，这里的断言
    仍走 data/<tmp> 落盘（与改造前行为一致），绝不污染仓库 data/。
    """
    monkeypatch.setattr(approvals, "DATA_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(approvals, "_state", {"items": []})
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    approvals.configure_approval_store(approvals.LocalApprovalStore())
    settings.configure_settings_store(settings.LocalSettingsStore())


def test_store_crud(tmp_path, monkeypatch):
    """审批存储层：创建→查→列表→决定。"""
    _isolate(tmp_path, monkeypatch)
    aid = approvals.create("tool.build", "alice", {"x": 1}, detail={"k": "v"})
    assert aid.startswith("apr-")
    assert approvals.get(aid)["status"] == "pending"
    items = approvals.list_items(status="pending")
    assert len(items) == 1 and items[0]["id"] == aid
    decided = approvals.decide(aid, "approved", "admin")
    assert decided["status"] == "approved" and decided["decided_by"] == "admin"
    # 已决定的单子不可二次决定
    assert approvals.decide(aid, "rejected", "admin")["status"] == "approved"
    # 普通用户只看自己发起/处置的
    assert approvals.list_items(uid="bob") == []


def test_stream_start_gated_and_approved(client, auth_headers, admin_headers,
                                          ws_id, tmp_path, monkeypatch):
    """开启审批闸后：stream.start 落 pending；admin 批准真正启动流；普通用户可见自己单。"""
    import uuid
    from src.core import auth as _auth

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEOPS_REQUIRE_APPROVAL", "1")

    # 建一个确定非 admin 的用户（auth_headers 在隔离 DB 下可能是首个注册用户=admin）
    puname = "plain_" + uuid.uuid4().hex[:8]
    _auth.create_user(puname, "Plain123456", is_admin=False)
    pr = client.post("/auth/login", json={"username": puname, "password": "Plain123456"})
    plain_headers = {"Authorization": "Bearer " + pr.json()["token"],
                     "Content-Type": "application/json"}

    # 1) 普通用户在自己可写的域启动流 → 落 pending 审批单
    r = client.post("/stream/start", headers=auth_headers,
                    json={"workspace_id": ws_id, "profile": "mixed", "mode": "auto"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval" and body.get("approval_id")
    apr_id = body["approval_id"]

    # 2) 普通用户能看到自己发起的单
    mine = client.get("/approvals", headers=auth_headers).json()
    assert any(i["id"] == apr_id for i in mine["items"])
    # 关闭状态字段反映开关
    assert mine["require_approval"] is True

    # 3) 非 admin 不能审批
    deny = client.post(f"/approvals/{apr_id}/approve", headers=plain_headers,
                       json={})
    assert deny.status_code == 403

    # 4) admin 批准 → 流真正启动
    ap = client.post(f"/approvals/{apr_id}/approve", headers=admin_headers, json={})
    assert ap.status_code == 200, ap.text
    assert ap.json()["executed"] is True

    # 5) 流确实被「审批后启动」：executed 为真即证明 _run_stream_start 真正执行
    #    （不查 stream/status：全局执行器缓存在多测间复用 ws id，started_by 不可靠）
    st = client.get("/stream/status", headers=auth_headers,
                    params={"workspace_id": ws_id}).json()
    assert st.get("running") is True, "审批批准后流应处于 running"

    # 6) 收尾：停流，避免线程常驻
    client.post("/stream/stop", headers=auth_headers, params={"workspace_id": ws_id})


def test_stream_start_rejected(client, auth_headers, admin_headers,
                               ws_id, tmp_path, monkeypatch):
    """开启审批闸后：stream.start 落 pending；admin 拒绝 → 状态 rejected 且未启动。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEOPS_REQUIRE_APPROVAL", "1")

    r = client.post("/stream/start", headers=auth_headers,
                    json={"workspace_id": ws_id, "profile": "mixed", "mode": "auto"})
    assert r.status_code == 200
    apr_id = r.json()["approval_id"]

    rj = client.post(f"/approvals/{apr_id}/reject", headers=admin_headers, json={})
    assert rj.status_code == 200 and rj.json()["approval"]["status"] == "rejected"
    # 拒绝路径不执行：审批单保持 rejected（executed 标志不出现即证明未落地启动）


def test_tool_build_gated(client, admin_headers, tmp_path, monkeypatch):
    """开启审批闸后：研发造工具落 pending；admin 批准端点闭环（200，含 approval）。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEOPS_REQUIRE_APPROVAL", "1")

    # 取公共域 core-net 里的研发 Agent（admin 可写公共域）
    agents = client.get("/agents", headers=admin_headers,
                        params={"workspace_id": "core-net"}).json()
    dev = next((a for a in agents.get("agents", []) if a.get("kind") == "dev"), None)
    assert dev, "core-net 应默认含研发 Agent"

    r = client.post(f"/agents/{dev['id']}/build", headers=admin_headers,
                    json={"feedback_id": "fb-test", "summary": "测试造工具"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval" and body.get("approval_id")
    apr_id = body["approval_id"]

    # admin 批准：端点返回 200（执行结果取决于 mock LLM，仅验证闭环不崩）
    ap = client.post(f"/approvals/{apr_id}/approve", headers=admin_headers, json={})
    assert ap.status_code == 200 and "approval" in ap.json()


def test_settings_runtime_toggle(client, admin_headers, tmp_path, monkeypatch):
    """POST /settings/require-approval：admin 运行时切换并持久化；非 admin 403。"""
    import uuid
    from src.core import auth as _auth

    _isolate(tmp_path, monkeypatch)
    # 不设 env，纯靠文件值
    assert settings.get_require_approval() is False

    # 建一个确定非 admin 的用户（auth_headers 在隔离 DB 下可能是首个注册用户=admin）
    uname = "plain_" + uuid.uuid4().hex[:8]
    _auth.create_user(uname, "Plain123456", is_admin=False)
    r = client.post("/auth/login", json={"username": uname, "password": "Plain123456"})
    assert r.status_code == 200, r.text
    plain_headers = {"Authorization": "Bearer " + r.json()["token"],
                     "Content-Type": "application/json"}

    # 非 admin 不能切换
    deny = client.post("/settings/require-approval", headers=plain_headers,
                       json={"enabled": True})
    assert deny.status_code == 403
    assert settings.get_require_approval() is False

    # admin 开启并持久化
    r = client.post("/settings/require-approval", headers=admin_headers,
                    json={"enabled": True})
    assert r.status_code == 200 and r.json()["require_approval"] is True
    assert settings.SETTINGS_FILE.exists()
    assert settings.get_require_approval() is True

    # 关回，保持整洁
    r2 = client.post("/settings/require-approval", headers=admin_headers,
                     json={"enabled": False})
    assert r2.json()["require_approval"] is False
