"""issue #49：流式媒体响应必须带 X-Accel-Buffering: no。

外网访问通常是「用户反代 → 飞牛 nginx → 扩展」两层。扩展下发的播放/HLS/封面
地址本就是同源相对路径，但 nginx 默认 proxy_buffering on 会把整段音频缓冲完
才向客户端出字节（表现为长时间转圈后失败）。这里锁死代理自己生成的每个流式
出口都带禁缓冲头，避免后续重构无声回退。
"""
import httpx
import pytest

from proxy.app import (
    _media_stream_headers,
    _range_kind,
    serve_file_with_range,
    stream_tee_response,
)


def test_media_stream_headers_defaults():
    headers = _media_stream_headers()
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Accept-Ranges"] == "bytes"


def test_media_stream_headers_keeps_caller_values():
    headers = _media_stream_headers({"Cache-Control": "no-store"})
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "range_header,expected",
    [
        (None, "none"),
        ("bytes=0-", "open"),
        ("bytes=-1024", "suffix"),
        ("bytes=100-200", "partial"),
        ("bytes=abc", "invalid"),
    ],
)
def test_range_kind(range_header, expected):
    assert _range_kind(range_header) == expected


@pytest.mark.anyio
async def test_serve_file_full_response_not_buffered(tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"0123456789")
    response = serve_file_with_range(str(path), None, "audio/mpeg")
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Accept-Ranges"] == "bytes"
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"0123456789"


@pytest.mark.anyio
async def test_serve_file_range_response_not_buffered(tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"0123456789")
    response = serve_file_with_range(str(path), "bytes=2-5", "audio/mpeg")
    assert response.status_code == 206
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Content-Range"] == "bytes 2-5/10"


def test_stream_tee_response_not_buffered():
    upstream = httpx.Response(
        200,
        headers={"content-type": "audio/mpeg", "content-length": "4"},
        content=b"data",
    )
    response = stream_tee_response(upstream, "online:kuwo:1", None)
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Accept-Ranges"] == "bytes"
