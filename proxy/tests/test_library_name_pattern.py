"""issue #47：曲库文件命名模板 FNMUSIC_LIBRARY_NAME_PATTERN。

默认值必须与历史行为（"歌手 - 歌名"）完全一致，否则升级后新落盘歌曲会
与老曲库形成重复条目；自定义模板只影响改后新落盘的歌曲。
"""
import pytest

from proxy.app import CONF, library_basename


@pytest.fixture(autouse=True)
def _default_pattern(monkeypatch):
    monkeypatch.setitem(CONF, "library_name_pattern", "{artist} - {title}")


def test_default_pattern_matches_legacy_behavior():
    assert library_basename("晴天", "周杰伦") == "周杰伦 - 晴天"
    assert library_basename("晴天", "") == "晴天"
    assert library_basename("晴天", "unknown") == "晴天"
    assert library_basename("晴天", "晴天") == "晴天"


def test_custom_title_with_source():
    CONF["library_name_pattern"] = "{title} [{source}]"
    assert library_basename("晴天", "周杰伦", "netease") == "晴天 [netease]"
    # 无来源时占位符退化为 online，不出空的 []
    assert library_basename("晴天", "周杰伦") == "晴天 [online]"


def test_missing_artist_drops_separator():
    CONF["library_name_pattern"] = "{artist} - {title}"
    assert library_basename("晴天", "") == "晴天"
    CONF["library_name_pattern"] = "{artist} / {title}"
    # 模板字面量里的路径分隔符只清洗，不产生子目录
    assert library_basename("晴天", "") == "晴天"


def test_path_separators_are_sanitized():
    CONF["library_name_pattern"] = "{artist}/{title}"
    assert library_basename("晴天", "周杰伦") == "周杰伦_晴天"
    CONF["library_name_pattern"] = "a:b\\c - {title}"
    assert library_basename("晴天", "周杰伦") == "a_b_c - 晴天"


def test_empty_pattern_falls_back_to_default():
    CONF["library_name_pattern"] = ""
    assert library_basename("晴天", "周杰伦") == "周杰伦 - 晴天"


def test_long_title_is_truncated():
    CONF["library_name_pattern"] = "{title}"
    assert len(library_basename("长" * 500, "")) <= 200
