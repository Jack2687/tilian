# UPI 结果交付校验

2026-10-15 参考附件中“返回链接不等于可以交付”的检查思路，在当前 API/Worker 架构的结果边界补充本地校验。保留现有金额规则，不引入附件的代理位置排序、账单生成或外部地址信任分支。

- 引擎即使返回 `ok=true`、合法链接和零金额，只要同时明确返回拒绝、失败、取消或过期状态，就停止交付。检查顶层及 `extra` 中的支付/委托/页面状态，也检查嵌套 intent 和 submission 状态。
- 引擎返回的有效期是 Unix 秒；已到期时停止交付。`0` 或缺失仍表示未知有效期。
- 指引页必须是 HTTPS、精确的 `payments.stripe.com` 域名及带非空标识的 `/upi/instructions/…` 路径。畸形 URL 返回失败，不抛出解析异常。
- UPI 失败结果清除顶层及嵌套公开字段中的链接、深链、二维码和刷新标记，保留错误原因与诊断；避免前端在失败行继续展示二维码。
- 明确拒绝、取消和过期属于终态，不自动重试。UPI 最终结果在等待首张二维码和发布 `result_pending` 之前完成校验。

旧引擎缺少状态或返回未知状态时，继续遵守原有 URL、零金额及优惠生效规则；这不代表状态已验证，也不构成支付成功证明。本次不访问指引页、不联网探测支付状态、不提交真实支付任务。

主要实现：`backend/app/upi_results.py`、`backend/app/engine_adapter.py`、`backend/app/worker.py`。

离线验证在 `backend` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_upi_delivery.py tests/test_engine_adapter.py tests/test_worker_retry.py tests/test_engine_live_protocol.py -q
.\.venv\Scripts\ruff.exe check app/upi_results.py app/engine_adapter.py app/worker.py tests/test_upi_delivery.py tests/test_worker_retry.py
```

结果：105 项通过，Ruff 通过。子进程测试使用本地固定输出，未调用支付引擎或附件代码。

2026-10-15 11:53（本机时间），确认无运行中或排队任务后已重载本地 Worker。重载后 `/health/ready` 返回就绪，数据库、Redis、Kafka 正常，Worker 消费组为 Stable、1 个成员。新执行的任务会使用本轮校验；历史结果未重写。
