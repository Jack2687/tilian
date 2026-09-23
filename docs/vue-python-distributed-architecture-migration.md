# 提链控制台架构改造任务书

## 实施进度（2026-09-20）

第一轮改造已经落地：

- [x] 新建 Vue 3 + TypeScript + Vite 前端，完成用户提链工作台和响应式布局；
- [x] 新建 FastAPI `/api/v1`，统一错误、健康检查、Prometheus 指标和 CORS；
- [x] 增加 `legacy` 兼容模式，无损代理当前 Node 卡密、任务和统计接口；
- [x] 增加 PostgreSQL SQLAlchemy 模型、Alembic 初始 migration、凭证加密和卡密哈希；
- [x] 增加 Redis owner-token 租约、Kafka 幂等 producer/consumer、outbox relay 和 DLQ 配置；
- [x] 增加多通道 Python EngineAdapter、Worker 并发、重试和额度提交/释放；
- [x] 增加 Docker Compose 及 API/Web/Worker 镜像；
- [x] 在 `db.example.internal` 完成 PostgreSQL、Redis、Kafka、API、双 Worker 和 Vue Web 的集成冒烟；
- [ ] 执行 Worker 中断、Kafka 重复消费、Redis 故障和额度对账演练；
- [ ] 生产灰度后关闭 `legacy` 写路径并下线 Node 协调器。

开发机默认保留 `legacy` 模式。内网生产实例已使用独立随机密钥、导入旧卡密并切换到
`distributed` 模式，入口为 `http://db.example.internal:18080`。在扩大真实流量前，仍须完成
重复消费、Worker 中断和额度对账演练。

## 1. 文档目的

本文用于指导现有提链控制台从“Node.js 单体服务 + 静态 HTML/JS + 本地文件/进程内状态”改造成：

- Vue 3 前端，与后端 API 完全分离；
- Python 后端，负责认证、任务编排、业务 API 和管理 API；
- PostgreSQL 作为唯一业务数据源；
- Redis 作为缓存、分布式锁、限流和短期任务状态存储；
- Kafka 作为任务异步化和领域事件总线；
- API、任务消费者、执行 Worker 均可水平扩展；
- 支持单机开发、Docker Compose 测试环境和生产集群部署。

目标不是一次性重写全部提链引擎，而是先稳定接口和任务模型，再逐步迁移现有引擎。迁移期间保留旧 Node 服务作为兼容层，直到新链路完成灰度验证。

## 2. 当前基线与改造边界

### 2.1 当前代码可复用部分

当前仓库已经具备以下能力，迁移时应优先复用业务规则，避免重复实现：

| 当前能力 | 现状 | 改造后的归属 |
| --- | --- | --- |
| 提链引擎 | `gcash_engine/`、`gopay_engine/`、`momo_engine/`、`pix_engine/`、`ideal_engine/` 等 Python 引擎 | Python Worker，通过统一任务协议调用 |
| 任务调度 | `src/engine.js`，进程内队列、重试、租约、任务状态 | Python Orchestrator + Kafka Consumer |
| HTTP API | `server.js` 中的 `/api/*` | Python API Service，保持兼容路径和响应字段 |
| 前端 | `public/*.html`、`public/js/*.js` | Vue 3 SPA，逐页替换 |
| 集群执行 | `src/cluster_executor.js`，本地/SSH 节点 | Worker Deployment；SSH 方式仅作为迁移期兼容方案 |
| 高可用表结构 | `sql/ha_schema.sql` | 扩展为正式 PostgreSQL migration |
| Redis 并发租约 | `src/state_store.js` 中的可选 Redis 逻辑 | Redis Lease/Lock/Cache 组件 |
| 卡密、用户、日志规则 | `src/cards.js`、`src/users.js`、`src/logs.js` | PostgreSQL Repository + Service |

### 2.2 本次改造不包含

- 不改变各支付渠道的上游协议和提链业务规则；
- 不在第一阶段重写 Python 引擎内部的浏览器指纹、代理、重试算法；
- 不把支付凭证、AT、代理密码写入 Kafka 消息或普通日志；
- 不允许把 PostgreSQL、Redis、Kafka 同时当作“最终账本”。最终状态、额度和审计记录只能以 PostgreSQL 为准。

