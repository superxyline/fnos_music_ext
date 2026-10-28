"""native 命令分发（移植自 xiaomusic.command_handler，只服务桥接配置的关键词）。

匹配策略与上游 do_check_cmd/match_cmd 一致：
1. 停止口令优先：口语/规范停止词一律走 stop（含「停止播放X」这类被
   「播放」模糊匹配抢走的Query）；「N分钟后关机」除外（那是定时关机）
2. 完全匹配：query 恰为关键词 → 对应动作
3. 模糊匹配：query 含关键词 → 取关键词后的文本为参数（个别关键词取前）
4. 激活口令过滤：未在播放且配置了激活口令时，只放行激活口令
5. 停止兜底：没匹配上任何指令但明显是"别放了"的口语（小爱自带技能接走的
   那种），取消下一曲定时器并置 is_playing=False——否则音箱被小爱停了，
   桥接的定时器到点又会续播一首（用户反馈"停一会又播"的根因）
6. 执行前先停小爱自带技能应答，避免"不支持"提示音抢占
"""
import re

try:
    from xiaomusic.config import KEY_WORD_ARG_BEFORE_DICT
    from xiaomusic.utils.text_utils import chinese_to_number
except ImportError:  # 无 xiaomusic 环境（本地测试）：字面量兜底，与上游一致
    KEY_WORD_ARG_BEFORE_DICT = {"分钟后关机": True}
    chinese_to_number = None

from .keywords import (
    KEYWORDS_STOP_DEFAULT,
    STOP_HINT_RE,
    KEYWORDS_FAV_SEQ_DEFAULT,
    FAV_SEQ_HINT_RE,
    SLEEP_TIMER_HINT_RE,
    SLEEP_CANCEL_RE,
    FAV_CURRENT_HINT_RE,
    SINGER_FAV_PREFIXES,
    SINGER_FAV_PATTERNS,
    SINGER_FAV_STOPWORDS,
    SINGER_FAV_STOPWORD_PARTS,
)

# 点歌意图词：Query 含这些词说明"要放歌"。与停止口令冲突时按位置判：
# 「停止播放音乐」停止词在前 → 停止；「播放安静」点歌词在前 → 点歌
# （安静是歌名）。没有点歌词时停止词直接生效（「别放了」）。
_PLAY_INTENT_RE = re.compile(r"播放|放首|放几首|来几首|来首|随便放|我想听|想听")

# 定时关机（N分钟后关机）含"关机"，不能被停止口令抢走
_TIMED_STOP_RE = re.compile(r"分钟.*关机|关机.*分钟")

# 睡眠定时口令的中文数字（阿拉伯数字直接 int）
_SLEEP_NUM_RE = r"[0-9零一二三四五六七八九十百两]+"

# 「N分钟后关机」保持上游 stop_after_minute 行为，睡眠定时口令不接
def _sleep_timer_minutes(query) -> "int | None":
    """解析睡眠定时时长（分钟）；非睡眠定时口令返回 None。

    「30分钟后关机」走上游定时关机，这里让路；「取消定时」由调用方先判。
    """
    if _TIMED_STOP_RE.search(query):
        return None
    if not SLEEP_TIMER_HINT_RE.search(query):
        return None
    m = re.search(rf"({_SLEEP_NUM_RE})\s*个半小时", query)
    if m:
        n = _sleep_num(m.group(1))
        return n * 60 + 30 if n else None
    m = re.search(rf"({_SLEEP_NUM_RE})\s*个?小时", query)
    if m:
        n = _sleep_num(m.group(1))
        return n * 60 if n else None
    m = re.search(r"半小时", query)
    if m:
        return 30
    m = re.search(rf"({_SLEEP_NUM_RE})\s*分钟?", query)
    if m:
        n = _sleep_num(m.group(1))
        return n if n else None
    return None


