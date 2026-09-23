# 直卡提链引擎（card_engine）

自包含的直卡协议提链引擎，逻辑对齐 `GPT-Register-Tool` 源项目（services/protocol-payment/direct_card）：
`direct_card_extract.py` 原样拷贝（**不依赖 AppData 里的原工具**），外层 `card_service.py`
对齐源项目 manager 层的代理池轮换语义，可整体拷到服务器部署。

## 国家与代理

| 角色 | 国家 | 说明 |
| --- | --- | --- |
| 建单/账单国家 | 由调用方选择 | 写入 `billing_details.country/currency` |
| 实际出口代理 | **用户写什么就用什么** | 国家选择不会修改代理 URL、用户名中的 `region-XX` 或代理池顺序 |
| 促销 | 建单请求携带优惠活动 | 初始 checkout 已应用优惠时不再调用 checkout/update |
| 出口校验 | 默认关闭 | 仅在显式指定 `checkoutProxyCountry`/`updateProxyCountry` 时启用 |

## 目录结构

```
card_engine/
├── card_service.py         # CLI 入口（Node.js 提链控制台调用它）
├── direct_card_extract.py  # 源项目直卡协议引擎（原样拷贝，自包含）
├── requirements.txt        # curl_cffi
└── README.md
```

## 提链流程（逐步日志）

`preflight(代理出口校验) → checkout(建单+优惠) → verify(金额校验)
→ resolve(输出 Checkout 长链)`

每步输出 `[stage] [status] msg`，最后一行单行 JSON：
- 成功：`{"ok": true, "link": "https://chatgpt.com/checkout/...", "extra": {...}}`
- 失败：`{"ok": false, "error": "<中文原因>", "reason_code": "<错误码>", "extra": {...}}`（原始英文在 `extra.error_raw`）

## 代理池规则

- 建单代理 = 主代理池；促销更新代理 = 出口/第二代理池，省略则复用主池
- `--proxy-pool` / `--exit-pool`：用户写的**整个代理池**（JSON 数组），引擎按原始顺序
  轮换生成 `(建单, 更新)` 候选对；网络故障自动换下一对，**压 0 失败（NonZero）立即停止**；代理格式任意
  （`host:port` / `host:port:user:pass` / URL）
- 单代理显式模式：只有填写出口国家参数时才开启 preflight 校验；代理池模式自动跳过 preflight

## 命令行用法

```bash
python card_service.py --at <access_token> --proxy host:port:user:pass \
    --exit-proxy host:port:user:pass \
    --proxy-pool '["host:port:user:pass", ...]' \
    --exit-pool '["...", ...]' \
    --country PH --currency PHP \
    --checkout-ui-mode hosted --json ""
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--country` / `--currency` | `PH` / `PHP` | 建单/账单国与币种 |
| `--checkout-proxy-country` | 空 | 可选的建单代理期望出口 |
| `--update-proxy-country` | 空 | 可选的促销更新代理期望出口 |
| `--checkout-ui-mode` | `custom` | checkout UI 模式（hosted/custom） |
| `--skip-proxy-check` | 关 | 跳过代理出口校验（池模式自动开启） |
| `--timeout` | `45` | 单请求超时秒数 |

## 服务器部署

1. 拷贝整个 `card_engine/` 目录（保持结构），`pip install -r requirements.txt`
2. 默认引擎路径 `<提链根>/card_engine/card_service.py`，环境变量 `DIRECT_CARD_SERVICE` 覆盖；
   Python 解释器用 `PYTHON_BIN`

## 与旧接入点的差异

- 旧 `src/direct_card_service.py` 通过 LOCALAPPDATA 注入源项目路径（服务器部署会失效），已删除；
  现引擎完全自包含
- 旧版单代理无池内轮换、关闭出口校验；现版对齐源项目：代理池候选对 + NonZero 快速失败 + 出口校验
