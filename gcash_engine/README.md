# GCash 提链引擎（gcash_engine）

自包含的 GCash 协议提链引擎，整套逻辑取自 `GPT-Register-Tool/sms_tool` 的钱包适配器
（wallet_provider / wallet_transport / paypal_proxy / checkout_contract / gen_pp_link），
原样拷贝进本目录，**不依赖 AppData 里的原工具**，可整体拷到服务器部署。

## 目录结构

```
gcash_engine/
├── gcash_service.py      # CLI 入口（Node.js 提链控制台调用它）
├── requirements.txt      # Python 依赖清单
├── README.md
└── sms_tool/             # 钱包协议引擎包（从 GPT-Register-Tool/sms_tool 拷贝）
    ├── __init__.py
    ├── checkout_contract.py   # Stripe checkout 协议契约（纯标准库）
    ├── wallet_provider.py     # 钱包流程编排（建单→促销压0→能力校验→出链）
    ├── wallet_transport.py    # HTTP 传输层（分阶段代理路由 + 代理池轮换）
    ├── paypal_proxy.py        # 代理规范化 / 会话轮换 / 连通性探测
    └── gen_pp_link.py         # ChatGPT checkout/approve HTTP 工具
```

## 提链流程（逐步日志）

`checkout(建单) → promotion(促销压0) → stripe_init / checkout_page（校验 gcash 渠道 + 0元）
→ resolve(提取 GCash Checkout 授权链接)`

每一步向 stdout 输出 `[stage] [status] msg`（status = run/ok/fail），最后一行输出
单行 JSON：

- 成功：`{"ok": true, "link": "https://chatgpt.com/checkout/...", "extra": {...}}`
- 失败：`{"ok": false, "error": "<失败原因>", "reason_code": "<错误码>", "extra": {...}}`

失败原因完整返回：错误码（如 `wallet_checkout_not_zero_due`、`wallet_method_unavailable`、
`access_token_invalid_or_expired`、`checkout_cloudflare_blocked`、`wallet_transport_error`）
+ 失败阶段（checkout/promotion/stripe_init/...）+ 促销诊断（是否 eligible、促销是否回显）。

## 代理池规则（用户写什么就用什么）

- `--proxy`：建单代理（引擎从用户代理池轮询分配，每 AT 一条）
- `--proxy-pool`：用户写的**整个代理池**（JSON 数组），各阶段在主代理失败时自动
  轮换兜底；代理格式任意（`host:port` / `host:port:user:pass` / URL），自动规范化
- `--exit-proxy`：出口/账单侧代理（第二代理池，可空 = 同主池）
- **账单国家自动解析**：未在附加参数指定 `country` 时，引擎从代理串自动解析出口国
  （`region-XX` / `country-XX` / `cc-XX` / `geo-XX` 标记、kookeey `user-...-XX-sid-...`、
  `gate-xx` 国家网关），账单国=代理出口国（用户写什么代理就用什么）；解析不到回退 `PH`
- **Billing country mismatch（HTTP 400）**：自动探测代理实际出口国并给出中文原因与
  解决指引（换对应国家出口代理 / 在附加参数指定与出口一致的 `country`），错误码 `checkout_billing_mismatch`

## 命令行用法

```bash
python gcash_service.py --at <access_token> --proxy host:port:user:pass \
    --proxy-pool '["host:port:user:pass", ...]' \
    --country PH --currency PHP \
    --promo-campaign-id plus-1-month-free --checkout-ui-mode custom \
    --require-zero --json ""
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--country` | `PH` | 建单国家（GCash 为菲律宾 PH；代理出口须与之匹配） |
| `--currency` | `PHP` | 货币（按国家自动推断） |
| `--promo-campaign-id` | `plus-1-month-free` | 促销活动 ID |
| `--checkout-ui-mode` | `custom` | checkout UI 模式（custom=oaics 会话 / hosted=Stripe payment_pages） |
| `--require-zero` / `--no-require-zero` | `--require-zero` | 0 元才算成功；`--no-require-zero` 输出原价链接 |
| `--timeout` | `90` | 单请求超时秒数 |

## 服务器部署

1. 把整个 `gcash_engine/` 目录拷到服务器（保持目录结构）
2. `pip install -r requirements.txt`（Python 3.10+）
3. 首次部署二维码回调需要安装 Chromium：`python -m playwright install chromium`
4. 提链控制台（Node.js）通过 `src/extractor.js` 调用：
   - 默认路径：`<提链根>/gcash_engine/gcash_service.py`
   - 环境变量覆盖：`GCASH_SERVICE=/path/to/gcash_service.py`
   - Python 解释器覆盖：`PYTHON_BIN=python3`

## 与旧接入点的差异（本目录 vs AppData link_service.py）

- 原 `link_service.py` 只支持 PayPal/GoPay；GCash 此前无 Web 接入入口
- 本服务把整个用户代理池传给传输层，主代理失败自动换池内下一条
- 每步输出 `[stage] [status] msg`，前端与 PayPal 一样展示阶段徽章与进度
- GCash 成功后，后端在本次提链同一会话中继续进入授权链接，复用 Cookie、代理、User-Agent 和 Referer，读取/截图真实二维码图片并通过 `extra.qr_image` 回调；前端只接收并显示图片，不访问链接、不生成二维码、不读取二维码内容
- 失败返回结构化 `error + reason_code + error_stage`，卡密只在真正成功时扣次
