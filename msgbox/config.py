"""Message Box - 路径与配置管理"""

import os
from pathlib import Path

PLUGIN_DIR = Path.home() / ".claude" / "plugins" / "message-box"
CENTRAL_DB = Path(os.environ.get("MESSAGE_BOX_DB_PATH") or str(PLUGIN_DIR / "msg_box.db"))
CONFIG_FILE = PLUGIN_DIR / "config.yaml"
SESSIONS_DIR = PLUGIN_DIR / "sessions"
DINGTALK_STATE_DB = Path(os.environ.get("MESSAGE_BOX_DINGTALK_STATE_DB") or str(PLUGIN_DIR / "dingtalk_state.db"))

# 环境变量配置（带默认值）
IDLE_DURATION = int(os.environ.get("MESSAGE_BOX_IDLE_DURATION", "30"))
SLEEP_DURATION = int(os.environ.get("MESSAGE_BOX_SLEEP_DURATION", "60"))
PEEK_COOLDOWN = int(os.environ.get("MESSAGE_BOX_PEEK_COOLDOWN", "1"))
# wait 命令检测到第一条消息后，继续收集同批次消息的缓冲窗口（秒）
WAIT_BATCH_WINDOW = float(os.environ.get("MESSAGE_BOX_WAIT_BATCH_WINDOW", "1.0"))
