# -*- coding: utf-8 -*-
"""Caddyfile 与 caddy_runner 单元测试（v0.8.13 / v0.8.22 隔离加固）。

覆盖：
1. Caddyfile 存在且语法骨架合法（443 listener + reverse_proxy 127.0.0.1:8000）
2. caddy_runner._resolve_caddy_exe 能在 PATH/常见路径找到 caddy（如已装）
3. ensure_caddy_binary 返回正确布尔值
4. _port_listening / _read_pid / find_pid_by_port / _pid_is_caddy 健壮性

隔离约定：所有会读写 PID 文件的用例必须 monkeypatch 到 tmp_path，
绝不允许触碰生产 data/.caddy.pid（测试曾直接删生产文件导致 caddy
status 误报未运行）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_caddyfile_template_exists_and_skeleton_valid():
    """Caddyfile 模板必须存在，关键字段齐全。"""
    caddyfile = ROOT / "scripts" / "Caddyfile"
    assert caddyfile.exists(), f"Caddyfile 缺失：{caddyfile}"
    text = caddyfile.read_text(encoding="utf-8")
    # 站点地址明确列出 IP/主机名，让 internal 证书覆盖 SAN
    assert "127.0.0.1, localhost, {lan_ip}" in text, "Caddyfile 应覆盖 127.0.0.1 / localhost / {lan_ip}"
    # 自签证书
    assert "tls internal" in text, "应使用 tls internal 自签证书"
    # 反代到后端
    assert "127.0.0.1:8000" in text, "应反代到 127.0.0.1:8000"
    assert "reverse_proxy" in text, "应使用 reverse_proxy 指令"


def test_render_caddyfile_replaces_lan_ip():
    """_render_caddyfile 把 {lan_ip} 替换为真实局域网 IP。"""
    from scripts import caddy_runner
    runtime = caddy_runner._render_caddyfile()
    text = runtime.read_text(encoding="utf-8")
    assert "{lan_ip}" not in text, "运行时 Caddyfile 不应残留占位符"
    assert caddy_runner.get_lan_ip() in text, "运行时 Caddyfile 应包含当前局域网 IP"
    # 监听 443（站点块内会隐式监听默认 HTTPS 端口）
    assert "127.0.0.1, localhost," in text, "站点地址应保持三元素格式"


def test_caddy_runner_resolves_path():
    """caddy_runner._resolve_caddy_exe 不抛异常，未安装时返回 None。"""
    from scripts import caddy_runner
    # 函数不应抛异常
    exe = caddy_runner._resolve_caddy_exe()
    # 不强求找到 caddy（取决于测试机是否装），但若返回必须是 Path
    if exe is not None:
        assert isinstance(exe, Path)
        assert exe.exists()
        assert exe.stat().st_size > 1_000_000  # Caddy 至少 30MB


def test_ensure_caddy_binary_returns_bool():
    """ensure_caddy_binary 返回 bool，不抛异常。"""
    from scripts import caddy_runner
    result = caddy_runner.ensure_caddy_binary()
    assert isinstance(result, bool)


def test_port_listening_safe_with_invalid_port():
    """_port_listening 对无效端口不应抛异常。"""
    from scripts import caddy_runner
    # 大概率没人在 1 上监听
    result = caddy_runner._port_listening(1)
    assert isinstance(result, bool)


def test_pid_handling_when_no_pid_file(tmp_path, monkeypatch):
    """无 PID 文件 / 内容非法时 _read_pid 返回 None。

    用 tmp_path 隔离：绝不触碰生产 data/.caddy.pid。
    （此前直接 unlink 生产 PID 文件，导致运行中的 Caddy 被 status 误报
    「未运行」——测试污染生产状态的真实缺陷。）
    """
    from scripts import caddy_runner
    monkeypatch.setattr(caddy_runner, "PID_FILE", tmp_path / ".caddy.pid")

    # 不存在 → None
    assert caddy_runner._read_pid() is None
    # 内容非法 → None（不抛异常）
    (tmp_path / ".caddy.pid").write_text("not-a-number", encoding="utf-8")
    assert caddy_runner._read_pid() is None
    # 空文件 → None
    (tmp_path / ".caddy.pid").write_text("", encoding="utf-8")
    assert caddy_runner._read_pid() is None
    # 正常数字 → int
    (tmp_path / ".caddy.pid").write_text("12345", encoding="utf-8")
    assert caddy_runner._read_pid() == 12345


def test_find_pid_by_port_returns_int_or_none():
    """find_pid_by_port 对任意端口不抛异常，返回 int 或 None。"""
    from scripts import caddy_runner
    # 1 号端口大概率无人监听
    result = caddy_runner.find_pid_by_port(1)
    assert result is None or isinstance(result, int)


def test_pid_is_caddy_safe_with_bogus_pid():
    """_pid_is_caddy 对不存在的 PID 返回 False，不抛异常（防误杀守门员）。"""
    from scripts import caddy_runner
    # 4194303 是 Windows 用户态 PID 上限附近，几乎不可能存在
    assert caddy_runner._pid_is_caddy(4194303) is False
    assert caddy_runner._pid_is_caddy(None) is False
    assert caddy_runner._pid_is_caddy(0) is False


def test_caddy_status_returns_tuple(tmp_path, monkeypatch):
    """caddy_status 返回 (bool, dict) 元组。

    monkeypatch PID_FILE 到 tmp_path：status 的自愈逻辑会回写 PID 文件，
    必须隔离，避免测试期间改动生产状态。
    """
    from scripts import caddy_runner
    monkeypatch.setattr(caddy_runner, "PID_FILE", tmp_path / ".caddy.pid")
    running, info = caddy_runner.caddy_status()
    assert isinstance(running, bool)
    assert isinstance(info, dict)
    for key in ("pid", "port_443", "http", "port_pid", "pid_file_stale", "pid_file_healed"):
        assert key in info, f"info 缺少字段 {key}"
    assert info["http"] == "127.0.0.1:8000"