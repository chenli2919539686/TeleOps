# -*- coding: utf-8 -*-
"""Caddy HTTPS 反代启停管理（v0.8.13 / v0.8.22 状态自愈加固）。

依赖：
- tools/caddy.exe（Caddy Windows 二进制，由 caddy_setup.py 部署）
- scripts/Caddyfile（配置模板）
- 后端必须先在 127.0.0.1:8000 运行

用法（一般通过 teleops_ctl.py caddy 调用）：
    ensure_caddy_binary()  → bool       是否就绪（不存在时返回 False 并打印提示）
    caddy_start()          → (ok, msg)  启动后台进程
    caddy_status()         → (running, info_dict)  端口为准，PID 文件丢失时自愈
    caddy_stop()           → bool       PID 文件 + 端口反查双保险
    find_pid_by_port(port) → int|None   netstat 反查监听进程
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
RUNTIME_CADDYFILE = PROJECT_ROOT / "data" / ".caddy.Caddyfile"
BACKEND_HTTP = "127.0.0.1:8000"
LISTEN_HTTPS_PORT = 443

_DETACHED_NO_WINDOW = 0x00000008 | 0x08000000


def get_lan_ip() -> str:
    """获取本机局域网 IP，用于 Caddy 证书 SAN。失败回退 127.0.0.1。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        # 连接一个不会真实发送数据的公网地址，用来选对本网卡 IP
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _render_caddyfile() -> Path:
    """将模板 Caddyfile 中的 {lan_ip} / {upstreams} 替换为实际值，写入运行时配置。

    - {lan_ip}: 当前局域网 IP（用于 internal 证书 SAN）
    - {upstreams}: 反代后端列表，空格分隔；默认 127.0.0.1:8000（单节点）；
      多副本部署用环境变量 TELEOPS_BACKENDS 指定（如 "127.0.0.1:8000 127.0.0.1:8001"）
    """
    template = CADDYFILE.read_text(encoding="utf-8")
    lan_ip = get_lan_ip()
    rendered = template.replace("{lan_ip}", lan_ip)
    backends = os.environ.get("TELEOPS_BACKENDS", "127.0.0.1:8000").strip() or "127.0.0.1:8000"
    rendered = rendered.replace("{upstreams}", backends)
    RUNTIME_CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CADDYFILE.write_text(rendered, encoding="utf-8")
    return RUNTIME_CADDYFILE


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


def find_pid_by_port(port):
    """netstat -ano 反查监听端口的 PID。找不到返回 None。"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            text=True,
            encoding="gbk",
            errors="ignore",
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    needle = f":{port}"
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            try:
                return int(parts[-1])
            except (ValueError, IndexError):
                continue
    return None


def _pid_is_caddy(pid):
    """tasklist 判断 PID 是否是 caddy.exe（用于防误杀其它 443 服务）。"""
    if not pid or pid <= 0:
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            text=True,
            encoding="gbk",
            errors="ignore",
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    # 无匹配时 tasklist 输出 "INFO: No tasks are running..."，不含 caddy
    return "caddy" in out.lower()


def caddy_status():
    """返回 (running: bool, info: dict)。

    判断口径：以「443 端口在监听」为准，PID 文件只是辅助。
    PID 文件可能丢失（被清理/误删，如测试误删）或与端口实际持有者不一致，
    若只信 PID 文件会误报「未运行」，进而导致 caddy off 停不掉进程。

    自愈：端口在监听且持有者是 caddy 时，把反查到的真实 PID 回写 PID 文件，
    这样后续 caddy off / status 都恢复正常，无需人工干预。
    """
    pid = _read_pid()
    alive = _is_pid_alive(pid) if pid else False
    port_up = _port_listening(LISTEN_HTTPS_PORT)
    port_pid = find_pid_by_port(LISTEN_HTTPS_PORT) if port_up else None

    healed = False
    # PID 记录缺失/过期，但 443 由 caddy 持有 → 回写真实 PID 自愈
    if port_pid and port_pid != pid and _pid_is_caddy(port_pid):
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(port_pid), encoding="utf-8")
            healed = True
        except OSError:
            pass
        pid = port_pid
        alive = True

    running = port_up or alive
    info = {
        "pid": pid,
        "alive": alive,
        "port_443": port_up,
        "port_pid": port_pid,
        # 端口在监听但 PID 记录不可用（未自愈成功，如持有者非 caddy）
        "pid_file_stale": bool(port_up and not healed and not alive),
        "pid_file_healed": healed,
        "exe": str(_resolve_caddy_exe() or CADDY_EXE),
        "http": os.environ.get("TELEOPS_BACKENDS", BACKEND_HTTP),
    }
    return running, info


def caddy_start():
    """启动后台 Caddy。返回 (ok: bool, msg: str)。"""
    if not ensure_caddy_binary():
        return False, "caddy.exe 未就绪"

    # 已经在跑就跳过（端口在监听，或 PID 活着且确实是 caddy 进程）
    running, info = caddy_status()
    if running and (info["port_443"] or _pid_is_caddy(info["pid"])):
        return True, f"已在运行（PID={info['pid']}）"

    # 但端口被占用（且不是我们），报错
    if info["port_443"] and not info["alive"]:
        return False, "443 端口被其它进程占用，先停掉"

    if not CADDYFILE.exists():
        return False, f"Caddyfile 缺失：{CADDYFILE}"

    # 生成运行时 Caddyfile（替换 {lan_ip}）
    runtime_cfg = _render_caddyfile()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)

    exe = get_caddy_exe()
    cmd = [
        str(exe), "run",
        "--config", str(runtime_cfg),
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
    """停止后台 Caddy。

    双保险：PID 文件 + 端口反查。PID 文件丢失时（被清理/误删），
    通过 netstat 找到 443 的实际持有者杀掉；仅当确认是 caddy 进程才杀，
    防止误杀占用 443 的其它服务（如其它 HTTPS 反代）。
    """
    pid = _read_pid()
    targets = []
    if pid and _is_pid_alive(pid):
        targets.append(pid)
    port_pid = find_pid_by_port(LISTEN_HTTPS_PORT)
    if port_pid and port_pid not in targets and _pid_is_caddy(port_pid):
        targets.append(port_pid)

    if not targets:
        PID_FILE.unlink(missing_ok=True)
        return False

    killed = False
    for t in targets:
        # 前一次 taskkill 可能已把后续目标一并终止，跳过已死的避免误报
        if not _is_pid_alive(t):
            continue
        try:
            subprocess.check_call(["taskkill", "/F", "/T", "/PID", str(t)])
            killed = True
        except subprocess.CalledProcessError:
            pass
    PID_FILE.unlink(missing_ok=True)
    time.sleep(0.5)
    # 以「端口已释放」为最终成功标准：哪怕一个都没杀成，端口空了就算停了
    return killed or not _port_listening(LISTEN_HTTPS_PORT)