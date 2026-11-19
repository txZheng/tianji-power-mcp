# 天机气象 · 电力提示 MCP

把天机气象 `/v2` 的预报要素，转成**面向电力资产的天气提示**，通过 [Model Context Protocol](https://modelcontextprotocol.io) 交给任意支持 MCP 的 agent 调用。

不是又一个"给个坐标返回一堆数"的天气接口——它直接回答电力行业的问题：这里明天有没有大风、会不会覆冰、光伏几点被云挡住、理论出力大概多少。

> **English summary** — A Model Context Protocol server that turns TIANJI Weather `/v2` forecasts into power-sector advisories (high wind, icing, extreme temperature, heavy rain, cloud cover) plus a demonstrative theoretical power model for wind and PV. Python 3.10+, STDIO transport, one API request per location/window, and every result carries its trigger thresholds and data gaps.

---

## 它做什么，不做什么

**做**：

- 六类电力场景提示，按事件合并，附触发条件、证据数值、时间窗
- 逐时要素快照（含露点、轮毂风速、风力等级等派生量）
- 理论出力演示：风电按功率曲线（切入/额定/切出/轮毂高度/容量可调），光伏按辐照度 × 性能比 × 温度修正
- 明确的 `data_gaps`：这次判定缺了哪些要素、结论因此打了多少折扣

**不做**：

- 不做功率预测，不做发电量考核口径，不含尾流、限电、机组可用率
- 不做资产台账管理，输入是坐标而不是场站
- 不替代正式气象预报——所有结果都是**参考提示**

## 特性

- **一次取齐，按需计算**：同一坐标 + 同一窗口只发一次 API 请求，之后所有工具复用缓存，零额外请求
- **要素注册表驱动**：未授权要素在组装请求前就被挡掉，不会因为一个坏字段让整次请求失败
- **额度自保护**：进程级调用预算、缓存 TTL、非法参数在发请求前拦截
- **离线可测**：36 项单元测试全部基于真实响应 fixture，不联网、不需要凭据

## 快速开始

```bash
git clone <repo-url> && cd tianji-power-mcp

python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env        # 填入你的 API key 与订阅 ID
```

凭据从环境变量或工作区 `.env` 读取：

```bash
TJW_API_KEY=...
TJW_SUBSCRIPTION_ID=...
# 可选：TJW_BASE_URL / TJW_MAX_CALLS
```

启动（stdio 传输）：

```bash
.venv/bin/python -m tianji_power_mcp.server
```

接入选你用的宿主，例如：

```json
{
  "mcpServers": {
    "tianji-power": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "tianji_power_mcp.server"],
      "cwd": "/absolute/path/to/tianji-power-mcp"
    }
  }
}
```

接好后直接用自然语言提问即可，例如：

- 「大同未来 3 天风光情况如何」
- 「112.55,37.87 明天有没有覆冰条件」
- 「用切入 3、额定 13、切出 20、容量 5MW 算一下这里的理论出力」

坐标格式统一为 `经度,纬度`（经度在前）。

## 工具

| 工具 | 用途 | 是否发请求 |
|---|---|---|
| `get_capability` | 已授权要素、已知不可用要素、调用预算、缓存策略 | 否 |
| `get_dataset` | **唯一取数入口**，返回窗口内全部已授权要素；`fields` 投影列、`limit` 截断返回行数 | 首次 1 次 |
| `power_weather_hint` | 六类提示，按事件合并，带触发条件与证据 | 首次 1 次 |
| `wind_solar_snapshot` | 风电/光伏关键要素逐时快照 | 首次 1 次 |
| `theoretical_power` | 理论出力演示，`kind="wind"` 或 `kind="pv"` | 首次 1 次 |

### `limit` 的语义

`get_dataset` 的 `limit` **只截断本次返回的行数**，不改变取数窗口、不影响缓存、更不影响其他工具计算所用的完整数据。响应同时给出 `total_rows` / `returned_rows` / `truncated`，避免把截断后的结果误当成全量。

## 取数模型

天机 `/v2` 一次返回该点位该窗口的全部要素，服务端不做字段级筛选。因此客户端的设计是：

1. 首次访问某窗口时，把**全部已授权要素**拼成一个请求发出去
2. 结果进缓存，键只含 `(坐标, 起始小时, 取数窗口, 分辨率)`——**不含要素集合**
3. 之后风电、光伏、理论出力、原始取数都从这份缓存里投影出需要的列
4. 窗口按整天向上取整（问 6 小时实际取 24 小时），同一天内的后续提问都能命中

所以「先问风光提示，再问理论出力」总共只有 **1 次** API 调用。

## 支持的要素

| 要素 | 含义 | 单位 |
|---|---|---|
| `t2m` | 2 米温度 | °C |
| `rh2m` | 2 米相对湿度 | % |
| `psz` | 地表气压 | Pa |
| `tp` | 总降水 | mm/hr |
| `pres` | 降雪量 | mm |
| `ssrd` | 地表向下短波辐射（GHI） | W/㎡ |
| `ws10m` / `ws100m` | 10 米 / 100 米风速 | m/s |
| `wd100m` | 100 米风向 | ° |
| `cldt` / `cldh` / `cldm` / `cldl` | 总 / 高 / 中 / 低云量 | 0~1 |

**按订阅授权**：要素可用性取决于你的订阅，`get_capability` 会返回当前实际可用的清单。已知常见的未授权要素（如 `gust`、`cape`、`dpt2m`、`prei`、`ddsf_ave`、`bdsf_ave`）在本仓库的注册表里被显式标注，不会进入请求。

## 设计原则

1. **注册表驱动**：`elements.py` 是唯一要素来源，未授权要素在请求组装前就被挡掉
2. **降级而不失败**：缺要素时返回 `data_gaps` 说明影响，而不是整次请求 403
3. **阈值透明**：判定阈值随结果一起返回，可按自己的运行规程覆盖
4. **代理判据显式标注**：没有能见度要素时雾是代理判据；没有液态水含量与冻雨要素时覆冰是气象条件代理、**不代表冰厚**
5. **理论出力只是演示**：按功率曲线由风速/辐照映射，不含机组可用率、尾流与调度约束

## 局限与免责

- 所有输出均为**参考提示，非正式气象预报，不构成生产指令**
- 覆冰、大雾为**代理判据**，不是物理量；重冰区、航空放行等场景不足以支撑决策
- 理论出力是**未标定的演示模型**，实际发电量还需乘可用率、扣尾流与限电，并按现场标定
- 光伏按水平面 GHI 计算，未做倾斜面转换（可用 `tilt_gain` 显式补偿）
- 数据来源：中科天机气象 API，使用请遵守对应服务条款

## 项目结构

```
src/tianji_power_mcp/
  config.py     配置与凭据（环境变量 > 工作区 .env）
  elements.py   要素注册表（ALL_FIELDS / 已授权 / 已拒 / 别名）
  client.py     /v2 客户端：一次取齐 + 投影、预算、缓存、要素剔除
  derive.py     派生量：露点、风切变、轮毂风速、风力等级、降水相态
  hints.py      提示规则引擎与事件合并
  power.py      风电理论出力（功率曲线）
  pv.py         光伏理论出力（辐照 × PR × 温度修正）
  server.py     MCP 工具定义
tests/          36 项离线测试 + 真实响应 fixture
scripts/        fetch_fixture.py / smoke_stdio.py / smoke_live.py
```

## 开发与测试

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 离线，零额度
.venv/bin/python scripts/smoke_stdio.py               # 冒烟：握手 + tools/list
.venv/bin/python scripts/smoke_live.py                # 端到端（消耗 1 次额度）
```

调试时**不要用 `print()`** 往 stdout 写东西——stdio 传输下 stdout 就是协议通道，会污染协议流。日志请写 `sys.stderr`。

## 许可

[MIT](LICENSE)
