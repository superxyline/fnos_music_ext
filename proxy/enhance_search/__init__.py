#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enhance_search - 飞牛音乐多平台搜索/歌词/封面内核。

整体取自 kuilei0926/FnMusicEnhance（app/server/search/，纯标准库实现，
平台代码上游可溯至 musicdl 与 Lyrico），供歌曲匹配页（match_core）接入
QQ/酷狗音源。上游为 aggregate/sources 的服务端接口层本仓库未使用，
仅保留以减少与上游的diff。
"""

from . import sources
from . import platforms  # noqa: F401  导入即填充 SOURCE_REGISTRY 的 impl
from . import aggregate  # noqa: F401
