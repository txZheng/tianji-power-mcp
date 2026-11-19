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
- **离线可测**：43 项离线测试全部基于真实响应 fixture，不联网、不需要凭据

## 安装

三种方式任选其一，都要求 **Python ≥ 3.10**。

### 方式一：下载压缩包（不想装 git 的用户）

```bash
curl -LO https://github.com/txZheng/tianji-power-mcp/archive/refs/heads/main.zip
unzip main.zip && cd tianji-power-mcp-main
python3 -m venv .venv
.venv/bin/pip install -e .
```

如果 Releases 页面提供了打包好的版本，也可以直接下载对应 zip 解压。

### 方式二：git clone

```bash
git clone https://github.com/txZheng/tianji-power-mcp.git
cd tianji-power-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 方式三：pip 直接安装（不用克隆）

```bash
pip install "git+https://github.com/txZheng/tianji-power-mcp.git"
```

装完会得到 `tianji-power-mcp` 命令，可以直接作为宿主的启动命令：

```bash
tianji-power-mcp
```

用 [uv](https://docs.astral.sh/uv/) 的话可以更省事，连虚拟环境都不用建：

```bash
uvx --from "git+https://github.com/txZheng/tianji-power-mcp.git" tianji-power-mcp
```

> 方式三没有项目目录，因此**无法读取 `.tjweather-rc`**，凭据要通过宿主的环境变量注入（见下文各宿主配置里的 `env` 字段）。

## 配置凭据

还没有 API 凭据的话，先到天机气象官网申请：

- [API-KEY 申请与权益查询](https://www.tjweather.com/info/doc/video/tutorial1.html) —— 注册、申请 API-KEY、查看订阅权益
- [天机 API 概览](https://www.tjweather.com/info/doc/download/api/readme.html) —— 端点、错误码、调用频率
- [JSON 格式 API 文档](https://www.tjweather.com/info/doc/download/api/weather/json.html) —— 要素码表与参数说明

拿到 key 之后，凭据由服务自己读取，**宿主的 JSON 配置里不需要出现任何密钥**：

- 读取顺序：`环境变量` > 凭据文件 `.tjweather-rc`（兼容旧的 `.env`）
- 用 `pip install -e .`（源码方式）安装时，服务的 `PROJECT_ROOT` 就是仓库目录，因此**无论宿主把工作目录设成什么，都能读到仓库里的 `.tjweather-rc`**
- 用 `pip install git+...`（非源码方式）安装时没有仓库目录，只能走环境变量

`.tjweather-rc` 已被 git 忽略。建议再收紧权限，避免同机其它用户读到：

```bash
chmod 600 .tjweather-rc
```

内容格式：

```bash
cp .tjweather-rc.example .tjweather-rc
# 编辑 .tjweather-rc，填入你自己的 key 与订阅 ID
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `TJW_API_KEY` | ✅ | 天机气象 API key |
| `TJW_SUBSCRIPTION_ID` | ✅ | `/v2` 端点必需的订阅 ID（也接受 `real_sub_id`） |
| `TJW_BASE_URL` | ❌ | 默认 `https://api.tjweather.com/v2` |
| `TJW_MAX_CALLS` | ❌ | 进程级调用预算，默认 20 |

## 启动

这个服务走 **STDIO 传输**：没有端口、没有网页、没有交互界面，由宿主作为子进程拉起并对话。

```bash
tianji-power-mcp
# 或：python -m tianji_power_mcp.server
```

在终端里直接运行会**静默等待**（在等 stdin 上的 JSON-RPC 消息），这是正常的，`Ctrl+C` 退出。想确认它是否健康，跑自检脚本（不消耗额度）：

```bash
python scripts/smoke_stdio.py
```

## 接入宿主

所有宿主都需要这三个参数，只是写法不同：

| 字段 | 值 |
|---|---|
| `command` | `tianji-power-mcp`（pip 安装）或 `<REPO_DIR>/.venv/bin/python`（源码安装） |
| `args` | pip 安装留空；源码安装为 `["-m", "tianji_power_mcp.server"]` |
| `env` | `TJW_API_KEY`、`TJW_SUBSCRIPTION_ID`（若不用凭据文件） |

### 让 agent 自己接入（推荐）

不想手改配置的话，把下面这段整段发给你正在用的 agent（DeepSeek Harness、WorkBuddy、Cursor 等），它会自己完成下载、安装、注册和自检。

