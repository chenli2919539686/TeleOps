#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TeleOps 一键演示启动器（求职作品 · 面试现场即用）。

解决的问题：面试官当场要看"运维告警 RCA 作战室"跑起来——但手动起后端、注册账号、
切到个人业务域、点开实时告警流太繁琐，容易翻车。

本脚本把整条链路收敛成**一条命令**：
  1. 检测/启动后端（复用 teleops_ctl，后台脱离父进程运行）
  2. 注册或登录演示账号 `interviewer`（幂等：已存在则直接登录）
  3. 定位该账号的个人业务域
  4. 停掉该域已有告警流后，启动 **5G 主题** live 告警流（快节拍 + 循环）
  5. 自动打开浏览器到作战室界面
  6. 打印账号 / 走查要点 / 文档路径

用法：
  python scripts/demo.py                 # 默认 http://127.0.0.1:8000
  python scripts/demo.py --port 9000     # 指定端口（需先 teleops_ctl start --port 9000）
  python scripts/demo.py --no-browser    # 不自动开浏览器（服务器/无界面环境）

说明：
  - 依赖最少：仅用标准库 urllib，不要求 requests。
  - 演示数据已重主题为 5G（gNodeB/BBU/UPF/AMF 等），开箱即对味电信运维。
  - 无 DeepSeek Key / 无外网也能演示：系统自动降级 mock 模式，RCA 仍产出。
  - 不删除任何数据，仅注册一个演示账号并启动一条演示流，反复运行安全。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_USER = "interviewer"
DEMO_PASS = "Interview123"

# ANSI 颜色
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _ok(m): print(f"{_GREEN}[OK]{_RESET} {m}")
def _warn(m): print(f"{_YELLOW}[WARN]{_RESET} {m}")
def _info(m): print(f"{_CYAN}[INFO]{_RESET} {m}")
def _err(m): print(f"{_RED}[ERR]{_RESET} {m}")


def _http(method: str, url: str, token: str | None = None, body: dict | None = None,
          timeout: int = 20) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw}
    except Exception as e:  # noqa: BLE001
        return -1, {"detail": str(e)}


