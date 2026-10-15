# GoPay 操作与改造说明：当前项目对照

本文记录核查时的本地工作区，包含未纳入本次 GitHub 提交的账单相关修改；文中的当前行为和行号属于该核查快照，不代表发布版本已包含这些修改。

核查日期：2026-10-15。资料为用户提供的 `D:/Downloads/GoPay提链操作与改造说明.md`，对照当前工作区 `D:/baiduProject/upi提链/newtilian/tilian`。

附件描述的是另一个项目 `jipwo-extract`。本机没有附件引用的 `D:/GPT/...` 源码、测试记录和审计报告，因此本文区分“附件声称的行为”与“当前代码已确认的行为”。附件中的命令与改造步骤未执行，历史成功数和 119 项测试结果未独立验证。

## 结论

差异集中在实际支付分支、真实资料的传递、结果验证和写入后的重试边界，并非仅仅更换地区、语言或时区。当前项目默认 ID/IDR，默认走 custom，并能区分 `oaics_` 与标准 Stripe session；但 OAICS 分支不是附件描述的原生账单、确认令牌及 Intent 流程。

## 已验证的对照

| 项目 | 附件描述 | 当前项目 |
|---|---|---|
| 应用入口 | `/extract/` 页面、extractions API、local_channels、vendor 引擎 | Vue 页面、`/api/v1/tasks`、Worker、`gopay_service.py`、`gopay_custom_flow.py` |
| 账号资料 | 保留每账号原有 device/session/cookie 和账单；缺设备上下文停止 | 导入结构主要保留 AT、SessionToken、邮箱；Worker 使用本地生成或缓存的指纹，GoPay CLI 无独立 session/billing 输入 |
| 国家币种 | GoPay 配置约束 ID/IDR | 默认 ID/IDR，但 country/currency 可由任务参数覆盖 |
| OAICS 分支 | 独立 `gopay_oaics.py`，读取与验证 checkout，原生 GoPay 确认流程 | 无该文件；调用 `_start_oaics_gopay`，取得匹配链接后提前返回 |
| Intent | 区分已处理的 SetupIntent 与仍需确认的 Intent，防止重复确认 | 当前 OAICS 分支没有附件中的条件读取／确认实现 |
| 账单来源 | checkout 已有真实资料优先，每账号或批次真实账单补缺 | 标准 `cs_` 分支为环境注入、代理坐标反查、内置地址表；没有附件的包装层账单解析和入口传递链 |
| 姓名来源 | 原有真实付款人资料 | 反查和池取址会生成姓名；异常还存在固定最小账单回退 |
| 实际零元验证 | 验证实际金额、币种和优惠状态，不接受缺证据 | OAICS 允许金额缺失，返回结果写固定零；标准 `cs_` 对缺失金额也有继续路径 |
| 地址同步 | `tax_region` 仅发非空字段，同步失败停止 | 标准分支可发送空地址字段；网络失败／HTTP 拒绝仅警告继续 |
| 最终交付 | 严格 HTTPS、准确 Midtrans 主机、Snap v3/v4 UUID 页面 | 可返回 Stripe 中间链接；标准分支跳转解析失败时可能回退原链接并成功返回 |
| 第三方跳转 | 显式隔离会话 headers/cookies/auth，每跳校验 | 复用 session 内联跳转，没有附件的显式隔离和严格逐跳校验 |
| 写入结果不明 | 停止自动重建，先核对原订单 | 部分 POST 网络异常归为可重试网络错误，没有 GoPay 写入结果未知状态 |
| 源码版本清单 | 两层 manifest 哈希校验 | 当前 GoPay 调用链没有相应清单校验机制 |

## 关键代码证据

### OAICS 与零元结果

`gopay_custom_flow.py:801` 正确地避免把 OAICS 送进 Stripe `payment_pages/init`，但随后进入本项目的 `_start_oaics_gopay`（`:562`），不是附件中的 `extract_oaics_gopay`。

该函数的自定义方法标识取本地常量 `gopay`（`:78`、`:582`）。它在 `:602` 读取返回金额，`:603` 的检查允许 `None`。因此“没有实际金额”不会阻止取得链接；`:814–818` 随后返回成功和固定 `amount_minor: 0`。请求里填写预期零金额不等于上游确认实际为零。

