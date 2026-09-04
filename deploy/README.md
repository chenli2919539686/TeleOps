# TeleOps 自托管部署（Phase 0）

用 `docker-compose` 把 **backend（FastAPI，单端口同时提供 API + 界面）** 与 **Caddy（自动 HTTPS 反向代理）** 编排起来，实现「公网安全访问 + 进程守护 + 密钥不入库」。

> 本沙箱无 Docker，以下文件为交付物，无法在此构建/拉起；请在你的机器（Linux 服务器或 Windows + Docker Desktop）上执行。

## 0. 环境前提

- **Linux 服务器**：装 Docker Engine + Compose v2（`docker compose version` 显示 v2.x）。
- **Windows 本机（你当前环境）**：装 **Docker Desktop**，安装时勾选 **WSL 2** 后端；启动后在 `Settings → Resources → WSL Integration` 开启对应发行版。若 `docker compose up` 报 WSL / 脚本执行类错误，多为 WSL 未就绪，重启 Docker Desktop 即可。
- 镜像基础为 `python:3.11-slim`，首次构建会拉该镜像；Docker Hub 拉取慢可先 `docker pull python:3.11-slim` 预热。
- **密钥不进镜像**：`deploy/.env` 由 `deploy/.env.example` 复制，仅存在于宿主，compose 把它作为环境变量注入容器；`.dockerignore` 已排除根 `.env`、`deploy/.env` 与 `node_modules/` 等，镜像层里没有明文密钥。

---

## 1. 目录说明

```
deploy/
├── docker-compose.yml   # 编排：backend + caddy 两服务
├── Dockerfile           # backend 镜像（python:3.11-slim，仅装真实依赖）
├── requirements.txt     # 精简依赖（剔除 gradio/chromadb/pandas/httpx/pyyaml）
├── Caddyfile           # 自动 HTTPS 反代 backend:8000
├── .env.example        # 密钥样例
└── README.md            # 本文件
```

## 2. 快速开始

```bash
# 进入项目根目录（docker-compose.yml 的 context 是项目根）
cd /path/to/TeleOps

# 1) 准备密钥
cp deploy/.env.example deploy/.env
nano deploy/.env        # 填 DEEPSEEK_API_KEY 与 TELEOPS_DOMAIN
# 若你本地根目录 .env 已填过 DeepSeek Key，直接把那串值粘到 deploy/.env 的
# DEEPSEEK_API_KEY= 后面即可，无需重新去平台申请

# 2) 启动（自动构建镜像 + 拉起 Caddy）
docker compose -f deploy/docker-compose.yml up -d --build

# 3) 查看日志
docker compose -f deploy/docker-compose.yml logs -f
```

启动后访问：
- 本地自测：`http://localhost`（Caddy 在 80）或 `https://localhost`（Caddy 在 443，浏览器忽略自签警告）
- 公网生产：`https://你的域名`

## 3. 两种用法

### A. 本地自测（localhost）
- `TELEOPS_DOMAIN=localhost`
- Caddy 用**内部 CA 自签**证书，浏览器会提示「不安全」，忽略即可（或信任 Caddy 根证书）。
- 适合在你自己机器上验证整套链路。

### B. 公网生产（真实域名）
1. 准备一个域名，把 A 记录指向服务器**公网 IP**。
2. `TELEOPS_DOMAIN=teleops.example.com`。
3. 服务器**安全组 / 防火墙放行 80 与 443**（Let's Encrypt 验证与证书续期都需要 80）。
4. 强烈建议设置 `TELEOPS_API_TOKEN`（开启写接口鉴权），并在前端「设置」里填入相同值。
5. 启动后 Caddy 自动向 Let's Encrypt 申请证书并**自动续期**（证书存于 `caddy_data` volume）。

## 4. 密钥管理（不入库）

- `deploy/.env` 已在根 `.gitignore` 中被忽略，**切勿提交明文密钥**。
- 生产环境推荐用 Docker Secret 或宿主机的秘密管理（如 `sops` / Vault）；本方案先用 `.env` 文件落地，足够 Phase 0。
- `DEEPSEEK_API_KEY` 不填时，后端自动降级为离线 Mock，Demo 仍可完整跑通。

## 5. 数据持久化

