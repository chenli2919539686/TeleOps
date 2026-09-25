# 07 · 人工审批（HITL）闭环

> 版本：v0.8.35 起可用
> 定位：企业级「高风险动作不裸奔」护栏。开启后，研发造工具、启动告警流等高风险动作不再直接执行，而是落人工审批单，由管理员批准后才真正执行（Human-in-the-Loop）。

---

## 一、为什么需要

运维 Agent 的「造工具 / 重启服务 / 启动告警流」属于**爆破半径大**的动作。对齐 AgentOS 的「爆破半径 / VIP / 时段」安全自愈思路，TeleOps 在动作真正执行前加一道人工闸：

- 避免 Agent 自动决策直接对生产产生影响；
- 满足企业「变更需审批留痕」的合规要求；
- 与既有审计日志打通，审批/执行/拒绝全链路可回放。

---

## 二、两层审批设计

TeleOps 的审批是**两层**的，不要混淆：

| 层 | 触发点 | 开关 | 说明 |
|---|---|---|---|
| ① 构建期全局闸 | `tool.build`（`POST /agents/{id}/build`）、`stream.start`（`POST /stream/start`） | `TELEOPS_REQUIRE_APPROVAL` 环境变量 + admin 运行时开关 | 开启后**所有**高风险动作落 pending 审批单，不直接执行 |
| ② 运行时按工具风险 | 运维 Agent 派发前 | `ToolRegistry.require_human_approval` 列 | `restart_service` 等高危工具被标记，Agent 在真正调用前二次确认 |

本档聚焦**第①层**（全局闸 + 审批闭环）。第②层是工具元数据驱动的既有能力。

---

## 三、开关与配置

### 方式一：环境变量（启动期固定）
```bash
TELEOPS_REQUIRE_APPROVAL=1   # 1/true 开启；不设置或 0 关闭
```

### 方式二：admin 运行时开关（持久化，推荐）
```http
POST /settings/require-approval
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "enabled": true }
```
- 仅 **admin** 可调用，普通用户返回 `403`。
- 写入 `data/settings.json`，**重启后保持**（文件优先于环境变量，env 作兜底）。
- 读取优先级（`src/core/settings.py`）：`data/settings.json` 的 `require_approval` 字段 → 回退环境变量 `TELEOPS_REQUIRE_APPROVAL`。

### 前端
审批面板（「审批队列」视图）顶部有开关按钮，**仅 admin 可见**；点击即调用上述端点切换，文案实时回显「已开启 / 未开启」。

---

## 四、闭环端点

| 动作 | 端点 | 权限 | 说明 |
|---|---|---|---|
| 列审批单 | `GET /approvals` | 登录 | admin 见**全部**，普通用户仅见自己发起/处置的单；返回体含 `require_approval` 当前开关状态 |
| 提交待审 | `POST /agents/{id}/build`、`POST /stream/start` | 登录（开启闸时） | 不直接执行，返回 `{"status":"pending_approval","approval_id":"apr-..."}` |
| 批准并执行 | `POST /approvals/{id}/approve` | **admin** | 批准后真正执行：tool.build 跑研发造工具流程；stream.start 真正启动告警流（`_run_stream_start`） |
| 拒绝 | `POST /approvals/{id}/reject` | **admin** | 标记 rejected，不执行 |

审批单结构（`src/core/approvals.py`，JSON 落盘 `data/approvals.json`）：

```json
{
  "id": "apr-<uuid>",
  "subject": "tool.build | stream.start",
  "requested_by": "<username 或 sub>",
  "payload": { "agent_id": "...", "feedback": {...} },   // 批准时原样回放执行
  "detail": { "k": "v" },
  "status": "pending | approved | rejected | approved_failed",
  "decided_by": "<admin>",
  "created_at": "...",
  "decided_at": "..."
}
```

---

## 五、执行语义（关键）

- **状态/控制面解耦**：`stream.start` 被审批时只是「没启动」，不占用任何资源；批准后 `_run_stream_start` 重建 playlist + processor 并经 `stream_executor.start` 真正启动（含 `is_running` 409 守卫，重复启动会被拦）。
- **执行失败不卡单**：tool.build 批准执行若失败，审批单标记 `approved_failed` 并回带错误，不影响其它审批；前端照常展示。
- **拒绝即终止**：reject 后该动作永不执行，审计留痕 `approval.reject`。
- **审计对账**：每一次 `pending / approve / reject / approved_failed` 都经 `_audit_write` 进审计日志，含 `approval_id`、`subject`、`workspace_id`，可回放。

---

## 六、测试覆盖

`tests/test_approvals.py`（5 例，临时目录隔离，不污染 `data/`）：

1. `test_store_crud` — 审批存储层 create/list/get/decide + 越权不可见。
2. `test_stream_start_gated_and_approved` — 开启闸后 stream.start 落 pending；非 admin 批准 403；admin 批准后流真正启动（`executed=True`）。
3. `test_stream_start_rejected` — 拒绝后不启动。
4. `test_tool_build_gated` — tool.build 落 pending；admin 批准闭环不崩。
5. `test_settings_runtime_toggle` — admin 运行时切换 + 持久化；非 admin 403。

---

## 七、运维要点

- **默认关闭**：不配置时高风险动作直接执行（与历史行为一致），开箱即用不阻断。
- **生产建议开启**：对外部署（尤其多用户 / 局域网 / 公网）建议 `TELEOPS_REQUIRE_APPROVAL=1` 或在设置面板开启，给运维 Agent 一道护栏。
- **与多副本兼容**：审批单与运行时设置均为**本地 JSON 文件**，当前是单文件单写；多副本部署下若需跨副本共享审批态，应外移到 Redis/DB（见 `docs/06` 的状态外部化思路，本期未做）。
- **不要滥用闸**：闸只拦「构建期全局」两类动作；工具级风险（第②层）走 `ToolRegistry.require_human_approval`，二者互补。