## 3. 目标架构

```text
                         +----------------------+
                         |  Browser              |
                         |  Vue 3 + Vite SPA     |
                         +----------+-----------+
                                    |
                              HTTPS / JSON
                                    |
                         +----------v-----------+
                         | API Gateway / Ingress |
                         +----------+-----------+
                                    |
                 +------------------+------------------+
                 |                                     |
       +---------v----------+                +---------v----------+
       | Python API Service |                | Admin API Service  |
       | FastAPI + JWT      |                | same deployable     |
       +----+-----------+---+                +---------+----------+
            |           |                              |
            |           +------------------------------+
            |                                          |
   +--------v---------+                       +--------v---------+
   | PostgreSQL       |                       | Redis            |
   | source of truth  |                       | cache/lock/rate  |
   | tasks/quota/logs |                       | limit/lease      |
   +--------+---------+                       +--------+---------+
            |                                          |
            +------------------+-----------------------+
                               |
                     +---------v----------+
                     | Kafka Cluster      |
                     | task/event topics  |
                     +---------+----------+
                               |
             +-----------------+------------------+
             |                                    |
   +---------v----------+                +--------v---------+
   | Orchestrator       |                | Result/Event     |
   | task consumer      |                | consumer         |
   | retry/lease        |                | projection/log   |
   +---------+----------+                +------------------+
             |
   +---------v-----------------------------------+
   | Python Worker Pool                          |
   | GCash / GoPay / MoMo / UPI / Card / PIX... |
   +---------------------------------------------+
```

### 3.1 服务职责

| 服务 | 主要职责 | 是否无状态 |
| --- | --- | --- |
| `web` | 托管 Vue 静态资源，反向代理 `/api` | 是 |
| `api` | 用户、卡密、任务提交、任务查询、管理 API；鉴权和参数校验 | 是 |
| `orchestrator` | 消费任务、拆分 item、额度预留、发布执行命令、重试和超时 | 尽量无状态，租约在 Redis，结果在 PG |
| `worker` | 调用现有 Python 引擎，回传标准化阶段事件和最终结果 | 是；凭证只存在任务执行内存 |
| `projector` | 消费领域事件，更新任务明细、统计、日志读模型 | 是，可多副本 |
| `scheduler` | 定时补偿、超时扫描、死信重放、清理过期缓存 | 可多副本，使用 Redis leader lock |

第一阶段可以把 `api`、`orchestrator`、`projector`、`scheduler` 放在同一个 Python 进程中，通过进程角色参数启动；第二阶段再拆成独立 Deployment。

## 4. 技术约束与统一规范

### 4.1 Python 后端

- Python 3.11+；FastAPI、Pydantic v2、SQLAlchemy 2.x、Alembic、`asyncpg`；
- Kafka 客户端统一使用 `aiokafka`；Redis 使用 `redis-py` asyncio API；
- 所有外部输入先经过 Pydantic schema 校验，再进入 Service；
- 路由层不直接操作数据库，不直接调用提链引擎；
- 所有时间使用 UTC 存储，API 返回 ISO 8601；
- API 使用版本前缀 `/api/v1`，旧 `/api/*` 在迁移期由兼容路由转发。

### 4.2 Vue 前端

- Vue 3 + TypeScript + Vite + Pinia + Vue Router；
- API 请求集中在 `src/api/`，禁止组件内散落 `fetch`；
- 任务列表使用轮询起步，后续可增加 SSE；不直接连接 Kafka/Redis；
- 用户端和管理端共享鉴权、错误提示、任务状态枚举及类型定义；
- 旧页面与 Vue 页面并存时，使用 `/legacy/*` 前缀隔离，避免路由冲突。

### 4.3 机密和数据分级

| 数据 | 存储规则 |
| --- | --- |
| 用户、任务、额度、审计日志 | PostgreSQL |
| 会话、限流计数、幂等键、短期租约、热点任务摘要 | Redis，设置 TTL |
| Kafka 消息 | 只放 ID、状态、脱敏字段和引用；默认保留 7 天 |
| AT、代理密码、支付敏感字段 | 仅在执行上下文短暂存在；必要时使用 KMS/应用级加密后放 PG |
| 日志 | 禁止打印 AT、完整卡密、代理认证信息、支付链接中的敏感 query |

