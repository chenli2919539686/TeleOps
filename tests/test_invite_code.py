"""测试注册邀请码：服务端 TELEOPS_INVITE_CODE 开启时，未带/错带邀请码应拒绝。"""
import uuid
import pytest
from fastapi.testclient import TestClient
from src.api import server as srv


@pytest.fixture
def client():
    return TestClient(srv.app)


@pytest.fixture(autouse=True)
def _restore_invite(monkeypatch):
    original = srv.INVITE_CODE
    yield
    monkeypatch.setattr(srv, "INVITE_CODE", original)


def test_auth_status_reports_invite_required(monkeypatch, client):
    monkeypatch.setattr(srv, "INVITE_CODE", "")
    r = client.get("/auth/status")
    assert r.status_code == 200
    assert r.json()["invite_required"] is False

    monkeypatch.setattr(srv, "INVITE_CODE", "SECRET-123")
    r = client.get("/auth/status")
    assert r.json()["invite_required"] is True


def test_register_without_invite_when_disabled(client):
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/auth/register", json={
        "username": f"open_{sfx}", "password": "Open123456",
    })
    assert r.status_code == 200, r.text
    assert "token" in r.json()


def test_register_with_correct_invite_when_enabled(monkeypatch, client):
    monkeypatch.setattr(srv, "INVITE_CODE", "WELCOME-2026")
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/auth/register", json={
        "username": f"inv_{sfx}", "password": "Invite123456",
        "invite_code": "WELCOME-2026",
    })
    assert r.status_code == 200, r.text
    assert "token" in r.json()


def test_register_with_wrong_invite_rejected(monkeypatch, client):
    monkeypatch.setattr(srv, "INVITE_CODE", "WELCOME-2026")
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/auth/register", json={
        "username": f"bad_{sfx}", "password": "BadCode123456",
        "invite_code": "WRONG-CODE",
    })
    assert r.status_code == 403, r.text
    assert "邀请码" in r.json().get("detail", "")


def test_register_with_empty_invite_rejected(monkeypatch, client):
    monkeypatch.setattr(srv, "INVITE_CODE", "WELCOME-2026")
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/auth/register", json={
        "username": f"empty_{sfx}", "password": "Empty1234567",
        "invite_code": "",
    })
    assert r.status_code == 403, r.text
