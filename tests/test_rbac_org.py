# -*- coding: utf-8 -*-
"""Phase 1 回归：组织树 + 标准 RBAC 的可见性语义锁定。

说明（与 auth.enforce 文档一致）：可见性采用「自上而下」模型——
用户能看到「自己所属 org 及其所有子孙 org」绑定的业务域；兄弟 org / 无交集
org 之间互相不可见；super_admin 看全部。个人域仍按 owner_id 单独隔离。

本用例直接构造组织树与用户，验证 workspace_store._visible_to_user 与
auth.enforce 的判定，防止后续重构破坏隔离契约。
"""
import uuid

import pytest

from src.api import server
from src.core import db, auth

ws_store = server.ws_store


def _sfx():
    return uuid.uuid4().hex[:6]


def _user(name, org_id, is_admin=False):
    return auth.create_user(name, "Pytest123456", is_admin=is_admin, org_id=org_id)


@pytest.fixture
def org_tree():
    """构造组织树：grp-root -> {grp-eng -> grp-eng-1, grp-fin}。"""
    db.execute("INSERT OR IGNORE INTO org_units (id,name,parent_id,path,level,org_type) "
               "VALUES (?,?,?,?,?,?)",
               ("grp-eng", "工程部", db.ROOT_ORG_ID, "/" + db.ROOT_ORG_ID + "/grp-eng", 1, "dept"))
    db.execute("INSERT OR IGNORE INTO org_units (id,name,parent_id,path,level,org_type) "
               "VALUES (?,?,?,?,?,?)",
               ("grp-eng-1", "工程一部", "grp-eng",
                "/" + db.ROOT_ORG_ID + "/grp-eng/grp-eng-1", 2, "team"))
    db.execute("INSERT OR IGNORE INTO org_units (id,name,parent_id,path,level,org_type) "
               "VALUES (?,?,?,?,?,?)",
               ("grp-fin", "财务部", db.ROOT_ORG_ID, "/" + db.ROOT_ORG_ID + "/grp-fin", 1, "dept"))
    yield
    for oid in ("grp-eng-1", "grp-eng", "grp-fin"):
        db.execute("DELETE FROM org_units WHERE id=?", (oid,))


def test_super_admin_sees_all_workspaces(org_tree):
    """super_admin 不受组织树限制，可见任意业务域。"""
    admin = _user("rbac_admin_" + _sfx(), db.ROOT_ORG_ID, is_admin=True)
    wid = ws_store.create("admin可见域", "alert-prometheus", "auto",
                          owner_id=None, org_id="grp-eng")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, admin) is True
    finally:
        ws_store.delete_workspace(wid)


def test_org_member_sees_own_org_workspace(org_tree):
    """同 org 用户（sre，含 ws.view）能看到绑定到本 org 的业务域。"""
    u = _user("rbac_eng_" + _sfx(), "grp-eng")
    wid = ws_store.create("工程部域", "alert-prometheus", "auto",
                          owner_id=None, org_id="grp-eng")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, u) is True
    finally:
        ws_store.delete_workspace(wid)


def test_org_ancestor_sees_descendant_workspace(org_tree):
    """父 org 用户能看到子 org 绑定的业务域（自上而下管理视图）。"""
    u = _user("rbac_engmgr_" + _sfx(), "grp-eng")
    wid = ws_store.create("工程一部域", "alert-prometheus", "auto",
                          owner_id=None, org_id="grp-eng-1")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, u) is True
    finally:
        ws_store.delete_workspace(wid)


def test_sibling_org_cannot_see_each_other(org_tree):
    """兄弟 org（财务部）看不到工程部绑定的业务域。"""
    u = _user("rbac_fin_" + _sfx(), "grp-fin")
    wid = ws_store.create("工程部域2", "alert-prometheus", "auto",
                          owner_id=None, org_id="grp-eng")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, u) is False
    finally:
        ws_store.delete_workspace(wid)


def test_descendant_cannot_see_ancestor_org_workspace(org_tree):
    """子 org 用户看不到父 org 绑定的业务域（自上而下模型：仅祖先可见子孙）。

    若产品后续需要「子也能看父的共享域」，应改 auth.enforce 的 path 判定方向。
    """
    u = _user("rbac_eng1_" + _sfx(), "grp-eng-1")
    wid = ws_store.create("工程部域3", "alert-prometheus", "auto",
                          owner_id=None, org_id="grp-eng")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, u) is False
    finally:
        ws_store.delete_workspace(wid)


def test_personal_workspace_owner_only(org_tree):
    """个人域按 owner_id 隔离：仅本人可见，跨 org 的其他用户不可见。

    注：若业务域同时绑定了 owner 所在 org（org_id），则同 org 其他成员会经由
    组织 RBAC 看到它——这是设计内的。真正的「仅本人」靠用户专属个人 org（唯一）。
    本用例用跨 org 用户验证隔离契约。
    """
    owner = _user("rbac_owner_" + _sfx(), "grp-eng")
    other = _user("rbac_other_" + _sfx(), "grp-fin")  # 不同 org
    wid = ws_store.create("个人域", "alert-prometheus", "auto",
                          owner_id=owner["id"], org_id="grp-eng")["id"]
    try:
        w = db.query_one("SELECT owner_id, org_id FROM workspaces WHERE id=?", (wid,))
        assert ws_store._visible_to_user(w, owner) is True
        assert ws_store._visible_to_user(w, other) is False
    finally:
        ws_store.delete_workspace(wid)