## 5. PostgreSQL 数据模型

在现有 `sql/ha_schema.sql` 基础上通过 Alembic 管理版本。核心表如下：

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `users` | `id`, `uid`, `name`, `status`, `last_seen_at` | 用户身份和在线状态 |
| `cards` | `code_hash`, `quota`, `used_quota`, `reserved_quota`, `status` | 卡密只存 hash，展示时保留脱敏值 |
| `tasks` | `id`, `uid`, `mode`, `status`, `request_id`, `version`, `created_at` | 任务主表；`request_id` 唯一保证创建幂等 |
| `task_items` | `task_id`, `item_index`, `channel`, `status`, `attempt`, `result`, `error_code` | 每个账号/AT 的执行结果 |
| `quota_ledger` | `request_id`, `task_id`, `item_index`, `delta`, `kind` | 额度预留、提交、释放的不可变流水 |
| `task_events` | `event_id`, `task_id`, `item_index`, `event_type`, `payload`, `created_at` | 业务审计和补偿依据 |
| `execution_leases` | `lease_key`, `worker_id`, `expires_at` | PG 记录用于审计，实时租约以 Redis 为准 |
| `audit_logs` | `actor_id`, `action`, `resource_id`, `metadata` | 管理员操作审计 |

必须满足：

1. `tasks.request_id`、`quota_ledger(request_id, kind)`、`task_events.event_id` 均有唯一约束；
2. 额度预留与任务创建在同一 PostgreSQL 事务中完成；
3. 任务状态更新使用 `version` 乐观锁，禁止旧消费者覆盖新状态；
4. JSONB 只保存扩展字段，查询和筛选字段单独建列并建立索引；
5. 任务明细、事件和日志按月或按季度分区，保留策略由运维配置。

## 6. Redis 使用规范

Redis 是加速层和协调层，不是业务账本。统一 key 前缀：`tilian:{env}:`。

| Key | TTL | 用途 |
| --- | --- | --- |
| `session:{token_id}` | 30 min | 登录会话/刷新令牌引用 |
| `rate:{scope}:{id}` | 1 min | 登录、兑换卡密、任务提交限流 |
| `task:summary:{task_id}` | 24 h | 任务查询热点摘要 |
| `task:lease:{task_id}:{item_index}` | 2 min，续租 | 防止重复执行 |
| `quota:lock:{card_id}` | 5 s | 卡密并发修改互斥；最终仍靠 PG 事务 |
| `scheduler:leader` | 15 s，续租 | 定时调度/补偿任务选主 |
| `idempotency:{request_id}` | 24 h | API 幂等结果缓存 |

所有锁必须带 owner token，释放使用 Lua 校验 owner；任何 Redis 操作超时不能导致额度提交成功或任务永久卡死。Redis 故障时：读请求可回源 PG，提交任务按配置选择“拒绝并提示稍后重试”或降级到单节点模式，生产默认拒绝高风险写操作。

## 7. Kafka Topic 与消息契约

Kafka 使用 3 副本、`acks=all`、幂等生产者。消息统一包含：

```json
{
  "event_id": "uuid",
  "event_type": "task.created.v1",
  "occurred_at": "2026-09-20T10:00:00Z",
  "trace_id": "uuid",
  "task_id": "task-id",
  "item_index": 0,
  "payload": {}
}
```

Topic 规划：

| Topic | Key | 生产者 | 消费者 |
| --- | --- | --- | --- |
| `tilian.task.commands.v1` | `task_id:item_index` | API/Orchestrator | Worker |
| `tilian.task.events.v1` | `task_id:item_index` | Worker | Projector、审计、监控 |
| `tilian.task.retry.v1` | `task_id:item_index` | Orchestrator | Retry Consumer |
| `tilian.payment.events.v1` | `task_id:item_index` | Worker/回调 | Projector |
| `tilian.audit.events.v1` | `actor_id` 或 `task_id` | API/管理端 | 审计落库 |
| `tilian.dlq.v1` | 原消息 key | 各消费者 | 人工/定时重放 |

