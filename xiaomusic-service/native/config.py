"""native 引擎的 Config 工厂。

与 xiaomusic 引擎（app.py create_xiaomusic）同源同参，差异只有：
- web_music_proxy=False：音箱直连桥接 /play 取流，不经 xiaomusic 代理（少一跳）
- 不设 port 相关监听（native 不挂 xiaomusic 的 api 子应用）

口令默认值来自 native/keywords.py（与 app.py push_playlist 共用，单一来源）。
"""
from pathlib import Path

from xiaomusic.config import Config

from .keywords import (
    KEYWORDS_PLAYLIST_DEFAULT,
    KEYWORDS_PLAYLOCAL_DEFAULT,
    USER_KEY_WORD_DICT_DEFAULT,
)


def build_config(
    *,
    conf_dir,
    music_dir,
    nas_ip,
    public_port,
    settings: dict | None = None,
) -> Config:
    """构造 native 引擎的 xiaomusic Config。

    settings 来自桥接 conf/setting.json（与 xiaomusic 引擎共用，切换不丢配置）：
    account / password / mi_did / music_list_url。
    """
    settings = settings or {}
    music_dir = Path(music_dir)
    return Config(
        conf_path=str(conf_dir),
        music_path=str(music_dir),
        temp_path=str(music_dir / "tmp"),
        download_path=str(music_dir / "download"),
        cache_dir=str(music_dir / "cache"),
        cache_max_size_mb=200,
        hostname=f"http://{nas_ip}",
        port=8090,                      # 不监听，仅为 Config 完整性
        public_port=public_port,
        ffmpeg_location="/usr/bin",     # fnmusic-ext 镜像 apt 安装的 ffmpeg
        account=settings.get("account", ""),
        password=settings.get("password", ""),
        mi_did=settings.get("mi_did", ""),
        enable_analytics=False,
        disable_download=True,          # 不需要下载功能（点歌走飞牛搜索+收藏）
        enable_file_watch=False,        # 音乐目录仅存点歌下载产物，无需监控
        web_music_proxy=False,          # 音箱直连桥接 /play，不经 xiaomusic 代理
        delay_sec=0,
        continue_play=True,
        get_duration_type="mutagen",    # 用 mutagen 算时长，免装 ffprobe
        # 指令拉取通道：小爱对话记录 API（LATEST_ASK_API，实测可用）。
        # 不要开 get_ask_by_mina——mina 的 ubus nlp_result_get 在本机固件走
        # ROM 代理模式，只返回 {code:0,data:{code:0}} 没有 info 字段，
        # conversation.py 每轮 KeyError，口令永远拿不到。
        get_ask_by_mina=False,
        # 必须显式开启指令轮询：默认 false 时 poll_latest_ask 首行空转，
        # 口令永远到不了（音箱自带音乐技能接走全部语音）。
        enable_pull_ask=True,
        # 口令变体（初始值；后续由桥接 sync 时 push_config 刷新）
        keywords_playlist=settings.get(
            "keywords_playlist", KEYWORDS_PLAYLIST_DEFAULT),
        keywords_playlocal=settings.get(
            "keywords_playlocal", KEYWORDS_PLAYLOCAL_DEFAULT),
        user_key_word_dict=settings.get(
            "user_key_word_dict", dict(USER_KEY_WORD_DICT_DEFAULT)),
        music_list_url=settings.get("music_list_url", ""),
        # 歌单内容：xiaomusic 引擎的 modifiysetting 把 music_list_json 写进
        # setting.json，MusicLibrary._append_music_list 消费它——native 直接读，
        # 启动即带歌单，不必等首次 sync
        music_list_json=settings.get("music_list_json", ""),
    )
