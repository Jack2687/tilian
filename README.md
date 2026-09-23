# 提链控制台

多通道提链控制台，支持 **PayPal / GoPay / GCash / MoMo / UPI / BLIK / PIX / iDEAL / 直卡** 九个通道。

前后端分离架构：

- `frontend/` — Vue 3 + TypeScript + Vite 前端
- `backend/` — FastAPI API 与任务 Worker（Python）
- `engines/`、`*_engine/` — 各通道执行引擎（Python 子进程）
- `deploy/` — PostgreSQL + Redis + Kafka 的 Docker Compose 编排

---

## 启动

需要 Python 3.11+、Node.js ≥ 18；完整集群还需要 PostgreSQL、Redis、Kafka。

### 1. 配置

```bash
cp backend/.env.example backend/.env
```

按需填写 `DATABASE_URL`、`REDIS_URL`、`KAFKA_BOOTSTRAP_SERVERS`。
`CREDENTIAL_ENCRYPTION_KEY`、`OWNER_HASH_SECRET`、`ADMIN_API_TOKEN` 必须改成自己的随机值。

### 2. 安装依赖

```bash
# 后端（会一并拉取所有引擎的运行依赖）
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # Linux/macOS

# 前端
cd ../frontend
npm install
```

### 3. 启动（三个终端）

```bash
# 终端 1：Python API
cd backend && .venv/Scripts/python.exe run.py

# 终端 2：任务 Worker
cd backend && .venv/Scripts/python.exe -m app.worker

# 终端 3：Vue 前端
cd frontend && npm run dev
```

访问 **http://127.0.0.1:5173**。

### Docker Compose

```bash
cd deploy && docker compose -f docker-compose.dev.yml up -d
```

详见 [`deploy/README.md`](deploy/README.md)。

---

## 执行模式

`backend/.env` 的 `EXECUTION_MODE` 决定任务由谁执行：

- `distributed` — Python Worker 消费 Kafka 任务，走完整的新架构链路（推荐）
- `legacy` — 兼容模式，由 `/api/v1` 提供 API、外部执行服务执行真实任务

多个 Worker 使用同一个 Kafka 消费组，因此**同一批任务只会被一个 Worker 领取**。
本机开发时建议为每台机器设置独立的消费组和主题前缀，避免与其他环境互相抢任务：

```dotenv
KAFKA_CONSUMER_GROUP=tilian-local-<机器标识>
KAFKA_COMMAND_TOPIC=tilian.local.<机器标识>.task.commands.v1
KAFKA_EVENT_TOPIC=tilian.local.<机器标识>.task.events.v1
```

---

## 目录结构

```text
backend/                 FastAPI API、Worker、任务编排、卡密与额度
  app/engine_adapter.py  各通道 → 引擎子进程的命令构造
  app/worker.py          Kafka 消费、重试、指纹轮换
frontend/                Vue 3 提链工作台与管理后台
engines/
  pay153-checkout-link-main/   UPI 引擎（含 upi_cli.py）
  freepp-backend/              PayPal 引擎与共享哨兵实现
  sms_tool/                    PayPal hosted 链路
blik_engine/  card_engine/  gcash_engine/  gopay_engine/
ideal_engine/ momo_engine/  pix_engine/    各通道引擎
deploy/                  Docker Compose 与部署说明
docs/                    架构、授权与通道说明
sql/                     数据库结构与高可用相关 SQL
```

---

## UPI 通道

UPI 默认使用 `upi-git5` rail：**纯协议、不启动浏览器**。哨兵令牌由本地 Node 桥生成，
风控上下文（部署证明、观察证明、Stripe 标识）在本地按 HAR 形状构造。

需要退回浏览器执行路径时，在任务的 `paramsText` 里显式指定 rail：

```json
{"upiRail": "upi"}
```

可用值：`upi`、`upi-git`、`upi-git2`、`upi-git3`、`upi-git4`、`upi-git5`。

---

## 管理员接口

需要 `ADMIN_API_TOKEN`，通过 `Authorization: Bearer <token>` 传入。常用接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/admin/overview` | 任务、卡密、额度总览 |
| `GET /api/v1/admin/tasks` | 任务列表 |
| `POST /api/v1/admin/cards/generate` | 批量生成卡密 |
| `GET /api/v1/admin/proxies` | 各通道代理池配置 |

健康检查：`GET /health/live`、`GET /health/ready`（`ready` 会检查数据库、Redis、Kafka 与在线 Worker）。

---

## 安全说明

- `backend/.env`、`data/`、各引擎的 `.env` 均已加入 `.gitignore`，不要提交。
- 卡密、Access Token 等在数据库中以加密形式保存，密钥由 `CREDENTIAL_ENCRYPTION_KEY` 提供。
  该密钥一旦更换，已保存的凭据将无法解密。
- 部署到公网前请务必更换所有默认密钥与密码。
