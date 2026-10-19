# 本仓库部署说明

签到脚本采用 [sickpoet/anyag-checkin](https://github.com/sickpoet/anyag-checkin) 的
[`cb7cc6e`](https://github.com/sickpoet/anyag-checkin/commit/cb7cc6ee2ea54f5df486f860be6f22559c4f9468) 版本。
`checkin.py`、`utils/`、依赖锁文件和测试均保留上游原版。

## 当前方案

- AnyRouter：保留 `production` 环境中已有的 `ANYROUTER_ACCOUNTS`，继续使用登录后的 Cookie 和 `api_user`。手动通过 GitHub 或 Linux.do 登录取得 Cookie，Cookie 失效时需要更新。
- AgentRouter：使用 AgentRouter 自身的用户名／邮箱和密码，每次通过浏览器重新登录。这里不填 GitHub 或 Linux.do 的密码。
- GitHub Actions 自动运行，不需要单独的服务器。定时触发可能延迟，以实际运行记录为准。

## 添加 AgentRouter

在 [Settings → Environments](https://github.com/namejee/anyrouter-check-in/settings/environments)
进入 `production`，新增 Environment secret `ANYROUTER_ACCOUNTS_EXTRA`：

```json
[
  {
    "name": "AgentRouter 主账号",
    "provider": "agentrouter",
    "email": "替换为 AgentRouter 用户名或邮箱",
    "password": "替换为 AgentRouter 自身密码"
  }
]
```

多个 AgentRouter 账号可放进同一个数组，使用不同的 `name`。
不要将真实密码、Cookie 提交到仓库或粘贴到公开日志。

## 验证与运行

在 [AnyRouter 自动签到](https://github.com/namejee/anyrouter-check-in/actions/workflows/checkin.yml)
选择 Run workflow，`providers` 填 `anyrouter,agentrouter`，其余保持默认。
这样手动测试会立即执行，避免进入上游的定时等待。

计划触发时间沿用上游：北京时间 00:10、08:10、14:10、20:10。
上游会根据实际启动时刻等待到目标窗口；运行中出现等待日志属于预期。
查看每个账号的结果和余额变化，不能只凭整个任务的绿色状态判断全部账号成功。
首次运行建立余额基线，不能单凭首次余额查询确认当日新增奖励。

## 部署适配

- Actions 启动时将旧 Secret 中的 `anyrouter.top`／`agentrouter.org`（含协议和尾斜杠的写法）转换为上游的 `anyrouter`／`agentrouter`，不改写 GitHub Secrets。
- 移除旧账号配置中的 WAF Cookie（`acw_tc`、`cdn_sec_tc`、`acw_sc__v2`），使用上游本次运行新获取的值；保留 session 等登录凭据。
- 启动时为密码、邮箱和 Cookie 注册日志遮罩。
- 不上传浏览器登录 Profile 到 Actions 公共缓存；本方案的 AnyRouter Cookie 模式与 AgentRouter 非持久登录不需要该缓存。

## 回退

替换前的版本保存在分支 `backup/before-sickpoet-20261019`，对应提交
`30f50ac2042c8a7f8bedddbec5d69d09bb9da213`。回退应把旧文件树作为新提交恢复到 `main`，
无需强制推送，也不需要重建仓库或删除 Secrets。
