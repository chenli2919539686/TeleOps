# -*- coding: utf-8 -*-
"""Caddyfile 与 caddy_runner 单元测试（v0.8.13）。

覆盖：
1. Caddyfile 存在且语法骨架合法（443 listener + reverse_proxy 127.0.0.1:8000）
2. caddy_runner._resolve_caddy_exe 能在 PATH/常见路径找到 caddy（如已装）
3. ensure_caddy_binary 返回正确布尔值
4. _port_listening / _read_pid 健壮性
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_caddyfile_exists_and_skeleton_valid():
    """Caddyfile 必须存在，关键字段齐全。"""
    caddyfile = ROOT / "scripts" / "Caddyfile"
    assert caddyfile.exists(), f"Caddyfile 缺失：{caddyfile}"
    text = caddyfile.read_text(encoding="utf-8")
    # 监听 443
    assert ":443 {" in text, "Caddyfile 应监听 :443"
    # 自签证书
    assert "tls internal" in text, "应使用 tls internal 自签证书"
    # 反代到后端
    assert "127.0.0.1:8000" in text, "应反代到 127.0.0.1:8000"
    assert "reverse_proxy" in text, "应使用 reverse_proxy 指令"


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


def test_pid_handling_when_no_pid_file():
    """无 PID 文件时 _read_pid 返回 None。"""
    from scripts import caddy_runner
    # 确保 pid 文件不存在（测试期间）
    if caddy_runner.PID_FILE.exists():
        caddy_runner.PID_FILE.unlink()
    assert caddy_runner._read_pid() is None


def test_caddy_status_returns_tuple():
    """caddy_status 返回 (bool, dict) 元组。"""
    from scripts import caddy_runner
    running, info = caddy_runner.caddy_status()
    assert isinstance(running, bool)
    assert isinstance(info, dict)
    assert "pid" in info
    assert "port_443" in info
    assert "http" in info
    assert info["http"] == "127.0.0.1:8000"