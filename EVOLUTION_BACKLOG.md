# TeleOps 演进待办清单（长期，慢慢完善）

> 来源：docs/04 能力对照与优化路线 + 用户路线图 Phase 1/2 残留 + 会话决策。
> 推进原则：**演进式、每步一提交、零回归风险**；优先做独立、低风险、可立即落地的项。
> 已收工的地基：多租户三层隔离 / 旁路审计 / 安全护栏（邀请码·限流·HTTPS·防火墙）/
> 人工审批 HITL / D3 状态外部化（多副本无状态，docs/06）。

## ✅ 已收工
- **异步审计写（v0.8.36）**：`src/core/audit_queue.py`（`AuditWriter` 单例，有界队列 +
  daemon 消费线程 + atexit flush）；`_audit_write` / `_audit_write_dummy` 异步入队，请求路径
  零阻塞，后台线程串行消费 `db.audit`（存储语义不变）。测试 `tests/test_audit_queue.py` 4/4。
- **人工审批 HITL 闭环（v0.8.35）**：两层闸 + admin 运行时开关；详见 `docs/07`。
- **多副本实战验证 + D3 状态共享（v0.8.34）**：真实 Redis + 双副本 7/7 通过；`docs/06`。
- **真实电信数据接入（v0.8.37，首切·管线打通）**：数据源定 `uccmisl/5Gdataset`（公开 5G KPI）。
  `src/adapters/fiveg_dataset.py` 加载器（列名容错 + 按指标方向阈值，解决 RSRP/RSRQ/SNR「越低越差」反算）
  + `FiveGKpiAdapter.load_dataset_alerts`；`scripts/fetch_5g_dataset.py` 取数、`scripts/replay_5g_dataset.py`
  回放到 `/adapters/alert/ingest` → 运维 Agent 根因。测试 `tests/test_adapters_real.py`（含 5G 共 19 例）。
  剩余：① 量化评估基准 **✅ 已落地（v0.8.38，见下「已收工」）**；② OSS 导出高保真源（用户给文件后改 adapter 配置即切）。

- **量化评估基准（v0.8.38）**：`src/eval/rootcause_bench.py` **真正接 `OpsAgent` + 真实 triage 降噪层**，
  5G 合成故障注入器造带 `true_root` 标签小样本（4 类退化模式：弱覆盖 / 干扰 / 传输丢包 / 拥塞，特征可区分，
  避免 stub 退化成标签对标签的平凡正确），算**置信度口径 + 位置口径双 Top-1** 与**真实噪声抑制率**，
  并诚实标注 `verify_mode`（offline-stub / live-agent / simulated 修复仿真）。重写
  `scripts/eval_closed_loop.py` 接真实基准、删掉旧 `diagnose()` 假匹配器，加 `--live` / `--error-rate`；
  新增 `tests/test_eval_rootcause_bench.py` 7 例（taxonomy 自洽 / 合成样本形状 / 指标敏感 / verify_mode 诚实）。
  前端 `/metrics/summary` 读 `data/eval_results.json` 现显示**真·指标**，替换旧 0.875/1.0 假值。
  零回归：全量 204 passed（含 triage 11 + eval 7）。

## ❌ 待完善（建议推进序）
1. **真实电信数据接入（首切已落地，剩一项收尾）**
   - ✅ 数据源已定 `uccmisl/5Gdataset` 并打通「真实 KPI → 根因」管线（v0.8.37）。
   - ✅ **① 量化评估基准** 已落地（v0.8.38，见下「已收工」）。
   - ② **OSS 导出高保真源**：等用户给一份真实 OSS/网管导出（小区级 KPI+告警+拓扑）。
     **切源能力已就绪且已离线验证（零代码）**：`FiveGKpiAdapter` 支持 `data/adapters.json[alert-5g].column_aliases`
     运行时覆盖列名映射（华为/中兴/爱立信列名差异），`fiveg_dataset.load_payloads` 已透传；
     模板示例见 `data/adapters.example.json` 的 `_oss_example`（含期望 schema + 别名映射样例）。
     **已附样例与验证**：`samples/oss_sample_huawei.csv`（华为风格样例，3 小区×5 时点 + 2 噪声小区，覆盖
     弱覆盖/干扰/传输丢包/拥塞四类退化）、`scripts/validate_oss_sample.py`（离线三层验证：列映射→统一
     Alert、噪声抑制、Agent 接线冒烟，断言全绿）、测试 `test_fiveg_oss_column_aliases_override` 与
     `test_fiveg_oss_sample_csv_end_to_end` 锁死该行为。
     **剩余动作**：用户给真实导出文件（CSV）→ 在 `alert-5g` 填 `dataset_path` + `column_aliases` 即切源；
     若导出为 Excel 再补 loader 的 xlsx 支持。
