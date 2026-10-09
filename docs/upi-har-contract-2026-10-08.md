# UPI HAR 响应契约核对（2026-10-08）

来源：用户提供的 `open-home-4538-20260908-160032-m9349b50643_post.com.har`，共 1,057 个条目。以下索引均为 `log.entries` 的 **0-based 索引**。分析仅解析本地文件，未重放请求；文档不记录凭据、账号信息或可复用的支付链接。

## 样本的证据边界

- 样本包含 UPI 二维码动作，也包含 `payment_method_provider_decline / processing_error`；没有证明支付成功的 `succeeded` 状态。HTTP 200、应用确认的 `status=success`、二维码生成均不等于付款成功。
- 关键条目的 `startedDateTime` 全为 `1970-01-01T00:00:00.000Z`，不能依据数组顺序可靠重建跨域请求的时间线或耗时。
- 473 的 `confirm_token`、729 返回的 token ID、735 的 `confirmation_token` 相同；473 与 735 关联的 SetupIntent secret 也相同。473 前没有记录任何 `api.stripe.com` 请求，而 token 创建条目在 729，进一步说明不能把索引顺序直接解释为真实调用时间。
- 473 选择 UPI，527 错误关联的支付方式也是 UPI；不能据此虚构“先试卡再选 UPI”或“失败后创建新 token 重试”。历史 token、会话、链接与验证结果均不得复用。

## 关联调用与响应

| 索引 | 请求 | 可验证的契约 |
| --- | --- | --- |
| 438 | `POST /backend-api/payments/checkout` | 返回 `tag=custom_checkout_session`、ID 前缀 `oaics_`、`checkout_provider=open_ai`、`checkout_ui_mode=custom`、`status=open`、`payment_status=unpaid`。顶层 `publishable_key`、`customer_session_client_secret` 非空；顶层 `client_secret`、`url` 为 null。 |
| 463 | `POST /backend-api/payments/checkout/taxes` | 请求字段为 `checkout_session_id`、`checkout_email`、`billing_country`、`billing_name`、`currency`、`processor_entity`、`billing_address`；响应包装为 `using_automatic_tax` 和 `checkout_session`。 |
| 473 | `POST /backend-api/payments/checkout/confirm` | 请求仅 `checkout_session_id`、`confirm_token`、`selected_payment_method_type=upi`。响应仅 `status=success`、`type=setup_intent`、`client_secret`（`seti_` 前缀）、`confirm_return_url`。 |
| 527 | `GET /v1/setup_intents/<id>` | `status=requires_payment_method`，`next_action=null`；`last_setup_error.type=card_error`、`code=payment_method_provider_decline`、`decline_code=processing_error`。错误中的支付方式类型为 UPI。 |
| 568、657 | `GET /v1/elements/sessions` | 使用 customer session 与 deferred intent；568 未保存可解析的响应体，657 保存了 JSON。657 的优先支付类型为 `upi`、`card`。 |
| 729 | `POST /v1/confirmation_tokens` | 创建 UPI confirmation token；响应 `payment_method_preview.type=upi`、`setup_future_usage=off_session`、`setup_intent=null`。 |
| 735 | `POST /v1/setup_intents/<id>/confirm` | `status=requires_action`；`next_action.type=upi_handle_redirect_or_display_qr_code`。 |
| 739–1027 | 其中 138 条 `GET /v1/setup_intents/<id>` | 均为 `requires_action`，带相同类型的 UPI 动作。区间中还穿插其他请求。 |
| 1030 | `GET /v1/setup_intents/<id>` | 记录了 `requires_payment_method`、`next_action=null`，错误码与 527 相同。由于时间戳失效，不据此推断失败的具体时间或重试次数。 |

该 HAR 不含 `/v1/payment_pages` 请求；不能用其证明旧 payment-pages 分支的线上行为。

## 请求初始化与金额形状

438 的请求字段完整集合为：

```json
{
  "entry_point": "all_plans_pricing_modal",
  "plan_name": "chatgptplusplan",
  "billing_details": {"country": "IN", "currency": "INR"},
  "promo_campaign": {
    "promo_campaign_id": "plus-1-month-free",
    "is_coupon_from_query_param": false
  }
}
```

该请求没有 `checkout_ui_mode`、`cancel_url` 或 `check_card_proxy`。这是样本的请求差异，不足以单独证明这些字段导致其他请求失败。

438 返回的 `checkout_state.total` 形状为：

```json
{
  "subtotal": {"minorUnitsAmount": 169407, "amount": null},
  "discount": {"minorUnitsAmount": 169407, "amount": "₹1,694.07"},
  "appliedBalance": {"minorUnitsAmount": 0, "amount": null},
  "taxExclusive": null,
  "taxInclusive": {"minorUnitsAmount": 0, "amount": null},
  "total": {"minorUnitsAmount": 0, "amount": null}
}
```

应读取 `checkout_state.total.total.minorUnitsAmount`；`amount` 是可空展示文本，不能代替最小货币单位金额。零金额仍是有效值，不能因 falsy 判断丢失。

568、657 的 Elements 参数一致：

- `customer_session_client_secret`、`key`、`stripe_js_id` 使用当前会话值。
- `type=deferred_intent`；`deferred_intent[mode]=subscription`、`[amount]=0`、`[currency]=inr`、`[setup_future_usage]=off_session`。
- `deferred_intent[payment_method_types][0..2]` 分别为 `link`、`card`、`upi`；顶层 `currency=inr`。
- `_stripe_version=2025-03-31.basil`、`elements_init_source=stripe.elements`、`referrer_host=chatgpt.com`；样本区域信息为 `locale=en-IN`、`browser_timezone=Asia/Kolkata`。

