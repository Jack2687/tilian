# GCash 提链逻辑说明

本文档基于当前本地项目实现整理，描述 GCash 从建单到返回授权链接和二维码图片的完整流程。

## 1. 目标

GCash 通道完成以下工作：

1. 使用账号 `access_token` 创建 GCash checkout 会话。
2. 按配置尝试应用 `plus-1-month-free` 促销。
3. 通过 Stripe custom payment 流程创建 GCash 支付方式。
4. 调用 OpenAI confirm 和 `custom_payment_method/start` 获取 Adyen 跳转地址。
5. 在同一 HTTP 会话中跟随跳转，得到 `m.gcash.com` 授权链接。
6. 使用同一会话的 Cookie、代理、User-Agent、Referer、locale 和时区进入 GCash 页面。
7. 只截取页面上的二维码图片并回调给前端显示，不读取二维码内容。

## 2. 主要模块

| 模块 | 职责 |
| --- | --- |
| `gcash_engine/gcash_service.py` | CLI 入口、参数解析、代理和错误处理、调用 custom flow |
| `gcash_engine/gcash_custom_flow.py` | GCash 六步协议流程、Adyen 跳转、二维码图片获取 |
| `gcash_engine/sms_tool/wallet_transport.py` | 钱包传输层和阶段请求封装 |
| `src/engine.js` | 多账号调度、代理分配、任务状态和重试 |
| `src/extractor.js` | Node.js 到 Python GCash 引擎的子进程桥接 |
| `src/fingerprint.js` | 账号指纹生成、国家 locale/timezone 参数和设备身份 |
| `public/js/app.js` | 接收 `qrImage` 并显示二维码弹窗 |

## 3. 主流程

### 3.1 任务调度

`src/engine.js` 为每个账号创建一个任务项：

1. 从代理池分配建单代理。
2. 在粘性代理开启时保存该代理，重试时继续复用。
3. 分配出口代理；未配置独立出口代理时复用建单代理。
4. 根据代理国家标记生成指纹国家，解析不到时使用用户选择的建单国家。
5. 为当前账号生成一套指纹，并在整个任务及重试过程中复用。
6. 调用 `src/extractor.js` 执行 GCash 提链。

账号任务使用的关键身份字段包括：

- `device_id`
- `oai_session_id`
- `User-Agent`
- `Accept-Language`
- `oai-language`
- `sec-ch-ua` 相关字段
- 代理和出口 IP

## 4. GCash 六步协议流程

实现入口：`gcash_engine/gcash_custom_flow.py:487` 的
`extract_gcash_custom_link()`。

### Step 1：创建 checkout

请求：

```text
POST https://chatgpt.com/backend-api/payments/checkout
```

核心参数：

```json
{
  "plan_name": "chatgptplusplan",
  "billing_details": {
    "country": "PH",
    "currency": "PHP"
  },
  "checkout_ui_mode": "custom",
  "payment_method_types": ["gcash"]
}
```

成功后需要取得：

- `checkout_session_id`
- `publishable_key`
- `custom_payment_methods[0].id`
- `customer_session_client_secret`

如果上游返回 `400 unusual activity`、代理连接失败或缺少 GCash 能力，流程在此阶段结束。

### Step 2：初始化 Stripe Elements

请求：

```text
GET https://api.stripe.com/v1/elements/sessions
```

使用 checkout session、publishable key、client secret 和 `gcash` payment method type 初始化支付会话。

### Step 3：创建 PaymentMethod

请求：

```text
POST https://api.stripe.com/v1/payment_methods
```

使用：

- `type=custom`
- `custom[type]=<GCash custom payment method id>`
- 菲律宾账单地址占位信息
- checkout 对应 Stripe 参数

成功后取得 `pm_*` PaymentMethod ID。

### Step 4：确认 checkout

请求：

```text
POST https://chatgpt.com/backend-api/payments/checkout/confirm
```

请求体包含：

- `checkout_session_id`
- `payment_method`
- `selected_payment_method_type`

如果返回 `blocked`，表示账号级确认被阻断。

### Step 5：启动 custom payment method

请求：

```text
POST https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start
```

请求体包含：

- `checkout_session_id`
- `payment_method`
- `custom_payment_method_type_id`

成功后从 `next_action.url` 或响应 JSON 中提取 Adyen URL。

### Step 6：跟随 Adyen 到 GCash

服务端使用同一个 `session` 请求 Adyen URL，并处理：

- HTTP `Location` 跳转
- HTML `meta refresh`
- HTML/JSON 中出现的 GCash URL
- 最终 `m.gcash.com/gcashapp/gcash-merchants-auth/index.html` 页面

最终只返回授权链接，不在前端重新访问该链接。

## 5. 同会话约束

GCash 链接和二维码必须在同一账号、同一提链会话中完成。

同一会话包括：

```text
access_token
HTTP Cookie
建单代理
出口代理
User-Agent
TLS impersonate
oai-device-id
oai-session-id
Referer
locale
timezone
```

二维码浏览器启动后，会把 Python HTTP session 中的 Cookie 复制到 Playwright BrowserContext，并使用相同代理进入最终 GCash URL。前端只接收后端生成的图片 Data URL。

