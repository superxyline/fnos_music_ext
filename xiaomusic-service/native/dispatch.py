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
except ImportError:  # 无 xiaomusic 环境（本地测试）：字面量兜底，与上游一致
    KEY_WORD_ARG_BEFORE_DICT = {"分钟后关机": True}

from .keywords import KEYWORDS_STOP_DEFAULT, STOP_HINT_RE

# 点歌意图词：Query 含这些词说明"要放歌"。与停止口令冲突时按位置判：
# 「停止播放音乐」停止词在前 → 停止；「播放安静」点歌词在前 → 点歌
# （安静是歌名）。没有点歌词时停止词直接生效（「别放了」）。
_PLAY_INTENT_RE = re.compile(r"播放|放首|放几首|来几首|来首|随便放|我想听|想听")

# 定时关机（N分钟后关机）含"关机"，不能被停止口令抢走
_TIMED_STOP_RE = re.compile(r"分钟.*关机|关机.*分钟")


class CommandDispatcher:
    def __init__(self, core):
        self.core = core
        self.config = core.config
        self.log = core.log
        # 停止口令集合（来自单一来源 keywords.py；setting.json 可覆盖）
        self.stop_keywords = [
            k.strip() for k in (KEYWORDS_STOP_DEFAULT or "").split(",") if k.strip()
        ]

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
        # 0) 停止口令优先：必须先于点歌模糊匹配，否则「停止播放音乐」会先被
        #    「播放」抢走变成点歌（arg=音乐）。「N分钟后关机」是定时关机除外。
        if self._stop_intent(query):
            self.log.info(f"[native] 匹配到停止口令. query:{query}")
            return "stop", ""

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
