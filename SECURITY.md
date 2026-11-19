# 安全说明

## 凭据处理

本项目的 API 凭据**只从环境变量或凭据文件 `.tjweather-rc` 读取**，且该文件已在 `.gitignore` 中：

```
.tjweather-rc
.env
```

- 仓库里**不含**任何真实 key 或订阅 ID；`.tjweather-rc.example` 只有占位符。
- 运行时拒绝把凭据写进日志：`get_capability` 返回的 key 是掩码形式（`abcd***xy`）。
- 请求以 query string 携带 key（服务端要求如此）。如果你的部署会记录 URL，请确保访问日志已脱敏。

## 不要提交的内容

以下路径已在 `.gitignore` 中，如果你新增类似文件请一并忽略：

- `.tjweather-rc` / `.tjweather-rc.*`（保留 `.example`）与 `.env`
- `.runtime/`（运行时黑名单缓存）
- `.venv/`、`__pycache__/`、`*.egg-info/`、`build/`、`dist/`

## 报告问题

发现凭据泄露或安全缺陷，请勿公开开 issue，走私密渠道报告：

- 首选 GitHub「私密安全报告」（Private vulnerability reporting；在仓库 Settings → Code security 中开启后，报告只有维护者可见，不会进入公开时间线）
- 专用安全邮箱暂未设置，设置后会更新本节

如果凭据已泄露，请立即在天机控制台轮换 key，并检查订阅的调用记录。
