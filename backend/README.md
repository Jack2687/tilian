# Python API and Worker

## 本地连接内网分布式服务

仓库根目录的 `backend/.env` 已配置为连接 `db.example.internal` 上的 PostgreSQL、Redis 和 Kafka；启动 API：

```powershell
cd backend
.\.venv\Scripts\python.exe run.py
```

前端另开终端：

```powershell
cd frontend
npm run dev
```

本机 Worker 使用同一个 `backend/.env`，本地 API 固定在 `8010`，避免与机器上已有的 8000 端口冲突：

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.worker
```

本地 API 和 Worker 必须使用独立的 Kafka 命令主题、事件主题和消费组，防止新任务被内网旧版本 Worker 接走。本机已在 `backend/.env` 配置：

```dotenv
KAFKA_CONSUMER_GROUP=tilian-local-pc-20250530ktea
KAFKA_COMMAND_TOPIC=tilian.local.pc-20250530ktea.task.commands.v1
KAFKA_EVENT_TOPIC=tilian.local.pc-20250530ktea.task.events.v1
```

其他开发机器请替换 `pc-20250530ktea` 为自己的机器标识，并同时重启 API 和 Worker。只修改消费组会重复消费原主题，不能实现隔离。数据库和 Redis 仍为共享实例；本配置只隔离新任务的执行路由，不隔离卡密、额度和任务记录。Outbox 按每条记录保存的 `topic` 投递，既有生产任务不会自动迁移到本机队列。

在 `backend` 目录执行以下只读诊断，不会加入消费组或执行任务：

```powershell
.\.venv\Scripts\python.exe tools/runtime_status.py
```

`/health/ready` 会检查数据库、Redis、Kafka 实时元数据和当前消费组是否有在线 Worker；它不代表上游支付服务提链成功。

## 本地兼容模式

先运行现有 Node 服务（3000 端口），再启动 Python API：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:EXECUTION_MODE="legacy"
$env:LEGACY_API_URL="http://127.0.0.1:3000"
.\.venv\Scripts\python.exe run.py
```

兼容模式由 Python 暴露 `/api/v1/*`，真实任务、卡密及现有引擎暂由旧服务执行。

## 分布式模式

从仓库根目录运行：

```powershell
docker compose -f deploy/docker-compose.dev.yml up --build
```

首次切换前导入旧卡密：

```powershell
$env:DATABASE_URL="postgresql+asyncpg://tilian:tilian-dev-password@127.0.0.1:5432/tilian"
$env:OWNER_HASH_SECRET="change-this-before-production"
python backend/tools/import_legacy_cards.py data/cards.json
```

入口：前端 `http://127.0.0.1:8080`，API 文档 `http://127.0.0.1:8000/api/docs`。

生产环境必须替换 `OWNER_HASH_SECRET`、`ADMIN_API_TOKEN` 和 PostgreSQL 密码，并显式设置 `CREDENTIAL_ENCRYPTION_KEY`（Fernet key）。
