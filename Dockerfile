FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-yaml \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code --no-audit --no-fund

WORKDIR /workspace

COPY . /opt/claude-message-box

RUN pip install -e /opt/claude-message-box --break-system-packages --quiet \
    && pip install mcp --break-system-packages --quiet \
    && mkdir -p /root/.claude/skills \
    && ln -sfn /opt/claude-message-box /root/.claude/skills/message-box \
    && mkdir -p /root/.claude/plugins/claude-message-box

# Register msgbox MCP Channel server in the user-level Claude config
RUN python3 -c "import json, pathlib; cfg = json.loads(pathlib.Path('/root/.claude.json').read_text()) if pathlib.Path('/root/.claude.json').exists() else {}; cfg.setdefault('mcpServers', {})['msgbox'] = {'type': 'stdio', 'command': 'msgbox', 'args': ['channel']}; pathlib.Path('/root/.claude.json').write_text(json.dumps(cfg, indent=2))"

ENV CLAUDE_CODE_SKIP_PROMPTS=1

CMD ["bash"]