业务数据落在 **named volume**，首跑从镜像内基线（`data/`、`kb/`、`tools/`）seed，之后持久化、重启不丢：

| Volume | 挂载点 | 内容 |
|---|---|---|
| `teleops_data` | `/app/data` | 业务域 / Agent / 需求 / 工具注册 / 操作记录 |
| `teleops_kb` | `/app/kb` | 知识库 markdown |
| `teleops_tools` | `/app/tools` | 研发 Agent 生成的工具脚本 |
| `teleops_traces` | `/app/traces` | 调试 trace |

查看 / 备份数据：
```bash
docker volume ls | grep teleops
docker run --rm -v teleops_data:/data -v $PWD/backup:/backup alpine cp -r /data /backup
```

## 6. 健康检查与守护

- backend 内置 `/health`（liveness：进程 + DB 存活、运行中任务数、限流状态）与 `/health/ready`（readiness：DB 可查询 + 数据目录可写），compose `healthcheck` 每 30s 探测一次。
- 数据层 SQLite 已开 WAL + busy_timeout，读写不互斥，锁冲突自动等待。
- `restart: unless-stopped`：进程崩溃 / 机器重启后自动拉起（替代手动后台启动）。
- 手动重启：`docker compose -f deploy/docker-compose.yml restart backend`

## 7. 常用命令

```bash
# 停止并保留数据
docker compose -f deploy/docker-compose.yml down

# 停止并删除数据卷（⚠️ 清空所有业务数据，仅演示用）
docker compose -f deploy/docker-compose.yml down -v

# 升级（改代码后重新构建）
docker compose -f deploy/docker-compose.yml up -d --build

# 进入容器排错
docker compose -f deploy/docker-compose.yml exec backend sh
```

## 8. 可选：可观测性（Prometheus + Grafana）

backend 内置 `/metrics`（Prometheus 文本格式，零第三方依赖）。用 `observability` profile 一条命令拉起 Prometheus + Grafana：

```bash
docker compose -f deploy/docker-compose.yml --profile observability up -d
# 打开 http://localhost:3000（默认 admin/admin，可在 deploy/.env 改）
# 已预置 Prometheus 数据源 + 「TeleOps 运行概览」面板（QPS/状态码/延迟分位/429 限流/任务·LLM 速率/业务 gauge/运行时长）
```

- `prometheus.yml`：每 15s 抓取 `backend:8000/metrics`；存储保留 7 天。
- `grafana/provisioning/`：数据源与面板均为启动自动加载（UI 改动可回写，重启不丢）。
- 生产请移除 compose 中 Grafana 的匿名访问两行，并改强密码。

## 9. 可选：启用 Chroma 向量检索

默认知识库用本地关键词检索（零额外依赖）。如需更优 RAG：
1. 取消 `deploy/requirements.txt` 中 `chromadb` 的注释并重建镜像；
2. 在 `deploy/.env` 设 `USE_CHROMA=1`。

## 10. 故障排查

| 现象 | 排查 |
|---|---|
| 访问 80/443 连不上 | 检查安全组是否放行 80/443；`docker ps` 看 caddy 是否 running |
| 证书申请失败（公网） | 域名 A 记录是否正确；80 端口是否可达；`docker logs <caddy>` 看 ACME 报错 |
| 界面能开但接口 401 | 若设了 `TELEOPS_API_TOKEN`，需在前端「设置」填相同值 |
| 界面空白 / API 404 | 确认 Caddyfile 已挂载且 backend 健康（`docker inspect` health） |
| LLM 无响应 | 检查 `DEEPSEEK_API_KEY`；未填则走 Mock，可在界面看「LLM: Mock 兜底」 |

---

## 版本边界说明

- 本目录为生产化部署产物（Phase 0 起步）。鉴权（JWT 多用户 / 共享 Token）、SQLite 持久化、
  pytest + CI、Prometheus 指标、限流、WAL 高可用与可观测性 Grafana 均已落地，详见项目根 `README.md`
  的「生产化改造（Phase 0 - Phase 3）」各章。
- 尚未做：组织级多租户硬隔离（决策与演进路径见根 README Phase 3 边界说明）。
- 单副本，无高可用；见 Phase 3。
