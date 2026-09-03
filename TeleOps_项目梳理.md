# TeleOps 项目完整梳理

> 整理时间：2026-08-30
> 项目路径：`C:\Users\Chenl\WorkBuddy\2026-08-30-13-28-10\TeleOps\`
> 状态：**W1 地基 → W4 部署 + 求职材料，全栈完成，已实测跑通闭环。**

---

## 0. 一句话结论

这是一个 **Agent 平台底座 + 两个智能体（研发数字员工 / 运维 Agent）的纵向闭环** 作品集，对应运营商「业务运维一体化」叙事。整套东西 **零依赖、可离线演示、可部署上线**；数据用真实公开日志保证可信度，拓扑/工具/反馈自造保证闭环可玩。

---

## 1. 项目定位与背景

| 项 | 内容 |
|---|---|
| 目标岗位 | 三大运营商 云网运维 / 数字化研发 / 安全运营（应届求职作品集） |
| 选定策略 | **策略 B · 一+二纵向闭环**：研发造工具 → 注册进工具库 → 运维调用 → 缺工具时反馈 → 研发再造 |
| 硬约束 | 混合方向、1 个月周期、白手起家、**无本地开发环境**（实测用 WorkBuddy 自带 Python 3.13 + Node 22） |
| 数据策略 | 告警库 + 知识库用**真实公开数据**；拓扑 / 工具注册表 / 反馈工单**自造**（真实运营商拓扑不公开） |
| 国产化叙事 | `LLMClient` 抽象层，换 DeepSeek / Qwen / GLM 只改 `config.py`；向量库与工具可私有化部署 |

---

## 2. 整体架构（五层）

```
┌─────────────────────────────────────────────────────────────┐
│  前台 / 服务层                                               │
│   web/ (8 视图, FastAPI 托管 /)  ·  app.py (Gradio 备选)      │
├─────────────────────────────────────────────────────────────┤
│  编排层  src/orchestration/graphs.py (LangGraph 工作流)       │
├─────────────────────────────────────────────────────────────┤
│  Agent 层                                                   │
│   src/agents/ops_agent.py  (降噪/根因/调工具/处置)           │
│   src/agents/dev_agent.py  (CodeGen造工具/注册/变更单/SOP)    │
├─────────────────────────────────────────────────────────────┤
│  能力层  src/core/                                           │
│   cmdb_graph.py (拓扑图)  ·  tool_registry.py (工具库)        │
│   kb_store.py (知识库 RAG)  ·  llm_client.py (LLM 抽象层)    │
├─────────────────────────────────────────────────────────────┤
│  数据层  data/ (topology/tools/feedback=自造; alerts=公开)    │
│           kb/ (MITRE ATT&CK / SRE 公开知识)                   │
└─────────────────────────────────────────────────────────────┘
```

每一层都「可替换」：模型换 `config.py`、图库换 networkx/内置兜底、向量库换 Chroma/字符级兜底、前端换 Gradio/API。这正是「平台底座」的体现。

---

## 3. 目录结构与各文件职责

### 配置与入口
| 文件 | 职责 |
|---|---|
| `src/config.py` | 全局路径 + 模型配置（provider、API Key、base_url、本地模型端点）；统一从这儿取路径 |
| `src/llm_client.py` | `LLMClient` 抽象层 + 离线确定性 Mock（`[TASK:xxx]` 路由）；无 Key 自动降级，保证任意机器可跑 |
| `web/`（+`src/api/server.py`） | **主界面**：FastAPI 同源托管 8 视图前端（作战室 / 闭环看板 / 消息栏 / 接入层 / 拓扑 / 工具库 / 知识库）+ REST API |
| `app.py` | **历史 W4 Gradio 三 Tab 前台**（standalone 备选，仅 HF Spaces 场景）；内置 `ensure_data()` 自动造数 |
| `demo.py` | **W1** 最小演示（数据层 + 能力层） |
| `demo_w2.py` | **W2** 闭环演示（运维↔研发纵向闭环） |
| `tests/`（pytest 46 项） | **W3/W4** 接口 / 鉴权 / 闭环 / Agent 增删 / 工具复用 / 指标 / 限流回归（免启服务，全绿即通过） |

### 能力层 `src/core/`
| 文件 | 职责 |
|---|---|
| `cmdb_graph.py` | `CMDBGraph`：加载拓扑，提供 `dependencies/dependents/neighbors/node_info`；networkx 优先，否则内置轻量图兜底 |
| `tool_registry.py` | `ToolRegistry`：SQLite **活视图**（list_tools/get 实时查库，工具一经注册全 Agent 即时可见）；`call` 动态 import `tools/*.py` 的 `run(params)`；高风险拦截返回 `blocked` |
| `kb_store.py` | `KBStore`：`retrieve(query,top_k)` 切片 MD 检索；Chroma 优先，否则**字符级重叠打分**兜底（解决中文无空格分词 bug） |

### Agent 层 `src/agents/`
| 文件 | 职责 |
|---|---|
| `ops_agent.py` | `OpsAgent`：`normalize` 降噪 → `rootcause` 根因推理（CMDB+知识库）→ `run_recommended_tools` 调工具 → `detect_missing_tool` 发现缺工具 → `build_plan` 汇总；对外 `handle_alert` |
| `dev_agent.py` | `DevAgent`：`generate_tool` 自动生成工具脚本并落盘 → `register_tool` 注册进 `tools.json` → `change_order` 变更单 → `write_sop` 沉淀 SOP 进 `kb/`；对外 `fulfill_feedback` |

### 编排层 `src/orchestration/`
| 文件 | 职责 |
|---|---|
| `graphs.py` | LangGraph：`build_ops_graph`（normalize→diagnose）与 `build_dev_graph`（codegen→register→sop）两个有状态工作流 |

### 服务层 `src/api/`
| 文件 | 职责 |
|---|---|
| `server.py` | FastAPI 后端：端点 `/health /topology /tools /tools/call /knowledge /alert /chat /feedback /closed-loop/run /traces`；`/feedback` 与 `/closed-loop/run` 自动触发研发闭环；带 CORS、`/docs` Swagger |

### 数据层 `data/` 与知识库 `kb/`
| 文件 | 来源 | 说明 |
|---|---|---|
| `data/topology.json` | **自造** | 11 节点电信云网拓扑（服务/库/中间件/网络/主机/OLT/ONU）+ 依赖边 |
| `data/tools.json` | **自造** | 工具注册表（当前含 `ping_host`/`restart_service`/`optical_power_probe`/`temperature_probe`） |
| `data/feedback.json` | **自造** | 反馈工单（F-001 光模块缺工具 / F-002 交换拥塞 SOP 缺失） |
| `data/alerts.json` | **真实公开** | 由 LogHub `bgl_sample.log` 转换的告警 |
| `data/raw/bgl_sample.log` `hdfs_sample.log` | 真实原文 | LogHub 公开日志样本 |
| `kb/mitre_attack_techniques.md` | 真实公开 | MITRE ATT&CK 技术知识 |
| `kb/sre_postmortem_patterns.md` | 真实公开 | SRE 事故复盘模式 |
| `kb/sop_optical_power_probe.md` `sop_temperature_probe.md` | **研发生成** | 闭环自动沉淀的 SOP |

### 工具脚本 `tools/`
| 文件 | 说明 |
|---|---|
| `net_ping.py` `svc_restart.py` | 示例工具（`restart_service` 标记为 `risk:high` 需人工确认） |
| `optical_power_probe.py` `temperature_probe.py` | **研发 Agent 自动生成**的探测工具 |

### 接入脚本 `scripts/`
| 文件 | 职责 |
|---|---|
| `gen_data.py` | 一键造【自造】数据：拓扑 / 工具注册表 / 反馈工单 |
| `ingest_public.py` | 接入【真实公开】数据：`--download` 下载、`--convert` 日志转 `alerts.json`、`--make-kb` 生成知识库 |

### 求职与可观测
- `CAREER.md`：定位 / 技术栈 / STAR 话术 / 三大亮点 / 面试 Q&A / 1 页简介
- `DEMO_SCRIPT.md`：2–3 分钟面试录屏分镜（三 Tab 演示脚本）
- `traces/`：`w2_round1.json` `w2_round2.json` `w2_dev.json` `api_alert.json` `api_feedback.json` `api_closed_loop.json`（闭环实证）

---

## 4. 核心闭环流程（重点）

```
① 运维收到告警 (onu-1 光模块/ host-1 温度过热)
      ↓ OpsAgent.handle_alert
② 降噪 normalize  →  根因推理 rootcause (CMDB 拓扑 + 知识库)
      ↓
③ run_recommended_tools 调已有工具；detect_missing_tool 发现缺失
      ↓ 缺工具！
④ 生成反馈工单 → DevAgent.fulfill_feedback
      ↓
⑤ 自动 generate_tool（写 tools/xxx.py）→ register_tool（进 tools.json）→ write_sop（进 kb/）
      ↓ 工具/知识就绪
⑥ 第二轮 运维复用新工具处置成功 → 闭环完成（可观测 trace 留存）
```

**对应代码落点**
- 步骤 ①②③④：`src/agents/ops_agent.py`
- 步骤 ⑤：`src/agents/dev_agent.py` + `src/core/tool_registry.py` + `kb_store.py`
- 步骤 ⑥：编排在 `src/orchestration/graphs.py`，对外触发在 `server.py`（`/closed-loop/run`、`/requirements/raise`）与 **web 主界面**（闭环看板 / 消息栏 / 工作台）

---

## 5. 数据策略（公开 + 自造，统一 Schema 接入）

- **真实公开**：`alerts.json` 来自 LogHub 公开日志 → 证明「真告警、真字段」；`kb/` 来自 MITRE ATT&CK / SRE 公开知识 → 证明「真知识、可 RAG」。
- **自造建模**：运营商真实拓扑/工具库/反馈不公开，由本人按场景建模，紧贴 JD。
- **关键点**：两类数据通过 `config.py` 统一路径 + `CMDBGraph`/`ToolRegistry`/`KBStore` 统一读取，**能力层零改动**即可切换。

---

## 6. 如何运行（入口一览）

```bash
cd TeleOps

# —— 无需 API Key，离线 Mock 即可跑 ——

python demo.py            # W1 数据层+能力层最小演示
python demo_w2.py         # W2 端到端纵向闭环（运维↔研发）
python -m pytest        # 46 项接口 / 鉴权 / 闭环 / Agent 增删 / 工具复用回归（免启服务，全绿即通过）

python -m uvicorn src.api.server:app --reload --port 8000   # W3 真实 HTTP 服务 = 主界面
# ✨ 浏览器打开 http://localhost:8000 即主界面（web/ 8 视图：作战室/闭环看板/消息栏/接入层…）
# /docs 看 Swagger；GET /alerts 可浏览 data/alerts.json 的 55 条真实告警样本

python app.py             # 备选：历史 Gradio 三 Tab（仅 HF Spaces 场景）
# 打开 http://localhost:7860 （app.py 内置 ensure_data 自动造数）

# —— 接真实大模型（可选）——
cp .env.example .env      # 填入 DEEPSEEK_API_KEY
# 重启 uvicorn / 重跑 demo_w2.py 即走 DeepSeek 真实推理；无 Key 自动 Mock 兜底
```

> 本机依赖已装进 WorkBuddy 隔离 Python（langgraph / openai / fastapi / gradio 等），直接用 `python` 即可，无需自建 venv。

**部署到 Hugging Face Spaces**：整个 `TeleOps/` 推到 Space git，`app.py` 会被自动用 `python app.py` 启动并读取 `PORT`；可选在 Secrets 加 `DEEPSEEK_API_KEY` 切真实模型。

---

## 7. 已验证的实证（traces/）

| trace | 证明 |
|---|---|
| `w2_round1.json` `w2_round2.json` `w2_dev.json` | W2 CLI 闭环：首轮缺工具 → 研发造 `optical_power_probe` → 次轮复用成功 |
| `api_alert.json` | W3 `/alert` 单告警处理产出 |
| `api_feedback.json` | W3 `/feedback` 自动造工具 + 沉淀 SOP |
| `api_closed_loop.json` | W3 `/closed-loop/run` 完整「缺工具→造工具→复用」闭环记录 |

---

## 8. 求职材料

- **`CAREER.md`**：一句话定位、技术栈表、STAR 话术、三大亮点（分别映射三类 JD）、5 条面试深挖 Q&A、1 页项目简介（可直接贴 GitHub）。
- **`DEMO_SCRIPT.md`**：2–3 分钟录屏分镜，三 Tab 顺序与口播词，含「若面试官想看接口」备用方案。

---

## 9. 已知事项与重新演示闭环的方法（重要）

1. **当前闭环不会再次触发造工具分支（这是 v0.7.2 起的「工具复用」特性，非缺陷）。**
   原因：之前测试时研发已自动生成 `optical_power_probe` 与 `temperature_probe`，已持久化进工具库（SQLite `tools` 表）与 `tools/`。且 v0.7.2 起 `ToolRegistry` 为 SQLite 活视图 + 登记需求前查库兜底——同一缺口二次发起会提示「♻️ 工具已存在，直接复用」，**不会**产生重复需求。
   **若要重演「缺工具 → 研发造 → 复用」**，先重置自动生成的工具：
   ```bash
   # 删除研发自动生成的工具脚本
   rm tools/temperature_probe.py tools/optical_power_probe.py
   # 重置注册表到基础工具（同时清自动生成项）
   python scripts/gen_data.py
   ```
   干净环境首次运行（工具库只含基础 2 项）闭环即正常触发。实证已留在 `traces/`。

2. **`LLMClient.mode` 默认值显示为 `live`**，真实解析在 `_ensure_client()` 内：无 Key 会立刻转 `mock`。不要被构造时的默认值误导。

3. **README 第 96 行小节标题**原有笔误（写「W1+W2 已完成」），本次已修正为「W1 → W4 全栈完成」。

---

## 10. 当前状态总结 & 可选下一步

✅ 地基 / 双 Agent / 闭环编排 / FastAPI / **Web 主界面（web/ 8 视图）** / 求职材料 —— 全部完成并实测。
✅ 零依赖离线可演示、可部署、数据真实。

**可选收尾（你之前还没拍板）**：
- 整理干净 git 仓库 + 写 GitHub 首页 README（可复用本梳理）；
- 压一版 30 秒「电梯演讲」话术放进 `CAREER.md`；
- 把 `DEMO_SCRIPT.md` 实际录成视频，作为投递附件。

需要的话我直接帮你把上面任意一项做完。
