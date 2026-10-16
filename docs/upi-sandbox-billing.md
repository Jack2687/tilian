# UPI 离线／沙箱账单测试库

按 2026-10-15 的需求及用户确认的“离线／沙箱测试数据”用途，提供 3000 条明确标记的模拟账单记录。所有街道、姓名和邮箱均为测试内容，城市与邮编仅作为区域测试种子；没有进行邮政地址认证，也没有创建支付或提链任务。

## 数据与范围

文件位于 `engines/pay153-checkout-link-main/tests/fixtures/upi_sandbox/`：

- `addresses.jsonl`：每行一条模拟账单，共 3000 条，唯一 ID 与模拟街道。
- `summary.json`：生成数量、地区分布、权重分布和完整性校验结果。
- `locations.json`：37 组城市／州／邮编测试种子，以及用户案例的来源记录。

| 地区 | 州码 | 模拟记录数 | 城市／邮编组合数 |
| --- | --- | ---: | ---: |
| Delhi | DL | 600 | 3 |
| Karnataka | KA | 600 | 7 |
| Maharashtra | MH | 600 | 13 |
| Tamil Nadu | TN | 600 | 10 |
| West Bengal | WB | 600 | 4 |

3000 条记录由这 37 组地理测试种子扩展，唯一性来自模拟街道和记录编号。`Delhi` 城市别名统一为 `New Delhi`，州仍为 `Delhi`。同一条记录的城市、州及六位邮编始终取自同一个种子，不随机拼接地理字段。

用户提供的 18 条案例中，12 条属于上述五个地区，6 条属于 Uttar Pradesh、Andhra Pradesh、Gujarat 或 Kerala。范围外案例保留在来源记录中，并标记 `included_in_sandbox=false`，不参与生成。

所有输出带有 `test_only=true` 和 `environment="sandbox"`。街道以 `SANDBOX ONLY - Test Street` 开头；联系人使用 `Sandbox Customer` 和 `example.invalid` 邮箱。原始案例中的街道只保存在来源记录中，不作为生成街道。

## 测试抽样权重

`test_sampling_weight` 是人为设置的相对抽样权重，用于离线测试覆盖，不是统计得到的支付成功率：

| 来源 | 权重 | 含义 |
| --- | ---: | --- |
| `baseline` | 1 | 项目现有池中的城市／邮编组合 |
| `user_reported_city_pin` | 3 | 用户报告成功的城市／邮编组合 |
| `user_reported_full_address` | 5 | 用户提供完整地址案例对应的城市／邮编组合 |

同一组合同时出现多个来源时取最高档，不相乘、不因重复案例叠加。Coimbatore／641018 的两次报告保留两个来源编号，权重仍为 3；Madurai／625001 的两条街道案例保留两个来源编号，权重仍为 5。

来源案例标记为 `user_reported_unverified`，地理种子标记为 `not_postally_verified`。只有成功样本、没有各组合的总尝试数及失败样本，无法据此计算成功率或证明地址造成了成功。

生成库默认每州等量、州内按种子轮转，保证覆盖。权重是供测试消费方显式使用的字段；离线加权抽样示例：

```python
import json
import random
from pathlib import Path

path = Path("tests/fixtures/upi_sandbox/addresses.jsonl")
rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
assert all(row["test_only"] is True and row["environment"] == "sandbox" for row in rows)
pool = [row for row in rows if row["address"]["state_code"] == "TN"]
sample = random.Random(20261015).choices(
    pool, weights=[row["test_sampling_weight"] for row in pool], k=20
)
# sample 仅供离线断言、界面预览或 mock 测试。
```

## 重新生成与验证

从仓库根目录执行：

```powershell
& .\backend\.venv\Scripts\python.exe .\engines\pay153-checkout-link-main\tools\generate_upi_sandbox_billing.py --count 3000
```

脚本只使用 Python 标准库，写文件前校验记录数、去重、地区分布、位置字段一致性、模拟标记及权重来源。相同输入与数量可以重复生成相同的数据文件。修改 `locations.json` 后可重新运行；不要把输出接入真实支付。

生成器和数据文件独立于 `UPI_BILLING_ADDRESS_POOL`、运行中的 API 与 Worker。当前服务仍然没有加载本测试库，不需要重启服务。

## 与真实账单库的边界

本测试库的 3000 条记录**不接入**提链流程：`line1` 是 `SANDBOX ONLY - Test Street NNNNNN` 占位符，姓名为 `Sandbox Customer NNNNNN`，邮箱是保留域 `example.invalid`，且 214 条使用的城市（Kalyan-Dombivli、Karad、Cumbum、Krishnagiri）不在真实池内。生成器与单元测试都强制这些取值，属于结构性测试数据，接入真实支付会在服务自身的 `_is_complete_indian_address` 校验上失败。

真实账单库需要新地址时走 [UPI 账单池](upi-billing-pool.md) 里的 `UPI_BILLING_POOL_FILE` 通道，而不是本测试库。本文件中 `locations.json` 的 `references` 保留了 7 条用户反馈的完整地址（F01 Karad、F02 Cumbum、F03/F05 Madurai 625001、F04 Mumbai 400070、F06 Kalyan-Dombivli、F07 Pune 411001），它们的实际内容已整理进 `data/upi_billing_addresses.json`；需要生效时在后端设置 `UPI_BILLING_POOL_FILE` 指向该文件。`addresses.jsonl` 始终只用于离线测试。

已完成验证：3000 条记录、3000 个唯一地址、0 条重复、每州 600 条、37 个种子全部覆盖；14 项离线单元测试和 Ruff 检查通过。落盘数据重新读取后与摘要及 SHA-256 一致。测试入口（在引擎目录执行）：

```powershell
& ..\..\backend\.venv\Scripts\python.exe -m unittest test_upi_sandbox_billing -v
```

## 对 Nominatim 方案的核对

本地代码当前没有读取 `PAY153_INDIA_REVERSE_ENABLED`，可见的 `.env` 文件中也没有这个设置；未发现 `/reverse` 调用。`billing_address_resolver.py` 的印度分支直接返回内置静态地址，绕过地图接口和通用缓存。因此，别人给出的“已启用”说明不适用于本地这份版本，即使单独添加这个环境变量也不会生效。

`https://nominatim.openstreetmap.org/reverse` 是反向地理编码接口。所给请求格式中的参数含义如下：

| 参数 | 含义 |
| --- | --- |
| `lat`、`lon` | WGS84 纬度与经度，必须替换占位符 |
| `format=jsonv2` | JSON 返回格式 |
| `zoom=18` | 请求建筑物级别的地址细节，不保证返回门牌 |
| `addressdetails=1` | 返回拆分后的地址字段 |
| `accept-language=en` | 优先使用英语结果 |

它查找坐标附近合适的 OSM 地图对象，可能返回邻近街道，地址字段也可能缺失。它不能验证付款人地址、投递能力或付款成功率。依据：[Nominatim 官方反向查询文档](https://nominatim.org/release-docs/latest/api/Reverse/)。

公共实例规定整个应用合计最多每秒 1 次请求，要求可识别的 User-Agent 或 Referer；允许的小规模批量操作须单线程、单机并缓存结果，禁止网格反向查询等系统性采集。不能把它当作随机坐标批量造地址的接口。依据：[Nominatim 公共实例使用政策](https://operations.osmfoundation.org/policies/nominatim/)。

本次沙箱生成完全离线。若测试地址解析器，可使用固定的 mock 响应覆盖字段缺失、范围外地区、超时和限流等场景；无需为了生成测试库请求公共地图服务。
