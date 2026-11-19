# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

本仓库有两个版本号，改的时候一起看：

- **Python 包版本**：写在 `pyproject.toml` 与 `src/tianji_power_mcp/__init__.py`，是使用者安装到的版本，也是 MCP 握手时 `serverInfo.version` 报的值
- **DSH bundle 版本**：写在 `dsh-bundles/tianji-power/package.json`，是 DeepSeek Harness 插件包的版本，只在 bundle 自身内容变化时递增

## [未发布]

## [0.1.1] - 2026-11-19

对应 DSH bundle 1.0.1。

### 修复

- 各工具现在只分析调用方请求的 `hours` 窗口。此前 `power_weather_hint` / `wind_solar_snapshot` / `theoretical_power` 会把按整天缓存的数据整段拿去计算，问 6 小时实际分析了 24 小时，事件时间窗与出力汇总都对不上
- `get_dataset` 的 `total_rows` 改为返回缓存中的完整行数；此前误报为本次窗口行数
- `get_dataset` 的 `truncated` 改为表示「返回行数 < 窗口行数」，只反映 `limit` 是否真的截断

### 新增

- `Series.dataset_rows` 记录缓存完整行数，`Series.window(hours)` 按请求时段裁行（缓存本身不变，仍按整天取数以复用额度）
- `get_dataset` 新增 `window_rows` 字段，`note` 会说明窗口余量与截断情况

### 行为变化

- 调用方式不变；返回值多了 `window_rows`，`total_rows`、`truncated` 的语义按上修正
- 若下游代码依赖旧的 `total_rows` 语义，需要相应调整

### 测试

- 离线测试 37 → 43 项，新增窗口语义与缓存口径的回归用例

## [0.1.0] - 2026-11-19

首个版本。

### 新增

- 5 个 MCP 工具：`get_capability`、`power_weather_hint`、`wind_solar_snapshot`、`theoretical_power`、`get_dataset`
- 六类电力场景提示（大风 / 覆冰 / 低温 / 高温 / 强降水 / 云遮挡），带触发条件、证据数值与 `data_gaps`
- 风电与光伏理论出力演示（功率曲线；辐照 × 性能比 × 温度修正）
- 一次取齐 + 整天缓存 + 进程级调用预算 + 要素注册表剔除重试
- DeepSeek Harness 配置型 bundle（`dsh-bundles/tianji-power`）
- 离线测试与真实响应 fixture、stdio 冒烟脚本
