"""Dockerfile 的 COPY 源路径必须能被 .dockerignore 放行（issue #10 回归）。

背景：3.8.3 的 Dockerfile 新加 `COPY proxy/fnlog.py proxy/version.py`，但
.dockerignore 只放行了 `!proxy/__init__.py` / `!proxy/env_merge.py`，两个新
文件被 `proxy/*` 挡在构建上下文外，`docker compose up --build` 直接报
`failed to calculate checksum ... "/proxy/version.py": not found`——在线包
与离线包（extend.sh 会再构建一次）全部装不上。

这里把「Dockerfile 里 COPY 了什么」和「.dockerignore 放行了什么」锁在一起：
以后再加文件却忘放行，测试直接失败。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "container" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"


def _matches(pattern: str, path: str) -> bool:
    """粗粒度模拟 Docker 的 pattern 匹配（够用于白名单场景）。"""
    pat = pattern.rstrip("/")
    if pat.endswith("/*"):
        base = pat[:-2]
        # 目录被排除后其子项同样不可用（Docker 不会递归重新包含）
        return path == base or path.startswith(base + "/")
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat + "/*")
    return path == pat or path.startswith(pat + "/")


def _included(path: str, lines: list[str]) -> bool:
    """按 .dockerignore 顺序求最终状态：后匹配覆盖前匹配。"""
    state = True
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:].strip() if neg else line
        if not pat:
            continue
        if _matches(pat, path):
            state = neg
    return state


def _dockerignore_lines() -> list[str]:
    return DOCKERIGNORE.read_text(encoding="utf-8").splitlines()


def _copy_sources() -> list[str]:
    """收集 Dockerfile 中 COPY 的源路径（去掉 --chown 之类的选项与目标目录）。"""
    sources: list[str] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if not re.match(r"\s*COPY\s", line):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        for src in tokens[:-1]:  # 最后一个是要拷贝到的目标
            if src.startswith("/"):
                continue
            sources.append(src.lstrip("./"))
    return sources


def test_every_copy_source_passes_dockerignore():
    lines = _dockerignore_lines()
    missing = [src for src in _copy_sources() if not _included(src, lines)]
    assert not missing, (
        "以下 Dockerfile COPY 源文件被 .dockerignore 排除，构建必失败：\n  "
        + "\n  ".join(sorted(set(missing)))
        + "\n请在 .dockerignore 追加对应 `!<path>` 放行"
    )


def test_guard_rejects_unlisted_proxy_file():
    """反向用例：没放行的 proxy 文件必须判为排除，避免上面的测试恒真。"""
    lines = _dockerignore_lines()
    assert not _included("proxy/some_new_module.py", lines)
    assert _included("proxy/fnlog.py", lines)
    assert _included("proxy/version.py", lines)


def test_offline_package_skips_online_build():
    """issue #10 第二点：离线包（内置镜像）不能再次触发联网构建。"""
    extend = (REPO / "extend.sh").read_text(encoding="utf-8")
    body = extend[extend.index("ensure_image_current()"):]
    body = body[: body.index("\n}") + 2]
    assert ".image.tar.gz" in body, "extend.sh 必须识别内置镜像归档"
    assert "--no-build" in body, "离线包应走 up -d --no-build"
    # 离线分支必须出现在 ensure_base_image 之前（否则先联网探测就挂了）
    assert body.index(".image.tar.gz") < body.index("ensure_base_image.sh")
