# 新旧架构提链通道对比与修复

日期：2026-09-22。对比范围为旧 `server.js → src/engine.js → src/extractor.js`，与新 `Vue → Python API → Kafka → Worker → EngineAdapter` 的九个通道。各通道继续使用仓库已有 Python 引擎。

## 九个通道

| 通道 | 现有引擎 | 新架构发现的问题 | 本次处理 |
| --- | --- | --- | --- |
| PayPal | `engines/sms_tool/link_service.py` | 部分金额、会话类型、账单国家、组合信息在顶层返回，Worker 只保存 extra 导致丢失 | 保持 BA 链接成功判定，补齐 extra 元数据映射 |
| GoPay | `gopay_engine/gopay_service.py` | 错误阶段、错误码与重试信息缺少旧版对外字段 | 补齐 reasonCode/errorStage/amountMinor 等；保留引擎的终止与重试判定 |
| GCash | `gcash_engine/gcash_service.py` | 成功结果后进程继续提供二维码；新 Adapter 等待进程退出，最终误判超时。仅获得链接、首张二维码未就绪时也不能直接终止进程 | 支持首个可交付结果完成；pending 结果等待首图，兼容二维码先于结果。保留二维码与会话元数据 |
| MoMo | `momo_engine/momo_service.py` | 新账号导入与请求结构丢失每账号 sessionToken，只剩全局参数方式；API 默认调用还可能生成 US 指纹 | 恢复嵌套/多行 Session JSON 的 sessionToken 解析、Vue 提交及每账号加密存储；Worker 解包并透传，默认 VN 指纹 |
| UPI | `engines/pay153-checkout-link-main/upi_cli.py` | 支付侧整个出口代理池未传给引擎；二维码与付款字段在顶层而未保存 | 补 `--proxy-pool`；继续区分促销建单代理与支付出口代理；保留 QR/付款字段。零金额及优惠校验保持 |
| BLIK | `blik_engine/blik_service.py` | 等待动态码事件被丢弃；API 仅修改数据库状态，没有把验证码送入实际引擎 stdin | 实时记录等待状态；验证账号阶段；加密控制消息并按尝试编号一次消费，送达同一子进程。只有确认 Plus 到账才成功 |
| PIX | `pix_engine/pix_cli.py` | 顶层二维码、付款字段没有进入 Worker 持久化结果 | QR/付款字段映射到 extra，保留支付链接与现有命令参数 |
| iDEAL | `ideal_engine/ideal_service.py` | 输出成功 JSON 后 `_wait_active_runtime()` 保活；旧版收到结果即完成，新版却等退出到超时。二维码显示字段也不一致 | 收到可交付结果即结束提链；保存二维码并修正 Vue 查看入口；NL/EUR 与零金额/优惠成功条件保持 |
| 直卡 | `card_engine/card_service.py` | 旧调用默认启用 sticky proxy；新版未传时引擎默认关闭。同一尝试内可能切换代理 | 默认传 `--sticky-proxy 1`，保留显式 `stickyProxy: false`；建单/出口双池、国家、币种、超时等 CLI 参数已核验兼容 |

## 共享执行链修复

- 恢复旧版代理入口规则：管理员池未启用、池为空或自定义建单池为空时，立即返回可读配置错误；不再静默改为直连。校验发生在绑定 CDK、预留额度之前。自定义本地/内网代理仍可使用。
- Worker 按通道补全默认指纹国家，每次重试选择下一条代理并更新指纹；同一次尝试内保持稳定环境。
- Adapter 读取阶段及 JSON 事件并实时持久化，支持 BLIK/二维码控制消息；提高 JSON 行上限，避免较大的 base64 二维码触发默认 64 KiB 限制。
- 取消、超时和终态会清理引擎进程树。终态二维码刷新返回 409，Vue 仅在会话活跃时显示刷新入口，已有二维码仍可查看。
- 取消和清理活跃任务会释放未使用预留额度。取消刷新并锁住账号行，避免旧快照覆盖已完成结果；结算对同一账号的 commit/release 跨类型幂等，防止取消后迟到的 Worker 再次扣次。
- 每账号 sessionToken 与 AT 一起保存在既有加密 credential 中，无需新增明文数据库字段；旧的仅 AT 密文兼容解包，公开任务快照及任务 payload 不包含该 sessionToken。
- 本地新架构与远程旧 Worker 原来共用生产消费组，会让本地提交任务被远程旧代码领取。当前本地环境改用独立 Kafka topics/group；健康检查改为请求实时 metadata 并检查消费组在线成员，增加只读 `backend/tools/runtime_status.py` 诊断工具。

## 验证范围与限制

完整后端回归结果：**67 passed**（8.58 秒）。运行时使用独立 SQLite 测试库并关闭 Redis/Kafka 连接，没有消费生产队列。仅出现 Starlette/AnyIO 依赖弃用警告。前端 `npm run build` 的 TypeScript 检查与 Vite 生产构建通过。

测试覆盖命令参数、事件、真实模拟子进程生命周期、控制消息、取消/清理与额度幂等。隔离集成包含 BLIK API 提交动态码 → 同一引擎进程 stdin → Worker 成功状态 → 额度结算。PayPal、GoPay、GCash、MoMo、直卡和 iDEAL 的现有 CLI 均已实际运行 `--help` 核验参数解析与依赖加载。

本次未用真实账号 AT 或真实代理发起上游支付，不能据此声称九个通道均已取得真实支付链接。账号优惠资格、代理连通性及出口环境、第三方接口状态仍影响真实提链结果。

终态结果返回后当前实现会关闭二维码会话，不提供支付后持续保活/刷新；需要新会话时重新提链。已有旧队列任务保留原路由，不自动迁移或重放到新环境。