def _sleep_num(text) -> "int | None":
    """时长里的数字：阿拉伯直接转；中文优先上游 chinese_to_number，
    无上游环境（本地测试/静态检查）时用够用的本地解析（十位以内组合）。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if chinese_to_number is not None:
        try:
            return int(chinese_to_number(text))
        except Exception:  # noqa: BLE001
            pass
    text = text.replace("两", "二")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = digits.get(head, 1) if head else 1
        ones = digits.get(tail, 0) if tail else 0
        if tens and (not tail or tail in digits):
            return tens * 10 + ones
        return None
    return digits.get(text)


class CommandDispatcher:
    def __init__(self, core):
        self.core = core
        self.config = core.config
        self.log = core.log
        # 停止口令集合（来自单一来源 keywords.py；setting.json 可覆盖）
        self.stop_keywords = [
            k.strip() for k in (KEYWORDS_STOP_DEFAULT or "").split(",") if k.strip()
        ]
        # 顺序播放本地收藏口令（必须先于上游默认词「顺序播放」判定）
        self.fav_seq_keywords = [
            k.strip() for k in (KEYWORDS_FAV_SEQ_DEFAULT or "").split(",") if k.strip()
        ]

    def _fav_seq_intent(self, query) -> bool:
        """Query 是否是「顺序播放本地歌曲」类口令（位置无关，整句判定）。

        上游默认词「顺序播放」（set_play_type_seq）在 key_match_order 默认表
        里排在前，模糊匹配会把「顺序播放本地歌曲」整句抢成模式切换、
        arg=本地歌曲——音箱什么都不播。这些口令必须在模糊匹配前显式优先。
        """
        return any(kw in query for kw in self.fav_seq_keywords) or bool(
            FAV_SEQ_HINT_RE.search(query))

    def _singer_fav_intent(self, query) -> str:
        """「播放周杰伦的本地收藏歌曲」类口令 → 歌手名；不是这类口令返回空串。

        只认带「本地/收藏」限定的说法（避免改变「播放周杰伦的歌」的上游点歌
        语义）；泛指词（全部/所有/我的…）不当歌手。口令前缀「帮我/请/给我」
        先削掉，ASR 常见的客气话不影响匹配。
        """
        q = (query or "").strip()
        for prefix in SINGER_FAV_PREFIXES:
            if q.startswith(prefix):
                q = q[len(prefix):].strip()
        if not q:
            return ""
        for pattern in SINGER_FAV_PATTERNS:
            m = pattern.match(q)
            if not m:
                continue
            singer = (m.group("singer") or "").strip(" 的，,。.!！")
            if not singer or singer in SINGER_FAV_STOPWORDS:
                continue
            if any(part in singer for part in SINGER_FAV_STOPWORD_PARTS):
                continue
            return singer
        return ""

    def _stop_intent(self, query) -> bool:
        """Query 是否是停止意图（位置感知：停止词必须早于点歌词）。"""
        if _TIMED_STOP_RE.search(query):
            return False  # 「30分钟后关机」是定时关机
        stop_kw = next((kw for kw in self.stop_keywords if kw in query), None)
        if stop_kw is None:
            return False
        play_m = _PLAY_INTENT_RE.search(query)
        return play_m is None or query.index(stop_kw) < play_m.start()

    async def dispatch(self, did, query, ctrl_panel=True):
        device = self.core.device_manager.devices.get(did)
        if device is None:
            self.log.warning("[native] 设备 %s 不在列表，忽略口令 %r", did, query)
            return
        opvalue, oparg = self.match_cmd(device, query, ctrl_panel)
        if not opvalue:
            # 停止兜底：口语停止被小爱自带技能接走（桥接没匹配上任何指令）时，
            # 音箱已停但桥接 is_playing 仍为 True、下一曲定时器仍 armed，
            # 到点 _play_next 自动续播——必须在这里拆掉定时器。
            if await self._stop_fallback(device, query):
                return
            self.log.info("[native] 未匹配到指令: %r", query)
            return
        # 执行前先停小爱，避免播放"不支持"提示
        try:
            await device.group_force_stop_xiaoai()
        except Exception as e:  # noqa: BLE001
            self.log.debug("[native] group_force_stop_xiaoai 失败: %s", e)
        func = getattr(self.core, opvalue, None)
        if func is None:
            self.log.warning("[native] 指令 %s 在 native 引擎无对应实现", opvalue)
            return
        self.log.info(
            "[native] 执行指令 %s arg=%r did=%s", opvalue, oparg, did
        )
        await func(did=did, arg1=oparg)

    async def _stop_fallback(self, device, query) -> bool:
        """未匹配指令的停止兜底。返回 True 表示已按停止处理（调用方应 return）。"""
        if not getattr(device, "is_playing", False):
            return False
        if _PLAY_INTENT_RE.search(query):
            return False  # 「放首别放过的歌」是点歌，不是停止
        if not STOP_HINT_RE.search(query):
            return False
        self.log.info("[native] 口语停止兜底生效（小爱已自行停止，拆桥接定时器）: %r", query)
        try:
            await device.cancel_group_next_timer()
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 取消下一曲定时器失败: %s", e)
        # 不发 TTS、不 force_stop：音箱已被小爱自己停下了；
        # is_playing=False 让 poller 的 reset_timer_when_answer 不再重新武装
        device.is_playing = False
        return True

    def match_cmd(self, device, query, ctrl_panel):
        """返回 (动作名, 参数)，未匹配返回 (None, None)。"""
        # -1) 睡眠定时：必须先于停止口令——「半小时后停止播放」含停止词，
        #     走到这里就立刻停了，定时无从谈起。取消口令先判（「取消定时」
        #     含「定时」二字，先到先得）。
        if SLEEP_CANCEL_RE.search(query):
            self.log.info(f"[native] 匹配到取消睡眠定时口令. query:{query}")
            return "cancel_sleep_timer", ""
        minutes = _sleep_timer_minutes(query)
        if minutes is not None:
            self.log.info(f"[native] 匹配到睡眠定时口令 {minutes}min. query:{query}")
            return "sleep_timer", minutes

        # 0) 停止口令优先：必须先于点歌模糊匹配，否则「停止播放音乐」会先被
        #    「播放」抢走变成点歌（arg=音乐）。「N分钟后关机」是定时关机除外。
        if self._stop_intent(query):
            self.log.info(f"[native] 匹配到停止口令. query:{query}")
            return "stop", ""

        # 0.5) 顺序播放本地收藏：先于上游默认词「顺序播放」（模式切换）与
        #      一切模糊匹配，否则整句被 set_play_type_seq 抢走、什么都不播
        if self._fav_seq_intent(query):
            self.log.info(f"[native] 匹配到顺序播放本地收藏口令. query:{query}")
            return "play_favorites_seq", ""

        # 0.55) 按歌手播本地收藏：先于「播放」模糊匹配——否则整句被上游点歌
        #       抢走（arg=周杰伦的本地收藏歌曲，去搜一个不存在的歌名，什么都不播）
        singer = self._singer_fav_intent(query)
        if singer:
            self.log.info(f"[native] 匹配到按歌手播本地收藏口令 歌手={singer!r}. query:{query}")
            return "play_singer_favorites", singer

        # 0.6) 收藏当前播放歌曲：显式优先（正则强制带「这首/当前/正在」限定，
        #      与「播放收藏」歌单口令天然不冲突）
        if FAV_CURRENT_HINT_RE.search(query):
            self.log.info(f"[native] 匹配到收藏当前歌曲口令. query:{query}")
            return "fav_current", ""

        # 1) 多匹配待选态：「第几个」类回复优先（移植自 command_handler.match_cmd）
        if getattr(device, "_pending_selection", None):
            matcharg = re.match(
                r"^第?([零一二三四五六七八九十百千万亿]+)[个首条集]$", query
            )
            if matcharg:
                self.log.info(f"[native] 匹配到选择指令. query:{query}")
                return "select_index", query

        # 1) 完全匹配
        opvalue = self.check_full_match_cmd(device, query, ctrl_panel)
        if opvalue:
            return opvalue, ""
        # 2) 按优先级模糊匹配
        for opkey in self.config.key_match_order:
            matcharg = re.match(rf"(.*){opkey}(.*)", query)
            if not matcharg:
                continue
            argpre, argafter = matcharg.groups()
            oparg = argafter
            if opkey in KEY_WORD_ARG_BEFORE_DICT:
                oparg = argpre
            opvalue = self.config.key_word_dict.get(opkey)
            if not opvalue:
                continue
            active_cmd_arr = self.config.get_active_cmd_arr()
            if (
                not ctrl_panel
                and not device.is_playing
                and active_cmd_arr
                and opvalue not in active_cmd_arr
                and opkey not in active_cmd_arr
            ):
                continue
            return opvalue, oparg
        return None, None

    def check_full_match_cmd(self, device, query, ctrl_panel):
        if query not in self.config.key_match_order:
            return None
        active_cmd_arr = self.config.get_active_cmd_arr()
        opvalue = self.config.key_word_dict.get(query)
        if (
            ctrl_panel
            or device.is_playing
            or not active_cmd_arr
            or opvalue in active_cmd_arr
        ):
            return opvalue
        return None
