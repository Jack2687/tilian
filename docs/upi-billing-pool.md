# UPI 账单池与输入校验

账单地址由 `engines/pay153-checkout-link-main/upi_link_service.py` 处理。地址按以下优先级产生：**调用方填写 > 通过五地区过滤的代理出口坐标反查 > 本地地址池**。所有进入 UPI 建单的地址都必须位于下面五个地区内。

按 2026-10-15 的需求，调用方地址、反查结果和本地地址池统一只接受指定的五个地区：Delhi (DL)、Karnataka (KA)、Maharashtra (MH)、Tamil Nadu (TN)、West Bengal (WB)，由 `UPI_BILLING_STATES` 定义。

主池现有 32 条静态地址、覆盖 15 个城市，全部落在这五个地区内：MH 10 条（Mumbai、Pune ×2、Navi Mumbai、Thane、Nashik、Nagpur）、DL 4 条（New Delhi）、KA 7 条（Bengaluru ×5、Mysuru、Mangaluru）、TN 7 条（Chennai ×4、Coimbatore ×2、Madurai）、WB 4 条（Kolkata ×3、Siliguri）。

通用解析器 `billing_address_resolver.py` 的印度列表覆盖更多邦/联邦属地且被其他渠道共用，因此保持原样；UPI 在合并时按 `UPI_BILLING_STATES` 过滤，池外记录既不参与抽样也不通过校验。

另有独立的 [UPI 离线／沙箱账单测试库](upi-sandbox-billing.md)，包含同样五个地区的 3000 条模拟记录及测试抽样权重，不由真实支付流程加载。

## 代理坐标反查（默认开启）

预算取地址前，引擎先探测代理出口的国家、城市和经纬度，再由 `billing_address_resolver.resolve_reverse_address()` 调用 Nominatim `/reverse` 取得该坐标附近的街道与城市。

- 只有当出口国家是 `IN` 时才反查；非印度出口直接回退地址池，不发起任何地图请求。
- 反查结果缺少街道、城市、邦码或六位邮编，或者邦不在指定五地区内，就整条作废并回退五地区本地池。原反查地点名称也不用于回退账单。
- **反查只提供地址，不提供姓名。** Nominatim 的 `name` 字段是商户或地标名（`amenity`/`shop`/`building`，如 "Sri Krishna Sweets"），不是人名，因此 `_reverse_billing_from_proxy()` 根本不返回 `name`，建单姓名一律由 `UPI_BILLING_NAMES` 生成。反查结果被接受或被丢弃，都不影响姓名的产生方式。
- 结果按坐标缓存，默认 30 天 TTL，进程内并发受锁保护，缓存写失败不影响提链。缓存文件默认写在引擎根目录的 `data/nominatim_reverse_cache.json`，可用 `PAY153_NOMINATIM_REVERSE_CACHE` 指定；每次请求之间强制间隔 1.05 秒以遵守 Nominatim 公共实例的限速。
- 反查允许邮编缺失，因此它是"坐标附近的地点"，不是付款人真实账单地址的证明，也不校验投递能力。
- 关闭方式：任务参数 `upiBillingReverse=0`，或后端环境变量 `UPI_BILLING_REVERSE=0`。
- 反查结果在 `_reverse_billing_from_proxy()` 与 `_reverse_address_fields()` 中都按 `UPI_BILLING_STATES` 过滤；支持邦名全称与两字母代码。已有缓存也必须经过过滤。反查城市无需已经收录在本地静态池中。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `UPI_BILLING_REVERSE` | 开启 | 设为 `0/false/no/off` 关闭坐标反查 |
| `UPI_BILLING_SYNC_CITY` | 关闭 | 未填地址时按代理城市挑选池内地址 |
| `UPI_BILLING_POOL_FILE` | 未设置 | 追加运营维护的地址文件 |
| `PAY153_NOMINATIM_REVERSE_CACHE` | 引擎 `data/` | 反查缓存文件路径 |

## 选择规则

- 合并静态地址来源后，先按地区过滤，再按街道、城市、邦和邮编去重；统一空白、大小写及已支持的城市别名，不再额外随机追加重复记录。
- 没有填写地址时，优先使用通过五地区过滤的反查结果；反查不可用或地区不符时，从有效池中选择整行地址。街道、城市、邦和邮编始终来自同一条记录。
- 多个候选可用时，同一引擎进程连续两次池选择不重复；只有一个候选时可复用。独立任务使用独立子进程，不保证跨任务、跨进程地址唯一。
- 已填写完整且通过本地校验的地址直接保留，不消耗池选择，不查询代理城市，也不发起反查。姓名按调用方优先、否则随机印度姓名处理，邮箱同理。
- 只传姓名、邮箱、空 `address` 或印度国家提示，仍视为未填写具体地址。显式填写了街道、第二行、城市、邦、邮编中的任何内容，就必须提供完整可校验的地址。
- 地址在一次建单尝试中解析一次，税费、账单快照和确认请求复用相同结果。反查同样只在鉴权前做一次并冻结，保证与后续建单使用同一条出口。
- 建单步骤详情会打印 `source=`，取值 `caller`、`openstreetmap_nominatim_reverse` 或 `upi_billing_pool`，便于区分本次用的是哪条来源。地区不符后回退时显示 `upi_billing_pool`。

