# PIX 提链引擎（pix_engine/）

PIX 巴西支付提链（Stripe Custom Checkout / OpenAI OA 链路），CLI 桥接与控制台
（extractor.js → `python pix_cli.py`）事件协议与 GCash/GoPay 一致：
阶段日志 `[stage] [status] msg`，成功/失败各输出一行单行 JSON。

## 目录

| 文件 | 说明 |
| :--- | :--- |
| `pix_service.py` | 核心引擎（建单/促销/Stripe/确认/轮询/0 元校验/保活） |
| `pix_cli.py` | CLI 入口（`--at/--proxy/--billing-json/--tax-kind/--confirm-protocol/--demo` 等） |
| `test_pix_service.py` | 离线 fixture 测试（双协议 + 错误分类），`python test_pix_service.py` |

依赖：`curl_cffi>=0.15`（仅真实运行需要；测试用注入 transport，无需网络）。

## OA 确认链路（两代协议并存）

### 1) confirm-token 链路（默认，对齐 2026-09 `bax.har`）

新版 `chatgpt.com/checkout/{entity}/{id}` 页面 + Stripe Elements 的真实请求顺序：

1. `POST /backend-api/payments/checkout` 建单（oaics_ 会话）
   → 返回 `publishable_key`、`customer_session_client_secret`(cuss_secret_)、
   `payment_method_types`(card/link/pix)、`requires_manual_approval`、`confirm_return_url`；
2. `GET api.stripe.com/v1/elements/sessions`：customer_session_client_secret +
   `deferred_intent[mode]=subscription&amount=0&currency=brl&setup_future_usage=off_session`
   + payment_method_types，`elements_init_source=stripe.elements`；
3. `POST /backend-api/payments/checkout/taxes`：账单地址/姓名/邮箱 → amount_total；
4. `POST api.stripe.com/v1/confirmation_tokens`：PIX 支付方式 + billing_details
   （含 `tax_id`=CPF/CNPJ、空 phone）+ `client_attribution_metadata`
   （client_session_id / elements_session_id / elements_session_config_id /
   merchant_integration_additional_elements[expressCheckout,payment,address]）
   + `mandate_data` + `setup_future_usage=off_session` → `ctoken_`；
5. `POST /backend-api/payments/checkout/confirm`（JSON）：
   `{"checkout_session_id", "confirm_token": "ctoken_...", "selected_payment_method_type": "pix"}`
   → `{"status": "succeeded"|"blocked"|...}`；需要二维码的流程由 confirm/poll 返回
   Stripe `pix_display_qr_code`（hosted_instructions_url + PNG/SVG + EMV data）。

> 该链路不调用 `payment_pages` init/confirm、不调用 `/approve`，与旧页面（0元风控.har 时代）不同。
>
> **⚠️ 2026-09 实测：OpenAI 已将 `/backend-api/payments/checkout/confirm` 限制为
> 仅内部 checkout 会话，外部调用一律 400
> `{"detail":"This endpoint is only for internal checkout sessions"}`。**
> `auto` 模式会识别该端点为契约错误并自动回退 legacy 链路（实测仍可用并正常
> 产出 0 元 PIX 二维码）；如需省掉一次无效的 token 探测可直接指定 `legacy`。

### 2) legacy 链路（保留）

`payment_pages/{id}/init` → `elements/sessions`（client_betas 旧参数）→ taxes →
snapshot → tax update → `payment_pages/{id}/confirm`（大表单）→ 若
`requires_approval` 调 `/backend-api/payments/checkout/approve` → `payment_pages/{id}` 轮询。

### 协议选择

| 配置 | 行为 |
| :--- | :--- |
| `PIX_OA_CONFIRM_PROTOCOL=auto`（默认） | 真实运行：token 优先；仅契约级错误（未知参数/端点 404/5xx / 上游“only for internal checkout sessions”）自动回退 legacy 重试一次 |
| `PIX_OA_CONFIRM_PROTOCOL=token` | 强制 confirm-token 链路 |
| `PIX_OA_CONFIRM_PROTOCOL=legacy` | 强制旧链路 |
| `--confirm-protocol auto\|token\|legacy` | CLI 同义覆盖 |

## 与 bax.har 的对齐确认（风控头，2026-09-08 真机抓包）

