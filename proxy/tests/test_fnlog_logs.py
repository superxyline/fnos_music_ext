"""运行日志落盘与下载（2026-11-25 用户需求）。

- 设置页「运行日志」开关（FNMUSIC_LOG_ENABLED）关闭时不写文件，只走 stdout
- logs 目录清单 / 单文件定位 / 打包下载，严格白名单防路径穿越
"""
import logging
import os
import sys
import time
import zipfile

import pytest

import proxy.fnlog as fnlog

# 文件 handler 走 os.O_NOFOLLOW/O_CLOEXEC（Linux 专属）：Windows 开发机只测纯逻辑，
# 真实写入在容器里验证（部署脚本会检查 logs/*.log 是否生成）。
LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="日志文件打开依赖 Linux O_NOFOLLOW")


def _use_tmp_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("FNMUSIC_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(fnlog, "_SET_UP", set())


@LINUX_ONLY
def test_setup_logging_writes_file(monkeypatch, tmp_path):
    _use_tmp_logs(monkeypatch, tmp_path)
    monkeypatch.setenv("FNMUSIC_LOG_ENABLED", "true")
    logger = fnlog.setup_logging("test.logger.a", "svc-a")
    logger.info("hello file log")
    for handler in logger.handlers:
        handler.flush()
    target = tmp_path / "svc-a.log"
    assert target.is_file()
    assert "hello file log" in target.read_text(encoding="utf-8")
    # 幂等：重复调用不重复挂 handler
    before = len(logger.handlers)
    fnlog.setup_logging("test.logger.a", "svc-a")
    assert len(logger.handlers) == before


@LINUX_ONLY
def test_logging_stops_when_disabled(monkeypatch, tmp_path):
    """运行中关掉开关：handler 仍在，但不再写入（文件为空/不存在）。"""
    _use_tmp_logs(monkeypatch, tmp_path)
    monkeypatch.setenv("FNMUSIC_LOG_ENABLED", "false")
    logger = fnlog.setup_logging("test.logger.b", "svc-b")
    logger.info("should not be written")
    for handler in logger.handlers:
        handler.flush()
    target = tmp_path / "svc-b.log"
    assert not target.exists() or target.read_text(encoding="utf-8") == ""
    assert fnlog.log_enabled() is False


def test_log_enabled_reads_env_file(monkeypatch, tmp_path):
    """没有环境变量时读 .env（其它容器进程也能"保存即生效"）。"""
    _use_tmp_logs(monkeypatch, tmp_path)
    monkeypatch.delenv("FNMUSIC_LOG_ENABLED", raising=False)
    monkeypatch.setattr(fnlog, "_ENABLED_CACHE", {"ts": 0.0, "value": True})
    (tmp_path.parent / ".env").write_text("FNMUSIC_LOG_ENABLED=false\n", encoding="utf-8")
    assert fnlog.log_enabled() is False
    (tmp_path.parent / ".env").write_text("FNMUSIC_LOG_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setattr(fnlog, "_ENABLED_CACHE", {"ts": 0.0, "value": False})
    assert fnlog.log_enabled() is True


def test_list_logs_and_resolve(monkeypatch, tmp_path):
    _use_tmp_logs(monkeypatch, tmp_path)
    (tmp_path / "proxy.log").write_text("a", encoding="utf-8")
    (tmp_path / "proxy.log.2026-11-25").write_text("b", encoding="utf-8")
    (tmp_path / "env_snapshot.txt").write_text("c", encoding="utf-8")
    (tmp_path / "secret.env").write_text("token=x", encoding="utf-8")

    names = {item["name"] for item in fnlog.list_logs()}
    assert names == {"proxy.log", "proxy.log.2026-11-25", "env_snapshot.txt"}

    assert fnlog.resolve_log_file("proxy.log").name == "proxy.log"
    assert fnlog.resolve_log_file("proxy.log.2026-11-25") is not None
    # 白名单：穿越、分隔符、非日志后缀一律拒绝
    for bad in ("../etc/passwd", "..%2Fproxy.log", "a/b.log", "secret.env", "", "proxy.log.bak"):
        assert fnlog.resolve_log_file(bad) is None, bad


def test_export_bundle_contains_logs(monkeypatch, tmp_path):
    _use_tmp_logs(monkeypatch, tmp_path)
    (tmp_path / "proxy.log").write_text("line1\n", encoding="utf-8")
    (tmp_path / "env_snapshot.txt").write_text("[env] x\n", encoding="utf-8")
    bundle = fnlog.export_bundle()
    assert bundle is not None and bundle.is_file()
    try:
        with zipfile.ZipFile(bundle) as zf:
            names = zf.namelist()
            assert "fnmusic-logs/proxy.log" in names
            assert "fnmusic-logs/env_snapshot.txt" in names
    finally:
        os.unlink(bundle)


def test_export_bundle_empty_returns_none(monkeypatch, tmp_path):
    _use_tmp_logs(monkeypatch, tmp_path)
    assert fnlog.export_bundle() is None


def test_purge_stale_removes_old(monkeypatch, tmp_path):
    _use_tmp_logs(monkeypatch, tmp_path)
    old = tmp_path / "old.log"
    old.write_text("x", encoding="utf-8")
    old_time = time.time() - 10 * 86400
    os.utime(old, (old_time, old_time))
    removed = fnlog.purge_stale(days=3)
    assert "old.log" in removed
    assert not old.exists()
