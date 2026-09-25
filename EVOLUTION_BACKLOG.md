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

## ❌ 待完善（建议推进序）
1. **真实电信数据接入**（最大价值跳：作品 → 行业方案）
   - 数据源需用户定（公开数据集 `uccmisl/5Gdataset` / OSS 导出 / 某现网导出）
   - 实现 `5G_kpi_adapter.parse_webhook` 把真实 5G KPI/告警喂进 `alert_stream`
2. **量化指标看板**（MTTR / 根因 Top-1 准确率 / 噪声抑制率）
   - 依赖真实数据先有；前端大屏加统计卡片（对齐 TelcoNet 的可量化说服力）
3. **MCP 接真实运维系统**（Grafana / Prometheus / Loki）
   - 架构已预留 `real_adapters.py`，落地较快；让 Agent 拉真实指标做诊断
4. **SQLite → Postgres**（连接串切换 + 迁移脚本）
   - `docs/04:28` 数据层已预留 DSN 切换点；建议多 worker 前先做
5. **多 worker**（uvicorn `--workers` + Caddy upstream 多目标）
   - **必须先切 Postgres**（SQLite 多进程写有坑，Windows spawn 也坑）；D3 Redis 已打底
6. **Caddy 多后端**（upstreams 指多副本 + `/health` 探活看 `replica_id`）

## 联动提醒
- 真·多副本投产时，HITL 审批单 / `data/settings.json` 是**本地 JSON 不跨副本**，
  需接 `docs/06` 的 Redis 外部化（本期未做）。
- Phase 2 垂直扩容（4/5/6）内部有依赖序：先 Postgres → 再多 worker → 再 Caddy 多后端。

## 下一步候选（等用户拍板）
- 异步审计写已完成 → 下一个最有性价比：
  - **真实数据接入**（价值最高，但需先定数据源）；
  - 或 **MCP 真连**（架构预留，落地快，无外部决策阻塞）。