消息语义要求：

- Kafka 至少一次投递，消费者必须幂等；
- 同一 `task_id:item_index` 使用相同 partition key，保证单 item 事件有序；
- 消费成功后再提交 offset；数据库写入和 offset 提交之间使用 `event_id` 去重；
- 失败按 1s/5s/30s/5min 退避，超过次数进入 DLQ；
- 不在 Kafka 中传完整凭证，Worker 通过任务 ID + 授权服务获取一次性执行材料。

## 8. API 契约迁移

### 8.1 用户端 API

| 方法 | 新路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 创建/恢复用户身份 |
| POST | `/api/v1/auth/login` | 用户登录，返回 access token |
| POST | `/api/v1/auth/heartbeat` | 更新在线状态 |
| GET | `/api/v1/me` | 当前用户、余额和权限 |
| POST | `/api/v1/cards/redeem` | 兑换卡密，返回幂等结果 |
| POST | `/api/v1/tasks` | 创建任务，支持 `Idempotency-Key` |
| GET | `/api/v1/tasks` | 分页查询用户任务 |
| GET | `/api/v1/tasks/{id}` | 查询任务和 item 结果 |
| POST | `/api/v1/tasks/{id}/cancel` | 取消任务 |
| POST | `/api/v1/tasks/{id}/items/{index}/qr/refresh` | 刷新二维码 |

### 8.2 管理端 API

管理 API 统一要求管理员 JWT + 权限声明：

`GET /api/v1/admin/overview`、`GET/POST /api/v1/admin/cards`、`GET /api/v1/admin/logs`、`GET /api/v1/admin/users`、`POST /api/v1/admin/stats/reset`、`GET /api/v1/admin/cluster`。

旧 `/api/*` 接口在迁移期保持响应字段兼容，并在响应头增加 `Deprecation: true` 和 `Sunset` 日期。禁止新前端继续调用旧路径。

## 9. 推荐仓库目标目录

```text
frontend/                         # Vue 3 + TypeScript + Vite
  src/api/                        # OpenAPI 生成或手写 API client
  src/stores/                     # auth/task/admin
  src/views/                      # user/admin 页面
backend/
  app/main.py                     # FastAPI 入口
  app/api/v1/                     # 路由
  app/domain/                     # 实体、状态机、事件
  app/services/                   # 任务、额度、认证、卡密服务
  app/repositories/               # PostgreSQL/Redis 仓储
  app/messaging/                  # Kafka producer/consumer
  app/workers/                    # orchestrator/projector/scheduler
  app/adapters/engines/           # 现有 Python 引擎适配器
  migrations/                     # Alembic
deploy/
  docker-compose.dev.yml
  k8s/                            # Deployment/Service/HPA/ConfigMap/Secret
contracts/
  openapi.yaml
  events/*.json                   # JSON Schema
tests/
  contract/                       # API 与事件契约测试
  integration/                    # PG/Redis/Kafka 集成测试
```

## 10. 分阶段实施任务

### 阶段 0：基线和冻结（1-2 天）

- [ ] 固定当前 Node 服务的 API 响应样例、任务状态枚举和错误码。
- [ ] 导出本地 JSON 中的用户、卡密、任务和统计数据，生成脱敏迁移样本。
- [ ] 建立 `contracts/openapi.yaml`、事件 JSON Schema 和兼容性测试。
- [ ] 明确生产环境的域名、TLS、Kafka/PG/Redis 连接方式和密钥托管方式。
- [ ] 设定迁移开关：`LEGACY_API_ENABLED`、`NEW_TASK_WRITE_ENABLED`、`TASK_READ_SOURCE`。

**完成标准：** 新旧服务对同一请求的字段、状态和错误码差异有记录；可以在测试环境重复导入样本数据。

### 阶段 1：基础设施和 Python 骨架（3-5 天）