## 6. 二维码获取逻辑

入口：`gcash_engine/gcash_custom_flow.py:447` 的 `_extract_qr_image()`。

获取顺序如下：

### 6.1 页面源码中的 Data URL

扫描 HTML 中带有二维码标记的 `data:image/*;base64,...` 图片。

### 6.2 页面中的图片资源

扫描带有二维码标记的 `img/source` 标签，使用同一 Python session 请求资源，再封装成 Data URL。

### 6.3 Playwright 页面截图

如果前两种方式没有结果，启动无头 Chromium：

1. 进入最终 GCash 页面。
2. 等待页面跳转和异步脚本加载。
3. 扫描主 frame 和子 frame 中的：
   - `canvas`
   - `img`
   - `svg`
   - 带背景图样式的元素
4. 选择可见、尺寸至少 `100x100`、宽高接近方形的元素。
5. 排除 `option1`、`ins_photo1` 等 GCash 操作说明图。
6. 对候选元素执行 PNG 截图。
7. 将截图封装为 `data:image/png;base64,...` 返回。

二维码只作为图片传输。代码不调用二维码解码库，也不读取二维码文本。

### 6.4 无限等待和诊断日志

最终 GCash 页面中的二维码可能异步加载，因此扫描循环没有时间上限，直到得到图片为止。

等待期间会每约 10 秒输出脱敏诊断：

- 当前页面路径
- 页面标题
- 可见元素数量
- 方形候选数量
- frame 数量
- 浏览器启动或导航异常

注意：无限等待只发生在确认进入最终 GCash 页面之后，不会阻塞 Adyen 跳转解析。

## 7. 前端回调

Python 返回字段：

```json
{
  "ok": true,
  "link": "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?...",
  "qr_image": "data:image/png;base64,...",
  "extra": {
    "payment_method": "gcash",
    "link_type": "gcash_protocol",
    "checkout_ui_mode": "custom",
    "qr_image": "data:image/png;base64,..."
  }
}
```

Node.js 将 `qr_image` 转换为任务项的 `extra.qrImage`，前端读取并显示：

```text
result.qrImage -> 二维码按钮 -> 图片弹窗
```

前端显示前会校验 Data URL MIME 类型，只接受 PNG、JPEG、WebP 或 GIF 图片。

## 8. 指纹和代理策略

当前配置位于 `src/config.js`：

```js
FINGERPRINT: {
  ENABLED: true,
  MODE: "per-submission",
  STICKY_PROXY: true
}
```

### 指纹

`per-submission` 模式下，每次新任务为每个账号生成一套新档案，任务重试复用该档案。

档案包含：

- 浏览器内核标签
- UA
- curl impersonate 目标
- locale 和 Accept-Language
- timezone
- `device_id`
- `oai_session_id`
- 屏幕、内存、并发数等浏览器参数

### 代理

`STICKY_PROXY=true` 时，同一账号的建单、支付确认、跳转和二维码获取使用同一代理路线，避免提链中途更换出口 IP。

## 9. 成功和失败判定

### 成功

同时满足：

- 引擎返回 `ok=true`
- 存在有效的 GCash/Adyen 授权链接
- Node.js 判断链接域名属于允许的钱包链接范围

二维码没有返回时，链接仍可作为提链成功结果，但前端不会显示二维码按钮，并会在阶段日志中记录二维码获取结果。

### 失败

常见阶段和原因：

| 阶段 | 典型原因 |
| --- | --- |
| `checkout` | Token 无效、代理失败、上游 unusual activity、GCash 能力未返回 |
| `promotion` | 账号没有促销资格，无法满足 0 元要求 |
| `stripe_init` | Elements 初始化失败 |
| `payment_method` | PaymentMethod 创建失败 |
| `confirm` | checkout 确认失败或账号被阻断 |
| `extract_link` | 未返回 Adyen URL |
| `resolve` | 跳转失败或最终页面二维码获取失败 |

代理错误例如 `Proxy CONNECT aborted` 属于建单/网络阶段问题，与二维码扫描逻辑无关；此时不会产生二维码会话。

## 10. 本地验证

### 语法检查

```powershell
python -m py_compile gcash_engine/gcash_custom_flow.py gcash_engine/gcash_service.py
node --check src/extractor.js
node --check src/engine.js
```

### 页面二维码验证

使用一个已经成功生成的 GCash 链接，在本地 Python 环境中调用 `_capture_qr_with_browser()`，只检查返回值是否为图片 Data URL：

```text
返回前缀：data:image/png;base64,
```

不保存账号 Token，不打印完整授权 URL，不对图片进行二维码解码。

## 11. 当前限制

1. 二维码由 GCash 页面异步生成时，获取阶段会持续等待。
2. 如果代理在建单阶段不可用，流程不会进入二维码阶段。
3. 如果最终页面只展示说明图而没有实际二维码，候选过滤会继续等待并输出诊断日志。
4. 浏览器截图依赖本机或服务器可用的 Chromium/Playwright 运行环境。
5. 本文档描述的是当前本地代码，服务器是否同步需要单独检查，不包含自动部署行为。