2. **量化指标看板**（MTTR / 根因 Top-1 准确率 / 噪声抑制率）— **✅ 看板已完善（v0.8.39）**
   - 前端 `web/app.js` `renderMetrics` 重写：真·根因 **Top-1 双口径**（置信度 / 位置）卡片 +
     噪声抑制率（带样本量上下文）+ 修复成功率(仿真) + 仿真决策时延；**MTTR 因缺真实工单数据显式占位（不编造）**。
   - 诚实口径徽章（`verify_mode` 诊断/修复分离标注）+ Agent 冒烟状态 + 故障类型/predictor/生成时间上下文。
   - 修隐藏 bug：旧明细表读 `ev.details`（已废弃字段）永远空白 → 改用 `rootcause_details` + `noise_details` 真实渲染。
   - 后端 `eval_closed_loop.py` 产出加 `mttr_minutes: null` + `mttr_note` 显式标记缺口。
   - 剩余：MTTR 真正数值需带时间戳的工单闭环数据（真实 OSS/网管导出补齐后填）。
3. **MCP 接真实运维系统**（Grafana / Prometheus / Loki） ✅ **已落地（v0.8.40）**
   - `GrafanaAdapter`（数据源代理）+ `PrometheusAdapter`（直连 `/api/v1/query_range`）+ `LokiLogAdapter`（LogQL `/loki/api/v1/query_range`）三者齐备，均「配置驱动 + demo 兜底」。
   - 端点：`POST /adapters/metrics-grafana|metrics-prometheus/query`、`POST /adapters/logs-loki|log-elk/logs`。
   - 测试：`tests/test_adapters_real.py`（Prometheus/Loki demo+live 解析）+ `tests/test_monitoring_endpoints.py`（路由 200/404/400）。详见 `docs/09-接入真实监控系统.md`。
   - **Agent 诊断工具化（v0.8.43 已落地）**：内置只读工具 `pull_metrics` / `pull_logs`（注册进 `ToolRegistry`，随基线播种），`OpsAgent.rootcause` 提示词引导 LLM 在 `recommended_tool` 推荐二者并借 `tool_args` 携带 PromQL/LogQL，`run_recommended_tools` 透传 `tool_args` + 告警 host；未配 `data/adapters.json` 自动回退 demo。详见 `docs/09` 第 3 节。
4. **SQLite → Postgres**（连接串切换 + 迁移脚本） ✅ **已落地（v0.8.41）**
   - `src/core/db.py` 方言翻译层：AUTOINCREMENT→IDENTITY、INSERT OR IGNORE→ON CONFLICT DO NOTHING、
     executescript 拆句按方言执行；`TELEOPS_DB_DSN=postgresql://...` 即切，默认仍 SQLite 零依赖。
   - 测试：`tests/test_db_postgres.py`（纯翻译单测常跑 + 真连集成测试按 `TELEOPS_TEST_PG_DSN` 启停）。
   - 部署：`deploy/docker-compose.yml` 加 `postgres` 服务（`--profile pg` 启用）。
