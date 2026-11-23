"""「收藏这首歌」回归测试（2026-11-23 用户反馈）。

现象：点「陶喆的就是爱你」播放正常，但说「收藏这首歌」时小爱报
「汪苏泷的分手季节已在收藏中」。

根因（NAS 日志实录）：上游在随机列表播完一轮时会**后台预缓存下一首**，
预缓存同样请求桥接 /play 取流；/play 原先无条件把每次取流记为"当前播放"，
于是 last_played_guid 被下一首覆盖（点歌 30 秒后即发生）。fav_current
只读 last_played_guid，收藏到的就是下一首。

修复：
  ① 收藏口令改从设备上报的 cur_music 反查桥接 URL 拿 guid（native/now_playing.py）；
  ② app.py 的 /play 不再把容器内部的预缓存请求记为当前播放。
端到端（真实设备/NAS）验证在部署脚本里跑；这里锁定解析逻辑与接线。
"""
from pathlib import Path

from native.now_playing import clean_display_name, resolve_current_guid

HERE = Path(__file__).resolve().parent
CORE_PY = (HERE / "native" / "core.py").read_text(encoding="utf-8")
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")


class _Dev:
    def __init__(self, cur_music):
        self.device = type("Info", (), {"cur_music": cur_music})()


# ---------------- 当前曲目解析 ----------------

def test_resolve_prefers_device_song_over_precache():
    """设备当前曲目优先：last_played 被下一首预缓存污染也不影响。"""
    name = "陶喆 - [kuwo]就是爱你"
    guid, title = resolve_current_guid(
        {"d1": _Dev(name)},
        {name: "http://192.168.x.x:8775/play?guid=tao123"},
        last_guid="wang456",              # 预缓存把下一首写进了 last_played
        last_title="汪苏泷 - 分手季节",
        did="d1",
    )
    assert guid == "tao123"
    assert title == name


def test_resolve_falls_back_to_last_played():
    """播的是下载到本地的 mp3（all_music 是文件路径）→ 回退 last_played（在线点歌）。"""
    name = "李荣浩 - 乌梅子酱"
    guid, _ = resolve_current_guid(
        {"d1": _Dev(name)},
        {name: "/data/xiaomusic/music/李荣浩 - 乌梅子酱.mp3"},
        last_guid="online:kuwo:228908",
        last_title=name,
        did="d1",
    )
    assert guid == "online:kuwo:228908"


def test_resolve_without_device_falls_back():
    """设备未知/未开播：回退 last_played。"""
    guid, title = resolve_current_guid({}, {}, last_guid="g1", last_title="某歌", did="d1")
    assert (guid, title) == ("g1", "某歌")


def test_resolve_url_encoded_guid():
    """guid 含 URL 编码时解码（在线源 guid 可能带特殊字符）。"""
    name = "某歌"
    guid, _ = resolve_current_guid(
        {"d1": _Dev(name)},
        {name: "http://nas:8775/play?guid=online%3Akuwo%3A228908"},
        did="d1",
    )
    assert guid == "online:kuwo:228908"


def test_clean_display_name_strips_source_tag():
    assert clean_display_name("陶喆 - [kuwo]就是爱你") == "陶喆 - 就是爱你"
    assert clean_display_name("[netease]晴天") == "晴天"
    assert clean_display_name("") == ""


# ---------------- 接线锁定 ----------------

def test_fav_current_uses_current_play_guid():
    """收藏口令必须走当前曲目解析，不得直接读 last_played_guid。"""
    body = CORE_PY[CORE_PY.index("async def fav_current("):]
    body = body[:body.index("\n    async def ", 10)]
    assert "self.current_play_guid(did)" in body
    assert 'getattr(self, "last_played_guid"' not in body
    assert "clean_display_name(" in body


def test_play_endpoint_ignores_internal_precache():
    """/play 只在非内部来源（真实音箱）时更新 last_played。"""
    body = APP_PY[APP_PY.index('@app.get("/play")'):]
    body = body[:body.index('@app.get("/playlist.json")')]
    assert "client_host" in body
    assert "GATEWAY_IP" in body
    assert "不记为当前播放" in body
    # 记录动作必须在 else 分支里，不能在无条件路径上
    assert body.index("else:") < body.index("XM.last_played_guid = guid")


def test_tts_prefers_native_channel():
    """TTS 必须优先走原生语音通道，避免 edge-tts 打断播放队列。

    2026-11-23 三次实测都不理想（edge-tts 走 play_by_music_url 的
    REPLACE_ALL）：停音乐 / TTS 单曲循环 / 恢复但从头播。原生 TTS
    （miio 命令、mina text_to_speech）不占播放队列，音乐自然续播。
    """
    body = APP_PY[APP_PY.index("def patch_tts_timer("):]
    body = body[:body.index('log.info("[tts] 原生播报优先已生效')]
    assert "_native_tts(" in body
    assert "TTS_COMMAND" in body, "支持 miio 命令的硬件走本地播报"
    assert "miio_command" in body
    assert "_TTS_ALIAS" in body, "X4B 需要映射到 ASX4B 的命令族"
    # 不得再盲信 mina：2026-11-23 实测 code=0 但设备无声
    assert "mina_service.text_to_speech" not in body
    assert "XiaoMusicDevice.text_to_speech = patched_text_to_speech" in body
    # 原生失败仍要有 edge-tts 回退 + 恢复播放兜底
    assert "_text_to_speech_edge_tts" in body
    assert "_tts_resume_music" in body
    assert "await self._play(resume)" in body
    # 必须在启动流程里注册（调用点在函数定义之前）
    assert "patch_tts_timer()" in APP_PY[:APP_PY.index("def patch_tts_timer(")]


def test_fav_current_confirms_before_claiming_success():
    """收藏后必须回查官方列表才报"已收藏"，且不得伪造本地快照。

    2026-11-23 用户反馈：提示已收藏，但 App 收藏列表里没有。旧实现
    add_favorite 后往 bridge.favorites 塞了一条只有 guid 的假记录，导致
    后续口令永远命中"已在收藏中"，再也不重试。
    """
    body = CORE_PY[CORE_PY.index("async def fav_current("):]
    body = body[:body.index("async def play_music_list_index(")]
    assert "_confirm_favorite(" in body
    assert '+ [{"guid": guid}]' not in body, "不得再伪造收藏快照"
    # "已收藏" 只在回查命中后播报
    assert body.index("if await self._confirm_favorite(") < body.index("好的，已收藏{label}")


def test_fav_current_always_calls_add_favorite():
    """不得再用"已收藏"状态提前返回。

    2026-11-23 实测：官方接口 isFavorite=true，App 却显示未收藏，用户
    被"已经在收藏里了"卡住。add_favorite 幂等（已收藏再调返回成功、
    数量不变），所以统一执行收藏再回查。
    """
    body = CORE_PY[CORE_PY.index("async def fav_current("):]
    body = body[:body.index("async def play_music_list_index(")]
    assert "if guid in favs" not in body, "不得用收藏快照提前返回"
    assert "已经在收藏里啦" not in body, "不再播报与 App 认知冲突的提示"
    # add_favorite 必须在回查之前无条件执行
    assert body.index("await fn.add_favorite(guid)") < body.index("_confirm_favorite(")
