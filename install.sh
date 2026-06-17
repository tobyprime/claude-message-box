#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$HOME/.claude/plugins/claude-message-box"
SKILL_LINK="$HOME/.claude/skills/message-box"

echo "==> Installing claude-message-box..."

# 1. Install Python package
pip install -e "$REPO_DIR" --quiet
echo "    Python package installed: msgbox"

# 2. Symlink to skills/ for auto-loading
ln -sfn "$REPO_DIR" "$SKILL_LINK"
echo "    Plugin linked: $SKILL_LINK → $REPO_DIR"

# 3. Create default config if not exists
if [ ! -f "$PLUGIN_DIR/config.yaml" ]; then
    mkdir -p "$PLUGIN_DIR"
    cat > "$PLUGIN_DIR/config.yaml" << 'YAML'
rules:
  popup: []
  popup_excluded: []
  silent: []
  silent_excluded: []
templates:
  brief: |
    ## 消息简报
    POPUP ({POPUP_MESSAGE_COUNT}):
    {NEW_POPUP_MESSAGES}

    NEW ({MESSAGE_COUNT}):
    {NEW_MESSAGES}

    SILENT ({SILENT_MESSAGE_COUNT}):
    {NEW_SILENT_MESSAGES}

    !{date "+%Y-%m-%d %H:%M:%S"}
  item: |
    [{MESSAGE_TYPE}] {MESSAGE_TITLE}: {MESSAGE_CONTENT_CUTTED}
YAML
    echo "    Default config created: $PLUGIN_DIR/config.yaml"
fi

echo ""
echo "==> Done!"
echo "    Restart Claude Code, then use /message-box:start_msg_box to activate"
echo "    Send messages:  msgbox send --type alert --title ... --content ..."
