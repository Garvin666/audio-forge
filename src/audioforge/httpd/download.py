"""``GET /api/file/<job>/<name>``：流式下载 + Range。

历史实现是 ``path.read_bytes()`` 整包进内存：实测 96 MB 的文件让进程 RSS 涨 96.2 MB
（正好 1.00× 文件大小），HEAD 请求也一样（先读再判 ``command != "HEAD"``）。按 200 MB
上传上限、mp3→wav 约 2–4 倍的产物估算，十个并发下载就能把服务打到 OOM。

现在改成：先发头（含 ``Content-Length`` / ``Accept-Ranges``），``HEAD`` 到此为止，
``GET`` 按 64 KiB 分块 ``write``，并支持单段 ``Range``（断点续传）。
"""

from __future__ import annotations

import mimetypes
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, unquote

__all__ = ["handle"]

_BLOCK = 1 << 16
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def handle(h, route: str) -> None:
    pieces = route.split("/", 4)          # ['', 'api', 'file', token, name]
    if len(pieces) != 5:
        h._error(HTTPStatus.BAD_REQUEST, "下载路径非法")
        return
    token, raw_name = pieces[3], unquote(pieces[4])
    if not token.isalnum():
        h._error(HTTPStatus.BAD_REQUEST, "job 标识非法")
        return
    out_dir = (h.server.cfg.jobs_dir / token / "out").resolve()
    path = (out_dir / raw_name).resolve()
    if out_dir not in path.parents or not path.is_file():
        h._error(HTTPStatus.NOT_FOUND, "文件不存在或已过期")
        return

    size = path.stat().st_size
    try:
        start, end, partial = _parse_range(h.headers.get("Range"), size)
    except ValueError:
        h._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Range 超出文件范围",
                 {"Content-Range": f"bytes */{size}"})
        return

    length = end - start + 1 if size else 0
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    h.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(length))
    h.send_header("Cache-Control", "no-store")
    for key, value in headers.items():
        h.send_header(key, value)
    h._responded = True
    try:
        h.end_headers()
        if h.command == "HEAD":
            return
        with h.server.dl_slots:
            _stream(path, h, start, length)
    except OSError:
        h.close_connection = True


def _stream(path: Path, h, start: int, length: int) -> None:
    with path.open("rb") as fp:
        fp.seek(start)
        left = length
        while left > 0:
            chunk = fp.read(min(_BLOCK, left))
            if not chunk:
                break
            h.wfile.write(chunk)
            left -= len(chunk)


def _parse_range(header: str | None, size: int) -> tuple[int, int, bool]:
    """解析单段 Range，返回 (start, end, partial)。

    * 没有 Range、语法不认识（含多段）→ 整包 200（RFC 允许忽略）
    * 语法认识但落在文件外 → ``ValueError``（调用方回 416）
    """
    if not header:
        return 0, max(0, size - 1), False
    match = _RANGE.match(header.strip())
    if not match:
        return 0, max(0, size - 1), False
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return 0, max(0, size - 1), False

    if not first:                       # bytes=-N 取尾部
        suffix = int(last)
        if suffix <= 0 or size == 0:
            raise ValueError("empty suffix")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        if start > end or start >= size:
            raise ValueError("range not satisfiable")
        end = min(end, size - 1)
    if size == 0:
        raise ValueError("empty file")
    return start, end, True
