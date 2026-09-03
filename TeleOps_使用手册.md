# TeleOps 智能体平台 · 使用手册

> 适用版本：W1 → W4 全栈（地基 / 双 Agent 闭环 / FastAPI / Web 主界面 web/ / 历史 Gradio 前台 / 求职材料）
> 项目路径：`TeleOps/`
> 适用系统：Windows（Git Bash）/ macOS / Linux

---

## 0. 这是什么（30 秒了解）

一个 **Agent 平台底座 + 两个智能体** 的纵向闭环演示：

- **运维 Agent**：收到告警 → 降噪 → 结合 CMDB 拓扑 + 知识库做根因推理 → 调工具处置 → 发现缺工具就反馈。
- **研发数字员工**：读反馈 → 自动生成工具脚本并注册进工具库 → 沉淀处置 SOP 进知识库。
- 两者经「故障反馈」联动，形成「运维缺工具 → 研发造工具 → 运维复用」闭环，对应运营商「业务运维一体化」。

**无需 API Key 即可离线演示**（无 Key 自动走确定性 Mock）。配置 Key 后切换真实大模型。

---

## 1. 环境准备

### 方案 A：在 WorkBuddy 内运行（推荐，依赖已装好）
直接在本会话的终端用 `python` 即可，langgraph / openai / fastapi / gradio 等都已装进隔离环境，无需自建 venv。

### 方案 B：在自己的机器运行
```bash
# 要求 Python ≥ 3.11（推荐 3.13）
cd TeleOps
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\activate.bat        # Windows PowerShell
pip install -r requirements.txt
```
之后文档里的 `python` 全部替换为 `python`（或 `python3`）即可。

> 所有重依赖（networkx / chromadb / gradio / fastapi 等）都做了 `try/except` 兜底，**即使没装也能跑基础链路**。

---

## 2. 第一次跑起来（最简路径）

**主界面 = FastAPI 后端同源托管 `web/` 前端**，一条命令起服务：

```bash
cd TeleOps
python -m uvicorn src.api.server:app --reload --port 8000
```

浏览器打开 **http://localhost:8000** 即为主界面。左侧导航：🎛️ Agent 作战室 / 🔄 闭环看板 / 📥 消息栏 / 🔌 接入层 / 🕸️ 拓扑视图 / 📦 工具库 / 📚 知识库。

> - 自带 `data/teleops.db`（含 core-net 业务域与基线 Agent）时开箱即用；全新环境先 `python scripts/gen_data.py` 造基础数据，再起服务。
> - 历史 Gradio 三 Tab 前台（`python app.py` → http://localhost:7860）已退居备选，仅 HF Spaces 轻量场景使用（见第 9 节）。

---

## 3. 数据准备（自造 + 真实公开）

项目数据分两类，分开生成：

| 数据 | 来源 | 生成命令 |
|---|---|---|
| `data/topology.json` 拓扑 | 自造 | `python scripts/gen_data.py` |
| `data/tools.json` 工具库 | 自造 | 同上（基础 2 个工具） |
| `data/feedback.json` 反馈 | 自造 | 同上 |
| `data/alerts.json` 告警 | **真实公开** LogHub | `python scripts/ingest_public.py --convert --dataset bgl --raw data/raw/bgl_sample.log` |
| `kb/*.md` 知识库 | **真实公开** MITRE/SRE | `python scripts/ingest_public.py --make-kb` |

完整示例（首次拉全量真实数据，需联网）：
```bash
python scripts/gen_data.py                                            # 自造：拓扑/工具/反馈
python scripts/ingest_public.py --download                            # 下载 LogHub 2k 真实日志到 data/raw/
python scripts/ingest_public.py --convert --dataset bgl --raw data/raw/bgl_2k.log   # 转 alerts.json
python scripts/ingest_public.py --make-kb                             # 生成公开知识库
```

> 项目已自带 `data/raw/bgl_sample.log` 与 `hdfs_sample.log` 真实样本，所以 `--convert` 用自带样本即可，**不联网也能跑**。

---

## 4. 四种运行入口

| 入口 | 命令 | 用途 |
|---|---|---|
| **W1 最小演示** | `python demo.py` | 验证数据层 + 能力层（CMDB/工具库/知识库） |
| **W2 闭环演示** | `python demo_w2.py` | 命令行看「运维缺工具→研发造工具→复用」端到端 |
| **W3 接口测试** | `python -m pytest` | pytest 套件（46 项：接口/鉴权/闭环/指标/限流，免启服务，全绿即通过） |
| **W3 真实服务（推荐入口）** | `python -m uvicorn src.api.server:app --reload --port 8000` | 起 HTTP 服务；**http://localhost:8000 即主界面**，/docs 看 Swagger |
| **W4 备选前台** | `python app.py` | 历史 Gradio 三 Tab（仅 HF Spaces 场景，见第 9 节） |

---

## 5. 用 Web 主界面（重点）

