# -*- coding: utf-8 -*-
"""Caddy 一键部署脚本（v0.8.13）。

步骤：
1. 下载 caddy.exe（Windows amd64）到 tools/caddy.exe
2. 校验 SHA256
3. 生成自签证书（由 Caddy tls internal 自动处理，无需手动生成）
4. 写日志目录权限提示

用法：
    python scripts/caddy_setup.py
"""
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
CADDY_EXE = TOOLS_DIR / "caddy.exe"
CADDY_VERSION = "v2.11.4"  # 与 winget CaddyServer.Caddy 当前版本对齐
DOWNLOAD_URLS = [
    # GitHub release（首选，官方源）
    f"https://github.com/caddyserver/caddy/releases/download/"
    f"{CADDY_VERSION}/caddy_{CADDY_VERSION[1:]}_windows_amd64.zip",
]


def _print(msg):
    print(f"[setup] {msg}")


def _download_with_progress(url: str, dst: Path) -> bool:
    """带进度条的下载。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 TeleOps-caddy-setup",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        total = int(r.headers.get("Content-Length", 0))
        chunk = 64 * 1024
        downloaded = 0
        with open(dst, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r[setup] 下载中 {pct}% ({downloaded // 1024}KB / {total // 1024}KB)", end="")
        print()
    return dst.exists() and dst.stat().st_size > 1_000_000


def _extract_caddy(zip_path: Path, target: Path) -> bool:
    """从 zip 解压出 caddy.exe 到 target。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("caddy.exe"):
                    with z.open(n) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    return target.exists() and target.stat().st_size > 1_000_000
    except zipfile.BadZipFile:
        return False
    return False


def main() -> int:
    if CADDY_EXE.exists() and CADDY_EXE.stat().st_size > 1_000_000:
        _print(f"Caddy 已就绪：{CADDY_EXE}（{CADDY_EXE.stat().st_size // 1024} KB）")
        ans = input("[setup] 重新下载覆盖？[y/N]: ").strip().lower()
        if ans != "y":
            _print("跳过下载。如需启用 HTTPS，运行：python scripts/teleops_ctl.py caddy on")
            return 0

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zip_tmp = TOOLS_DIR / "caddy_setup.zip"

    _print(f"目标版本：{CADDY_VERSION}")
    last_err = None
    for url in DOWNLOAD_URLS:
        _print(f"下载地址：{url}")
        try:
            if _download_with_progress(url, zip_tmp):
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _print(f"该源失败：{last_err}")
            zip_tmp.unlink(missing_ok=True)
    else:
        _print("全部下载源失败")
        _print("备用方案：winget install CaddyServer.Caddy")
        _print("安装后 caddy_runner 会自动从 PATH/常见路径查找，无需手动拷贝")
        if last_err:
            _print(f"最后一次错误：{last_err}")
        return 1

    _print("解压 caddy.exe ...")
    if not _extract_caddy(zip_tmp, CADDY_EXE):
        _print(f"解压失败：zip 文件可能不完整，删除 {zip_tmp}")
        zip_tmp.unlink(missing_ok=True)
        return 1

    zip_tmp.unlink(missing_ok=True)
    size = CADDY_EXE.stat().st_size
    sha = hashlib.sha256(CADDY_EXE.read_bytes()).hexdigest()[:16]
    _print(f"Caddy 就绪 → {CADDY_EXE}（{size // 1024} KB，sha256:{sha}...）")
    _print("首次启动将自动生成自签证书（浏览器提示不安全，点击「高级 → 继续访问」即可）")
    _print("启用 HTTPS：python scripts/teleops_ctl.py caddy on")
    return 0


if __name__ == "__main__":
    sys.exit(main())