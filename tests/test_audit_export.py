# -*- coding: utf-8 -*-
"""审计日志导出端点测试（GET /audit/export）。

重点验证「看得到的才能导出」——导出复用与 /audit 完全相同的隔离逻辑
（_base_where + _apply_filters），绝不越权泄露他人业务域记录。
审计行用直接 SQL 插入保证确定性（绕过异步审计队列）。
"""
import uuid

from src.core import db, auth


def _insert_audit(ts, actor, actor_id, action, workspace_id,
                 detail="{}", result="ok", ip="1.2.3.4"):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO audit_log (ts, actor, actor_id, action, workspace_id, "
        "detail, result, ip) VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor, actor_id, action, workspace_id, detail, result, ip))
    conn.commit()


def _make_user(client, prefix):
    u = f"{prefix}_" + uuid.uuid4().hex[:8]
    auth.create_user(u, "Pass123456")
    r = client.post("/auth/login", json={"username": u, "password": "Pass123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _make_ws(client, headers):
    name = "ws-" + uuid.uuid4().hex[:6]
    r = client.post("/workspaces", json={"name": name,
                     "adapter_id": "alert-prometheus", "mode": "auto"},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_export_requires_auth(client):
    r = client.get("/audit/export")
    assert r.status_code == 401


def test_export_csv_headers_and_bom(client, admin_headers):
    r = client.get("/audit/export?format=csv", headers=admin_headers)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.content.decode("utf-8-sig")  # 去掉 UTF-8 BOM
    first = body.splitlines()[0]
    assert first.startswith("id,ts,actor")
    assert "workspace_id" in first
    assert "detail" in first and "result" in first and "ip" in first


def test_export_json(client, admin_headers):
    r = client.get("/audit/export?format=json", headers=admin_headers)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_export_admin_sees_all_but_user_isolated(client, admin_headers):
    # 用显式非管理员账号做「普通用户」对照（避免依赖 auth_headers 是否为首个管理员）
    hA = _make_user(client, "expA")
    hB = _make_user(client, "expB")
    wsA = _make_ws(client, hA)
    wsB = _make_ws(client, hB)

    _insert_audit("2026-01-01T00:00:00", "A", None, "workspace.create", wsA)
    _insert_audit("2026-01-02T00:00:00", "B", None, "workspace.create", wsB)

    # 管理员看全量
    r = client.get("/audit/export?format=csv", headers=admin_headers)
    body = r.content.decode("utf-8-sig")
    assert wsA in body and wsB in body

    # 普通用户 A 只看自己可见域，不含他人业务域 wsB
    rA = client.get("/audit/export?format=csv", headers=hA)
    bodyA = rA.content.decode("utf-8-sig")
    assert wsA in bodyA
    assert wsB not in bodyA

    client.delete(f"/workspaces/{wsA}", headers=hA)
    client.delete(f"/workspaces/{wsB}", headers=hB)


def test_export_own_null_ws_auth_visible(client, admin_headers):
    # admin_headers 先确保已存在管理员，使后续 _make_user 必为非管理员
    hA = _make_user(client, "expA")
    me = client.get("/auth/me", headers=hA).json()
    uid = me["uid"]
    # 自己名下的 NULL 业务域认证记录（可见）
    _insert_audit("2026-03-01T00:00:00", me["username"], uid, "auth.login", None)
    # 他人名下的 NULL 业务域认证记录（不可见）
    _insert_audit("2026-03-02T00:00:00", "other", 999999, "auth.login", None)

    rA = client.get("/audit/export?format=csv", headers=hA)
    bodyA = rA.content.decode("utf-8-sig")
    assert me["username"] in bodyA
    assert "other" not in bodyA


def test_export_workspace_visibility_404(client, admin_headers):
    hA = _make_user(client, "visA")
    hB = _make_user(client, "visB")
    wsB = _make_ws(client, hB)
    # A 越权导出 B 的业务域 → 404（不暴露域是否存在）
    rA = client.get(f"/audit/export?format=csv&workspace_id={wsB}", headers=hA)
    assert rA.status_code == 404
    client.delete(f"/workspaces/{wsB}", headers=hB)


def test_export_since_until_filter(client, admin_headers):
    _insert_audit("2026-05-01T00:00:00", "x", None, "op.old", None)
    _insert_audit("2026-05-10T00:00:00", "x", None, "op.new", None)
    r = client.get("/audit/export?format=csv&since=2026-05-05",
                  headers=admin_headers)
    body = r.content.decode("utf-8-sig")
    assert "op.old" not in body
    assert "op.new" in body