```text
请帮我接入「天机气象 · 电力提示」MCP 服务，按下面步骤做，每步做完简短回报：

1. 安装
   git clone https://github.com/txZheng/tianji-power-mcp ~/mcp/tianji-power-mcp
   cd ~/mcp/tianji-power-mcp && python3 -m venv .venv && .venv/bin/pip install -e .

2. 凭据：向我索取 API key 和订阅 ID（不要自己编，也不要写进任何会进 git 的文件），
   写入 ~/mcp/tianji-power-mcp/.tjweather-rc：
     TJW_API_KEY=...
     TJW_SUBSCRIPTION_ID=...

3. 按你所在宿主注册这个 stdio 服务，服务名用 tianji-power：
   - DeepSeek Harness：用仓库里的 dsh-bundles/tianji-power，把其中 <REPO_DIR> 换成
     ~/mcp/tianji-power-mcp 后装进 profile
   - WorkBuddy：写进 ~/.workbuddy/mcp.json 的 mcpServers
   - Claude Desktop / Cursor：写进各自的 mcpServers 配置
   启动参数统一为：
     command = ~/mcp/tianji-power-mcp/.venv/bin/python
     args    = ["-m", "tianji_power_mcp.server"]
     cwd     = ~/mcp/tianji-power-mcp

4. 自检（不消耗 API 额度）
   cd ~/mcp/tianji-power-mcp && .venv/bin/python scripts/smoke_stdio.py
   应列出 5 个工具，其中包含 get_dataset。

5. 回报：安装路径、服务名、工具列表、以及 get_capability 返回的已授权要素数量。
   注意：不要为了验证去跑 scripts/smoke_live.py，它会消耗 API 调用额度。
```

只需要装好、凭据你自己后面再填的话，一句话就够：

```text
帮我接入天机电力气象 MCP：git clone https://github.com/txZheng/tianji-power-mcp ~/mcp/tianji-power-mcp 并
`pip install -e .`，然后按你当前宿主的 MCP 配置注册 stdio 服务
（command 用 .venv/bin/python，args 用 -m tianji_power_mcp.server），凭据我稍后提供。
```

agent 执行过程中可能会请你确认路径、Key、以及是否写入宿主配置——这些是正常的安全确认，确认后它会自己跑完自检。

### DeepSeek Harness

仓库里的 [`dsh-bundles/tianji-power`](dsh-bundles/tianji-power) 是一个可直接安装的 bundle（配置型，不含 JS 代码）。安装后由 `dsh-mcp-client` 按补丁里的配置拉起服务：

```yaml
# dsh-bundles/tianji-power/cordis.patch.yml（把 <REPO_DIR> 换成你的实际路径）
dsh-mcp-client:
  servers:
    power:
      serverName: power
      transport: stdio
      command: <REPO_DIR>/.venv/bin/python
      args: ["-m", "tianji_power_mcp.server"]
      cwd: <REPO_DIR>
```

把该目录装进 profile（用 `plugin_manager` 的 `install_bundle`，或在 profile 的 `cordis.yml` 里合并这段补丁），重启会话后工具会以 `mcp__power__*` 的名字出现。

### WorkBuddy

WorkBuddy 支持两级 MCP 配置，按需选一个：

| 级别 | 路径 | 适用 |
|---|---|---|
| 用户级 | `~/.workbuddy/mcp.json` | 所有项目复用 |
| 项目级 | `<项目目录>/.workbuddy/mcp.json` | 只在当前项目生效 |

也可以在客户端里走可视化入口：侧边栏 **插件 → MCP 服务器**，然后填同样的配置。

配置文件内容：

```json
{
  "mcpServers": {
    "tianji-power": {
      "type": "stdio",
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "tianji_power_mcp.server"],
      "cwd": "/absolute/path/to/tianji-power-mcp",
      "description": "天机气象 · 电力提示"
    }
  }
}
```

**凭据不写在这里。** 服务启动时会自己去仓库目录读 `.tjweather-rc`（上文的读取顺序），所以这份 JSON 里没有任何秘密，可以放心备份、截图、分享。只有在你用 `pip install git+...` 没克隆仓库时，才需要额外补 `"env": {"TJW_API_KEY": "...", "TJW_SUBSCRIPTION_ID": "..."}`。

用 pip 安装的话把 `command` 改成 `tianji-power-mcp`、`args` 留空即可。配置格式参见 [WorkBuddy MCP 文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)。

### 其它 JSON 型宿主（Claude Desktop / Cursor）

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