启动见第 2 节 → 打开 **http://localhost:8000**。视图一览：

| 视图 | 作用 |
|---|---|
| 🎛️ **Agent 作战室** | 按业务域列 Agent 卡片（🛰️ 运维 / 🛠️ 研发，可增删），点卡片打开右侧 **Agent 工作台**；卡片下方实时显示该域「当前闭环」进度 |
| 🔄 **闭环看板** | 需求状态机全览：`pending → in_progress → tool_ready → done`，点卡片可在右侧抽屉与 Agent 对话 |
| 📥 **消息栏** | 手动「发起需求 / 派发研发」的入口，闭环结果与提示在此回流 |
| 🔌 **接入层** | 适配器下拉：4 个可跑样板 + 7 个真实系统预留占位（Zabbix / iMaster / ELK…） |
| 🕸️ **拓扑视图** | CMDB 网络拓扑可视化 |
| 📦 **工具库** | 所有已注册工具（含研发自动生成件） |
| 📚 **知识库** | RAG 检索（SRE / MITRE / SOP），内置示例问句直达问答 |

### 5.1 运维 Agent：告警根因 → 工具缺口 → 发起闭环

1. 作战室点一张 🛰️ 运维 Agent 卡片 → 右侧打开 **Agent 工作台**（左缘可拖拽调宽，宽度自动记忆）。
2. 左上「① 告警输入」选告警，三选一：
   - 点 4 个**演示样例** chip（🌡️ 温度过热 / 💡 ONU 光弱 / 🔕 噪声 / 📦 端口错包）；
   - 展开 **📚 真实机群告警样本**：`data/alerts.json` 的 55 条 BlueGene/L 真实事件，可搜可筛（🚨 严重 3 条为真故障、🔕 噪声 52 条），点条目即填入 JSON；
   - 直接粘贴任意告警 JSON。
3. 点「▶ 分析告警」→ 右侧显示降噪 / 根因 Top-k + 置信度 / 证据 / 工具执行（结果区自动滚动，JSON 可悬停复制）。
4. 若根因指向库内**没有**的工具：结果底部出现「登记并派发研发」→ 一键登记需求并派发研发。
   > 若工具库**已有**该工具，会提示「♻️ 工具已存在，直接复用」，不再产生重复需求（工具库活视图，全局实时可见）。

### 5.2 研发 Agent：造工具

1. 作战室点一张 🛠️ 研发 Agent 卡片 → 左侧「① 研发反馈单」填单号与摘要（描述运维缺什么能力）。
2. 点「🛠️ 研发造工具」→ 自动生成 `tools/xxx.py`、注册进工具库、沉淀 SOP 进知识库，右侧显示全过程。
3. 消息栏/闭环看板可见该需求流转至 `tool_ready`；运维侧下一轮诊断即复用新工具。

### 5.3 闭环看板 / 消息栏联动

- 消息栏支持**手动闭环**：选运维 Agent「发起需求」→ 选研发 Agent「派发研发」→ 看板内需求变 `tool_ready` → 工作台复诊后 `done`。
- 工具一经造出即全队复用：同名缺口二次发起会直接返回复用，消息栏不会堆积重复 REQ（v0.7.2 起）。
- 作战室每张 Agent 卡片下方同步显示该域最近闭环进度，无需切页即可观察。

---

## 6. 用 FastAPI 后端（curl 示例）

先起服务：
```bash
python -m uvicorn src.api.server:app --reload --port 8000
```

常用端点（浏览器开 http://localhost:8000/docs 可交互）：
```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/topology
curl http://127.0.0.1:8000/tools
curl "http://127.0.0.1:8000/knowledge?q=光模块%20功率%20异常&top_k=2"

# 运维处理一条告警（用库里已有告警 ID，或不传 alert 由默认演示告警触发）
curl -X POST http://127.0.0.1:8000/alert -H "Content-Type: application/json" \
  -d '{"alert":{"alert_id":"A-TEST","metric":"temperature","host":"host-1","severity":"critical","value":"88C","message":"物理机 host-1 核心温度过热告警","is_noise":false}}'

# 提交反馈 → 自动触发研发造工具 + 沉淀 SOP
curl -X POST http://127.0.0.1:8000/feedback -H "Content-Type: application/json" \
  -d '{"feedback_id":"F-UI","summary":"运维根因推理需要工具 temperature_probe，但工具库缺失，请研发生成"}'

# 跑完整闭环
curl -X POST http://127.0.0.1:8000/closed-loop/run -H "Content-Type: application/json" -d '{}'
```

---

## 7. 接入真实大模型（可选）

默认无 Key 走 Mock。要接真实推理：

1. 去 https://platform.deepseek.com 注册拿 Key。
2. 复制配置模板并填 Key：
   ```bash
   cp .env.example .env
   ```
   编辑 `.env`：
   ```
   DEEPSEEK_API_KEY=你真实的key
   ```
