# 普通用户统一并发配置

普通用户的单任务并发上限统一来自管理员“并发配置”，已有用户与新注册用户使用同一规则。历史 `User.task_concurrency` 和 `user_policy.defaultConcurrency` 不再作为独立限制；无需批量修改既有用户记录。

有效上限为 `min(concurrency.default, concurrency.globalLimit, 100)`。其中 `globalLimit` 保留旧 API 字段名，界面显示为“单任务硬上限”；它不代表多个 Worker 或多个任务之间的总并发调度。当前没有 VIP 资格定义，`vip` 保留兼容、界面标记未启用。

`/api/v1/meta` 的 `limits.maxConcurrency` 返回最终有效上限。注册、登录、`/auth/me`、管理员用户列表和任务创建校验均使用相同配置。用户编辑不再提供独立并发，旧客户端尝试修改 `taskConcurrency` 会收到说明错误。用户策略中的 `defaultConcurrency` 为普通并发配置的兼容镜像，保存旧用户策略不会覆盖共享并发值。

管理员保存后，服务器下一次请求即使用最新配置。用户工作台可见时每 5 秒刷新上限，聚焦或返回页面时立即刷新；同源后台页面保存也会通知用户页。已保存的较低并发选择保留，超过新上限的选择自动缩减。每个通道的草稿独立保留。

每个任务的执行并发在创建时写入 `Task.concurrency`，Worker 按该值执行；调整配置不会改变已创建任务。超过当前上限的新提交在绑定 CDK、预占额度、写任务或队列事件之前被拒绝。

## 验证

后端并发、核心接口和任务入参共 37 项测试通过；新增并发用例使用独立 SQLite 与 ASGI 客户端，验证后台保存后既有用户、新注册、登录与任务准入的一致性，以及拒绝提交不会修改额度或队列。前端 5 项行为测试覆盖上限刷新、旧缓存、自选值、降低上限、草稿和退出清理；生产构建通过。

后端测试必须在导入应用前设置临时 SQLite 数据库，并将 `REDIS_URL`、`KAFKA_BOOTSTRAP_SERVERS` 设为空、`EXECUTION_MODE=distributed`、`APP_ENV=test`。项目默认 `.env` 连接共享服务，不应直接使用它运行写入型测试。前端可在 `frontend` 目录运行 `npm test` 和 `npm run build`。
