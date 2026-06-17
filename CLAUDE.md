# CLAUDE.md

## 项目：claude-message-box

Claude Code 消息队列插件，支持通过 hooks 接管会话输入，实现弹窗/普通/静默消息推送。

### 我的身份

- 名字：**Alkaid**（瑶光星）
- GitHub: tobylinas2
- Git 配置：`user.name=Alkaid`, `user.email=tobylinas2@users.noreply.github.com`
- 开发分支：`dev/alkaid`
- 我是 bot/assistant 账号，tobyprime 是用户

### 项目结构

```
msgbox/
├── cli.py          # CLI 入口（msgbox start/stop/send/subscribe 等）
├── config.py       # 路径与配置管理
├── db.py           # 中央消息数据库
├── session.py      # 会话跟踪数据库
├── filter.py       # 过滤引擎（ignore/popup/normal/silent 分类）
├── template.py     # 模板渲染引擎
├── yaml_config.py  # YAML 配置管理
└── sources/
    └── github.py   # GitHub webhook 接收器（HTTP 服务器）
```

### 运行中的服务

- **msgbox source-github** — 监听 `127.0.0.1:3001/webhook`，接收 GitHub webhook
- **smee-client** — 连接 `https://smee.io/kt2xnWX9XKQBxCPA`，转发 webhook 到本地
- 代理：`HTTP_PROXY=http://127.0.0.1:7890`（smee-client 需要）

### 消息分类规则

| 规则 | 匹配 | 效果 |
|------|------|------|
| ignore | `discussion_comment` | 所有讨论评论默认忽略 |
| ignore | `issue_comment` | 所有 issue 评论默认忽略 |
| ignore | `sender=tobylinas2` | 自己（bot）的所有事件忽略 |
| ignore_excluded | `discussion_comment + number=N` | 订阅的讨论评论透出 |
| ignore_excluded | `mentions=tobylinas2` | @提及自己的评论透出 |
| ignore_excluded | `discussion_comment + sender=tobyprime` | tobyprime 的评论透出 |
| ignore_excluded | `issue_comment + sender=tobyprime` | tobyprime 的 issue 评论透出 |
| popup | `discussion_comment + sender=tobyprime` | tobyprime 任何评论弹窗 |
| popup | `issue_comment + sender=tobyprime` | tobyprime 任何 issue 评论弹窗 |
| popup | `discussion_comment + mentions=tobylinas2` | @提及弹窗 |
| popup | `issue_comment + mentions=tobylinas2` | @提及弹窗 |
| popup | `pr + action=opened` | PR 新建弹窗 |
| popup | `pr + merged=true` | PR 合并弹窗 |
| popup | `issue + action=opened` | Issue 新建弹窗 |
| popup | `release` | Release 弹窗 |
| popup | `workflow_run + conclusion=failure` | CI 失败弹窗 |
| popup | `check_run + conclusion=failure` | Check 失败弹窗 |
| silent | `star/fork/ping/status/check_suite/push` | 高频率低价值事件静默 |

### 常用命令

```bash
msgbox start                      # 激活消息盒子
msgbox stop                       # 停用消息盒子
msgbox subscribe discussion <N>   # 订阅讨论评论
msgbox subscribe discussion <N> --popup  # 订阅并弹窗
msgbox unsubscribe discussion <N> # 取消订阅
msgbox subscriptions              # 查看订阅
msgbox source-github              # 启动 webhook 服务器
msgbox config rules               # 查看规则
msgbox config add-rule <type> <pattern> --props '{"key":"val"}'  # 添加规则
msgbox config remove-rule <type> <index>  # 删除规则
```

### 架构

```
GitHub → smee.io → smee-client → localhost:3001/webhook → classify → msgbox DB → hooks → 会话
```

### 注意事项

- 用 `gh api graphql` 回复 GitHub Discussion（REST API 对 discussion 支持不全）
- 回复后自己的 webhook 会回到 msgbox，但被 `ignore sender=tobylinas2` 过滤
- smee-client 需要代理才能连接外网