5. **多 worker + Caddy 多后端** ✅ **已落地（v0.8.42）**
   - `scripts/teleops_ctl.py` `start` 加 `--workers N`（uvicorn 多进程；>1 自动告警需 Postgres）
     + `--port P`（多副本各自独立 PID/日志文件，互不覆盖），`stop/restart/status` 同步支持 `--port`。
   - `scripts/Caddyfile` 反代 `{upstreams}` 占位符（默认 `127.0.0.1:8000`；`TELEOPS_BACKENDS`
     指定多副本 upstream）+ `health_uri /health` 主动探活；`caddy_runner._render_caddyfile` 注入。
   - 测试：`tests/test_caddy_runner.py` 骨架 + 渲染（含 `TELEOPS_BACKENDS` 多副本渲染断言）。
   - 部署：先 `TELEOPS_DB_DSN=postgresql://...` 切 Postgres，再 `start --workers N`；多机多副本用
     `TELEOPS_BACKENDS` 指各副本，Caddy 自动负载均衡 + 剔除不健康节点。详见 `docs/06` §8。

## 联动提醒
- HITL 审批单 / 运行时设置（`require_approval` 开关）的跨副本外部化**已在 v0.8.44 落地**
  （`TELEOPS_STATE_STORE=redis` 即落 Redis，详见 `docs/06` §8.4）。单机部署默认仍是本地 JSON，零依赖。
- **可回放审计时间线已在 v0.8.45 落地**：`GET /audit/timeline`（正序 + 密度分桶 minute/hour/day + 摘要统计），
  前端审计大屏升级为可回放时间线（密度轴 + 变速播放 + 暂停/继续 + 点击柱缩放），隔离规则与 `/audit` 一致。
- **SSO / OIDC 单点登录已在 v0.8.46 落地**：配置驱动 + mock 兜底，零外部依赖可演示（dev mock 无 IdP 可跑通，
  配 `TELEOPS_OIDC_ISSUER` 走真实 Authorization Code 流）。详见 `docs/04` 第 P2.5 条与 `src/core/oidc.py`。
- **审计 CSV 导出已在 v0.8.47 落地**：`GET /audit/export`（`format=csv/json`），复用 `/audit` 同一套多租户隔离与
  过滤（`_base_where` + `_apply_filters`），保证「看得到的才能导出」；UTF-8 BOM CSV，按当前筛选导出全部匹配记录，
  零外部依赖。前端审计大屏加「⬇ 导出 CSV」按钮（`fetch+blob` 下载，`apiFetch` 自动带 JWT）。详见 `docs/04` 第 8 条。
- **审计对象存储（OSS）归档已在 v0.8.48 落地**：`POST /audit/archive`，配置驱动 + 本地 mock 兜底（零依赖可演示，
  `TELEOPS_OSS_ENABLED=1` 即写本地 `data/oss_mock/`；配齐 `TELEOPS_OSS_BUCKET/ENDPOINT/ACCESS_KEY/SECRET_KEY` 走
  boto3 S3 兼容真实桶）。复用 `/audit/export` 同一套隔离与过滤，保证「看得到的才能归档」。详见 `docs/04` 第 9 条与
  `src/core/oss.py`。术语：此处 OSS = Object Storage，与「真实电信 OSS 数据导入」是两件事。
- **RBAC 引擎接线已在 v0.8.49 落地**：判定引擎 `auth.enforce` 此前完整但路由层零调用——本次经
  `src/api/deps.py::assert_perm`（路由层唯一权限判定入口，无权即 403 + 审计留痕）把能力闸**正交叠加**到既有租户闸之上：
  `stream_start` 加 `tool.exec`、`build_agent` + `register-gap` 加 `agent.manage`，实现 viewer/dev 被拦、sre/dev 各得所需；
  新增 `GET/POST /admin/roles`（org.manage 闸，可运营分配/回收角色，含两道锁死闸）。不放松既有 admin-only 闸。
  测试 `tests/test_rbac.py` 9/9。详见 `docs/04` 第 10 条。