> 注意：`_is_rebuild_retryable` 只对 502/503/504 与金额类错误重试。地址被上游拒绝**不会**触发重选地址，会直接终态失败（`upi_billing_invalid` 等）。因此反查冻结一次地址，在坐标或地图结果不佳时整轮重试都会沿用同一条。

## 本地校验范围

国家接受 `IN` / `India`，未传时兼容默认 `IN`。邦必须是已知两字母代码或邦名全称；输出统一使用邦名全称。邮编必须是六位 ASCII 数字且首位非零。

城市、邦与邮编前三位须匹配池中的已知组合，且邦必须在 `UPI_BILLING_STATES` 内。例如 `Mumbai / MH / 400026` 可通过，`Mumbai / KA / 110001` 会被拒绝；池外地区即使写法自洽也一律拒绝，如 `Gurgaon / HR / 122001`、`Hyderabad / TS / 500034`、`Kochi / KL / 682031`。支持 Bangalore/Bengaluru、Bombay/Mumbai、Delhi/New Delhi 等常用别名（`Gurgaon/Gurugram` 别名仍保留，但该城市已不在池内）。

这不是完整的邮政地址认证：不验证门牌与投递能力，也不覆盖印度全部城市和邮区。未覆盖或输入不一致时返回清晰错误，不静默替换用户地址。

账单 JSON 格式错误、非对象、部分地址或校验失败统一返回 `reason_code=upi_billing_invalid`、`retryable=false`。Worker 同样把该原因码列为终态，修正输入后再提交。

## 外部地址文件（可选）

设置 `UPI_BILLING_POOL_FILE` 后，服务会在内置池之外追加读取该文件，运营可以不改代码、不重新部署就接入整理好的地址。默认未设置，行为与只有内置池时完全一致。

- 路径由后端进程环境变量给出；worker 会把 `os.environ` 透传给引擎子进程，所以在后端环境里设置一次即可，单个任务不需要额外参数。
- 支持三种文件形态：`{"addresses": [...]}`、顶层 JSON 数组、以及每行一条的 JSONL。每条记录接受 `{"line1","city","state","postal_code"}`，也接受嵌套在 `address` 下；`state` 可写两字母代码或邦名全称。
- 仍然只接受 `UPI_BILLING_STATES` 的五个地区，池外记录被丢弃。加载的行与内置池一起做去重、格式校验和别名归一，并且同时进入随机抽样和显式地址校验。
- 文件缺失、无法读取或整体格式错误时不抛异常，只是没有额外行；单条坏记录跳过，不影响其余记录。
- 只缓存文件读取结果，不缓存合并结果，因此改文件后重启进程即可生效。

仓库自带 `data/upi_billing_addresses.json` 作为初始内容（7 条，来自用户反馈的完整地址）。需要用时显式指定：

```bash
UPI_BILLING_POOL_FILE=/app/data/upi_billing_addresses.json
```

`data/` 目录整体被 `.gitignore` 忽略，已为这个文件加了例外；部署时确认它随镜像或卷一起到位。

## 同城选择与缓存

`UPI_BILLING_SYNC_CITY` 默认关闭；关闭时完全跳过代理城市探测。启用后，也只在未填写地址时探测。结果在进程内按代理缓存，当前调用使用局部城市变量，避免不同调用混用城市。

同城池没有候选时仍使用全池（即五个地区加上外部文件）。印度地址来源不读取/写入通用 JSON 地址缓存；代理城市查询与坐标反查是两件独立的事，前者只在显式开启且未填地址时发生，后者默认开启。

## 验证

此前反查接入的离线验证记录为 374 项通过：215 项引擎用例与 159 项后端用例。本次追加五地区限制的针对性验证见下文。测试使用虚构输入和 mock，不创建真实提链任务，也不请求真实 Nominatim。

本次五地区过滤与姓名来源验证：`test_upi_billing_contract.py`、`test_upi_billing_cli.py`、`test_upi_link_service.py` 共 131 项通过、1 项既有跳过。测试文件 Ruff 通过；引擎文件的 5 项 Ruff 告警与指定提交相同，没有新增告警。全量回归为 220 项引擎用例 + 159 项后端用例通过。

主要回归入口：

```text
# engines/pay153-checkout-link-main
python -m pytest test_upi_billing_contract.py test_upi_billing_cli.py -q

# backend
python -m pytest tests/ -q
```

回归覆盖新增三组断言：`ExternalBillingPoolFileTests` 校验外部文件的三种形态、池外过滤、去重、缓存与失败回退；`test_billing_pool_is_limited_to_the_five_configured_states` 与 `test_rows_outside_the_configured_states_are_dropped` 校验地区范围；`ReverseAddressResolverTests` 与 `ReverseBillingExtractionTests` 覆盖反查的字段提取、五地区邦名/代码归一、缓存命中不重复联网、非印度结果与无效坐标拒绝、网络异常返回 None、池外地区丢弃并回退、不完整结果回退地址池、`UPI_BILLING_REVERSE` 开关，以及实际传入建单尝试的账单和日志来源均符合过滤结果。
