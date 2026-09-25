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
- Phase 2 垂直扩容（4/5/6）内部有依赖序：先 Postgres → 再多 worker → 再 Caddy 多后端。

## 下一步候选（等用户拍板）
- 剩余非阻塞增强：
  - **审计日志导出 CSV**（可回放时间线已做，导出为可选补充）。
- 价值最高的外部数据阻塞项仍是：**真实 OSS/网管导出源**（卡用户给文件，零代码切源）+ **MTTR 真·数值**（卡真实工单闭环数据）。