- Phase 2 垂直扩容（4/5/6）内部有依赖序：先 Postgres → 再多 worker → 再 Caddy 多后端。
- **生产部署清单（百人规模）已出（v0.8.49）**：`docs/10-生产部署清单（百人规模）.md`——基于现有
  `deploy/docker-compose.yml` 串联出形态 A（单机容器化+Postgres+多 worker）与形态 B（多副本+Redis 状态外置），
  含容量评估、一键命令、头号坑（限流 XFF 反代）、备份回滚、上线检查清单。补 `deploy/README.md` Phase 0 的百人缺口。
- **合规与数据主权方案已出（v0.8.49）**：`docs/11-合规与数据主权方案.md`——等保级别判断框架、数据出域风险、
  模型私有化（改 `TELEOPS_LLM_BASE_URL` 零代码）、等保 2.0 要求映射、信创适配、分阶段路线图。纯方案层，不强制改代码。

## 下一步候选（等用户拍板）
- 剩余非阻塞增强（均已无代码阻塞，纯等时机）：
  - **真实 OSS 归档导出**（另一条独立线，与 OIDC/审计导出同一「配置驱动 + mock 兜底」哲学；用户给目标即接）。
- 价值最高的外部数据阻塞项仍是：**真实 OSS/网管导出源**（卡用户给文件，零代码切源）+ **MTTR 真·数值**（卡真实工单闭环数据）。
- **演示收尾打磨已做（v0.8.49）**：`scripts/demo.py` 一条命令拉起演示（起后端→注册 interviewer 账号→进个人域→
  启动 5G 实时告警流→开浏览器）+ `docs/12-一键演示与走查.md`（电梯演讲/点击路径/无网降级/边界兜底）；
  演示数据重主题为 5G（`data/alerts.json` 噪声样本 + `src/core/alert_stream.py` 的 `FAULT_ALERTS` 故障剧本，
  原 BGL 超算样本备份 `data/alerts.bgl_backup.json`）。`test_triage` 数据集期望已同步更新。
- **方法论验收文档已出（v0.8.49）**：`docs/13-方法论验收文档.md`——整合 ADR×10 / RACI / 风险登记册 /
  数据字典（11 表 + 文件存储 + `TELEOPS_*` 变量）/ 三环境策略 / 质量门 / 验收清单，专为求职验收与面试讲述收敛。
  测试套件事实：共 282 个测试函数（pytest 节点约 410，含参数化），约 6–12 个 Redis/PG 依赖用例本地自动 skip；
  全量跑有 2 个已知**共享 session-DB 执行 flake**（`test_audit_export::test_export_admin_sees_all_but_user_isolated`
  + `test_oss_export::test_archive_json_format`，`sqlite3.OperationalError`，隔离跑绿），属测试隔离问题非产品缺陷。
- **测试基线笔记纠正（覆盖旧误记）**：先前把 `test_monitoring_tools` 7 例失败记为「预存」不准确——
  实为演示运行期 dev Agent 重写基线 `tools/pull_metrics.py`/`pull_logs.py` 旧契约所致，`git checkout --` 还原后
  19 例全绿（**非预存，是运行副作用**）。当前唯一已知 flake 是上面两条导出测试的共享-DB 争用，
  修复方向=改独立 DB fixture（待办，不属产品缺陷）。
