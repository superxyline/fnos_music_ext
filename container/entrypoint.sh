#!/bin/sh
# fnmusic-sources 容器入口：supervisor 起来后按 /repo/.env 只拉起所选音源（+WebUI）。
# 全部程序 autostart=false（supervisord.conf），实现按需加载：
# 未启用的音源进程不驻留内存；容器重启后本脚本按 .env 自愈恢复同样的进程集。
set -eu

SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

log() { echo "[entrypoint] $*"; }

export_source_env

supervisord -c "$SUP_CONF" &
SUP_PID=$!

# 等 supervisor 控制套接字就绪（最多 10s）。
# 用 pid 而非 status：status 的退出码反映“是否全部程序 RUNNING”，全 STOPPED
# （autostart=false 的初始态）也返回非 0，用它会白等满 10s 才超时跳出。
_i=0
until supervisorctl -c "$SUP_CONF" pid >/dev/null 2>&1; do
    _i=$((_i + 1))
    if [ "$_i" -ge 100 ]; then
        log "supervisor 套接字未就绪，继续等待（进程状态将由 healthcheck 反映）"
        break
    fi
    sleep 0.1
done

start_prog() {
    if supervisorctl -c "$SUP_CONF" start "$1" >/dev/null 2>&1; then
        log "started $1"
    else
        log "WARN: start $1 失败（supervisorctl status 查看原因）"
    fi
}

# 三选一音源：只启动 .env 选中的那个（多开视为异常，全部忽略只取第一个命中）
if [ "$(env_flag FNMUSIC_MUSICDL_ENABLED)" = "true" ]; then
    start_prog musicdl
elif [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" = "true" ]; then
    start_prog musicbox
elif [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    start_prog lxmusic
else
    log "未配置任何音源（FNMUSIC_MUSICDL_ENABLED/FNMUSIC_NETEASE_ENABLED/FNMUSIC_LX_ENABLED 均未启用）"
fi

# 网易云歌单常驻（issue #38）：无论音源选谁，musicbox 常驻提供网易登录态/
# 歌单/网易曲目取流（不参与搜索）。生效前提＝账号歌单开关已开，且显式常驻
# 开关未配置（老配置沿用「账号歌单即常驻」）或为 true；显式 false 时不常驻。
# 盘上有网易 cookie（登录启发）才拉起；权威校验由 webui 开机对账完成
# （原生形态无 XDG 环境变量可能找不到 cookie，由 webui 兜底）。
musicbox_cookie_present() {
    for _f in "${XDG_DATA_HOME:-/nonexistent}/netease-musicbox/cookie.txt" \
              "${HOME:-/root}/.netease-musicbox/cookie.txt" \
              "${XDG_CONFIG_HOME:-/nonexistent}/netease-musicbox/cookie.txt"; do
        if [ -s "$_f" ]; then
            return 0
        fi
    done
    return 1
}

resident_flag() {
    _rf="$(env_flag FNMUSIC_NETEASE_PLAYLIST_RESIDENT true)"
    case "$_rf" in
        true) return 0 ;;   # 显式开启（或未配置时的默认）
        *) return 1 ;;
    esac
}

# 公共歌单（issue #55）匿名可用：只要开了开关且选择里含 musicbox 歌单，就同样要拉起
public_playlists_needed() {
    [ "$(env_flag FNMUSIC_PUBLIC_PLAYLISTS_ENABLED)" = "true" ] || return 1
    _sel="$(printf '%s' "${FNMUSIC_PUBLIC_PLAYLISTS_SELECTION:-}" | tr -d ' \n')"
    case "$_sel" in
        "") return 0 ;;                       # 空选择＝默认榜单（proxy 侧同口径）
        *'"provider":"musicbox"'*) return 0 ;;
        *) return 1 ;;
    esac
}

if resident_flag && {
        [ "$(env_flag FNMUSIC_NETEASE_MY_PLAYLISTS)" = "true" ] || public_playlists_needed
    }; then
    _MB_STATE="$(supervisorctl -c "$SUP_CONF" status musicbox 2>/dev/null | awk '{print $2}')"
    # 账号歌单要盘上有 cookie；只开公共歌单时匿名即可，无需 cookie
    if [ "$_MB_STATE" != "RUNNING" ] && {
            [ "$(env_flag FNMUSIC_NETEASE_MY_PLAYLISTS)" != "true" ] || musicbox_cookie_present
        }; then
        start_prog musicbox
    fi
fi

# WebUI 默认随容器启动（--no-webui 安装会写 FNMUSIC_WEBUI_ENABLED=false）
if [ "$(env_flag FNMUSIC_WEBUI_ENABLED)" = "true" ]; then
    start_prog webui
fi

# 信号转发：docker stop → supervisorctl shutdown → 各程序优雅退出
trap 'supervisorctl -c "$SUP_CONF" shutdown >/dev/null 2>&1' TERM INT
log "fnmusic-sources ready (pid=$SUP_PID)"
wait "$SUP_PID"
