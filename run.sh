#!/usr/bin/env bash
# 启动 agenthub 会话管理服务
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -u -m agenthub.server --host 0.0.0.0 --port "${PORT:-8710}" \
  --allow "${ALLOW:-192.0.2.134}" --terminal
