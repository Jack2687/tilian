# 生产部署运维

生产编排文件为 `docker-compose.production.yml`，包含 Vue Web、FastAPI、两个 Worker、
PostgreSQL、Redis 和单节点 KRaft Kafka。数据库和中间件仅在 Docker 内部网络开放。

## 当前内网实例

- 项目目录：`/srv/tilian-v2`
- Web：`http://db.example.internal:18080`
- API：`http://db.example.internal:18000`
- 配置：`/srv/tilian-v2/.env.production`，权限必须保持为 `0600`
- 模式：`distributed`

## 常用命令

在 `/srv/tilian-v2` 下执行：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.production.yml ps
docker compose --env-file .env.production -f deploy/docker-compose.production.yml logs -f api worker
curl -fsS http://127.0.0.1:18000/health/live
curl -fsS http://127.0.0.1:18000/health/ready
```

更新镜像并滚动重建应用服务：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.production.yml build api web
docker compose --env-file .env.production -f deploy/docker-compose.production.yml up -d --no-deps api worker web
```

水平扩展 Worker：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.production.yml up -d --scale worker=4
```

单机 Kafka、PostgreSQL 和 Redis 只能提供进程级隔离，不具备跨机器容灾。扩展为多机集群时，
保持 API/Worker 环境变量不变，将连接地址改到外部 PostgreSQL HA、Redis Sentinel/Cluster 和
至少 3 broker 的 Kafka 集群，并通过负载均衡器扩展 API/Web 副本。

本机开发连接当前服务器时使用以下端口（服务器上已有其他服务占用标准端口）：

- PostgreSQL：`db.example.internal:15432`
- Redis：`db.example.internal:16379`
- Kafka：`db.example.internal:19092`

Kafka 使用 `INTERNAL://kafka:9092` 供容器内部通信，`EXTERNAL://db.example.internal:19092`
供本机 API/Worker 使用。不要把这些端口直接暴露到公网，公网部署应增加防火墙白名单。

## 安全约束

- 不把 `.env.production`、卡密明文、AT、代理密码写入 Git 或日志。
- 备份 PostgreSQL 卷前先验证恢复流程；Redis 和 Kafka 不是额度最终账本。
- 对外开放前配置 HTTPS 反向代理，公网不要直接暴露 `18000`。
- `OWNER_HASH_SECRET` 和 `CREDENTIAL_ENCRYPTION_KEY` 必须长期稳定，轮换前先制定数据迁移方案。
