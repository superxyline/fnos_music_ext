#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 歌曲匹配网关（二开）：后台独立 HTTP 入口（:8776，供 WebUI 容器转发）。
# 与主代理同 cgroup，服务停止时随 KillMode=mixed 一并回收；崩溃不拖垮主代理。
if [ "${FNMUSIC_MATCH_ENABLED:-true}" = "true" ]; then
    pkill -f 'proxy/match_gateway.py' 2>/dev/null || true
    # 与主代理同用 .venv-proxy（fastapi/uvicorn 只装在 venv 里）
    nohup "${BASE_DIR}/.venv-proxy/bin/python" "${BASE_DIR}/proxy/match_gateway.py" \
        >> "${BASE_DIR}/match_gateway.log" 2>&1 &
fi
# Explicit interpreter works with a 0644 checkout. The Python supervisor preflights
# configuration/imports, stages its socket privately, then journals the takeover.
exec /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" run --base "${BASE_DIR}"
