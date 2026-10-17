#!/usr/bin/env python3
"""修复 Windows 上打出的 fpk 权限位。

fnpack 在 Windows 上无法保留 Unix 权限位，打出来的 `cmd/*` 与仓库里的脚本
全是 0o666，而 fnOS 安装/启停时要直接执行它们（`cmd/install_init` 等），
权限不对会在应用中心安装阶段就失败。本工具重打包 fpk，把可执行脚本统一
置为 0o755，其余条目原样保留（含内层 app.tgz）。

用法：
    python tools/repack_fpk.py dist/fnmusic-ext-3.2.0.fpk            # 原地修复
    python tools/repack_fpk.py in.fpk out.fpk                        # 另存
"""
from __future__ import annotations

import gzip
import io
import os
import sys
import tarfile

# fnOS 会直接执行的外层脚本目录
_EXEC_PREFIXES = ("cmd/",)
# 内层 app.tgz 里需要可执行位的脚本（按后缀判断，外加带 shebang 的无后缀脚本）
_EXEC_SUFFIXES = (".sh",)


def _needs_exec(name: str, data: bytes | None, inner: bool) -> bool:
    if not inner:
        return name.startswith(_EXEC_PREFIXES) and not name.endswith("/")
    base = os.path.basename(name)
    if base.endswith(_EXEC_SUFFIXES):
        return True
    return bool(data and data[:2] == b"#!")


def _fix_member(member: tarfile.TarInfo, data: bytes | None, inner: bool) -> bytes | None:
    if member.isfile() and _needs_exec(member.name, data, inner):
        member.mode = 0o755
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    if data is not None:
        member.size = len(data)
    return data


def _repack_inner(raw: bytes) -> bytes:
    """处理内层 app.tgz：脚本 0755，其余保留。"""
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw))) as tin:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.GNU_FORMAT) as tout:
            for member in tin.getmembers():
                data = tin.extractfile(member).read() if member.isfile() else None
                data = _fix_member(member, data, inner=True)
                tout.addfile(member, io.BytesIO(data) if data is not None else None)
        return out.getvalue()


def repack(src: str, dst: str) -> tuple[int, int, int]:
    """返回 (外层修复数, 内层是否重打包, manifest 是否已转 LF)。"""
    outer_fixed = inner_fixed = manifest_fixed = 0
    tmp = f"{dst}.part"
    with tarfile.open(src, "r:gz") as tin, tarfile.open(
        tmp, "w:gz", format=tarfile.GNU_FORMAT
    ) as tout:
        for member in tin.getmembers():
            data = tin.extractfile(member).read() if member.isfile() else None
            if member.name == "app.tgz" and data:
                before = data
                data = _repack_inner(data)
                if data != before:
                    inner_fixed = 1
            elif member.name == "manifest" and data and b"\r\n" in data:
                # fnpack 在 Windows 上会按 CRLF 重新写出 manifest（字段值带 \r），
                # fnOS 安装器按行解析，统一回 LF 最稳。
                data = data.replace(b"\r\n", b"\n")
                manifest_fixed = 1
            if member.isfile():
                was_exec = bool((member.mode & 0o111))
                data = _fix_member(member, data, inner=False)
                if not was_exec and (member.mode & 0o111):
                    outer_fixed += 1
            tout.addfile(member, io.BytesIO(data) if data is not None else None)
    os.replace(tmp, dst)
    return outer_fixed, inner_fixed, manifest_fixed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    src = argv[1]
    dst = argv[2] if len(argv) > 2 else src
    outer, inner, manifest = repack(src, dst)
    print(
        f"[repack] {src} -> {dst}：外层修复 {outer} 个脚本，"
        f"内层 app.tgz {'已重打包' if inner else '无需改动'}，"
        f"manifest {'已转 LF' if manifest else '无需改动'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
