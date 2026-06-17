"""Message Box - 路径与配置管理"""

import os
from pathlib import Path

PLUGIN_DIR = Path.home() / ".claude" / "plugins" / "message-box"
CENTRAL_DB = Path(os.environ.get("MESSAGE_BOX_DB_PATH") or str(PLUGIN_DIR / "msg_box.db"))
CONFIG_FILE = PLUGIN_DIR / "config.yaml"
SESSIONS_DIR = PLUGIN_DIR / "sessions"

# 环境变量配置（带默认值）
IDLE_DURATION = int(os.environ.get("MESSAGE_BOX_IDLE_DURATION", "30"))
SLEEP_DURATION = int(os.environ.get("MESSAGE_BOX_SLEEP_DURATION", "60"))
PEEK_COOLDOWN = int(os.environ.get("MESSAGE_BOX_PEEK_COOLDOWN", "1"))