729 的业务参数包括 `payment_method_data[type]=upi`；账单姓名、地址字段；`setup_future_usage=off_session`；`mandate_data[customer_acceptance][type]=online`、`[online][infer_from_client]=true`；`client_context[currency]=inr`、`[mode]=subscription`、`[payment_method_types][0..2]=link/card/upi`、`[customer]`；`set_as_default_payment_method=false`。其他字段包含 SDK attribution、会话指纹字段和验证 token，这里仅确认其存在，不记录或复制其值。

729 的 `client_context[payment_method_types]` 与 438 的 checkout 完整列表 `link/card/upi` 一致，和 657 Elements 的 `payment_method_preference.ordered_payment_method_types=upi/card` 不同。HAR 能证明列表取值差异，不能单独证明前端变量来源；初始化时不应把展示排序列表误当成 checkout 完整允许列表。

735 的完整请求字段集合是 `return_url`、`confirmation_token`、`key`、`_stripe_version`、`client_attribution_metadata[client_session_id]`、`client_attribution_metadata[merchant_integration_source]`、`client_secret`；不含 `use_stripe_sdk`。

## UPI 动作形状

735 以及上述 `requires_action` 查询中：

```text
next_action
  type: upi_handle_redirect_or_display_qr_code
  upi_handle_redirect_or_display_qr_code
    hosted_instructions_url
    qr_code
      expires_at
      image_url_png
      image_url_svg
```

托管说明页位于 `payments.stripe.com`，二维码图片位于 `qr.stripe.com`。这部分响应没有直接的 `upi://` 字符串，也没有 `qr_code.data`。HAR 未捕获托管说明页请求，不能声称已验证其内容或从中取得有效 UPI 深链。解析器应保留服务端实际提供的托管链接、图片与过期时间；有二维码表示等待操作，不表示支付完成。

## SDK、指纹与遥测观察

- Stripe.js：449、537 加载 `https://js.stripe.com/basil/stripe.js`；729 的 SDK 标识记录版本 `8444022167`，集成类型为 `payment-element; deferred-intent`。
- 只读检查 449、537 的脚本文本：现有 `_STRIPE_BUILD_SALT_RE` 各匹配 5 次，结果均为 `8444022167`；最小公共片段是 `/*! STRIPE_JS_BUILD_SALT 8444022167*/`。现有解析器适配此样本，无需为该历史构建修改正则或硬编码版本；未执行脚本。
- Stripe API：Elements、confirmation token、SetupIntent confirm 的版本均为 `2025-03-31.basil`。
- Sentinel：422、541 加载 `/backend-api/sentinel/sdk.js`；430、544、639、1054 引用 `/sentinel/20260810913b/sdk.js`。
- HAR 包含 278 条 `r.stripe.com/b` 遥测请求，以及 SDK 会话/指纹/验证字段。其存在只反映历史浏览器行为；缺少对照证据，不能认定某个指纹、遥测值或 SDK 版本就是拒绝根因。
- 不把历史指纹、遥测、验证 token 或 SDK 版本硬编码为当前会话数据；SDK 初始化应继续采用当前合法运行环境生成的值。

## 已完成的代码修复

- 建单：auto 模式采用 438 的原生字段集合，省略 `checkout_ui_mode`、`cancel_url`、`check_card_proxy`；显式模式仍保留调用方选择。自动识别 `oaics_` 与 `cs_`（包含 test/live）会话。
- Elements：OAICS 使用基础版本 `2025-03-31.basil`，不混入 payment-pages 的 beta 参数。confirmation token 保留 Checkout 完整支付方式列表；旧 Stripe Custom Checkout 分支继续使用其独立版本。
- Intent 分流：此前 `_payment_intent_id` 对任何含 `_secret_` 的字符串直接取前缀，导致 `seti_` 被错误发送至 `/payment_intents/<seti_id>/confirm`。现在检查类型、ID 与 secret 的一致性，分别调用 SetupIntent/PaymentIntent confirm 与 poll，并使用服务端 `confirm_return_url`。
- 链接与状态：实际托管链接或 UPI 深链就绪才结束提链轮询；二维码图片地址不能代替付款链接。保留 SetupIntent 的 ID、状态和二维码，`requires_action` 对应 `paid=false`；拒付或取消在发出旧二维码前终止。
- 金额：只接受整数最小单位金额；显式总额无效时不降级为折扣或商品项的零金额。OAICS 最终税费响应缺少有效金额时，不沿用建单时的旧零金额。
- CLI 与诊断：支持 hosted instructions 字段，失败保留 reason code；支付提供方处理失败不再无依据地写成 `blocked`，并作为终态返回。
- SDK：现有构建版本解析器已经适配 HAR，未替换 SDK 文件或硬编码历史版本。指纹、遥测和验证凭据未从 HAR 导入运行配置。

## 验证记录

本地离线验证共 **55 项通过**：42 项 UPI 契约测试（包含新文件 `test_upi_setup_contract.py`、`test_upi_result_contract.py` 以及相关现有用例），13 项 `backend/tests/test_engine_adapter.py` 测试。`git diff --check` 通过。测试使用虚构会话与 mock HTTP；未执行历史请求重放、线上支付或部署。

新增契约测试可在 `engines/pay153-checkout-link-main` 下运行：

```text
python -m unittest test_upi_setup_contract test_upi_result_contract
```

后端适配测试可在 `backend` 下运行：

```text
python -m pytest tests/test_engine_adapter.py -q
```

这些验证证明本地请求和响应处理符合已观察的契约，不能证明当前外部服务接受新会话或优惠资格有效。本次覆盖提链返回；OAICS 既有 `--wait-paid` 持续到账观察尚未实现，不作为此次验证结果。
