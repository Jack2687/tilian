# UPI 审批通过后的支付设置失败（2026-10-08）

## 实际观察

本次只读查看了本地 API 使用的任务库。在当时可查询的 2026-10-08 11:30（北京时间）批次中，共 50 条任务项：33 条 `upi_payment_setup_declined`，11 条 `upi_risk_blocked`，6 条 `upi_promo_not_applied`。这是该批次的统计，不代表其他批次的成功率。

抽查的支付设置失败项依次记录：

1. 建单识别为 `cs_`，进入 Stripe `payment_pages` 分支。
2. 最终应付金额为 0，账单快照提交成功。
3. ChatGPT `checkout/approve` 返回 `result=approved`，记录“订单审批通过”。
4. 后续支付页轮询返回 `submission_attempt.state=failed`，携带 `checkout_approval_payment_failure_with_payment_error`、`setup_attempt_failed`、`generic_decline`。

审批通过与后续支付设置失败可以同时成立。数据库中的最终阶段仍为 `approve`，是因为代码在审批通过后没有记录轮询阶段，CLI 异常退出时也没有补充失败阶段。错误原因本身来自上游，不能因阶段展示缺陷而认定拒绝不存在。

用户提供的 20260908 HAR 记录的是 `oaics_` 分支，没有 `/v1/payment_pages` 请求，不能直接用于证明这批 `cs_` 请求的参数正确，也不能证明某项指纹、遥测或验证字段是拒绝原因。详见 [HAR 契约核对](upi-har-contract-2026-10-08.md)。

## 本次修复

- 在提交 Stripe 确认、支付设置轮询和 intent 轮询之前明确记录进行中步骤。
- CLI 异常时将实际最后步骤记为失败，保留此前审批通过记录。后续支付设置失败归入 `poll`，直接确认失败归入 `confirm`，真实审批失败仍归入 `approve`。
- `payment_pages` 先检查 submission 失败和嵌套 intent 终态，再处理二维码或链接。失败响应中的旧二维码和旧链接不再被当作可用结果。
- 只有二维码图片或普通嵌套 URL 时继续轮询；Stripe 托管说明链接或 UPI 深链才算提链就绪。
- 异步初态 `requires_payment_method` 且没有错误时允许继续轮询；有错误的该状态和 `canceled` 会终止。
- 增加 Stripe confirm 的 HTTP 状态与 Request-ID，以及 payment-page fetch 的 HTTP、Request-ID、submission 状态、独立 setup/payment intent 状态和上游错误码。仅记录受限标量字段，不记录响应正文、client secret、账单内容或完整支付链接。
- 删除把通用 decline 与特定验证字段缺失直接建立因果关系的注释。

`upi_payment_setup_declined` 保持不可自动重试，因此显示“1 次”符合现有终态规则。本次没有通过提高重试次数重复提交已被拒绝的支付设置。

## 验证与边界

83 项引擎离线测试和 18 项后端适配/重试测试通过。新增测试覆盖真实服务审批解析与轮询逻辑到 CLI 阶段日志的联动，以及失败响应带残留链接、仅二维码、异步初态和诊断脱敏。

测试不创建真实订单，不执行支付。本次修复解决本地状态处理与诊断缺陷，**没有证据证明已经消除上游 `generic_decline`**。下一次合法执行会自动使用新引擎代码并保存更具体的状态与 Request-ID；若上游仍只返回通用拒绝，需要结合这些标识核查对应支付服务端记录，不能从错误码猜测具体拒绝规则。