3. 重启服务（`uvicorn --reload` 已启动则自动生效；demo 脚本重跑即可）。界面 / 日志中 LLM 模式会显示 `live`，即走真实 DeepSeek 推理。
4. 想换国产模型（Qwen / GLM）：改 `src/config.py` 的 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL` 即可，Agent 代码不用动。

> 真实调用失败（限流/网络）会自动回退 Mock，演示不崩。

---

## 8. 重新演示闭环（重置步骤）⚠️

因为研发 Agent 之前已把工具落盘并写进 `tools.json`，现在库里已有 `temperature_probe` / `optical_power_probe`，所以闭环演示不会重演。重置方法：

```bash
# 1) 删除研发自动生成的工具脚本（Git Bash / macOS / Linux）
rm tools/temperature_probe.py tools/optical_power_probe.py
#    PowerShell 用： Remove-Item tools/temperature_probe.py, tools/optical_power_probe.py

# 2) 重置工具注册表到基础两套（会移除上面两项；同时重写 topology/feedback，内容不变）
python scripts/gen_data.py

# 3) （可选）删除孤立的 SOP 文件
rm kb/sop_temperature_probe.md kb/sop_optical_power_probe.md
```

重置后启动主界面（第 2 节）→ 消息栏选运维 Agent 点「发起需求」（或在工作台分析同款温度告警，出现缺口后点「登记并派发研发」），即可看到「缺工具 → 研发造 → 运维复用」完整过程；需求在闭环看板流转至 `done`。原始实证见 `traces/`。

> 提示：`gen_data.py` 会重写 `topology.json` / `feedback.json` / `tools.json`，若你改过这三个文件请先备份。

---

## 9. 部署到 Hugging Face Spaces

1. 在 HF 新建一个 **Space**（选 Gradio SDK）。
2. 把整个 `TeleOps/` 目录推到 Space 的 git 仓库。
3. HF 自动读 `requirements.txt` 安装依赖，并用 `python app.py` 启动；`app.py` 已读 `PORT` 环境变量并内置 `ensure_data()` 自动造数据。
4. 可选：在 Space 的 Secrets 加 `DEEPSEEK_API_KEY`，即从 Mock 切换为真实大模型。

> 本地主界面推荐用第 2 节的 uvicorn 方式（web/ 8 视图，功能最全）；`app.py`（Gradio）仅用于 HF Space 免后端进程的轻量演示。

---

## 10. 常见问题 / 排错

| 现象 | 原因 / 解决 |
|---|---|
| 启动报 `ModuleNotFoundError` | 用方案 B 建 venv 并 `pip install -r requirements.txt`；WorkBuddy 内直接 `python` 即可 |
| 页面样式/布局像是旧版 | 前端静态文件带版本戳，改动后需 **Ctrl+Shift+R** 强刷一次 |
| 前台顶部 LLM 模式显示 `live` 但无 Key | 那是构造初值，真正解析在首次调用时；无 Key 会自动转 `mock` |
| 闭环看板不触发造工具 | 见第 8 节，工具已被之前生成，需重置（或换一个仍缺的工具名告警） |
| 知识库检索为空 | 没跑 `--make-kb`；或查询词与 kb 内容无字符重叠（中文按字匹配） |
| `/alert` 想用库里真实告警 | 传 `{"alert_id":"A-9001"}` 这类 `alerts.json` 中存在的 ID |
| 想换模型/改端口 | 模型改 `src/config.py`；后端改 `uvicorn --port`；web 无需单独端口（同源托管 /） |

---

## 11. 目录速查

```
TeleOps/
├── web/                   # 主界面：8 视图前端（原生 HTML/CSS/JS，FastAPI 同源托管 /）
├── app.py                 # 历史 Gradio 三 Tab 前台（备选 / HF Spaces 用）
├── demo.py / demo_w2.py   # W1 / W2 演示
├── tests/                # pytest 套件（46 项，免启服务，不污染运行数据）
├── src/
│   ├── config.py          # 路径 + 模型配置
│   ├── llm_client.py      # LLM 抽象层 + 离线 Mock
│   ├── core/              # cmdb_graph / tool_registry / kb_store / workspace_store / requirement_board
│   ├── agents/            # ops_agent / dev_agent
│   ├── orchestration/     # graphs.py (LangGraph 编排) / dispatch.py
│   └── api/server.py      # FastAPI 后端（REST + 静态托管 web/）
├── scripts/               # gen_data.py / ingest_public.py / reset_demo.py
├── data/                 # topology / tools / feedback（自造）+ alerts（公开 55 条）+ teleops.db
├── kb/                   # MITRE / SRE 公开知识库 + 自动沉淀 SOP
├── tools/                # 工具脚本（含研发自动生成）
├── traces/               # 闭环可观测记录（实证）
├── CAREER.md             # 求职材料（话术 / Q&A / 简介）
├── DEMO_SCRIPT.md        # 面试录屏分镜
└── TeleOps_项目梳理.md    # 项目完整梳理
```
