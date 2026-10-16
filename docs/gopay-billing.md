# GoPay 印尼账单与坐标反查

GoPay 账单由 `gopay_engine/id_billing.py` 生成，在 `gopay_custom_flow.py` 的 taxes 步骤提交。地址按以下优先级产生：

**`MIN_GOPAY_BILLING_ADDRESS` 注入 > 代理坐标反查 > 内置印尼街道表（可同城匹配）**

本次改动（对齐 UPI 的账单来源链路）新增了中间那一层坐标反查。在此之前 GoPay 只能用出口**城市名**去内置街道表里匹配整座城市。

## 为什么坐标比城市名更细

`probe_exit_city()` 只返回城市名，`_pick_by_city()` 从该城市的内置街道里随机挑一条。结果是地址落在正确城市，但可能与出口实际位置相距很远。

坐标反查先用同一个探测拿到经纬度，再由 `resolve_reverse_address()` 调用 Nominatim `/reverse` 取该点附近的真实街道。探测与反查共用一次出口查询，不额外增加一轮网络往返。

## 字段与校验

反查结果必须同时满足才被采用，否则整条作废、回退内置街道表：

- 出口国家与账单国家一致（默认都是 `ID`）；出口不在印尼却用印尼地址，等于把账单国家和出口位置割裂开。
- `line1`、`city`、`state` 非空。
- `postal_code` 是 5 位数字（印尼邮编格式）。

`resolve_reverse_address()` 返回的是**扁平**结构（`line1`/`city` 在顶层）；调用方传嵌套的 `{"address": {...}}` 也接受，两种形状都归一处理。

**反查只提供地址，不提供姓名。** Nominatim 的 `name` 字段是商户或地标名（`amenity`/`building`），不是人名，因此 `_reverse_address_from_payload()` 根本不返回 `name`，账单姓名一律由 `_FIRST_NAMES`/`_LAST_NAMES` 生成。这与 UPI 侧是同一约定。

## 缓存与限流

- 结果按坐标缓存（精确到小数点后五位），默认 30 天 TTL，上限 5000 条。
- 缓存文件默认写在 `gopay_engine/data/nominatim_reverse_cache.json`，可用 `GOPAY_NOMINATIM_REVERSE_CACHE` 指定。
- 请求之间强制间隔 1.05 秒，遵守 Nominatim 公共实例的限速。
- 缓存写入失败不影响提链；反查失败一律回退，不抛错、不阻断。

## 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `GOPAY_BILLING_REVERSE` | 开启 | 设为 `0/false/no/off` 关闭坐标反查，回到城市名匹配 |
| `GOPAY_NOMINATIM_REVERSE_CACHE` | `gopay_engine/data/` | 反查缓存文件路径 |
| `MIN_GOPAY_BILLING_ADDRESS` | 未设置 | JSON 注入完整账单，优先级最高，绕过全部内置来源 |

任务参数 `gopayBillingReverse` 可覆盖环境变量（`false` 关闭）。引擎子进程继承宿主完整环境，因此 `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`/`SSL_CERT_FILE` 无需额外透传，反查与提链主链路天然共用同一套证书配置。

## 与 UPI 的差异

| 项 | UPI | GoPay |
|---|---|---|
| 地区限制 | 限定 DL/KA/MH/TN/WB 五地区 | 不限，只要求国家为 `ID` 且邮编 5 位 |
| 主数据来源 | 本地池 + 可选外部文件 | 内置印尼街道表，无外部文件通道 |
| 优惠代理 | 促销与支付可用不同出口 | 只有建单/出口两池，反查用建单出口 |

GoPay 没有 `UPI_BILLING_POOL_FILE` 那样的外部地址文件通道；需要接入整理好的地址时用 `MIN_GOPAY_BILLING_ADDRESS`。

## 验证

`gopay_engine/test_id_billing.py` 新增 22 项离线用例（全部 mock，不发真实请求，socket 被阻断）：

- 反查：坐标映射、邦/城市字段、缓存命中不重复联网、非印尼结果拒绝、字段缺失拒绝、无效坐标不发起请求、网络异常返回 `None`。
- 账单优先级：反查覆盖同城匹配、嵌套形状接受、姓名始终来自姓名库、邮编/国家/街道缺失时回退内置表、无坐标时行为不变、`MIN_GOPAY_BILLING_ADDRESS` 仍最高优先。
- 开关：默认开启、四种关闭写法、关闭或坐标缺失或国家不符时不发起任何地图请求。
- 探测：`probe_exit_geo` 字段映射、`probe_exit_city` 保持兼容、空代理不请求、探测失败降级为空。

运行：

```bash
cd gopay_engine && python -m pytest test_id_billing.py -q
```

后端全量 179 项通过，引擎侧 `gopay_engine` 22 项、`blik_engine` 19 项、`pix_engine` 29 项、`engines/pay153-checkout-link-main` 220 项通过。`gcash_engine` 有 2 项既有失败，已用 `git stash` 在改动前的基线上复现，与本次改动无关。

Ruff：本次改动的文件无新增告警（`gopay_service.py` 的 4 项、`gopay_custom_flow.py` 的 1 项在改动前已存在）。
