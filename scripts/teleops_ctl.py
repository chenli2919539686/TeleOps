#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TeleOps 后端进程管理脚本

子命令：
  start      后台启动 uvicorn（绑 TELEOPS_HOST，默认 0.0.0.0:8000）
  stop       停止后台进程（按 PID 文件 + 端口兜底）
  restart    先停再起
  status     查看进程 / 端口 / 健康 / 局域网 URL
  logs       tail 最近 50 行服务日志
  url        仅打印局域网访问地址
  firewall   防火墙白名单：on 放行 RFC1918 私网段 / off 删除 / status 查看（需管理员）
  toggle     运行中则停，停则起
  caddy      Caddy HTTPS 反代：on / off / status

进程信息持久化：
  data/.server.pid             当前进程 PID
  data/teleops_server.log      uvicorn stdout/stderr

用法：
  python scripts/teleops_ctl.py start
  python scripts/teleops_ctl.py status
"""

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = Path(r"C:/Users/Chenl/.workbuddy/binaries/python/envs/teleops/Scripts/python.exe")
PID_FILE = PROJECT_ROOT / "data" / ".server.pid"
LOG_FILE = PROJECT_ROOT / "data" / "teleops_server.log"
HOST = os.environ.get("TELEOPS_HOST", "0.0.0.0")
PORT = int(os.environ.get("TELEOPS_PORT", "8000"))

# Windows process flag 组合：不弹控制台窗口 + 脱离父进程（父退子不退）
_DETACHED_NO_WINDOW = 0x00000008 | 0x08000000

# 防火墙白名单：仅放行 RFC1918 私网段（家庭/小企业/校园 LAN）+ 本机 loopback。
# 拒绝任何公网 IP 直连。TELEOPS_FIREWALL_ALLOWED 可自定义（逗号分隔 CIDR）。
_FW_RULE_NAME = "TeleOps 8000 (LAN private)"
_FW_DEFAULT_ALLOWED = "127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
_FW_ALLOWED = os.environ.get("TELEOPS_FIREWALL_ALLOWED", _FW_DEFAULT_ALLOWED)

# ANSI 颜色（PowerShell/新版终端支持；老 cmd 会显示乱码但不影响功能）
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GRAY = "\033[90m"
_RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"{_GREEN}[OK]{_RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"{_YELLOW}[WARN]{_RESET} {msg}")


def _err(msg: str) -> None:
    print(f"{_RED}[ERR]{_RESET} {msg}")


def _info(msg: str) -> None:
    print(f"{_GRAY}[INFO]{_RESET} {msg}")


def get_lan_ip() -> str:
    """通过 UDP 连接 8.8.8.8 获取本机出网网卡 IP（不发实际包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip() or 0) or None
    except (ValueError, OSError):
        return None


def is_pid_alive(pid: int | None) -> bool:
    """Windows: 用 OpenProcess 判断 PID 是否还活着"""
    if pid is None or pid <= 0:
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


