"""零依赖 multipart/form-data 解析器。

Python 3.13 起标准库移除了 ``cgi``，而 ``email`` 那套解析器要先把整个请求体读进
内存再按行重排，处理上百 MB 的上传不划算。这里的做法是：

1. 请求体先由 ``server`` 分块落到一个临时文件（内存占用恒定）；
2. 本模块对那个文件做 ``mmap``，用 ``find`` 定位 boundary 得到每个 part 的
   ``[start, end)`` 区间；
3. 文件 part 直接按区间流式拷贝到目标路径，全程不把内容读进内存。

只支持 ``multipart/form-data`` 的常用子集：普通字段 + 单层文件 part。嵌套
multipart 与本项目的用途无关，遇到会当成普通字段处理。
"""

from __future__ import annotations

import mmap
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "MultipartError",
    "Part",
    "parse_multipart",
    "get_boundary",
    "sanitize_filename",
]

_COPY_BLOCK = 1 << 20
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名：带任何扩展名同样不可用（CON.txt 与 CON 是同一个设备），
# 且大小写不敏感。落盘名命中这些前缀时，Windows 上会直接失败或指向设备。
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class MultipartError(ValueError):
    """请求体不是合法的 multipart 数据。"""


@dataclass
class Part:
    name: str | None
    filename: str | None = None
    content_type: str | None = None
    value: bytes | None = None
    path: Path | None = None
    size: int = 0

    @property
    def is_file(self) -> bool:
        return self.path is not None

    def text(self) -> str:
        return (self.value or b"").decode("utf-8", "replace")


def get_boundary(content_type: str) -> bytes:
    """从 Content-Type 头里取出 boundary。"""
    if not content_type or "multipart/form-data" not in content_type.lower():
        raise MultipartError("Content-Type 不是 multipart/form-data")
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.I)
    if not m:
        raise MultipartError("Content-Type 缺少 boundary")
    return (m.group(1) or m.group(2)).encode("latin-1")


def sanitize_filename(name: str, fallback: str = "upload") -> str:
    """把客户端给的文件名收成安全的单段文件名（去目录、去控制字符、限长）。

    除路径穿越与控制字符，这里还要处理两个 Windows 独有的静默坑：

    * **尾点 / 尾空格会被文件系统吃掉**：``a.mp3 `` 落到 NTFS 上就是 ``a.mp3``，
      与同批真正叫 ``a.mp3`` 的文件撞名，去重逻辑（``dest.exists()``）看不到冲突。
    * **保留设备名**：``CON`` / ``NUL`` / ``COM1`` … 带扩展名一样是设备名，
      写入行为未定义，命中时前缀一个下划线绕开。
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("_", name).strip().strip(".")
    if not name:
        return fallback
    stem, ext = os.path.splitext(name)
    stem = stem.rstrip(" .")
    if len(stem) > 100:
        stem = stem[:100].rstrip(" .")
    if not stem:
        return fallback
    if stem.split(".")[0].upper() in _WIN_RESERVED:
        stem = "_" + stem
    return f"{stem}{ext[:16]}"


def _parse_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.decode("utf-8", "replace").split("\r\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        headers[key.strip().lower()] = val.strip()
    return headers


def _disposition(headers: dict[str, str]) -> tuple[str | None, str | None]:
    cd = headers.get("content-disposition", "")
    name = None
    filename = None
    m = re.search(r'\bname="([^"]*)"', cd)
    if m:
        name = m.group(1)
    # RFC 5987 的 filename*=UTF-8''%E4%B8%AD.mp3 优先于 filename=
    m = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        from urllib.parse import unquote

        filename = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'\bfilename="([^"]*)"', cd)
        if m:
            filename = m.group(1)
        else:
            m = re.search(r"\bfilename=([^;]+)", cd)
            if m:
                filename = m.group(1).strip()
    return name, filename


def parse_multipart(
    spool_path: str | Path,
    boundary: bytes,
    dest_dir: str | Path,
    *,
    max_field_bytes: int = 64 * 1024,
) -> list[Part]:
    """解析落在磁盘上的请求体，返回 part 列表。

    文件 part 的内容会被写到 ``dest_dir`` 下，``Part.path`` 指向落盘文件。

    请求体必须以收尾分隔符 ``--boundary--`` 结束；缺了它一律按「被截断」处理并抛
    ``MultipartError``（见循环里 ``nxt < 0`` 那段）。多个 part 里只要出现字段超限，
    也整体失败 —— 与「宁可 4xx 也不产半份结果」的取舍一致。
    """
    spool_path = Path(spool_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    delim = b"--" + boundary
    sep = b"\r\n" + delim
    parts: list[Part] = []

    with spool_path.open("rb") as fp:
        size = os.fstat(fp.fileno()).st_size
        if size == 0:
            raise MultipartError("请求体为空")
        mm = mmap.mmap(fp.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            cursor = mm.find(delim)
            if cursor < 0:
                raise MultipartError("请求体里找不到 boundary 分隔符")
            cursor += len(delim)

            index = 0
            while True:
                if mm[cursor : cursor + 2] == b"--":  # 收尾标记 --boundary--
                    break
                if mm[cursor : cursor + 2] == b"\r\n":
                    cursor += 2

                nxt = mm.find(sep, cursor)
                if nxt < 0:
                    # 正常请求一定以 `--boundary--` 收尾；找不到下一个分隔符，
                    # 只可能是请求体被截断（连接中断、代理截长度、客户端算错
                    # Content-Length）。此时绝不能把剩下的字节当成最后一个 part
                    # 落盘 —— 那会静默产出一个「能过、但内容是残缺的」文件，
                    # 比直接报错难查得多。
                    raise MultipartError(
                        "请求体缺少收尾 boundary（上传被截断，或 Content-Length 与实际不符）"
                    )

                part = _read_part(mm, fp, cursor, nxt, dest_dir, index, max_field_bytes)
                if part is not None:
                    parts.append(part)
                    index += 1
                cursor = nxt + len(sep)
        finally:
            mm.close()
    if not parts:
        raise MultipartError("请求体里没有任何有效 part")
    return parts


def _read_part(
    mm: mmap.mmap,
    fp: BinaryIO,
    start: int,
    end: int,
    dest_dir: Path,
    index: int,
    max_field_bytes: int,
) -> Part | None:
    head_end = mm.find(b"\r\n\r\n", start, end)
    if head_end < 0:
        return None
    headers = _parse_headers(bytes(mm[start:head_end]))
    body_start = head_end + 4
    body_len = max(0, end - body_start)
    name, filename = _disposition(headers)

    if filename is not None:
        safe = sanitize_filename(filename, fallback=f"upload-{index}")
        # 保留原始文件名（输出名会沿用它），同批重名才追加序号
        dest = dest_dir / safe
        if dest.exists():
            stem, ext = os.path.splitext(safe)
            n = 1
            while dest.exists():
                dest = dest_dir / f"{stem}-{n}{ext}"
                n += 1
        with dest.open("wb") as out:
            fp.seek(body_start)
            left = body_len
            while left > 0:
                chunk = fp.read(min(_COPY_BLOCK, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        return Part(
            name=name,
            filename=safe,
            content_type=headers.get("content-type"),
            path=dest,
            size=dest.stat().st_size,
        )

    if body_len > max_field_bytes:
        raise MultipartError(f"表单字段 {name!r} 超过 {max_field_bytes} 字节上限")
    return Part(
        name=name,
        content_type=headers.get("content-type"),
        value=bytes(mm[body_start : body_start + body_len]),
        size=body_len,
    )