| 请求头/行为 | bax.har 真机 | 引擎实现 |
| :--- | :--- | :--- |
| `oai-client-version` / `oai-client-build-number` | prod-8b282ae6… / 10379027 | 默认同值 + warmup 页面探测自动升级 + env 覆盖 |
| `oai-telemetry` | `[1,551.7,13,37,18,2,0,555]` 每请求不同 | 动态生成近似结构 |
| `oai-web-deployment-attestation` | JWT（deployId+签名，~1h） | env（PIX/MIN_GCASH/MIN_OAICS）→ 页面 HTML/RSC 正则抓取（对齐 gcash attestation.py） |
| `openai-sentinel-token` | 每请求前 mint（`{"p":…}`） | 真 sdk.js 桥 mint（依赖 gcash_engine/sentinel_token.py + sentinel_assets/，缺失会 stderr 告警） |
| `x-oai-is-client-observation` | `v1.r.p.<16>` 每次新生成 | 按观测格式随机生成；`PIX_CLIENT_OBSERVATION` 注入真实值、`=off` 关闭（不可用固定串 "true"） |
| UA/TLS 版本 | Chrome/152 | chrome150/146/142/136…（按部署 curl_cffi 支持上限自动过滤） |

> 测试注入 transport 且未显式指定协议时，`auto` 解析为 legacy（fixture 为旧端点集）。

## blocked / 人工审核语义

confirm 返回 `{"status":"blocked"}` 为**终态**（不回退、不重试）：

- 建单响应 `requires_manual_approval: true` → `pix_checkout_manual_approval`
  （reason 记法；错误文案只陈述上游返回 blocked，不做原因推断）；
- 其它 → `pix_checkout_blocked`（同样只陈述上游返回）。

## 其余行为（沿用既有引擎约定）

- **代理池**：主代理由调用方/Node 调度层选定并固定；网络类错误由 Node 换代理+换指纹重试；
- **严格 0 元校验**：Stripe 权威应付金额（payment_intent.amount / invoice.amount_due /
  total_summary.due）+ PIX EMV tag54 双校验，非 0 元链接一律拒绝输出（`requireZero=false` 可关）；
- **巴西账单**：优先级 = 显式 `billing` 参数 > 真实账单素材池 > 内置真实注册 CNPJ 池
  （Receita ATIVA，公司名+官方注册地址自洽，避免 Stripe PIX PSP 的
  `partner_generic_decline`）。素材池文件默认 `data/pix_billing_profiles.json`
  （可用 `PIX_BILLING_PROFILES` 覆盖路径），JSON 数组，每条必须含
  `name/tax_id/line1/city/state/postal_code`（建议放自己验证过的真实 CPF/CNPJ+
  匹配地址），每次尝试随机抽取一套；`tax_kind=cpf` 仅生成校验位合法的随机 CPF
  （未注册，实测易被拒，不建议）；
- **保活等支付**：`--wait-paid` 时出码后同会话线程继续轮询 Stripe submission +
  subscriptions，支付落地输出 `[paid]` / `payment_status` 事件；
- 其它环境变量：`PIX_STRIPE_JS_REVISION`（stripe.js deploy revision）、
  `PIX_HCAPTCHA_TOKEN`（confirmation_tokens 的 radar hcaptcha token）、
  `PIX_SENTINEL=0`（关闭 sentinel mint）、`PIX_ATTESTATION`（固定 attestation）、
  `PIX_CHATGPT_CLIENT_VERSION` / `PIX_CHATGPT_CLIENT_BUILD`（覆盖 OpenAI 客户端
  版本头，默认随最新抓包滚动，warmup 探测到页面 prod-<40hex> 版本串时自动升级）；
- **版本对齐（风控相关）**：客户端版本头陈旧、静态 `oai-telemetry`、TLS/UA 版本
  落后真机（2026-09 真机 Chrome/152）都是 `requires_manual_approval`/blocked 的
  放大因素；指纹池已按部署 curl_cffi 实际支持上限（0.16 → chrome150）扩容，
  运行时自动过滤不支持目标，老环境无回归风险；
- **出口与账单国家一致**：PIX 巴西账单建议使用巴西出口代理（控制台日志会在代理
  出口国 ≠ 建单国时给出提示），数据中心/非 BR 出口会放大人工审核概率。

## 部署

    pip install -r pix_engine/requirements.txt

Node 侧通过 `src/extractor.js` 的 `runPixService` 调用（`PIX_SERVICE` 环境变量可覆盖
CLI 路径），无需其它 Python 依赖。