def find_pid_by_port(port: int) -> int | None:
    """通过 netstat -ano 找监听端口的 PID"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            text=True,
            encoding="gbk",
            errors="ignore",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
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


def cmd_start(quiet: bool = False) -> None:
    pid = read_pid()
    if pid and is_pid_alive(pid):
        _ok(f"已在运行 PID={pid}（端口 {PORT}）")
        _print_url()
        return

    # 端口兜底：PID 文件丢了但端口还被人占着，先杀掉
    port_pid = find_pid_by_port(PORT)
    if port_pid and (pid is None or port_pid != pid):
        _warn(f"端口 {PORT} 被 PID={port_pid} 占用（非本服务进程），先 kill")
        _kill_pid(port_pid)
        time.sleep(1)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["TELEOPS_HOST"] = HOST
    env["TELEOPS_PORT"] = str(PORT)

    cmd = [
        str(PYTHON_BIN),
        "-m",
        "uvicorn",
        "src.api.server:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_DETACHED_NO_WINDOW if sys.platform == "win32" else 0,
        env=env,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    if not quiet:
        _ok(f"已启动 PID={proc.pid} → 监听 {HOST}:{PORT}（日志: {LOG_FILE.name}）")

    # 等服务 ready
    for i in range(15):
        time.sleep(0.5)
        if not is_pid_alive(proc.pid):
            _err(f"进程启动后立即退出，查看日志: {LOG_FILE}")
            return
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                if not quiet:
                    _ok(f"端口 {PORT} 已就绪（耗时 {(i + 1) * 0.5:.1f}s）")
                _print_url()
                return
        except OSError:
            continue
    _warn("15 秒内未拿到端口，可能启动中；可稍后用 status 复查")


def _kill_pid(pid: int) -> bool:
    try:
        subprocess.check_call(["taskkill", "/F", "/PID", str(pid)])
        return True
    except subprocess.CalledProcessError:
        return False


def cmd_stop() -> None:
    pid = read_pid()
    if not pid or not is_pid_alive(pid):
        # 兜底按端口找
        port_pid = find_pid_by_port(PORT)
        if port_pid:
            _warn(f"PID 文件失效，按端口找到 PID={port_pid}，尝试停止")
            pid = port_pid
        else:
            _info("未在运行")
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
            return
    if _kill_pid(pid):
        _ok(f"已停止 PID={pid}")
    else:
        _err(f"停止 PID={pid} 失败")
    PID_FILE.unlink(missing_ok=True)


def cmd_restart() -> None:
    cmd_stop()
    time.sleep(0.5)
    cmd_start()


def cmd_status() -> None:
    pid = read_pid()
    alive = is_pid_alive(pid) if pid else False
    port_pid = find_pid_by_port(PORT)
    port_alive = port_pid is not None

    print("=" * 50)
    print(f" 进程 PID 文件: {pid or '-'}    实际存活: {alive}")
    print(f" 端口 {PORT} 占用 PID: {port_pid or '-'}    监听中: {port_alive}")
    print(f" 绑定地址: {HOST}:{PORT}")
    print(f" 日志文件: {LOG_FILE}（{'存在' if LOG_FILE.exists() else '尚未生成'}）")
    print("=" * 50)

    if port_alive:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                body = r.read().decode()
                print(f" 健康检查 /health:")
                for line in body.split(","):
                    print(f"   {line.strip()}")
        except Exception as e:
            _warn(f"健康检查失败: {e}")
        _print_url()


def cmd_logs(lines: int = 50) -> None:
    if not LOG_FILE.exists():
        _info("日志文件不存在（服务可能从未启动）")
        return
    text = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    chunks = text.splitlines()[-lines:]
    print("\n".join(chunks))


def cmd_url() -> None:
    _print_url()


def _print_url() -> None:
    ip = get_lan_ip()
    print(f" 本机访问:   http://127.0.0.1:{PORT}")
    print(f" 局域网访问: http://{ip}:{PORT}")


def _build_firewall_add_args() -> list:
    """构造 netsh advfirewall add rule 命令参数列表（纯函数，便于测试）。"""
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={_FW_RULE_NAME}",
        "dir=in", "action=allow",
        "protocol=TCP",
        f"localport={PORT}",
        f"remoteip={_FW_ALLOWED}",
    ]


def _build_firewall_delete_args() -> list:
    return ["netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={_FW_RULE_NAME}"]


def _build_firewall_status_args() -> list:
    return ["netsh", "advfirewall", "firewall", "show", "rule",
            f"name={_FW_RULE_NAME}"]


def _firewall_delete() -> bool:
    """删除白名单规则（不存在也不报错）。"""
    del_cmd = _build_firewall_delete_args()
    res = subprocess.run(del_cmd, capture_output=True, text=True, encoding="gbk",
                         errors="ignore")
    # netsh delete 在规则不存在时仍返回 0，但输出"没有与指定条件相匹配的规则。"
    return res.returncode == 0


def _firewall_add() -> bool:
    """添加白名单规则（先删后加，避免重复）。"""
    _firewall_delete()
    add_cmd = _build_firewall_add_args()
    print("  ", " ".join(add_cmd))
    res = subprocess.run(add_cmd, capture_output=True, text=True, encoding="gbk",
                         errors="ignore")
    return res.returncode == 0


def _firewall_status() -> bool:
    """返回规则是否存在。"""
    show_cmd = _build_firewall_status_args()
    res = subprocess.run(show_cmd, capture_output=True, text=True, encoding="gbk",
                         errors="ignore")
    return res.returncode == 0 and _FW_RULE_NAME in res.stdout


def cmd_firewall(action: str = "on") -> None:
    """防火墙白名单管理。

    on    默认放行 RFC1918 私网段（127.0.0.1 + 192.168.0.0/16 + 10.0.0.0/8 + 172.16.0.0/12）
    off   删除白名单规则（恢复默认阻断）
    status 查看当前规则是否存在
    """
    if action == "status":
        if _firewall_status():
            _ok(f"白名单规则「{_FW_RULE_NAME}」已生效")
            print(f"  放行段: {_FW_ALLOWED}")
        else:
            _warn(f"白名单规则「{_FW_RULE_NAME}」不存在，公网/私网访问均可能被 Windows 默认策略拦截")
        return

    if action == "off":
        if _firewall_delete():
            _ok(f"已删除白名单规则「{_FW_RULE_NAME}」（8000 端口恢复默认阻断）")
        else:
            _warn("规则删除失败或不存在")
        return

    # on：先删后加
    print(f"尝试添加白名单（需管理员权限；自定义段可用环境变量 TELEOPS_FIREWALL_ALLOWED）:")
    if _firewall_add():
        _ok(f"白名单规则「{_FW_RULE_NAME}」已生效 → 端口 {PORT}")
        _info(f"  放行段: {_FW_ALLOWED}")
        _info(f"  拦截:   上述范围之外的公网 IP")
    else:
        _err("添加失败：请右键以管理员身份运行本脚本（netsh 需要 elevated privileges）")


def cmd_caddy(action: str = "status") -> None:
    """Caddy HTTPS 反代管理（v0.8.13）。

    on     在 443 启动 Caddy，把 443 反代到本地 8000（自动生成自签证书）
    off    停止 Caddy
    status 查看是否在运行
    """
    # 延迟导入避免硬依赖（同目录脚本，不用包路径）
    try:
        import caddy_runner
        _run = caddy_runner.caddy_start
        _st = caddy_runner.caddy_status
        _stop = caddy_runner.caddy_stop
        ensure_caddy_binary = caddy_runner.ensure_caddy_binary
    except ImportError as e:
        _err(f"caddy_runner 模块未就绪: {e}")
        _info("请先运行 python scripts/caddy_setup.py 完成一次性部署")
        return

    if action == "status":
        running, info = _st()
        if running:
            _ok(f"Caddy 已运行（PID={info['pid']}）")
            print(f"  HTTPS 入口: https://<你的局域网IP>:443")
            print(f"  反代目标:  {info['http']}")
        else:
            _warn("Caddy 未运行")
        return

    if action == "off":
        if _stop():
            _ok("Caddy 已停止")
        else:
            _warn("Caddy 未运行或停止失败")
        return

    if action == "on":
        if not ensure_caddy_binary():
            _err("Caddy 二进制未就绪，请先运行 python scripts/caddy_setup.py")
            return
        ok, msg = _run()
        if ok:
            _ok(f"Caddy 已启动 → {msg}")
            ip = get_lan_ip()
            print(f"  本机 HTTPS: https://127.0.0.1:443")
            print(f"  局域网 HTTPS: https://{ip}:443")
            _info("首次访问浏览器会提示「证书不受信任」，点「高级 → 继续访问」即可（自签证书）")
        else:
            _err(f"启动失败: {msg}")


def cmd_toggle() -> None:
    pid = read_pid()
    if pid and is_pid_alive(pid):
        cmd_stop()
    else:
        cmd_start()


def main() -> int:
    parser = argparse.ArgumentParser(description="TeleOps 后端进程管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start", help="后台启动服务")
    sub.add_parser("stop", help="停止服务")
    sub.add_parser("restart", help="重启服务")
    sub.add_parser("status", help="查看进程 / 健康 / 访问 URL")
    sub.add_parser("logs", help="查看最近 50 行日志")
    sub.add_parser("url", help="仅打印访问 URL")
    firewall_p = sub.add_parser("firewall", help="防火墙白名单（需管理员）：on 放行 RFC1918 / off 删除 / status 查看")
    firewall_p.add_argument("firewall_action", choices=["on", "off", "status"], nargs="?",
                            default="on", help="操作类型（默认 on）")
    caddy_p = sub.add_parser("caddy", help="Caddy HTTPS 反代管理：on / off / status")
    caddy_p.add_argument("caddy_action", choices=["on", "off", "status"], nargs="?",
                         default="status", help="操作类型（默认 status）")
    sub.add_parser("toggle", help="运行中则停，停则起")

    args = parser.parse_args()
    cmd = args.cmd

    handlers = {
        "start": lambda: cmd_start(quiet=False),
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": lambda: cmd_logs(50),
        "url": cmd_url,
        "firewall": lambda: cmd_firewall(args.firewall_action),
        "caddy": lambda: cmd_caddy(args.caddy_action),
        "toggle": cmd_toggle,
    }
    handlers[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())