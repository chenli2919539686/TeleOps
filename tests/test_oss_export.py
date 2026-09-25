"""OSS 归档导出测试（配置驱动 + 本地 mock 兜底）。

覆盖：
- off 模式（默认未配置）→ /audit/archive 返回 503 并提示如何启用；
- mock 模式（TELEOPS_OSS_ENABLED=1）→ 写本地 mock 目录并返回 key/url（目录经 monkeypatch 指向 tmp_path，不碰真实 data/）；
- json 格式归档走同一隔离与字节路径；
- 隔离复用：普通用户归档仅可见域（与 /audit/export 共用 _collect_rows），不含他人业务域。

boto3 真实 s3 路径不在单测范围（缺依赖即 skip 思路已由 mock 覆盖）；
真实云连通性需用户配 TELEOPS_OSS_* 现场验证。
"""
import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_audit_export import (  # noqa: E402
    _make_user, _make_ws, _insert_audit,
)


@pytest.fixture(autouse=True)
def _clear_oss_env(monkeypatch, tmp_path):
    for k in ("TELEOPS_OSS_ENABLED", "TELEOPS_OSS_BUCKET",
              "TELEOPS_OSS_ENDPOINT", "TELEOPS_OSS_REGION",
              "TELEOPS_OSS_ACCESS_KEY", "TELEOPS_OSS_SECRET_KEY",
              "TELEOPS_OSS_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    # 把 mock 落盘目录指向临时目录，避免触碰真实 data/（沙箱禁止 rmtree 真实目录）
    import src.core.oss as oss
    mock_root = tmp_path / "oss_mock"
    mock_root.mkdir()
    monkeypatch.setattr(oss, "_MOCK_ROOT", str(mock_root))
    yield


def test_archive_off_by_default_returns_503(client, admin_headers):
    r = client.post("/audit/archive", json={"format": "csv"}, headers=admin_headers)
    assert r.status_code == 503
    assert "OSS" in r.json()["detail"] or "启用" in r.json()["detail"]


def test_oss_status_exposes_mode(client):
    r = client.get("/oss/status")
    assert r.status_code == 200
    assert r.json()["oss_mode"] == "off"


def test_archive_mock_writes_file(client, admin_headers):
    import src.core.oss as oss
    os.environ["TELEOPS_OSS_ENABLED"] = "1"
    assert oss.oss_mode() == "mock"
    wsA = _make_ws(client, admin_headers)
    _insert_audit("2026-05-01T00:00:00", "A", None, "workspace.create", wsA)

    r = client.post("/audit/archive", json={"format": "csv"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "mock"
    assert d["backend"] == "mock"
    assert d["count"] >= 1
    # 文件确实落到 mock 目录
    assert os.path.exists(d["local_path"])
    with open(d["local_path"], "rb") as f:
        content = f.read().decode("utf-8-sig")
    assert "id,ts,actor" in content  # CSV 表头
    assert "workspace.create" in content
    client.delete(f"/workspaces/{wsA}", headers=admin_headers)


def test_archive_json_format(client, admin_headers):
    import src.core.oss as oss
    os.environ["TELEOPS_OSS_ENABLED"] = "1"
    wsA = _make_ws(client, admin_headers)
    _insert_audit("2026-05-02T00:00:00", "A", None, "workspace.create", wsA)

    r = client.post("/audit/archive", json={"format": "json"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "mock"
    assert d["bytes"] > 0
    assert os.path.exists(d["local_path"])
    client.delete(f"/workspaces/{wsA}", headers=admin_headers)


def test_archive_user_scope_isolated(client, admin_headers):
    """普通用户归档只看自己可见域，不含他人业务域（隔离复用 _collect_rows）。"""
    import src.core.oss as oss
    os.environ["TELEOPS_OSS_ENABLED"] = "1"

    hA = _make_user(client, "ossA")
    wsA = _make_ws(client, hA)
    hB = _make_user(client, "ossB")
    wsB = _make_ws(client, hB)
    _insert_audit("2026-05-03T00:00:00", "A", None, "workspace.create", wsA)
    _insert_audit("2026-05-03T00:00:01", "B", None, "workspace.create", wsB)

    rA = client.post("/audit/archive", json={"format": "csv"}, headers=hA)
    assert rA.status_code == 200, rA.text
    with open(rA.json()["local_path"], "rb") as f:
        bodyA = f.read().decode("utf-8-sig")
    # 按列解析，只检查 workspace_id 列，避免 detail JSON 中嵌入 id 造成误判
    rows = list(csv.DictReader(bodyA.splitlines()))
    ws_ids = {row["workspace_id"] for row in rows}
    assert wsA in ws_ids
    assert wsB not in ws_ids  # 他人业务域不泄露
    # 且仅 A 自身认证类记录（actor_id 维度）可见；本例无 auth 记录，仅校验域维度

    client.delete(f"/workspaces/{wsA}", headers=hA)
    client.delete(f"/workspaces/{wsB}", headers=hB)


def test_archive_requires_auth(client):
    r = client.post("/audit/archive", json={})
    # 未登录 → 401（中间件对写接口强制校验）
    assert r.status_code in (401, 403)