- Claude Desktop：`~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）
- Cursor：`~/.cursor/mcp.json`

### 接入后怎么问

接好之后直接用自然语言提问，不需要记参数：

- 「大同未来 3 天风光情况如何」
- 「112.55,37.87 明天有没有覆冰条件」
- 「用切入 3、额定 13、切出 20、容量 5MW 算一下这里的理论出力」
- 「这个点位光伏这周理论出力多少」

坐标格式统一为 `经度,纬度`（经度在前）。工具返回里的 `data_gaps` 请一并转述，它说明这次判定缺了哪些要素。

## 工具

| 工具 | 用途 | 是否发请求 |
|---|---|---|
| `get_capability` | 已授权要素、已知不可用要素、调用预算、缓存策略 | 否 |
| `get_dataset` | **唯一取数入口**，返回窗口内全部已授权要素；`fields` 投影列、`limit` 截断返回行数 | 首次 1 次 |
| `power_weather_hint` | 六类提示，按事件合并，带触发条件与证据 | 首次 1 次 |
| `wind_solar_snapshot` | 风电/光伏关键要素逐时快照 | 首次 1 次 |
| `theoretical_power` | 理论出力演示，`kind="wind"` 或 `kind="pv"` | 首次 1 次 |

### 窗口与 `limit` 的语义

`hours` 是所有工具共用的**分析窗口**：缓存按整天向上取整取数（问 6 小时实际取 24 小时），但提示、快照、理论出力、`get_dataset` 都只处理你请求的那几个小时，整天余量仅用于同日复用。

`get_dataset` 的 `limit` **只截断本次返回的行数**，不改变取数窗口、不影响缓存、更不影响其他工具计算所用的完整数据。响应里的三个行数字段含义不同：

- `window_rows`：本次请求窗口内的行数（由 `hours` 决定）
- `total_rows`：缓存里的完整数据集行数（按整天向上取整），**不是**本次返回或本次窗口的行数
- `returned_rows`：实际返回的行数（受 `limit` 影响）

`truncated` 表示 `returned_rows < window_rows`，即 `limit` 真的截断了这次返回；窗口小于缓存或发生截断时，`note` 会给出说明。

## 取数模型

天机 `/v2` 一次返回该点位该窗口的全部要素，服务端不做字段级筛选。因此客户端的设计是：

1. 首次访问某窗口时，把**全部已授权要素**拼成一个请求发出去
2. 结果进缓存，键只含 `(坐标, 起始小时, 取数窗口, 分辨率)`——**不含要素集合**
3. 之后风电、光伏、理论出力、原始取数都从这份缓存里投影出需要的列
4. 窗口按整天向上取整（问 6 小时实际取 24 小时），同一天内的后续提问都能命中；但各工具只分析调用方请求的 `hours`，整天余量不会被算进结果

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
.tjweather-rc.example  凭据模板（复制成 .tjweather-rc 后填写）
LICENSE          MIT
SECURITY.md      凭据处理与漏洞报告约定
CHANGELOG.md     版本变更记录
dsh-bundles/     DeepSeek Harness 用的配置型 bundle
src/tianji_power_mcp/
  config.py     配置与凭据（环境变量 > .tjweather-rc）
  elements.py   要素注册表（ALL_FIELDS / 已授权 / 已拒 / 别名）
  client.py     /v2 客户端：一次取齐 + 投影、预算、缓存、要素剔除
  derive.py     派生量：露点、风切变、轮毂风速、风力等级、降水相态
  hints.py      提示规则引擎与事件合并
  power.py      风电理论出力（功率曲线）
  pv.py         光伏理论出力（辐照 × PR × 温度修正）
  server.py     MCP 工具定义
tests/          43 项离线测试 + 真实响应 fixture
scripts/        fetch_fixture.py（1 次额度）/ smoke_stdio.py（零额度）/ smoke_live.py（1 次额度）
```

## 开发与测试

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 离线，零额度
.venv/bin/python scripts/smoke_stdio.py               # 冒烟：握手 + tools/list
.venv/bin/python scripts/smoke_live.py                # 端到端（消耗 1 次额度）
```

调试时**不要用 `print()`** 往 stdout 写东西——stdio 传输下 stdout 就是协议通道，会污染协议流。日志请写 `sys.stderr`。

## 版本

版本号只有一个，写在三处，发布时一起改：

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | Python 包元数据，`pip show` / PyPI 上显示的值 |
| `src/tianji_power_mcp/__init__.py` | 运行时版本，MCP 握手时作为 `serverInfo.version` 上报 |
| `dsh-bundles/tianji-power/package.json` | DSH 插件包的 npm 版本，与包版本保持一致 |

改完代码按[语义化版本](https://semver.org/lang/zh-CN/)递增（修 bug 加第三位、加功能加第二位、不兼容升主版本），在 [CHANGELOG.md](CHANGELOG.md) 记一条，再打同名 tag（如 `v0.1.1`）。当前：**0.1.1**。

## 许可

[MIT](LICENSE)
