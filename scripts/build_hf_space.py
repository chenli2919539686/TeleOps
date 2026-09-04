"""生成 HF Spaces（Docker SDK）可推送目录 hf_space_build/。

只复制运行 FastAPI 服务所需的文件，剔除 gradio 演示脚本 / 文档 / 调试 trace /
本地脚本，并写入精简依赖清单（去掉 gradio、chromadb、pandas 等不需要的重依赖）。

用法：python scripts/build_hf_space.py
产物：./hf_space_build/  （含 Dockerfile / README.md / requirements.txt / .env.example / .gitignore）
"""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "hf_space_build"


def _rmtree(path: Path) -> None:
    """递归删除目录。

    优先用 os.walk + os.unlink/os.rmdir 直接删（Linux 正常环境，行为干净）。
    部分沙箱/CI 会 patch os.unlink/shutil.rmtree（安全删除走回收站），在回收站不
    可用时 fail-closed 导致构建中断；此时兜底用系统命令直接删——hf_space_build 是
    可重建的构建产物，删了无妨。
    """
    if not path.exists():
        return
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                try:
                    os.unlink(os.path.join(root, f))
                except FileNotFoundError:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        os.rmdir(path)
    except OSError:
        if os.name == "nt":
            os.system(f'rmdir /s /q "{path}" >nul 2>&1')
        else:
            os.system(f'rm -rf "{path}"')

# 需要复制的目录（保持相对结构）
COPY_DIRS = ["src", "web", "data", "kb", "tools"]

# 需要在根目录生成的文件
DOCKERFILE = r"""# HF Spaces · Docker SDK 构建文件
FROM python:3.11-slim

WORKDIR /app

# 编译部分 python 包需要的基础工具
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF Spaces Docker SDK 要求容器监听 0.0.0.0:7860（自动注入 PORT 环境变量）
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "python -m uvicorn src.api.server:app --host 0.0.0.0 --port ${PORT}"]
"""

README = """---
title: TeleOps 智能体平台
emoji: 🛰️
colorFrom: indigo
colorTo: cyan
sdk: docker
app_port: 7860
pinned: false
---

# TeleOps · 运维/研发智能体作战室

一个将「运维 Agent（告警根因）」与「研发 Agent（自动造工具）」可视化作战的
多智能体平台 Demo：研发在上、运维在下，虚线表示「缺工具 → 派单研发 → 工具回传」
的闭环链路；支持按业务域（接入一个接口=一个域）创建与隔离 Agent、状态灯实时联动、
右侧消息栏按域隔离流转需求。

## 这个 Space 能做什么
- **Agent 作战室**：点开任意 Agent 卡片进入工作台，运行根因分析 / 造工具。
- **自动闭环**：运维缺工具 → 登记需求 → 研发造工具并沉淀 SOP → 运维复用。
- **多业务域**：每个北向接入对应一个独立业务域，Agent 与需求按域隔离。
- **操作记录**：工作台产出自动写回消息栏「操作记录」tab。

## 使用说明
1. 打开界面后默认进入「Agent 作战室」，顶部切换不同业务域。
2. 点开一个运维 Agent 工作台 → 选预设告警 → 「分析告警」。若诊断出工具缺口，
   点「登记并派发研发」即可跑通研发→回传闭环。
3. 右侧「消息栏」可切换「需求流转 / 操作记录」。

## 环境变量（Space → Settings → Secrets）
- `DEEPSEEK_API_KEY`：配置后切换真实大模型；**不配置则自动降级为离线 Mock**，Demo 仍可完整跑通。
- `TELEOPS_API_TOKEN`：可选。设置后所有写接口需要 `Authorization: Bearer <token>`；
  界面「设置」中填入相同 Token 即可。不设置则完全开放（适合公开 Demo）。
- `TELEOPS_CORS_ORIGINS`：可选。逗号分隔的允许跨域域名；默认仅放行本地前端。
- `TELEOPS_RATE_LIMIT`：可选，默认 `on`。按 IP 限流读 300/分、写 60/分、登录 10/分（429 + Retry-After）；
  公开 Space 建议保持开启，需要调阈值用 `TELEOPS_RATE_LIMIT_READ/WRITE/LOGIN`。

> 代码见 GitHub（src/ 为 FastAPI 后端，web/ 为原生前端，单端口同时托管 API 与界面）。
"""

REQUIREMENTS = """# TeleOps 运行所需最小依赖（已剔除 gradio / chromadb / pandas 等重依赖）
fastapi>=0.110
uvicorn[standard]>=0.27
pydantic>=2.0
openai>=1.0
python-dotenv>=1.0
httpx>=0.27
# 上限必须锁 0.3：langgraph 1.x 重构了 langgraph.graph 导出与 StateGraph 语义，
# 本项目基于 0.2.x API，装到 1.x 会直接 import 失败（CI / Spaces 部署会挂）
langgraph>=0.2,<0.3
"""

ENV_EXAMPLE = """# 在 HF Space → Settings → Secrets 中设置；本地可复制为 .env
# 真实大模型 Key（不配置则自动降级离线 Mock）
DEEPSEEK_API_KEY=your_key_here

# 可选：开启写接口 Token 鉴权（开启后界面「设置」需填入相同值）
TELEOPS_API_TOKEN=

# 可选：允许跨域的域名（逗号分隔）；默认仅 localhost:8001
TELEOPS_CORS_ORIGINS=

# 可选：限流开关（默认 on）；公开 Space 建议保持开启
# TELEOPS_RATE_LIMIT=on
# TELEOPS_RATE_LIMIT_LOGIN=10
"""

GITIGNORE = """__pycache__/
*.pyc
.env
traces/
*.log
"""


def build():
    if DEST.exists():
        _rmtree(DEST)
    DEST.mkdir(parents=True)

    for d in COPY_DIRS:
        src = ROOT / d
        if not src.exists():
            print(f"! 跳过不存在的目录: {d}")
            continue
        dst = DEST / d
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"+ 复制目录 {d}/")

    (DEST / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (DEST / "README.md").write_text(README, encoding="utf-8")
    (DEST / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (DEST / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    (DEST / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    print("+ 生成 Dockerfile / README.md / requirements.txt / .env.example / .gitignore")

    # 产物自检
    assert (DEST / "Dockerfile").exists()
    assert (DEST / "src" / "api" / "server.py").exists()
    assert (DEST / "web" / "index.html").exists()
    print(f"\n✅ 已生成 HF Spaces 目录：{DEST}")
    print("下一步（需 HF 账号 + token）：")
    print("  cd hf_space_build")
    print("  git init && git add -A && git commit -m 'init TeleOps space'")
    print("  huggingface-cli login   # 或设 HF_TOKEN")
    print("  huggingface-cli repo create teleops --type space --sdk docker --private")
    print("  git remote add origin https://huggingface.co/spaces/<你>/teleops")
    print("  git push -u origin main")


if __name__ == "__main__":
    build()
