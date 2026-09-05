# -*- coding: utf-8 -*-
"""Caddy HTTPS 反代启停管理（v0.8.13）。

依赖：
- tools/caddy.exe（Caddy Windows 二进制，由 caddy_setup.py 部署）
- scripts/Caddyfile（配置模板）
- 后端必须先在 127.0.0.1:8000 运行

用法（一般通过 teleops_ctl.py caddy 调用）：
    ensure_caddy_binary()  → bool       是否就绪（不存在时返回 False 并打印提示）
    caddy_start()          → (ok, msg)  启动后台进程
    caddy_status()         → (running, info_dict)
    caddy_stop()           → bool
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
CADDY_EXE = TOOLS_DIR / "caddy.exe"
CADDYFILE = Path(__file__).resolve().parent / "Caddyfile"
PID_FILE = PROJECT_ROOT / "data" / ".caddy.pid"
LOG_FILE = PROJECT_ROOT / "data" / "caddy_server.log"
BACKEND_HTTP = "127.0.0.1:8000"
LISTEN_HTTPS_PORT = 443

_DETACHED_NO_WINDOW = 0x00000008 | 0x08000000


def _err_print(msg):
    print(f"[ERR] {msg}", file=sys.stderr)


def _resolve_caddy_exe() -> Path | None:
    """寻找 caddy.exe：优先 tools/caddy.exe，其次 PATH 与常见全局安装位置。"""
    if CADDY_EXE.exists() and CADDY_EXE.stat().st_size > 1_000_000:
        return CADDY_EXE
    # PATH
    import shutil
    p = shutil.which("caddy")
    if p and Path(p).exists() and Path(p).stat().st_size > 1_000_000:
        return Path(p)
    # winget 默认位置
    candidates = [
        Path(r"C:/Program Files/caddy/caddy.exe"),
        Path(r"C:/Program Files (x86)/caddy/caddy.exe"),
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 1_000_000:
            return c
    return None


def ensure_caddy_binary() -> bool:
    """检查 caddy.exe 是否就绪（tools 优先，回退 PATH/全局）。"""
    if _resolve_caddy_exe() is not None:
        return True
    _err_print(f"Caddy 二进制缺失：tools/caddy.exe 与 PATH 都找不到")
    _err_print("请先运行：python scripts/caddy_setup.py（自动下载到 tools/）")
    _err_print("或运行：winget install CaddyServer.Caddy")
    return False


def get_caddy_exe() -> Path:
    """获取实际可用的 caddy.exe 路径，假定已 ensure。"""
    p = _resolve_caddy_exe()
    assert p is not None
    return p


def _is_pid_alive(pid):
    if not pid or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        v = int(PID_FILE.read_text(encoding="utf-8").strip() or 0) or None
        return v
    except (ValueError, OSError):
        return None


def _port_listening(port):
    """检查端口是否被监听。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def caddy_status():
    """返回 (running: bool, info: dict)。"""
    pid = _read_pid()
    alive = _is_pid_alive(pid) if pid else False
    port_up = _port_listening(LISTEN_HTTPS_PORT)
    running = alive and port_up
    info = {
        "pid": pid,
        "alive": alive,
        "port_443": port_up,
        "exe": str(_resolve_caddy_exe() or CADDY_EXE),
        "http": BACKEND_HTTP,
    }
    return running, info


def caddy_start():
    """启动后台 Caddy。返回 (ok: bool, msg: str)。"""
    if not ensure_caddy_binary():
        return False, "caddy.exe 未就绪"

    # 已经在跑就跳过
    running, info = caddy_status()
    if running:
        return True, f"已在运行（PID={info['pid']}）"

    # 但端口被占用（且不是我们），报错
    if info["port_443"] and not info["alive"]:
        return False, "443 端口被其它进程占用，先停掉"

    if not CADDYFILE.exists():
        return False, f"Caddyfile 缺失：{CADDYFILE}"

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)

    exe = get_caddy_exe()
    cmd = [
        str(exe), "run",
        "--config", str(CADDYFILE),
        "--adapter", "",  # 默认 native
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_DETACHED_NO_WINDOW if sys.platform == "win32" else 0,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    # 等端口 ready（最长 20 秒）
    for i in range(40):
        time.sleep(0.5)
        if not _is_pid_alive(proc.pid):
            return False, f"进程启动后立即退出，查看日志：{LOG_FILE}"
        if _port_listening(LISTEN_HTTPS_PORT):
            return True, f"PID={proc.pid} 端口 443 已就绪（{(i+1)*0.5:.1f}s）"
    return False, "20 秒内未监听 443 端口"


def caddy_stop():
    """停止后台 Caddy。"""
    pid = _read_pid()
    if not pid:
        return False
    if not _is_pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return False
    try:
        subprocess.check_call(["taskkill", "/F", "/PID", str(pid)])
    except subprocess.CalledProcessError:
        pass
    PID_FILE.unlink(missing_ok=True)
    # 给端口一点释放时间
    time.sleep(0.5)
    return True