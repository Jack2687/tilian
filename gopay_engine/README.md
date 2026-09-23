# GoPay 提链引擎（gopay_engine）

自包含的 GoPay 协议提链引擎，整套逻辑取自 `GPT-Register-Tool/sms_tool` 的钱包适配器
（wallet_provider / wallet_transport / gopay_browser / paypal_proxy / checkout_contract /
gen_pp_link），原样拷贝进本目录，**不依赖 AppData 里的原工具**，可整体拷到服务器部署。

## 目录结构

```
gopay_engine/
├── gopay_service.py      # CLI 入口（Node.js 提链控制台调用它）
├── requirements.txt      # Python 依赖清单
├── README.md
└── sms_tool/             # 钱包协议引擎包（从 GPT-Register-Tool/sms_tool 拷贝）
    ├── __init__.py
    ├── checkout_contract.py   # Stripe checkout 协议契约（纯标准库）
    ├── wallet_provider.py     # 钱包流程编排（建单→促销→confirm→approve→poll→resolve）
    ├── wallet_transport.py    # HTTP 传输层（分阶段代理路由 + 代理池轮换）
    ├── paypal_proxy.py        # 代理规范化 / 会话轮换 / 连通性探测
    ├── gopay_browser.py       # GoPay Midtrans 链接浏览器兜底提取（可选）
    └── gen_pp_link.py         # ChatGPT checkout/approve HTTP 工具
```

## 提链流程（逐步日志）

`checkout(建单) → promotion(促销压0) → stripe_init → payment_method(支付方式)
→ confirm → approve → poll(轮询) → resolve(提取 GoPay 授权链接)`

每一步向 stdout 输出 `[stage] [status] msg`（status = run/ok/fail），最后一行输出
单行 JSON：

- 成功：`{"ok": true, "link": "<pm-redirects/midtrans 授权链接>", "extra": {...}}`
- 失败：`{"ok": false, "error": "<失败原因>", "reason_code": "<错误码>", "extra": {...}}`

失败原因完整返回：错误码（如 `wallet_checkout_not_zero_due`、`promo_not_applied`、
`access_token_invalid_or_expired`）+ 失败阶段（checkout/promotion/confirm/approve/poll…）
+ 促销诊断（是否 eligible、促销是否回显）。

## 代理池规则（用户写什么就用什么）

- `--proxy`：建单代理（引擎从用户代理池轮询分配，每 AT 一条）
- `--proxy-pool`：用户写的**整个代理池**（JSON 数组），各阶段在主代理失败时自动
  轮换兜底；代理格式任意（`host:port` / `host:port:user:pass` / URL），自动规范化
- `--exit-proxy`：出口/账单侧代理（第二代理池，可空 = 同主池）
- **账单国家自动解析**：未在附加参数指定 `country` 时，引擎从代理串自动解析出口国
  （`region-XX` / `country-XX` / `cc-XX` / `geo-XX` 标记、kookeey `user-...-XX-sid-...`、
  `gate-xx` 国家网关），账单国=代理出口国（用户写什么代理就用什么）；解析不到回退 `ID`
- **Billing country mismatch（HTTP 400）**：自动探测代理实际出口国并给出中文原因与
  解决指引（换对应国家出口代理 / 在附加参数指定与出口一致的 `country`），错误码 `checkout_billing_mismatch`

## 命令行用法

```bash
python gopay_service.py --at <access_token> --proxy host:port:user:pass \
    --proxy-pool '["host:port:user:pass", ...]' \
    --country ID --currency IDR \
    --promo-campaign-id plus-1-month-free --checkout-ui-mode hosted \
    --require-zero --json ""
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--country` | `ID` | 建单国家（GoPay 为印尼 ID） |
| `--currency` | `IDR` | 货币 |
| `--promo-campaign-id` | `plus-1-month-free` | 促销活动 ID |
| `--checkout-ui-mode` | `hosted` | checkout UI 模式（hosted/custom） |
| `--require-zero` / `--no-require-zero` | `--require-zero` | 0 元才算成功；`--no-require-zero` 输出原价链接 |
| `--gopay-browser-fallback` | `0` | 协议直取失败后是否浏览器兜底（需 playwright/camoufox） |
| `--timeout` | `90` | 单请求超时秒数 |

## 服务器部署

1. 把整个 `gopay_engine/` 目录拷到服务器（保持目录结构）
2. `pip install -r requirements.txt`（Python 3.10+）
3. 提链控制台（Node.js）通过 `src/extractor.js` 调用：
   - 默认路径：`<提链根>/gopay_engine/gopay_service.py`
   - 环境变量覆盖：`GOPAY_SERVICE=/path/to/gopay_service.py`
   - Python 解释器覆盖：`PYTHON_BIN=python3`

## 与旧接入点的差异（本目录 vs AppData link_service.py）

- 原 `link_service.py --mode gopay` 只传单个代理，失败不会在池内轮换；
  本服务把整个用户代理池传给传输层，主代理失败自动换池内下一条
- 原实现不输出可被前端解析的逐步阶段日志；本服务每步输出
  `[stage] [status] msg`，前端与 PayPal 一样展示阶段徽章与进度
- 失败返回结构化 `error + reason_code + error_stage`，卡密只在真正成功时扣次