- [ ] 初始化 `backend/`、依赖锁定、代码质量检查、pytest/ruff/mypy/CI。
- [ ] 接入 PostgreSQL、Redis、Kafka 健康检查和启动探针。
- [ ] 用 Alembic 创建用户、卡密、任务、任务明细、额度流水、事件和审计表。
- [ ] 实现配置分层：本地 `.env`、测试 ConfigMap、生产 Secret；禁止提交真实密钥。
- [ ] 实现统一错误结构：`code`、`message`、`trace_id`、`details`。

**完成标准：** API、Worker、Projector 在 Docker Compose 中可启动；数据库 migration 可重复执行；依赖故障能被探针正确报告。

### 阶段 2：任务和额度核心链路（5-8 天）

- [ ] 实现任务创建事务：校验参数 -> 预留额度 -> 写 `tasks/task_items/quota_ledger` -> 发布 command。
- [ ] 实现任务状态机：`queued`、`running`、`waiting_blik`、`waiting_plus`、`success`、`failed`、`timed_out`、`canceled`。
- [ ] 实现 Kafka command producer、Worker consumer、事件 projector 和 DLQ。
- [ ] 迁移现有重试、超时、取消、QR 刷新和 Plus 等待语义。
- [ ] 通过 Redis lease 保证同一 item 同时只有一个 Worker 执行；租约丢失时 Worker 必须停止提交结果。

**完成标准：** 重复提交、重复消费、Worker 崩溃、API 重试均不会重复扣额度或覆盖新状态；任务最终状态可从 PG 重建。

### 阶段 3：引擎适配和灰度（5-10 天）

- [ ] 为每个现有引擎实现统一 `EngineAdapter`：输入 schema、阶段事件、成功结果、可重试错误、不可重试错误。
- [ ] Worker 只通过 adapter 启动现有 Python 脚本，保留代理、指纹和渠道配置。
- [ ] 先灰度 `simulate` 模式，再灰度单一渠道，最后开放多渠道。
- [ ] 迁移 `src/cluster_executor.js` 的节点容量概念为 Worker 并发配置和 Kubernetes HPA。
- [ ] 兼容期内保留旧 Node API；新 API 可通过 feature flag 路由到新任务链路。

**完成标准：** 新旧链路对同一组测试账号的成功率、错误分类、耗时和额度结果在允许误差内一致。

### 阶段 4：Vue 前端替换（4-8 天）

- [ ] 初始化 Vue 3 + TypeScript + Vite + Pinia + Router。
- [ ] 抽取登录、用户状态、卡密兑换、任务创建、任务详情、日志和管理台页面。
- [ ] 引入统一任务状态组件，避免不同页面自行解释状态。
- [ ] 任务详情先用 2-3 秒轮询；确认事件投影稳定后再增加 SSE。
- [ ] 增加前端错误边界、请求超时、重复提交保护和权限路由守卫。

**完成标准：** 用户端和管理端不再依赖 `public/js`；浏览器 Network 中只出现 `/api/v1/*`；刷新页面后任务状态可恢复。

### 阶段 5：集群化和下线旧服务（3-5 天）

- [ ] 部署 API 多副本，配置 Ingress、TLS、CORS、健康探针和优雅退出。
- [ ] 部署 Orchestrator/Projector/Worker 多副本，按 Kafka consumer group 扩容。
- [ ] 配置 PostgreSQL 主备/连接池、Redis Sentinel 或 Cluster、Kafka 3 broker/3 replica。
- [ ] 完成断电、进程杀死、网络抖动、Kafka 重平衡、Redis 故障和 PG 主备切换演练。
- [ ] 关闭旧 API 写入，观察至少一个完整业务周期后再下线 Node 静态服务。

**完成标准：** 任一 API/Worker 实例故障不会丢任务；单实例滚动发布期间提交、查询和消费持续可用；无重复扣费。

## 11. 集群部署基线

开发环境使用 Docker Compose：1 个 API、1 个 Worker、1 个 PostgreSQL、1 个 Redis、1 个 Kafka。

生产初始规模建议：

