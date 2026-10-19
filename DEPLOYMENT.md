# 本仓库部署说明

签到脚本采用 [sickpoet/anyag-checkin](https://github.com/sickpoet/anyag-checkin) 的
[`cb7cc6e`](https://github.com/sickpoet/anyag-checkin/commit/cb7cc6ee2ea54f5df486f860be6f22559c4f9468) 版本。
登录与签到流程沿用上游；本仓库增加浏览器启动崩溃的有限重试和固定代理部署适配。依赖锁文件保持上游版本。

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

固定代理保存在 `production` 的 `FIXED_PROXY_URL` Secret 中，例如
`http://user:password@host:port` 或 `socks5://user:password@host:port`。
代理通过 Runner 内的 mihomo 转接，支持带认证的 HTTP(S) 和 SOCKS5；只供配置为使用代理的站点使用。
用户名／密码含 `@`、`:`、`#` 等特殊字符时，应先进行 URL 编码。

在 [AnyRouter 自动签到](https://github.com/namejee/anyrouter-check-in/actions/workflows/checkin.yml)
选择 Run workflow，`providers` 填 `anyrouter,agentrouter`，其余保持默认。
手动测试直接处理指定范围，不执行上游的长时间窗口对齐。

每天计划北京时间 **08:03** 触发一次，UTC cron 为 `3 0 * * *`；处理全部账号。
维护版注明 AnyRouter 在 08:05 后登录才发放奖励，因此定时任务最早 **08:06** 执行签到，
若环境提前准备完成则最多等待三分钟；延迟启动或手动运行不等待。
上游最长数小时的窗口对齐步骤已移除，GitHub 实际启动仍可能延迟。
查看每个账号的结果和余额变化，不能只凭整个任务的绿色状态判断全部账号成功。
首次运行建立余额基线，不能单凭首次余额查询确认当日新增奖励。

部署后的[四账号实测记录](https://github.com/namejee/anyrouter-check-in/actions/runs/39283870327)：
两个 AnyRouter 账号取得服务端签到回执并读取余额；两个 AgentRouter 账号通过密码重新登录，
并成功验证用户信息和读取余额。AgentRouter 首次建立余额基线，不据此声称新增了某个固定金额的奖励。

## 部署适配

- 固定使用 Ubuntu 24.04，避免 `ubuntu-latest` 迁移系统版本改变浏览器运行环境。
- Actions 启动时将旧 Secret 中的 `anyrouter.top`／`agentrouter.org`（含协议和尾斜杠的写法）转换为上游的 `anyrouter`／`agentrouter`，不改写 GitHub Secrets。
- 移除旧账号配置中的 WAF Cookie（`acw_tc`、`cdn_sec_tc`、`acw_sc__v2`），使用上游本次运行新获取的值；保留 session 等登录凭据。
- 启动时为密码、邮箱和 Cookie 注册日志遮罩。
- 不上传浏览器登录 Profile 到 Actions 公共缓存；本方案的 AnyRouter Cookie 模式与 AgentRouter 非持久登录不需要该缓存。
- 浏览器仅在启动发生 SIGSEGV／进程提前关闭时最多重试两次；不会因此重复登录或签到。
- 关闭浏览器二进制后台自动更新，缓存与依赖锁版本绑定。
- 任一账号在日志中报告失败时，整个 Actions 任务标记为失败，便于发现部分账号失败。

## 回退

替换前的版本保存在分支 `backup/before-sickpoet-20261019`，对应提交
`30f50ac2042c8a7f8bedddbec5d69d09bb9da213`。回退应把旧文件树作为新提交恢复到 `main`，
无需强制推送，也不需要重建仓库或删除 Secrets。
