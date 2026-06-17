---
name: start_msg_box
description: 激活消息盒子，使当前会话的 hooks 开始接管消息输入。配合 /message-box:stop_msg_box 停用。
disable-model-invocation: true
---

Activate the message box for this session.

```bash
msgbox start
```

After activation, SessionStart hook will block waiting for new messages, and PostToolUse will peek for updates.