- **GitHub Actions CI 已修复并转绿（v0.8.50）**：⚠️ 先纠正认知——CI **并非"未接线"**，早在 v0.7.5（`4225bb9`）就建了
  `.github/workflows/ci.yml`，只是一直是红的（本机全绿掩盖）。此前用 `Glob(".github/**")` 查不到是**点号目录被 glob
  默认排除**导致的误判，我据此错误地报了"CI 缺失"。本次合并了旧配置的优点（py3.11/3.13 矩阵、workflow_dispatch、
  master 分支）与新增守卫，并把 4 个真问题修掉 → **py3.11 + py3.13 双矩阵 success**（run 36158192237）。
  排障关键手法：**job 日志需认证(403)，但 annotations 可公开读** → 让测试门失败时把 `FAILED` 行以 `::error::`
  抛出即可免凭据拿到精确失败用例名（否则 Windows 本机无法复现 Linux 失败，只能盲猜）。
  - 问题1 **依赖漂移**：`fakeredis` 缺 `[lua]` extra（本机装过 lupa、全新环境没有）→ Lua 不生效，
    `test_state_store` 4 例 + `test_semaphore` 2 例红（该拦截的被放行）。改 `fakeredis[lua]>=2.0`。
  - 问题2 **异步落库竞态**：审计后台线程写，断言类用例立刻查 → 每次红不同用例、两 Python 版本来回漂。
    加 `TELEOPS_AUDIT_SYNC`（调用时读 env）测试同步、生产异步；`test_audit_queue.py` 自我豁免保留异步语义测。
  - 问题3 **子串误命中**：`assert 'ws-2' not in body` 被 `ws-20` 误判（隔离逻辑没坏，同逻辑的
    `test_oss_export` 按列比对一直绿）→ 改 `csv.DictReader` 按列精确比对。
  - 问题4 ruff F821：`src/workers/stream_tasks.py` 用 `Any` 未导入（被 `from __future__ import annotations` 掩盖）。
- **CI 配置现状**：`.github/workflows/ci.yml`——推送/PR 到 main 触发（ubuntu-latest），
  五道守卫：敏感文件（`.env` 不得入库，排除 `*.example/sample/template`）/ `ruff check .` /
  `compileall` / `node --check web/app.js` / 全量 `pytest tests/ -q`。新增 `.ruff.toml`：只开
  E9/F63/F7/F82 关键规则（先守致命错误、再逐步收紧，避免首次跑红被迫挂 continue-on-error 使门禁失效）——
  上线即抓出 `src/workers/stream_tasks.py` 用了 `Any` 却未导入（F821，被 `from __future__ import annotations`
  掩盖才没在运行时炸）。**v0.8.51 已消除 Redis 缺口**：测试 fixture 优先读 `TELEOPS_TEST_REDIS_URL`，
  CI 起 `redis:7-alpine` service 并注入 → **D3 多副本状态共享 + 审批外部化两套回归守卫已在 CI 真跑并通过**。
  Postgres 真连代码路径已就绪（`TELEOPS_TEST_PG_DSN` + 测试门内 `pip install psycopg || true`），但因
  postgres service 在托管 runner 偶发拉取失败会让 job 变红，为保门禁稳定暂不纳入强制 CI（本地/Staging 有 PG 即真跑）。
- **共享-DB flake 已修（不再有豁免项）**：`tests/test_audit_export.py` 的 `_insert_audit` 原直连共享 DB 连接、
  在 `db._LOCK` 之外 `commit()`，与异步审计写后台线程争用同一连接 → 报 `cannot commit - no transaction is active`。
  改为走线程安全的 `db.execute()`（持锁并提交）后全量稳定全绿；`test_oss_export` import 复用同一函数，一并修复。
  **教训**：所谓"偶发 flake"往往有确定根因（并发/锁边界），先查根因别急着归类。
- **LLM 端点级熔断已落地（故障域，ADR-011）**：新增 `src/core/circuit_breaker.py`（closed/open/half_open，
  `TELEOPS_LLM_CB_THRESHOLD` 默认 5 / `_RESET` 默认 60s / `_ENABLED` 默认开，禁用时完全惰性）；
  `src/llm_client.py` complete() 接入——熔断打开即 fail-fast 降级 Mock，不再打已死端点（否则每条告警
  等 30s 超时，流水线"假死"、`/stream/stop` 也要等 join 超时）；`/health` 加 `llm_breaker` 快照，
  `/metrics` 加 `teleops_llm_breaker_state` gauge。新增 `tests/test_circuit_breaker.py` 10 例（含
  "熔断打开后真实调用次数停止增长"的实效验证）；`tests/conftest.py` 加 `_llm_breaker_reset` autouse fixture，
  防全局熔断单例跨用例污染导致"随机降级"型 flake。
