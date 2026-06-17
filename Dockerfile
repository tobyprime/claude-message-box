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
    && mkdir -p /root/.claude/skills \
    && ln -sfn /opt/claude-message-box /root/.claude/skills/message-box \
    && mkdir -p /root/.claude/plugins/claude-message-box

ENV CLAUDE_CODE_SKIP_PROMPTS=1

CMD ["bash"]