| 组件 | 副本 | 扩容依据 |
| --- | ---: | --- |
| Vue/Web | 2 | CPU、连接数 |
| API | 2 | CPU、P95 延迟 |
| Orchestrator | 2 | Kafka lag、任务排队长度；Redis leader lock 防双调度 |
| Projector | 2 | Kafka lag |
| Worker | 2 起步 | 渠道并发、CPU/RSS、上游 429/5xx |
| PostgreSQL | 主库 + 热备 | 连接数、IOPS、复制延迟 |
| Redis | Sentinel 3 节点或 Cluster | 命中率、内存、命令延迟 |
| Kafka | 3 broker | lag、磁盘、吞吐 |

关键环境变量：

```text
APP_ENV=prod
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
KAFKA_BOOTSTRAP_SERVERS=kafka-0:9092,kafka-1:9092,kafka-2:9092
KAFKA_CONSUMER_GROUP=tilian-prod
TASK_LEASE_TTL_SECONDS=120
TASK_MAX_RETRIES=3
LEGACY_API_ENABLED=true
```

## 12. 可观测性、安全与验收

### 12.1 指标和日志

- API：请求量、P50/P95/P99、4xx/5xx、按路由和错误码统计；
- Kafka：每个 topic 的 consumer lag、重试数、DLQ 数；
- 任务：排队时长、执行时长、成功率、超时率、按渠道/节点拆分；
- 额度：预留、提交、释放、对账差异；
- Redis/PG：连接池使用率、命令延迟、锁等待、慢查询、复制延迟；
- 使用 OpenTelemetry 贯穿 `trace_id`，日志输出 JSON；敏感字段脱敏。

### 12.2 安全要求

- 管理员 JWT 与普通用户权限分离，管理接口逐路由授权；
- CORS 只允许正式前端域名；生产禁止直接暴露 PG、Redis、Kafka；
- 卡密只存 hash，兑换接口限流并记录审计；
- 所有回调校验签名、时间窗和幂等键；
- 镜像以非 root 用户运行，依赖和基础镜像定期扫描；
- 任务取消、卡密禁用、统计清理等高风险操作必须写审计日志。

### 12.3 发布验收清单

- [ ] 单元测试覆盖额度状态机、任务状态机、事件去重和租约续期。
- [ ] 集成测试覆盖 PG/Redis/Kafka 重启和重复消息。
- [ ] 契约测试保证 Vue 与 Python API 字段一致。
- [ ] 压测 100/200/300 并发，记录 CPU、RSS、事件循环延迟、Kafka lag、PG 连接数和上游 429/5xx。
- [ ] 故障演练后，任务最终状态、额度流水和审计记录可对账。
- [ ] 回滚时可切回旧 Node API，且新服务已创建的任务仍可查询、不可重复扣费。

## 13. 主要风险和处理策略

| 风险 | 处理策略 |
| --- | --- |
| 双写导致任务状态分叉 | 迁移期明确唯一写入方；以 PG 版本号和事件去重约束收敛 |
| Kafka 重复消费 | `event_id` 唯一表 + 幂等 projector + offset 成功后提交 |
| Redis 丢失租约 | Worker 续租失败立即停止写结果；由补偿任务依据 PG 状态重新入队 |
| PG 连接耗尽 | API/Worker 分离连接池，设置上限和超时，禁止长事务 |
| 旧 Node 与新 Worker 同时扣费 | 额度提交集中到 Python Service，旧链路只读或关闭扣费能力 |
| 引擎进程泄漏 | Worker 子进程超时、父进程退出清理、RSS/句柄监控和隔离队列 |
| 凭证进入消息/日志 | 事件 schema 禁止敏感字段，CI 扫描日志和消息样本 |
| 上游限流放大 | 按渠道、代理出口、Worker 设置并发和启动间隔，逐级压测 |

## 14. 交付物

1. `frontend/` Vue 应用及生产构建产物；
2. `backend/` Python API、Worker、Projector、Scheduler；
3. Alembic migrations 和数据迁移脚本；
4. `contracts/openapi.yaml`、事件 JSON Schema、兼容性测试；
5. Docker Compose、Kubernetes manifests、环境变量模板；
6. 监控面板、告警规则、故障演练记录和回滚手册；
7. 本文档的实施记录：每个任务标注负责人、开始/完成日期、变更 PR 和验收结果。
