# UPI 调查接续记录：2026-09-18

源会话：`01a0a953-2e12-7940-925a-0d46059d08fb`。

本记录用于承接历史调查，并区分可复核事实、未完成实验和待修复的软件问题。它不代表新的线上验证结果。历史 HAR 说明见 [UPI_HAR_ANALYSIS.md](../UPI_HAR_ANALYSIS.md)。

## 当前可确认的状态

- 历史 HAR 记录了完整请求流程及当时的二维码；其中链接已经过期，不能当作当前有效结果。
- 历史 confirm 返回 HTTP 200，只能说明该次请求得到 HTTP 层成功响应；仍需检查响应业务状态及后续阶段。它不等于 approve 通过，也不等于当前已生成有效 UPI 链接。
- 使用 AT 调用 API 成功，与浏览器页面是否持有有效登录态是两种不同证据。页面出现未登录壳不能否定此前 API 响应，也不能证明缺少页面会话就是 `blocked` 的唯一原因。
- 成功 HAR 中出现 passive captcha 相关字段，与失败结果出现 `setup_attempt_failed / generic_decline`，不足以建立两者的因果关系。此前“缺少该字段就是根因”的说法撤回为未证实假设。
- `generic_decline`、`setup_attempt_failed`、普通 submission failed 都是失败信息；只有上游实际返回 `blocked` 时，才可描述为明确的 blocked 状态。不能单凭通用错误断言优惠已应用、支付对象未生成或某一特定风控因素导致失败。

## 本地实验的完成程度

已核对 `ab3_obs_cookie_95_90000.log` 中的执行与错误信息：

- A 组加载了未登录页面壳。
- B 组页面导航等待 90 秒后超时。
- 六次 HTTP 读取虽然记录为 HTTP 200，但对应 `emit` 均因文件句柄 `fh` 已关闭而报 `I/O operation on closed file`。

因此，这一轮只留下部分阶段观察和输出故障，未形成完整、可比较的 A/B 结果。不能将六个 HTTP 200 等同于六份完整实验结果，也不能据此确认页面会话因素的因果作用。

历史讨论中的 `91/7/2` 汇总，目前缺少可复核的全量输出。当前 `in_elig_result.json` 只有单条结果，不能用它重建或证明全量比例。在找回对应完整结果前，该汇总仅作为待核查的历史陈述。

## 本轮软件修复范围

本轮生产代码修复仅限于 Python 与 Node 的错误文案：通用支付设置失败显示为“UPI Stripe 支付设置失败”，只保留实际出现的已知错误码；包含明确 blocked 信息的失败继续显示拦截。通用错误不再被自动描述为风控 blocked，也不再据此断言优惠已应用。

2026-09-18 本轮离线验证通过：Node 错误展示测试 6 条，Python 诊断与相关文案测试 8 条。验证覆盖通用失败、明确 blocked、已知错误码保留及重复本地化；未运行完整仓库测试。

本记录及文案纠偏不改变支付流程、凭据获取、验证码处理或重试机制；未运行新的联网探针或支付实验。

## 尚未完成的软件问题

| 问题 | 当前证据与影响 | 后续本地工作 |
|---|---|---|
| 普通 HTTP 403 被误判为无优惠资格 | `upi_link_service.py` 的 `localize_upi_error` 将部分 promotion update 的 403 转成无资格；`upi_cli.py` 又将其归为资格终态。相同服务文件的 `_upi_promo_error` 已声明普通 403 不应等同于资格拒绝。 | 仅对明确资格语义返回资格终态；为普通 403 和明确拒绝添加离线分类用例。 |
| Python 与 Node 优惠终态契约不一致 | Python CLI 将已领取与无资格合并，并对部分优惠失败默认输出可重试；Node 的 `classifyUpiRetry` 已将无资格、已领取及未压零设为终态。 | 分别保留既有 reason code，统一明确终态的返回值；保留 Node 对旧 worker 的兼容防护。 |
| 原始诊断在 UPI 桥接处丢失 | CLI 输出 `error_raw`，UPI 的 `src/extractor.js` 返回结构未透传；`src/engine.js` 保存诊断时却读取该字段。 | 明确服务端诊断契约，先脱敏、限制长度，再决定保存位置；验证公开视图不包含原始诊断。 |
| A/B 实验输出生命周期错误 | 日志中的 `emit` 在 `fh` 关闭后执行，造成读取成功而结果写出失败。 | 可用虚构数据离线复现并修复输出句柄生命周期；历史部分结果不得补写为完整实验。 |
| 部分历史文案包含未证明的因果判断 | HAR 成功路径和个别错误码不能单独证明拒绝根因。 | 后续更新文档时持续区分观察、假设与验证结果。 |

本轮不修改上表其他生产代码问题。

## 离线验证入口

本轮实际执行的 Node 错误展示测试：

```text
node --test test/upi_error_messages.test.js
```

本轮实际执行的 Python 测试使用 mock 验证 CLI 分类、原始错误和二次本地化，可在 `engines/pay153-checkout-link-main` 目录运行：

```text
python -m unittest test_upi_link_service.UpiErrorDiagnosticsTests test_upi_link_service.UpiHarContractTests.test_setup_intent_failure_does_not_claim_a_risk_block test_upi_link_service.UpiHarContractTests.test_payment_page_blocked_localizes_to_risk_retry
```

这些离线测试只验证软件行为，不证明当前外部服务接受支付请求或能生成有效新链接。
