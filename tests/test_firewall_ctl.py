# -*- coding: utf-8 -*-
"""teleops_ctl firewall 子命令的纯函数测试（v0.8.11）。

只测参数构造逻辑（不真调 netsh，需要管理员权限），保证：
1. 默认 RFC1918 白名单段齐全（127 + 192.168 + 10 + 172.16）
2. 子网掩码格式正确
3. 端口默认 8000
4. 自定义 TELEOPS_FIREWALL_ALLOWED 覆盖生效
5. delete/show 命令构造正确
"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_ctl():
    """导入 ctl 模块，环境变量改了要 reload。"""
    if "scripts.teleops_ctl" in sys.modules:
        return importlib.reload(sys.modules["scripts.teleops_ctl"])
    return importlib.import_module("scripts.teleops_ctl")


def _find_param_value(args, key):
    """在 netsh 风格参数列表中找到 key=... 项并返回 value；找不到返回 None。"""
    prefix = key + "="
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def test_default_firewall_allowlist_rfc1918():
    """默认白名单覆盖 RFC1918 全部私网段。"""
    os.environ.pop("TELEOPS_FIREWALL_ALLOWED", None)
    ctl = _load_ctl()
    args = ctl._build_firewall_add_args()
    allowed = _find_param_value(args, "remoteip")
    assert allowed is not None, f"缺少 remoteip 参数：{args}"
    parts = allowed.split(",")
    # 必须包含以下四段
    for must in ("127.0.0.1", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"):
        assert must in parts, f"白名单缺少 {must}，当前 {parts}"
    assert len(parts) == 4, f"白名单段数应恰好 4，实际 {len(parts)}"


def test_firewall_port_default_8000():
    """默认端口 8000（与 PORT 常量一致）。"""
    os.environ.pop("TELEOPS_PORT", None)
    ctl = _load_ctl()
    args = ctl._build_firewall_add_args()
    port = _find_param_value(args, "localport")
    assert port == "8000", f"默认端口应为 8000，实际 {port}"


def test_firewall_port_override():
    """TELEOPS_PORT 自定义应生效。"""
    os.environ["TELEOPS_PORT"] = "9000"
    try:
        ctl = _load_ctl()
        args = ctl._build_firewall_add_args()
        port = _find_param_value(args, "localport")
        assert port == "9000"
    finally:
        os.environ.pop("TELEOPS_PORT", None)


def test_firewall_custom_allowed_override():
    """TELEOPS_FIREWALL_ALLOWED 自定义段覆盖默认。"""
    os.environ["TELEOPS_FIREWALL_ALLOWED"] = "192.168.100.0/24,10.0.0.5"
    try:
        ctl = _load_ctl()
        args = ctl._build_firewall_add_args()
        allowed = _find_param_value(args, "remoteip")
        assert allowed == "192.168.100.0/24,10.0.0.5"
    finally:
        os.environ.pop("TELEOPS_FIREWALL_ALLOWED", None)


def test_delete_and_status_args_correct():
    """delete / show 命令参数齐整。"""
    ctl = _load_ctl()
    del_args = ctl._build_firewall_delete_args()
    assert del_args[:4] == ["netsh", "advfirewall", "firewall", "delete"]
    assert del_args[4] == "rule"
    assert "name=TeleOps 8000 (LAN private)" in del_args

    show_args = ctl._build_firewall_status_args()
    assert show_args[:4] == ["netsh", "advfirewall", "firewall", "show"]
    assert show_args[4] == "rule"
    assert "name=TeleOps 8000 (LAN private)" in show_args


def test_add_rule_name_and_direction():
    """入站规则 + TCP 协议 + 放行动作。"""
    ctl = _load_ctl()
    args = ctl._build_firewall_add_args()
    assert "dir=in" in args
    assert "action=allow" in args
    assert "protocol=TCP" in args
    assert "name=TeleOps 8000 (LAN private)" in args