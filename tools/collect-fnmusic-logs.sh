#!/usr/bin/env bash
# fnmusic-ext 一键收集诊断日志（在 NAS 上以 root/sudo 执行）
# 用法：sudo bash collect-fnmusic-logs.sh
# 产出：/tmp/fnmusic-diag-<时间>.tar.gz
set -u

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/fnmusic-diag-${STAMP}"
mkdir -p "$OUT"

# 1) 音源容器全量日志（supervisor 把各服务输出都重定向到容器 stdout）
docker logs --since 2h fnmusic-sources > "$OUT/container.log" 2>&1

# 2) 宿主网关（socket 接管代理）日志
journalctl -u fnmusic-ext.service --since "2 hours ago" --no-pager > "$OUT/proxy.log" 2>&1

# 3) 服务状态 / 版本
docker exec fnmusic-sources supervisorctl status > "$OUT/supervisor.txt" 2>&1
appcenter-cli list 2>/dev/null | grep -i fnmusic > "$OUT/appcenter.txt" 2>&1
APP_DIR="$(ls -d /vol*/@appcenter/fnmusic-ext/repo 2>/dev/null | head -1)"
if [ -n "${APP_DIR}" ] && [ -f "${APP_DIR}/VERSION" ]; then
    cat "${APP_DIR}/VERSION" > "$OUT/version.txt"
fi
docker ps --filter ancestor=fnmusic-sources --format '{{.Names}} {{.Status}}' > "$OUT/containers.txt" 2>&1

# 4) 健康检查（网关 + 管理台）
curl -s -m 5 http://127.0.0.1:8776/health > "$OUT/health-proxy.json" 2>&1
curl -s -m 5 http://127.0.0.1:8774/api/status > "$OUT/health-webui.json" 2>&1

# 5) 环境（脱敏：只留键名，不打印值）
if [ -n "${APP_DIR}" ] && [ -f "${APP_DIR}/.env" ]; then
    cut -d= -f1 "${APP_DIR}/.env" | grep -v '^\s*$' > "$OUT/env-keys.txt" 2>&1
fi

tar -czf "${OUT}.tar.gz" -C /tmp "$(basename "$OUT")"
echo "已生成：${OUT}.tar.gz"
ls -lh "${OUT}.tar.gz"
