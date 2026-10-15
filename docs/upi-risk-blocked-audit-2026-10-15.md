# UPI 遥测、指纹与拦截来源核查

本文记录核查时的本地工作区，包含未纳入本次 GitHub 提交的账单相关修改；文中的当前行为和行号属于该核查快照，不代表发布版本已包含这些修改。

核查时间：2026-10-15。基线为用户指定的确切 Git 提交 `8d25555472177109392758b9516bf0e7c8fc1bf5`，直接从本地 Git 对象读取；不使用附件 UPI6 的修改版代替。对象为本次读取时的当前工作区。

**结论：没有发现建单到 UPI 提交这段的遥测、指纹、请求头、审批或确认代码相对该提交发生变化。最新一批失败中存在明确的上游拦截响应，不是本次新增结果校验器把普通失败重新标成 `upi_risk_blocked`。现有证据仍不能确定上游为什么拦截。**

## 最新一批任务记录

任务创建于 2026-10-15 13:16:32（北京时间），共 30 个任务项。只读取已有记录，没有再次提交任务。

| 数量 | 数据库错误码 | 原始错误及阶段 |
|---:|---|---|
| 23 | `upi_risk_blocked` | `approve` 阶段，原始错误明确包含 `result=blocked`；调用栈经过 `_checkout_approve_custom` |
| 2 | `upi_risk_blocked` | 创建订单阶段，HTTP 400，响应正文为 `Our systems have detected unusual activity. Please try again later.` |
| 5 | `upi_promo_not_applied` | 优惠未应用，属于另一类失败 |

两个建单阶段的错误摘要为：

```text
UPI risk_blocked: checkout create HTTP 400;
{"detail":"Our systems have detected unusual activity. Please try again later."}
```

以上 25 条不是仅凭普通 HTTP 403 推断的风控错误。当前保存的异常没有可用于关联上游调查的请求 ID，也没有完整的实际请求遥测快照，因此不能把代码一致推断成每次请求内容完全相同。

## 与指定提交一致的代码

对 Python 使用函数源码和 AST 比较；对其他源文件规范化 CRLF/LF 后比较全文。

| 检查项 | 当前位置 | 结果 |
|---|---|---|
| 后台指纹生成 | `backend/app/fingerprints.py` | 全文一致 |
| Worker 指纹构造 | `backend/app/worker.py:47`，`build_attempt_fingerprint` | 函数一致 |
| Worker 调用执行逻辑 | `backend/app/worker.py:136`，`execute_item` | 函数一致 |
| 引擎启动参数 | `backend/app/engine_adapter.py:146`，`command` | 函数一致 |
| HTTP 客户端、TLS 配置 | `upi_link_service.py:1276`，`UpiHttpClient` | 类一致 |
| 协议上下文 | `upi_link_service.py:1742` | 函数一致 |
| ChatGPT 请求头 | `upi_link_service.py:2034` | 函数一致 |
| 遥测生成 | `upi_link_service.py:2196`，`_upi_telemetry` | 函数一致 |
| Stripe 请求头 | `upi_link_service.py:2232` | 函数一致 |
| Sentinel 请求头 | `upi_link_service.py:2359` | 函数一致 |
| Stripe 页面确认 | `upi_link_service.py:3881` | 函数一致 |
| 订单审批 | `upi_link_service.py:4040`，`_checkout_approve_custom` | 函数一致 |
| 确认令牌生成 | `upi_link_service.py:4704` | 函数一致 |
| Checkout 确认 | `upi_link_service.py:4819` | 函数一致 |
| PaymentIntent / SetupIntent 确认 | `upi_link_service.py:4893`、`:4919` | 函数一致 |
| 建单执行 | `upi_link_service.py:5247` | 函数一致 |
| 税费及账单快照请求逻辑 | `upi_link_service.py:5502`、`:5537` | 函数一致，账单输入可能不同 |
| 建单后的印度阶段 | `upi_link_service.py:5569`，`_run_upi_india_phase` | 函数一致 |
| 错误分类 | `upi_cli.py`、`localize_upi_error`、`_raise_if_upi_risk_blocked` | 分类代码一致 |

表中 `upi_link_service.py` 位于 `engines/pay153-checkout-link-main/`。

`upi_cli.py`、`upi_sentinel.py` 及 Sentinel 的 bootstrap、bridge、SDK 三个 JS 文件与基线全文一致。以下是部分当前文件规范化换行后的 SHA-256，基线计算结果相同：

| 文件 | SHA-256 |
|---|---|
| `backend/app/fingerprints.py` | `535d40c4dcbf75032bcea263a3d0ff980be2d330e7186eee2296e245692d992a` |
| `upi_cli.py` | `2746d0b41c2489600ef7879b4febc6e86cb452e1e23f0125f0d60366b6a13eb4` |
| `upi_sentinel.py` | `c36c017fcb6c0548eb2d00f02b4675dd6562a4f2b6ab8e671ea77dcd29775631` |
| `sentinel_bootstrap.js` | `59cfca34c5f62ea2b1ddff8c3708c10b79c7d23716dd8113a44f5eb1a962f21a` |
| `sentinel_bridge.js` | `ca6e08e9267ada8d82ed2756dcd074faf19e1546391074bded636d4487e1218a` |
| `sentinel_sdk.js` | `f21fa2a6859b320ef3157e3cf17d9a8da7fbf824aba55502b5fc3f421c03ef11` |

## 实际变化及其边界

当前 `upi_link_service.py` 与基线相比，有变化的既有顶层函数是：`_upi_billing_address_rows`、`_probe_upi_exit_city`、`_resolver_indian_address_row`、`_normalize_upi_billing`、`build_upi_billing`、`extract_upi_link`。新增函数也集中在外部地址文件、出口信息读取、反查及地址来源日志。

因此，代码层面已确认的不同是地址准备和输入：当前增加认证前的坐标反查、五地区过滤及外部地址文件功能。初次 Checkout 请求只包含国家和币种，完整地址用于后续税费、账单快照、支付确认。地址变化可能改变后续输入，但此次没有证据能将它定为上游拦截的原因。

后台新增的 `upi_results.py` 在引擎输出之后处理结果，只产生支付设置被拒、取消或过期分类，不生成 `upi_risk_blocked`，也不改写发送给上游的遥测或请求头。本轮读取的 25 条错误已经在引擎异常中记录，发生于该结果校验之前。

两版本都存在较宽的历史规则：普通 HTTP 403 也可被归为 `upi_risk_blocked`。这意味着单看此错误码不能确定具体原因；但最新一批的 25 条都有 `result=blocked` 或 `unusual activity` 的额外依据，不能用这条历史规则解释为本轮误分类。

## 能确认与不能确认的内容

- 能确认：本次所核对的源码和已有失败记录，没有支持“遥测指纹生成代码被改了”的证据。
- 不能确认：此前成功运行与本次运行的账号状态、实际账单输入、运行环境、依赖、上游状态及每次请求值完全一致。提交号只约束源代码，不约束这些运行条件。
- 未执行真实付款、未反复重放被拒请求、未调整指纹或遥测以尝试通过风控，也未修改本轮生产逻辑。

这份报告可用于后续对照已有成功记录或提供给服务方排查。要确定具体拒绝原因，需要同条件的请求/响应证据或服务方的请求追踪信息；不应仅凭历史某次成功就回滚没有变化的遥测代码。
