"""NCM 容器解析与解密。

NCM 是网易云音乐客户端使用的加密音频容器。整体布局（全部小端）::

    offset  size       field
    0       8          magic  b"CTENFDAM"
    8       2          gap（固定 0x01 0x70）
    10      4          key_len
    14      key_len    key_data      ← 每字节 XOR 0x64 后 AES-128-ECB 解密
    ..      4          meta_len
    ..      meta_len   meta_data     ← 每字节 XOR 0x63，去掉 22 字节前缀，base64，AES 解密，得 JSON
    ..      4          crc32
    ..      1          gap
    ..      4          cover_len（新版客户端会重复写一遍等值长度，见 ``_read_cover``）
    ..      cover_len  cover（jpeg / png 原始字节）
    ..      rest       audio         ← 256 字节周期 keystream XOR

audio 段没有额外编码，XOR 还原后即是原始 mp3 / flac 码流，因此本模块不做转码，
只做容器剥离。需要换格式时交给 ``transcode`` 模块。
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .aes import AES128ECB, pkcs7_unpad
from .id3 import MAX_COVER_BYTES, build_tag, looks_tagged

__all__ = ["NcmError", "NcmInfo", "probe", "decrypt", "guess_format", "keystream_table"]

MAGIC = b"CTENFDAM"
GAP = b"\x01\x70"

CORE_KEY = b"hzHRAmso5kInbaxW"
# 注意：这两个常量必须与下面的 hex 完全对应，源码里的 ASCII 字面量容易被看串。
# CORE_KEY = 687A4852416D736F356B496E62617857
# META_KEY = 2331346C6A6B5F215C5D2630553C2728  ->  b"#14ljk_!\\]&0U<'("
META_KEY = b"#14ljk_!\\]&0U<'("
KEY_PREFIX = b"neteasecloudmusic"

META_B64_PREFIX = b"163 key(Don't modify):"

CHUNK = 4 << 20  # 4 MiB，必须是 256 的整数倍
_ALIGN = 256


class NcmError(ValueError):
    """NCM 文件结构或加密内容不符合预期。"""


@dataclass
class NcmInfo:
    """一份 NCM 文件的头部信息与解密后音频的位置。"""

    path: Path
    audio_offset: int
    audio_size: int
    fmt: str = "mp3"
    music_name: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    bitrate: int | None = None
    duration_ms: int | None = None
    cover_offset: int = 0
    cover_size: int = 0
    cover_ext: str | None = None
    raw_meta: dict = field(default_factory=dict)
    # 解出体积与元数据推算的对照（见 _size_check）：{} 表示元数据不足、无从判断
    size_check: dict = field(default_factory=dict)
    # 是否在输出前面写了 ID3 标签（只有显式要、且输出是 mp3 时才可能为 True）
    tagged: bool = False

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    def summary(self) -> dict:
        return {
            "file": self.path.name,
            "format": self.fmt,
            "title": self.music_name,
            "artists": self.artists,
            "album": self.album,
            "bitrate": self.bitrate,
            "duration_s": round(self.duration_ms / 1000, 2) if self.duration_ms else None,
            "audio_bytes": self.audio_size,
            "has_cover": bool(self.cover_size),
            "size_consistent": self.size_check.get("consistent"),
        }


# --------------------------------------------------------------------------- #
# keystream
# --------------------------------------------------------------------------- #


def keystream_table(key: str) -> bytes:
    """由变长密钥生成 256 字节周期 keystream。

    第一步是 RC4 的 KSA，第二步把 PRGA 的第一轮「展开」成查表：因为 NCM 的
    位置索引对 256 取模，第 i 个字节的 key 只取决于 ``i % 256``，于是整条流可以
    用一条 256 字节表循环异或，不必逐字节跑 PRGA。
    """
    if not key:
        raise NcmError("keystream 密钥为空")

    box = bytearray(range(256))
    j = 0
    n = len(key)
    for i in range(256):
        j = (j + box[i] + ord(key[i % n])) & 0xFF
        box[i], box[j] = box[j], box[i]

    table = bytearray(256)
    for k in range(256):
        j1 = (k + 1) & 0xFF
        table[k] = box[(box[j1] + box[(box[j1] + j1) & 0xFF]) & 0xFF]
    return bytes(table)


def _xor_keystream(src: BinaryIO, dst: BinaryIO, table: bytes, size: int) -> int:
    """把 src 剩余 ``size`` 字节按周期表异或写入 dst，返回写出字节数。

    用大整数一次性异或整块（``int.from_bytes``），比逐字节循环快一到两个数量级；
    分块大小取 256 的整数倍，保证每块起点都落在周期边界上。
    """
    period = table * (_ALIGN // len(table) + 4)
    written = 0
    remaining = size
    while remaining > 0:
        want = CHUNK if remaining > CHUNK else remaining
        buf = src.read(want)
        if not buf:
            break
        if len(buf) != want:
            # 中途短读会让后续分块落到错的周期相位上，宁可报错也不产出坏音频
            raise NcmError(f"音频段提前结束：期望 {want} 字节，实得 {len(buf)}")
        stream = (period * (len(buf) // len(period) + 1))[: len(buf)]
        mixed = (int.from_bytes(buf, "little") ^ int.from_bytes(stream, "little")).to_bytes(
            len(buf), "little"
        )
        dst.write(mixed)
        written += len(buf)
        remaining -= len(buf)
    return written


# --------------------------------------------------------------------------- #
# 头部解析
# --------------------------------------------------------------------------- #


def _read_exact(fp: BinaryIO, n: int, what: str) -> bytes:
    data = fp.read(n)
    if len(data) != n:
        raise NcmError(f"{what} 读取不完整：期望 {n} 字节，实得 {len(data)}")
    return data


def _decode_key_block(key_raw: bytes) -> str:
    """解出 keystream 密钥。

    实测存在两种布局，用长度模 16 的余数区分：

    * 余 0（新版）：整块是密文，解密后以 ``neteasecloudmusic`` 开头，其后是变长密钥
      （实测同一客户端产出过 98 与 99 字符的密钥，长度可变，不能按 32 字节硬截）。
    * 余 1（旧版）：密文带 17 字节明文前缀 ``neteasecloudmusic``，需先切除再解密。
    """
    if not key_raw:
        raise NcmError("key 块为空")

    aes = AES128ECB(CORE_KEY)

    if len(key_raw) % 16 == 0:
        plain = aes.decrypt(key_raw)
        if not plain.startswith(KEY_PREFIX):
            raise NcmError("key 块解密后缺少 neteasecloudmusic 标识")
        body = pkcs7_unpad(plain)[len(KEY_PREFIX) :]
        return body.rstrip(b"\x00").decode("ascii", "replace")

    body = key_raw[len(KEY_PREFIX) :]
    body = body[: len(body) - len(body) % 16]
    if not body:
        raise NcmError("key 块去掉前缀后不足一个 AES 分组")
    plain = pkcs7_unpad(aes.decrypt(body))
    return plain.rstrip(b"\x00").decode("ascii", "replace")


def _decode_meta_block(meta_raw: bytes) -> dict:
    """解 meta 块，得到客户端写入的 JSON 元数据。"""
    if not meta_raw:
        return {}

    payload = meta_raw[len(META_B64_PREFIX) :] if meta_raw.startswith(META_B64_PREFIX) else meta_raw
    try:
        blob = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001 - 元数据坏了不该阻断音频提取
        raise NcmError(f"meta 块 base64 解码失败：{exc}") from exc

    blob = blob[: len(blob) - len(blob) % 16]
    if not blob:
        raise NcmError("meta 块不足一个 AES 分组")

    plain = pkcs7_unpad(AES128ECB(META_KEY).decrypt(blob))
    if plain.startswith(b"music:"):
        plain = plain[6:]

    try:
        meta = json.loads(plain.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise NcmError(f"meta 块不是合法 JSON：{exc}") from exc
    return meta if isinstance(meta, dict) else {}


def _read_cover(fp: BinaryIO, total: int) -> tuple[int, int, str | None]:
    """读封面段，返回 (offset, size, 扩展名)。

    新版客户端会把封面长度连写两遍；靠「下一段是否以 JPEG/PNG 魔术字节开头」区分，
    而不是靠版本号。
    """
    pos = fp.tell()
    if total - pos < 4:
        return pos, 0, None

    first_len = struct.unpack("<I", _read_exact(fp, 4, "封面长度"))[0]
    if first_len == 0:
        return fp.tell(), 0, None
    if total - fp.tell() < 4:
        return fp.tell(), 0, None

    peek = _read_exact(fp, 4, "封面长度/魔术字节")
    second_len = struct.unpack("<I", peek)[0]
    if peek.startswith(b"\xff\xd8\xff") or peek.startswith(b"\x89PNG"):
        fp.seek(-4, 1)
        cover_len = first_len
    elif second_len == first_len and first_len <= total - fp.tell():
        cover_len = second_len  # 新版客户端连写两遍长度
    else:
        fp.seek(-4, 1)  # 既非魔术字节也非重复长度 → 按单字段处理
        cover_len = first_len

    cover_off = fp.tell()
    if cover_len == 0 or cover_len > total - cover_off:
        return cover_off, 0, None

    head = fp.read(4)
    fp.seek(cover_off)
    ext = "jpg" if head.startswith(b"\xff\xd8\xff") else None
    if ext is None and head.startswith(b"\x89PNG"):
        ext = "png"
    return cover_off, cover_len, ext


def _read_header(fp: BinaryIO, path: Path) -> tuple[NcmInfo, str]:
    """解析头部，返回 (info, keystream_key)。"""
    total = fp.seek(0, 2)
    fp.seek(0)

    magic = _read_exact(fp, 8, "文件头")
    if magic != MAGIC:
        raise NcmError(f"不是 NCM 文件（文件头为 {magic!r}，应为 {MAGIC!r}）")
    fp.read(2)  # gap

    key_len = struct.unpack("<I", _read_exact(fp, 4, "key 长度"))[0]
    key_raw = bytes(b ^ 0x64 for b in _read_exact(fp, key_len, "key 块"))
    stream_key = _decode_key_block(key_raw)

    meta_len = struct.unpack("<I", _read_exact(fp, 4, "meta 长度"))[0]
    meta: dict = {}
    if meta_len:
        meta_raw = bytes(b ^ 0x63 for b in _read_exact(fp, meta_len, "meta 块"))
        try:
            meta = _decode_meta_block(meta_raw)
        except NcmError:
            meta = {}  # 元数据解析失败不影响音频提取

    fp.read(4)  # crc32
    fp.read(1)  # gap

    cover_off, cover_size, cover_ext = _read_cover(fp, total)
    audio_off = cover_off + cover_size

    # meta 里的 artist 形如 [["By2", "11097"], ...]：先名字、后内部 ID
    artists: list[str] = []
    for item in meta.get("artist") or []:
        if isinstance(item, (list, tuple)) and item:
            artists.append(str(item[0]))
        elif isinstance(item, str):
            artists.append(item)

    info = NcmInfo(
        path=path,
        audio_offset=audio_off,
        audio_size=total - audio_off,
        fmt=(meta.get("format") or "").lower() or "mp3",
        music_name=meta.get("musicName") or meta.get("album") or None,
        artists=artists,
        album=meta.get("album") or None,
        bitrate=meta.get("bitrate") or None,
        duration_ms=meta.get("duration") or None,
        cover_offset=cover_off if cover_size else 0,
        cover_size=cover_size,
        cover_ext=cover_ext,
        raw_meta=meta,
    )
    if info.audio_size <= 0:
        raise NcmError("音频段为空，文件可能被截断")
    return info, stream_key


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def guess_format(head: bytes) -> str | None:
    """从码流头 4 字节推断容器/编码格式。"""
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    return None


def _peek_plain(fp: BinaryIO, info: NcmInfo, table: bytes, n: int = 4096) -> bytes:
    """解出音频段的头 ``n`` 字节明文。

    注意：NCM 的音频段本身是密文，直接在原文件上读头字节去猜格式是没有意义的
    （``fLaC`` / ``ID3`` 这些魔术字节同样被异或掉了），必须先按 keystream 还一位。
    """
    if info.audio_size <= 0:
        return b""
    fp.seek(info.audio_offset)
    raw = fp.read(min(n, info.audio_size))
    if not raw:
        return b""
    stream = (table * (len(raw) // len(table) + 1))[: len(raw)]
    mixed = int.from_bytes(raw, "little") ^ int.from_bytes(stream, "little")
    return mixed.to_bytes(len(raw), "little")


def _size_check(info: NcmInfo, written: int) -> dict:
    """用 meta 里的时长 × 码率粗算应得体积，与实得体积对照。

    **这是启发式，不是校验和**：VBR / 元数据不准的文件本来就会偏，所以结果只用于
    「提示可能被截断」，绝不据此抛错或拒绝产出（误报比漏报更伤用户）。
    头部那 4 字节 crc32 的确切覆盖范围尚未实测确认，故不参与判断（见 docs/FORMAT.md）。
    """
    duration_ms = info.duration_ms or 0
    bitrate = info.bitrate or 0
    if duration_ms <= 0 or bitrate <= 0 or written <= 0:
        return {}
    expected = duration_ms / 1000 * bitrate / 8
    if expected <= 0:
        return {}
    ratio = written / expected
    return {
        "expected_bytes": int(expected),
        "actual_bytes": written,
        "ratio": round(ratio, 3),
        # 0.5–2.0 只用来抓「少了一半以上」这种明显截断
        "consistent": 0.5 <= ratio <= 2.0,
    }


def probe(path: str | Path) -> NcmInfo:
    """只读头部，不解整段音频。"""
    p = Path(path)
    with p.open("rb") as fp:
        info, stream_key = _read_header(fp, p)
        real = guess_format(_peek_plain(fp, info, keystream_table(stream_key)))
        if real:
            info.fmt = real
    return info


def decrypt(
    src: str | Path,
    out_dir: str | Path | None = None,
    *,
    extract_cover: bool = False,
    tags: bool = False,
    overwrite: bool = True,
) -> tuple[Path, NcmInfo]:
    """解出原始音频，返回 (输出路径, 头部信息)。

    ``tags=True`` 会在 mp3 码流前面拼一个 ID3v2.3 标签（标题/艺人/专辑/封面）。
    默认关闭：本工具的核心承诺是「逐字节还原码流」，加标签就改变了输出字节。
    """
    src = Path(src)
    if not src.is_file():
        raise NcmError(f"文件不存在：{src}")

    with src.open("rb") as fp:
        info, stream_key = _read_header(fp, src)
        table = keystream_table(stream_key)

        # 用真实码流头校正 meta 里声明的格式，决定输出扩展名
        head = _peek_plain(fp, info, table)
        real = guess_format(head)
        if real:
            info.fmt = real

        tag = _build_tag(fp, info, head) if tags else b""

        out_dir = Path(out_dir) if out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{src.stem}.{info.fmt}"
        tmp = out.with_name(out.name + ".part")

        if out.exists() and not overwrite:
            raise NcmError(f"输出已存在：{out}")

        fp.seek(info.audio_offset)
        try:
            with tmp.open("wb") as dst:
                if tag:
                    dst.write(tag)
                written = _xor_keystream(fp, dst, table, info.audio_size)
            info.audio_size = written
            info.size_check = _size_check(info, written)
            info.tagged = bool(tag)
            if written == 0:
                raise NcmError(f"解密后输出为空：{out}")
            # 先写 .part 再原子改名：中途失败不会在用户音乐目录里留半截文件
            tmp.replace(out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        if extract_cover and info.cover_size:
            ext = info.cover_ext or "jpg"
            cover_path = out_dir / f"{src.stem}.cover.{ext}"
            fp.seek(info.cover_offset)
            cover_path.write_bytes(fp.read(info.cover_size))

    return out, info


def _build_tag(fp: BinaryIO, info: NcmInfo, head: bytes) -> bytes:
    """按需读封面并拼 ID3 标签；不该写的场景一律返回空字节（不报错）。"""
    if info.fmt != "mp3":
        # flac 该写 Vorbis comment、wav 没有标签位 —— 往这些容器里塞 ID3 只会制造垃圾
        return b""
    if looks_tagged(head):
        # 码流自带 ID3 头：再前置一个会出现「两台设备显示不同歌名」
        return b""
    cover = b""
    if 0 < info.cover_size <= MAX_COVER_BYTES:
        fp.seek(info.cover_offset)
        cover = fp.read(info.cover_size)
    return build_tag(
        title=info.music_name,
        artists=info.artists,
        album=info.album,
        cover=cover or None,
        cover_ext=info.cover_ext,
    )