此提前返回路径也绕过后面的标准分支账单生成、taxes、snapshot。不能把标准分支后来新增的反查账单功能当成 OAICS 已经具备完整账单提交。

标准 `cs_` 分支仍有初始化、确认、轮询及部分金额一致性检查，但 `:860` 对金额检查只拒绝已知非零值，`:879`、`:1072` 将缺失金额作为零使用。该行为与附件所述严格验证不同。

### 入口与真实资料

`backend/app/main.py:491` 的账号解析最终在 `:534` 输出 AT、SessionToken、邮箱等字段；`backend/app/schemas.py:11` 的 AccountInput 没有每账号 device/billing 字段。`frontend/src/App.vue:715` 提交的账号对象也仅带邮箱、AT、SessionToken。

`backend/app/worker.py:178` 选择国家并调用指纹构造。GoPay 首次尝试会取本地缓存或生成资料；后续尝试生成新资料。`backend/app/engine_adapter.py:218` 的 GoPay CLI 参数传递没有账单和 SessionToken，`gopay_service.py:587` 的 CLI 也没有这些参数。因此在本页面粘贴附件中的 Session JSON，不能期待原有 deviceId、billing_details 自动沿用至 GoPay。

`gopay_custom_flow.py:608` 的函数签名已经保留附件提及的 `force_payment_method` 和 `force_zero_checkout` 兼容参数。这一项无需因为附件而重复增加。

### 账单与同步

标准分支 `gopay_custom_flow.py:930` 附近调用 `id_billing.generate_id_billing`；后者 `:482` 的优先级为环境变量注入、反查、内置池。没有附件的“checkout 包装层寻找最完整地址、调用方仅补缺、冲突停止、写后重读”实现。

标准分支 `gopay_custom_flow.py:1022` 构造 `tax_region`，地址缺项会填空串；`:1058–1064` 在网络或 HTTP 失败后仍继续。snapshot 的异常也属于非阻断警告。附件要求的失败即停止尚未具备。

### 链接交付与重试

`gopay_custom_flow.py:552` 用正则从响应整体提取候选 URL；OAICS `:814` 直接返回该候选。标准分支 `:1357–1405` 内联最多八跳解析，解析失败可能回退原始 redirect，并返回成功。当前代码不具有附件中只接受规范 Midtrans Snap 页面、严格逐跳目标检查的交付条件。

第三方请求复用原 session，但本次代码检查不能证明实际发送了商户凭证；只能确认不存在附件的显式隔离保障。不能把“缺少隔离实现”直接说成已经发生凭证泄漏。

`backend/app/wallet_results.py` 对 GoPay 主要做字段映射，`engine_adapter.py:584–590` 的 GoPay 成功门禁没有附件所述最终金额、币种、优惠及 Midtrans URL 的完整验证。

`gopay_custom_flow.py:457` 把若干网络／启动／跳转错误标记为可重试；`backend/app/worker.py:84` 根据错误码、文字及 retryable 判定。已有优惠和部分拒绝的终态规则，但没有区分“请求未写入”和“POST 可能已处理，响应丢失”的 GoPay 专用写入状态。不能概括成所有失败都会重试。

以上主要针对默认 custom 路径。备用 hosted 路径在 `gopay_engine/sms_tool/wallet_provider.py:2033–2058` 对 confirm、approve、poll、follow_redirect 异常已有 unknown 与禁止重试处理，并可要求核对结果；默认 custom 不使用该保护，checkout 也不在 hosted 的相关阶段集合内。外层 `engine_adapter.py:749`、`:774–777` 的执行超时或无结果仍标记为可重试，未按已发生的写入阶段区分。

标准 custom 轮询在 `gopay_custom_flow.py:1273–1300` 已先拦截 submission_attempt 的 failed／declined；但 OAICS start 的提前返回没有附件所述完整失败状态门禁。两条分支的已有保护不能混为一谈。

## 核查边界

本轮只读检查当前源码和附件，新增此对比记录。未改动支付逻辑、未应用附件改造、未执行附件命令、未创建订单或钱包授权请求。文档所述历史成功不能证明当前项目或改造后的当前项目具有相同结果。