def _health_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_backend(base: str, port: int) -> bool:
    """检测后端是否已起；否则用 teleops_ctl 后台启动并等待健康。"""
    if _health_ok(base):
        _ok(f"后端已在运行：{base}")
        return True
    _info(f"后端未启动，尝试通过 teleops_ctl 拉起（端口 {port}）…")
    ctl = PROJECT_ROOT / "scripts" / "teleops_ctl.py"
    env = dict(os.environ)
    env["TELEOPS_PORT"] = str(port)
    try:
        subprocess.Popen(
            [sys.executable, str(ctl), "start"],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        _err(f"启动后端失败：{e}")
        return False
    for _ in range(40):  # 最多等 ~40s
        time.sleep(1)
        if _health_ok(base):
            _ok(f"后端已就绪：{base}")
            return True
    _err("等待后端健康超时，请手动检查 data/teleops_server.log")
    return False


def get_token(base: str) -> str | None:
    """注册或登录演示账号，返回 JWT。处理邀请码。"""
    st, j = _http("GET", f"{base}/auth/status", timeout=10)
    invite_required = (st == 200) and bool(j.get("invite_required"))
    invite = os.environ.get("TELEOPS_INVITE_CODE", "")
    # 先试登录（账号可能已存在）
    st, j = _http("POST", f"{base}/auth/login",
                  body={"username": DEMO_USER, "password": DEMO_PASS}, timeout=10)
    if st == 200 and j.get("token"):
        _ok(f"演示账号已存在，直接登录：{DEMO_USER}")
        return j["token"]
    # 登录失败 → 注册
    if invite_required and not invite:
        _err("服务端开启了邀请码（TELEOPS_INVITE_CODE），但本机未设置，无法自动注册。\n"
             f"      请先设置环境变量后重试：set TELEOPS_INVITE_CODE=你的邀请码")
        return None
    reg_body = {"username": DEMO_USER, "password": DEMO_PASS}
    if invite_required:
        reg_body["invite_code"] = invite
    st, j = _http("POST", f"{base}/auth/register", body=reg_body, timeout=10)
    if st in (200, 201) and j.get("token"):
        _ok(f"已注册演示账号并登录：{DEMO_USER}")
        return j["token"]
    _err(f"注册/登录失败（{st}）：{j.get('detail')}")
    return None


def pick_workspace(base: str, token: str) -> str | None:
    """定位演示账号的个人业务域（ws-<username>）。"""
    st, j = _http("GET", f"{base}/workspaces", token=token, timeout=10)
    if st != 200:
        _err(f"获取业务域失败（{st}）：{j.get('detail')}")
        return None
    ws_list = j.get("workspaces", [])
    target = f"ws-{DEMO_USER}"
    for ws in ws_list:
        if ws.get("id") == target:
            return ws["id"]
    # 回退：取第一个可写的私有域
    for ws in ws_list:
        if ws.get("id", "").startswith("ws-"):
            return ws["id"]
    _err("未找到演示账号的个人业务域，请检查多租户初始化。")
    return None


def start_demo_stream(base: str, token: str, ws_id: str) -> None:
    # 先停掉已有流（幂等，失败忽略）
    _http("POST", f"{base}/stream/stop?workspace_id={ws_id}", token=token, timeout=10)
    body = {"profile": "story", "mode": "auto", "workspace_id": ws_id,
            "interval_ms": 900, "loop": True}
    st, j = _http("POST", f"{base}/stream/start", token=token, body=body, timeout=20)
    if st == 200 and j.get("status") == "running":
        _ok(f"已在该域启动 5G 实时告警流（playlist {j.get('playlist_len')} 条，循环播放）")
    elif st == 200 and j.get("status") == "pending_approval":
        _warn("人工审批闸（HITL）已开启，告警流进入待审批态——请到「设置」关闭"
              " require_approval（或设 TELEOPS_REQUIRE_APPROVAL=0 后重启）再运行本脚本。")
    elif st == 409:
        _warn("该域告警流已在运行，本次未重复启动。")
    else:
        _err(f"启动告警流失败（{st}）：{j.get('detail')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="TeleOps 一键演示启动器")
    ap.add_argument("--port", type=int, default=8000, help="后端端口（默认 8000）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--base", default=None, help="后端地址（默认 http://127.0.0.1:<port>）")
    args = ap.parse_args()
    port = args.port
    base = args.base or f"http://127.0.0.1:{port}"

    print(f"\n{_BOLD}===== TeleOps 运维告警 RCA 作战室 · 一键演示 ====={_RESET}\n")
    if not ensure_backend(base, port):
        return 1
    token = get_token(base)
    if not token:
        return 1
    ws_id = pick_workspace(base, token)
    if not ws_id:
        return 1
    start_demo_stream(base, token, ws_id)

    url = base
    print(f"\n{_BOLD}===== 演示已就绪 ====={_RESET}")
    _info(f"作战室界面：{_CYAN}{url}{_RESET}")
    _info(f"演示账号：{_BOLD}{DEMO_USER}{_RESET}  /  密码：{_BOLD}{DEMO_PASS}{_RESET}")
    _info(f"业务域：{ws_id}（已自动启动 5G 实时告警流）")
    print(f"\n{_BOLD}走查要点（详见 docs/12-一键演示与走查.md）：{_RESET}")
    print("  1. 登录后默认进入个人业务域，右侧「实时告警流」持续滚动 5G 告警 + RCA 根因")
    print("  2. 点开一条告警，看 运维Agent 的根因假设 + 工具探测；触发工具缺口会自动派发研发造工具")
    print("  3. 顶部「审计」可回放全程操作；「设置」可演示 HITL 审批 / RBAC 角色切换")
    print("  4. 生产部署与合规方案见 docs/10、docs/11（面试谈资）")
    print("  提示：无 DeepSeek Key 时系统自动 mock，RCA 仍完整可演示。\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
            _ok(f"已尝试打开浏览器：{url}")
        except Exception:  # noqa: BLE001
            _warn("无法自动打开浏览器，请手动访问上面的地址。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
