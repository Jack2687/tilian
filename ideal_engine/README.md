# ideal 提链引擎

这是当前提链控制台的荷兰 ideal（技术支付方式为 `ideal`）独立引擎目录。

iDEAL 的税区、Checkout snapshot/taxes、Stripe confirm 共用同一份随机荷兰账单身份。
每次尝试（包括同一账号的自动重试）都会重新生成浏览器指纹、会话和账单地址；
传入的账户邮箱仍保持不变。该地址生成器只由 iDEAL 使用，不改变其他渠道逻辑。

当前实现按荷兰优惠 HAR 对齐 iDEAL 0 元提链链路：

- `ideal_service.py` 是 Node.js 调用的 CLI，并复用 GCash 的 AT、`oai-did`、预热 Cookie 与指纹会话；
- 流程为 ChatGPT Checkout → Stripe iDEAL confirm → `pay.ideal.nl` initiate；
- 返回 `qrCodeUrl` 并生成 SVG/PNG data URI，同时监听 iDEAL status SSE；
- 首次 Checkout 请求直接携带 `plus-1-month-free`，并在 Stripe `init/confirm`
  阶段强制校验 `expected_amount=0`；
- 不硬编码 HAR 中的 AT、Cookie、签名或加密 PUT 正文；
- 阶段日志格式与 GCash/GoPay/PIX 一致：`[stage] [status] message`，最后一行固定输出单行 JSON。

默认参数为 `country=NL`、`currency=EUR`。代理池、出口代理、指纹、优惠和银行参数
已经预留；默认提交 `plus-1-month-free`，iDEAL 始终要求 Stripe 最终应付金额为 0，
不支持回退到原价链路。

## 本地冒烟

```bash
python ideal_engine/ideal_service.py --demo
python ideal_engine/ideal_service.py --at '<access_token>' --proxy '<proxy>' --email '<email>'
```
