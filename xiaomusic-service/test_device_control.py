"""设备控制（管理台「设备控制」卡片）的结构性回归测试（不做网络/依赖导入）。

背景（2026-11-28）：小爱音箱页支持对单台已接管设备直接下发指令或暂停/继续。
关键设计决策一旦被改回去会退化，必须锁住：
  - 暂停/继续必须走 mina player_pause/player_play（可续播），不能换成语音口令
    的「暂停」——那映射到 stop，会清空播放不留进度；
  - 文本口令必须先本地预判（dispatch 恒返回 None，不预判就无法提示"未识别"）；
  - 识别成功后异步执行（点歌含搜索/下载，同步等待会拖死请求）；
  - 设备快照的 did 必须从 Device 对象取（不信任 dict key）。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")
WEBUI_APP = (HERE.parent / "webui-service" / "app.py").read_text(encoding="utf-8")
WEBUI_JS = (HERE.parent / "webui-service" / "static" / "app.js").read_text(encoding="utf-8")
WEBUI_HTML = (HERE.parent / "webui-service" / "static" / "index.html").read_text(encoding="utf-8")


def _section(src, start_marker, end_marker):
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def test_devices_state_reads_did_from_device_object():
    body = _section(APP_PY, 'async def xm_devices_state(', '# ---------------- 状态页与 UI')
    # did 从 Device 对象取，不信任 devices dict 的 key（与 _now_playing 同模式）
    assert 'getattr(info, "did", "")' in body
    assert "is_playing" in body and "cur_music" in body
    # 登录态一起暴露，前端据此提示
    assert "mi_logged" in body


def test_device_cmd_pause_play_uses_mina_not_command_stop():
    body = _section(APP_PY, 'async def xm_device_cmd(', '# ---------------- 状态页与 UI')
    assert 'action not in ("pause", "play")' in body
    # 直接 mina 通道（player_pause/player_play，可续播、保留进度）
    assert "player_pause(device_id)" in body
    assert "player_play(device_id)" in body
    # device_id 从 DevicePlayer（device.device_id，UUID）取，不是数字 did
    assert 'getattr(device, "device_id", None)' in body
    # 登录未就绪必须明确报错而不是静默
    assert "mina is None" in body
    # 状态同步（2026-11-28 实测缺它按钮永远「暂停」）：轮询链路感知不到 ubus
    # 暂停，必须手动翻 is_playing；暂停拆定时器防到点跳歌；继续按剩余时长
    # 重建定时器（暂停时长补进上游 _paused_time，offset 计算才正确）
    assert "device.is_playing = False" in body
    assert "cancel_group_next_timer" in body
    assert "device.is_playing = True" in body
    assert "_paused_time" in body and "_fn_paused_at" in body
    assert "set_next_music_timeout" in body
    assert "get_offset_duration" in body
    # 同设备互斥 + 去抖（2026-11-28 频繁连点换歌）：pause/play 并发会乱序打给
    # 小米；400ms 内同动作重复直接吞掉（不同动作不受影响）
    assert "_CTRL_LOCKS.setdefault(did, asyncio.Lock())" in body
    assert "_CTRL_LAST" in body and "< 0.4" in body


def test_next_timer_lost_reference_race_patched():
    """失引用旧定时器到期误触发 _play_next 是频繁暂停/继续换歌的根因。

    上游 _do_next 到期读 self._next_timer 当"当前定时器"——被后续 set/cancel
    覆盖的旧 task 到期会把新 timer 误当自己的，cancel 后强行 _play_next。
    patch 后必须按 task 自身引用校验，被替代的旧 task 静默退出。
    """
    assert "def patch_next_timer(" in APP_PY
    # 必须在 lifespan 内被调用（只有定义没有注册等于没修）
    start = APP_PY.index("async def lifespan(")
    end = APP_PY.index("app.router.lifespan_context", start)
    assert "patch_next_timer()" in APP_PY[start:end]
    body = APP_PY[APP_PY.index("async def patched_set(self, sec):"):
                  APP_PY.index("def patch_audio_id_resolver(")]
    assert "self._next_timer is not this_task" in body
    assert "asyncio.current_task()" in body
    assert "静默退出" in body
    assert "_play_next()" in body  # 正主到期仍要换歌（语义不变）
    assert 'PLAY_TYPE_SIN' in body  # 单曲模式到点停止语义保留


def test_device_cmd_query_is_prechecked_and_async():
    body = _section(APP_PY, 'async def xm_device_cmd(', '# ---------------- 状态页与 UI')
    # 预判用与 dispatch 同一匹配器；未识别返回错误而不是静默吞掉
    assert "XM.dispatcher.match_cmd(device, query, True)" in body
    assert "未识别的口令" in body
    # 口语停止兜底也要认（「别放了」等不在词表但语义是停止）
    assert "_stop_fallback(device, query)" in body
    # 识别成功异步执行立即返回（点歌含搜索/下载，同步等待会拖死请求）
    assert "asyncio.create_task(XM.do_check_cmd(" in body


def test_webui_proxies_device_cmd_and_status_merges_snapshot():
    # status 合并设备快照（复用已有 5s 轮询，前端零新增请求负担）
    status = _section(WEBUI_APP, 'async def api_xiaomusic_status(', '@app.post("/api/xiaomusic/sync")')
    assert '/api/xm/devices_state' in status
    assert 'health["devices_state"] = dev' in status
    # device_cmd 转发存在
    assert '"/api/xm/device_cmd"' in WEBUI_APP


def test_webui_control_card_wired():
    js = WEBUI_JS
    assert 'id="xm-ctrl-list"' in js
    assert "function xmCtrlRender(" in js
    # 输入框内容保护：设备集合变化才重建行
    assert "keys !== xmCtrlKeys" in js
    # 暂停/继续按钮按 is_playing 切换，走 action 而非文本口令
    assert "action: playing ? \"pause\" : \"play\"" in js
    assert "/api/xiaomusic/device_cmd" in js
    # devices_state 是 {ok, mi_logged, devices} 对象（2026-11-28 修复：不能直接
    # 当数组传），必须先取 .devices
    assert "xmDs.devices" in js
    # 空态哨兵：初始 xmCtrlKeys 是 ""，若拿它当判据，空态永远写不进 DOM，
    # 卡片会一直停在「加载设备状态…」
    assert '"__empty__"' in js


def test_cache_version_bumped():
    # 改了 app.js/style.css 必须同步提高 ?v=（HANDOFF §3.5，3.8.3 缓存教训）
    assert "app.js?v=3.8.5" in WEBUI_HTML
    assert "style.css?v=3.8.5" in WEBUI_HTML


def test_play_transcode_for_unsupported_formats():
    """FLAC 直推不支持解码的音箱（实测 L05B「播放中但静音」）必须转码。

    transcode_mode=auto（默认）时用容器 ffmpeg 把 FLAC 流式转 mp3 320k；
    转码流不支持 Range（从头转），探测流不带 Range 开启。
    """
    body = _section(APP_PY, '@app.get("/play")', "def _needs_transcode(")
    # 探测不带 Range，直通分支按需重开（保证转码从头、直通保留 Range 语义）
    assert "open_stream(guid, range_header=None)" in body
    assert "if range_header:" in body and "open_stream(guid, range_header=range_header)" in body
    tr = APP_PY[APP_PY.index("TRANSCODE_TYPES = "):]
    assert 'audio/flac' in tr
    assert '"off"' in tr and '"never"' in tr  # 可关闭
    assert "libmp3lame" in tr and "-b:a\", \"320k" in tr.replace('", "', '", "')
    assert "audio/mpeg" in tr
    # ffmpeg 启动失败退回直通，不能 500
    assert "ffmpeg 不可用，退回直通" in tr
    # 子进程清理：客户端断开/结束时 kill + 关上游流
    assert "proc.kill()" in tr and "resp.aclose()" in tr


def test_devices_state_exposes_source_label():
    """设备行要带来源标注（2026-11-28 卡片融合，原「正在播放」卡片已删）。"""
    body = _section(APP_PY, 'async def xm_devices_state(', '# ---------------- 状态页与 UI')
    assert '"source_label"' in body
    # 判定链与 _now_playing 一致：cur_music → all_music URL guid → _guid_source
    assert "_guid_source(" in body
    assert "re.search(r\"[?&]guid=([^&]+)\", url" in body
