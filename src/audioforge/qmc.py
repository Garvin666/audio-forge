"""QQ 音乐 QMC 加密格式解密（纯 Python，零依赖）。

和 NCM 的根本区别：**NCM 是容器，QMC 不是**。NCM 有 ``CTENFDAM`` 魔数、加密的 key 块、
加密的元数据块，能按结构切开；QMC 是对**整个文件**做流加密，没有头部、没有长度字段，
解完才知道里面是 flac 还是 mp3。所以这里没有「读头 → 拒绝」这条路径，只能
「先解 4 KiB → 嗅探 → 再决定」。

三种形态，按「需不需要额外信息」递增：

1. **v1 静态密钥** —— ``.qmc0`` / ``.qmc3`` / ``.qmcflac`` / ``.qmcogg`` / ``.qmc2``，
   以及 ``.tkm`` / ``.bkc*`` 这类老后缀。整文件异或一张 128 字节固定表，
   下标按绝对偏移走，**离线可解**。
2. **v2 内嵌 EKey** —— ``.mflac`` / ``.mgg`` / ``.mflac0`` / ``.mgg1`` / ``.mmp4``。
   文件尾部嵌了一段 base64 的 EKey（PC 版是「长度 + ekey」，安卓版是 ``QTag`` 结构），
   用腾讯 TEA（16 轮、改过的 CBC）解出 master key，再按 key 长度分流密码：
   1–300 字节走 **Map**（压成 128 字节后同 v1 异或），>300 字节走**改造过的 RC4**
   （状态表长度 = 密钥长度而不是 256，按 0x1400 分段跳 keystream）。**离线可解**。
3. **v2 无内嵌 EKey** —— 尾部是 ``STag``（安卓）或 ``musicex``（PC MusicEx）。
   密钥只在服务器上，**离线解不了**，只能由调用方用 ``ekey=`` 手工提供。

一个顺带的好处：因为整个文件（含 flac 的 Vorbis comment、mp3 的 ID3）是被一起加密的，
解出来标签天然还在 —— 不像 NCM 那样需要单独回写 ID3。

算法参照 unlock-music 的 ``lib_um_crypto_rust``（逐字节对齐其 Rust 实现），
常量与边界语义都在下面逐条注明；单测里有自造的样本文件做往返验证。
"""

from __future__ import annotations

import base64
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .ncm import guess_format

__all__ = [
    "QmcError",
    "QmcInfo",
    "EXTENSIONS",
    "V2_EXTENSIONS",
    "V1_EXTENSIONS",
    "is_qmc_name",
    "parse_footer",
    "ekey_decrypt",
    "decrypt",
    "probe",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: v1 静态密钥表（128 字节）。下标 = 偏移，见 :func:`qmc1_transform` 的边界语义。
# fmt: off
QMC1_KEY = bytes(
    [
        0xC3, 0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52, 0xD8, 0xA1, 0x66, 0x62, 0x9F, 0x5B, 0x09, 0x00,
        0xC3, 0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E, 0xD8, 0x92, 0x3F, 0xBC, 0x90, 0xBB, 0x74, 0x0E,
        0xC3, 0x47, 0x74, 0x3D, 0x90, 0xAA, 0x3F, 0x51, 0xD8, 0xF4, 0x11, 0x84, 0x9F, 0xDE, 0x95, 0x1D,
        0xC3, 0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9, 0xD8, 0xF0, 0xF7, 0xA0, 0x90, 0xA1, 0xD6, 0xF3,
        0xC3, 0xF3, 0xD6, 0xA1, 0x90, 0xA0, 0xF7, 0xF0, 0xD8, 0xF9, 0x66, 0xFA, 0x9F, 0xD5, 0x09, 0xC6,
        0xC3, 0x1D, 0x95, 0xDE, 0x9F, 0x84, 0x11, 0xF4, 0xD8, 0x51, 0x3F, 0xAA, 0x90, 0x3D, 0x74, 0x47,
        0xC3, 0x0E, 0x74, 0xBB, 0x90, 0xBC, 0x3F, 0x92, 0xD8, 0x7E, 0x11, 0x13, 0x9F, 0x23, 0x95, 0x5E,
        0xC3, 0x00, 0x09, 0x5B, 0x9F, 0x62, 0x66, 0xA1, 0xD8, 0x52, 0xF7, 0x67, 0x90, 0xCA, 0xD6, 0x4A,
    ]
)
# fmt: on
_KEY_SIZE = 128
#: ⚠️ 0x7FFF 不是笔误：偏移 > 0x7FFF 时先对 0x7FFF 取模（不是 0x8000），
#: 所以第 0x7FFF 字节用 key[127]，而第 0x8000 字节用 key[1]。
_OFFSET_BOUNDARY = 0x7FFF

_TEA_DELTA = 0x9E3779B9
_TEA_ROUNDS = 16
_TEA_SALT_LEN = 2
_TEA_ZERO_LEN = 7

#: base64("QQMusic EncV2,Key:")，V2 版 EKey 的前缀（base64 里所以长得不一样）
_EKEY_V2_PREFIX = b"UVFNdXNpYyBFbmNWMixLZXk6"
_EKEY_V2_KEY1 = bytes(
    [0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40, 0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28]
)
_EKEY_V2_KEY2 = bytes(
    [0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25, 0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54]
)

_RC4_FIRST_SEGMENT = 0x0080
_RC4_OTHER_SEGMENT = 0x1400

#: PC 版「末尾 u32 = 密钥长度」的上限。官方取 0xFFFF（algo/qmc/qmc.go），
#: 不是 0x500 —— 早先按 1280 会不会截断不是问题，问题是**超过 1280 的真密钥会被
#: 当成没有尾部**，静默回落到 v1 静态密钥，解出一整首噪音却报「成功」。
_MAX_EKEY_LEN = 0xFFFF
# 一次读多少尾部。必须装得下「标记 + 大端长度 + 完整密钥」：
# PC 版旧格式允许 key 最长 0xFFFF（官方 qmc.go 的判据），所以这里要跟着放大，
# 否则大 key 文件会被判成「声明的长度超过实际数据」。每次解密只读一次，开销可忽略。
_TAIL_PROBE = _MAX_EKEY_LEN + 1024
_COPY_BLOCK = 1 << 20

V2_EXTENSIONS = frozenset(
    {
        "mgg",
        "mgg0",
        "mggl",
        "mgg1",
        "mflac",
        "mflac0",
        "mmp4",
        "qmcflac",
        "qmcogg",
        "qmc0",
        "qmc2",
        "qmc3",
        "qmc4",
        "qmc6",
        "qmc8",
    }
)
V1_EXTENSIONS = frozenset(
    {"bkcmp3", "bkcm4a", "bkcflac", "bkcwav", "bkcape", "bkcogg", "bkcwma", "tkm"}
)
EXTENSIONS = V2_EXTENSIONS | V1_EXTENSIONS

# 尾部长度的「兜底猜测」，用于嗅探不出来时命名（不保证对，仅作提示）
_EXT_HINT = {
    "mgg": "ogg",
    "mgg0": "ogg",
    "mggl": "ogg",
    "mgg1": "ogg",
    "mflac": "flac",
    "mflac0": "flac",
    "mmp4": "m4a",
    "qmcflac": "flac",
    "qmcogg": "ogg",
    "qmc0": "mp3",
    "qmc2": "ogg",
    "qmc3": "mp3",
    "qmc4": "ogg",
    "qmc6": "ogg",
    "qmc8": "ogg",
    "bkcmp3": "mp3",
    "bkcm4a": "m4a",
    "bkcflac": "flac",
    "bkcwav": "wav",
    "bkcape": "ape",
    "bkcogg": "ogg",
    "bkcwma": "wma",
    "tkm": "m4a",
}


class QmcError(ValueError):
    """不是可解密的 QMC 文件，或缺少解密所需的信息。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Footer:
    """文件尾部解析结果。``size`` 是要从末尾裁掉的字节数。"""

    size: int
    ekey: str | None = None
    kind: str = ""  # STag / QTag / PcV2MusicEx / PcV1Legacy
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "size": self.size, "has_ekey": bool(self.ekey)}
        out.update(self.extra)
        return out


@dataclass
class QmcInfo:
    file: str = ""
    ext: str = ""
    fmt: str | None = None
    footer: str | None = None
    cipher: str | None = None
    ekey_len: int = 0
    key_len: int = 0
    audio_size: int = 0
    needs_ekey: bool = False

    def summary(self) -> dict:
        return {
            "file": self.file,
            "ext": self.ext,
            "format": self.fmt,
            "footer": self.footer,
            "cipher": self.cipher,
            "ekey_len": self.ekey_len,
            "key_len": self.key_len,
            "audio_bytes": self.audio_size,
            "needs_ekey": self.needs_ekey,
        }


def is_qmc_name(name: str) -> bool:
    """按后缀判断是不是 QMC 家族（大小写不敏感，接受带点的形式）。"""
    return name.lower().lstrip(".").rsplit(".", 1)[-1] in EXTENSIONS


# --------------------------------------------------------------------------- #
# v1 静态密钥 / Map 共用的一遍异或
# --------------------------------------------------------------------------- #


def _xor_bytes(data: bytes, stream: bytes) -> bytes:
    """等长 XOR。用大整数一次算完 —— 逐字节 for 循环在 100 MB 上是分钟级。"""
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(
        len(data), "little"
    )


def qmc1_transform(data: bytes, offset: int, key: bytes = QMC1_KEY) -> bytes:
    """把 ``data`` 当作从绝对偏移 ``offset`` 开始的一段，做一次异或。

    异或是对合的 —— 同一个函数既加密也解密，测试里就靠这点造样本。
    分段只是因为 key 的下标要跟着**绝对偏移**走，且 0x7FFF 处的回绕要处理对。
    """
    n = len(data)
    out = bytearray(data)
    pos = offset
    i = 0
    while i < n:
        if pos <= _OFFSET_BOUNDARY:
            take = min(_OFFSET_BOUNDARY + 1 - pos, n - i)
            phase = pos % _KEY_SIZE
        else:
            rest = pos % _OFFSET_BOUNDARY
            take = min(_OFFSET_BOUNDARY - rest, n - i)
            phase = rest % _KEY_SIZE
        rotated = key[phase:] + key[:phase]
        stream = (rotated * (take // _KEY_SIZE + 1))[:take]
        out[i : i + take] = _xor_bytes(bytes(out[i : i + take]), stream)
        i += take
        pos += take
    return bytes(out)


# --------------------------------------------------------------------------- #
# 腾讯 TEA（16 轮，改了 CBC 的填充约定）
# --------------------------------------------------------------------------- #


def _tea_round(value: int, s: int, k1: int, k2: int) -> int:
    left = ((value << 4) & 0xFFFFFFFF) + k1
    right = (value >> 5) + k2
    mid = (s + value) & 0xFFFFFFFF
    return (left ^ mid ^ right) & 0xFFFFFFFF


def _tea_decrypt_block(block: int, k: tuple[int, int, int, int]) -> int:
    y = (block >> 32) & 0xFFFFFFFF
    z = block & 0xFFFFFFFF
    s = (_TEA_DELTA * _TEA_ROUNDS) & 0xFFFFFFFF
    for _ in range(_TEA_ROUNDS):
        z = (z - _tea_round(y, s, k[2], k[3])) & 0xFFFFFFFF
        y = (y - _tea_round(z, s, k[0], k[1])) & 0xFFFFFFFF
        s = (s - _TEA_DELTA) & 0xFFFFFFFF
    return (y << 32) | z


def _tea_cbc_decrypt(ciphertext: bytes, key16: bytes) -> bytes:
    """解一段，去掉「1 字节 pad 长度 + 2 字节 salt + 尾部 7 字节 0」这三截。"""
    if len(ciphertext) % 8 or len(ciphertext) < 10:
        raise QmcError(f"TEA 密文长度不合法：{len(ciphertext)}")
    k = struct.unpack(">IIII", key16)
    iv1 = 0
    iv2 = 0
    out = bytearray()
    for i in range(0, len(ciphertext), 8):
        block = int.from_bytes(ciphertext[i : i + 8], "big")
        mid = _tea_decrypt_block((block ^ iv2) & 0xFFFFFFFFFFFFFFFF, k)
        out += (mid ^ iv1).to_bytes(8, "big")
        iv1 = block
        iv2 = mid
    start = 1 + (out[0] & 0b111) + _TEA_SALT_LEN
    end = len(ciphertext) - _TEA_ZERO_LEN
    if any(out[end:]):
        raise QmcError("TEA 填充校验失败（尾部不是全 0）")
    return bytes(out[start:end])


def _make_simple_key() -> bytes:
    """8 字节 simple key：``int(|tan(106 + i/10)| * 100) & 0xFF``。

    与官方 ``simpleMakeKey(106, 8)`` 逐字节一致（实测 ``69 56 46 38 2B 20 15 0B``）。
    注意 ``& 0xFF`` 是截断而非钳位 —— 这是 Go ``byte(f)`` 的语义，别「修正」成 clamp。
    """
    return bytes(int(abs(math.tan(106.0 + i * 0.1)) * 100.0) & 0xFF for i in range(8))


_SIMPLE_KEY = _make_simple_key()


def ekey_decrypt(ekey: str | bytes) -> bytes:
    """把 EKey 字符串解成 master key。

    V2 版（带 ``QQMusic EncV2,Key:`` 前缀）要先过两层固定密钥的 TEA，截到第一个 0 字节，
    再按 V1 的规矩解一次：前 8 字节是 header，与 simple key 交错成 TEA 密钥。
    """
    raw = ekey.encode("latin-1") if isinstance(ekey, str) else bytes(ekey)
    if raw.startswith(_EKEY_V2_PREFIX):
        payload = base64.b64decode(raw[len(_EKEY_V2_PREFIX) :], validate=False)
        payload = _tea_cbc_decrypt(_tea_cbc_decrypt(payload, _EKEY_V2_KEY1), _EKEY_V2_KEY2)
        cut = payload.find(b"\x00")
        if cut != -1:
            payload = payload[:cut]
        raw = payload
    decoded = base64.b64decode(raw, validate=False)
    # 官方 deriveKeyV1 要求 ≥16 字节（8 字节 header + 至少 8 字节待解密 body）；
    # 少于它一定是坏 key，早点报出来比解出噪声再回头猜要好。
    if len(decoded) < 16:
        raise QmcError(f"EKey 解出来只有 {len(decoded)} 字节，至少要有 16 字节")
    header, cipher = decoded[:8], decoded[8:]
    tea_key = bytearray()
    for sk, hk in zip(_SIMPLE_KEY, header):
        tea_key.append(sk)
        tea_key.append(hk)
    return header + _tea_cbc_decrypt(cipher, bytes(tea_key))


# --------------------------------------------------------------------------- #
# v2 流密码：Map（短密钥）/ RC4（长密钥）
# --------------------------------------------------------------------------- #


def _key_mask_table(long_key: bytes) -> bytes:
    """Map 密码用的 0x8000 项掩码表（下标 = 已归一化的偏移）。

    这里**不做「先把 key 压成 128 字节」那种优化**。压成 128 字节只有在
    ``len(key)`` 能整除 256 时才与官方等价（128 / 256 能整除，96 / 200 / 300 不能），
    而真实的 mflac 密钥长度并不保证落在这几个值上 —— 一旦不整除，
    从偏移 128 开始整首歌都是错的，且没有任何异常。
    32 KiB 的表每个文件只建一次，比逐字节算平方取模还快。
    """
    n = len(long_key)
    if n == 0:
        raise QmcError("master key 为空")
    table = bytearray(0x8000)
    for o in range(0x8000):
        idx = (o * o + 71214) % n
        byte = long_key[idx]
        shift = ((idx & 0x07) + 4) & 0x07
        table[o] = ((byte << shift) | (byte >> shift)) & 0xFF
    return bytes(table)


class _MapCipher:
    name = "map"

    def __init__(self, master_key: bytes) -> None:
        self.key = master_key
        self.table = _key_mask_table(master_key)

    def decrypt(self, data: bytes, offset: int = 0) -> bytes:
        """官方语义：``o = offset if offset <= 0x7FFF else offset % 0x7FFF``。

        注意 0x7FFF 这一个字节用的是 ``o = 0x7FFF`` 本身，不是 ``% 0x7FFF``(=0)；
        错这一个字节的症状是全文件只有一个 packet 坏掉，极难发现。
        """
        out = bytearray(data)
        total = len(out)
        pos = offset
        i = 0
        while i < total:
            if pos <= _OFFSET_BOUNDARY:
                start = pos
                take = min(_OFFSET_BOUNDARY + 1 - pos, total - i)
            else:
                start = pos % _OFFSET_BOUNDARY
                take = min(_OFFSET_BOUNDARY - start, total - i)
            out[i : i + take] = _xor_bytes(
                bytes(out[i : i + take]), self.table[start : start + take]
            )
            i += take
            pos += take
        return bytes(out)


def _qmc2_hash(key: bytes) -> float:
    """RC4 分段要用的乘积累积（遇到 0 跳过、遇到不增就停 —— 原实现就这样）。"""
    h = 1
    for v in key:
        if v == 0:
            continue
        nxt = (h * v) & 0xFFFFFFFF
        if nxt == 0 or nxt <= h:
            break
        h = nxt
    return float(h)


def _segment_key(seg_id: int, seed: int, h: float) -> int:
    if seed == 0:
        return 0
    denom = ((seg_id + 1) * seed) & 0xFFFFFFFFFFFFFFFF
    return int(h / float(denom) * 100.0)


class _RC4:
    """改造过的 RC4：状态表长度 = 密钥长度（不是 256），初始化时按 u8 截断。"""

    def __init__(self, key: bytes) -> None:
        n = len(key)
        state = [i & 0xFF for i in range(n)]
        j = 0
        for i in range(n):
            j = (j + state[i] + key[i % n]) % n
            state[i], state[j] = state[j], state[i]
        self.state = state
        self.i = 0
        self.j = 0
        self.n = n

    def generate(self) -> int:
        n = self.n
        self.i = (self.i + 1) % n
        self.j = (self.j + self.state[self.i]) % n
        self.state[self.i], self.state[self.j] = self.state[self.j], self.state[self.i]
        return self.state[(self.state[self.i] + self.state[self.j]) % n]


class _RC4Cipher:
    name = "rc4"

    def __init__(self, master_key: bytes) -> None:
        rc4 = _RC4(master_key)
        self.key = bytes(master_key)
        self.hash = _qmc2_hash(self.key)
        # 官方是「每段从 KSA 盒的副本重启 PRGA，先丢弃 skipLen 个输出」。
        # 因为每段起点相同，丢弃序列对每段都一样，所以只需预生成一份
        # 长度 = 一段 + 最大 skip（skip < n）的 keystream，按 skip+段内偏移切片。
        self.stream = bytes(rc4.generate() for _ in range(_RC4_OTHER_SEGMENT + len(self.key)))

    def _first_segment(self, buf: bytearray, start: int, length: int, offset: int) -> None:
        n = len(self.key)
        for j in range(length):
            o = offset + j
            idx = _segment_key(o, self.key[o % n], self.hash) % n
            buf[start + j] ^= self.key[idx]

    def _other_segment(self, buf: bytearray, start: int, length: int, offset: int) -> None:
        n = len(self.key)
        seg_id = offset // _RC4_OTHER_SEGMENT
        block_off = offset % _RC4_OTHER_SEGMENT
        # ⚠️ 这里**不能**写成 ``& 0x1FF``。官方 getSegmentSkip 返回 ``idx % n``；
        # ``& 0x1FF`` 只在 n 恰为 2 的幂（真实样本的 512）时与之等价，
        # 换成 384 / 768 这类长度就会解出噪音，而 512 的样本一切正常 ——
        # 这正是「合成样本自证 + 单一真样本」最难发现的一类错。
        skip = _segment_key(seg_id, self.key[seg_id % n], self.hash) % n
        window = self.stream[skip + block_off : skip + block_off + length]
        buf[start : start + length] = _xor_bytes(bytes(buf[start : start + length]), window)

    def decrypt(self, data: bytes, offset: int = 0) -> bytes:
        buf = bytearray(data)
        n = len(buf)
        pos = offset
        start = 0
        if pos < _RC4_FIRST_SEGMENT:
            take = min(_RC4_FIRST_SEGMENT - pos, n - start)
            self._first_segment(buf, start, take, pos)
            start += take
            pos += take
        if pos % _RC4_OTHER_SEGMENT:
            take = min(_RC4_OTHER_SEGMENT - (pos % _RC4_OTHER_SEGMENT), n - start)
            self._other_segment(buf, start, take, pos)
            start += take
            pos += take
        while start < n:
            take = min(_RC4_OTHER_SEGMENT, n - start)
            self._other_segment(buf, start, take, pos)
            start += take
            pos += take
        return bytes(buf)


def _make_cipher(master_key: bytes) -> _MapCipher | _RC4Cipher:
    """1–300 字节走 Map，更长走 RC4 —— 这是密钥长度本身在暗示用哪套。"""
    if not master_key:
        raise QmcError("master key 为空")
    if len(master_key) <= 300:
        return _MapCipher(master_key)
    return _RC4Cipher(master_key)


# --------------------------------------------------------------------------- #
# 尾部解析
# --------------------------------------------------------------------------- #


def _is_b64(raw: bytes) -> bool:
    return all(
        c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in raw
    )


def _read_utf16le(raw: bytes) -> str:
    out = []
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            break
        if raw[i + 1] == 0 and 0 < raw[i] < 128:
            out.append(chr(raw[i]))
        else:
            break
    return "".join(out)


def parse_footer(tail: bytes) -> Footer | None:
    """解析文件末尾片段，返回 :class:`Footer`；没有尾部（=v1 老格式）返回 ``None``。

    顺序不能换：``STag`` → ``QTag`` → ``musicex`` → PC 版「长度 + ekey」。
    前三者有明确的尾部标记，最后一个只能靠「末尾 4 字节是大端长度」去猜，
    所以必须放最后 —— 否则任意文件都能被误判成有 ekey。
    """
    if len(tail) < 8:
        return None

    if tail.endswith(b"STag"):
        body = tail[:-4]
        payload, size_bytes = body[:-4], body[-4:]
        plen = int.from_bytes(size_bytes, "big")
        if len(payload) < plen:
            raise QmcError("STag 声明的长度超过实际数据")
        # STag 里**没有**密钥（官方 qmc.go 直接 `return errors.New("STag ... doesn't
        # contains media key")`），密钥在服务器上。所以这里只按结构裁长度，
        # 不再对正文做「必须是三段、版本必须等于 2」这类校验 —— 正文格式随客户端
        # 版本变，校验只会把能解的文件挡在门外，却挡不住真正的问题。
        text = payload[len(payload) - plen :].decode("utf-8", "replace")
        parts = text.split(",")
        extra: dict = {"raw": text[:120]}
        if len(parts) >= 3:
            extra["song_id"] = parts[0]
            extra["media_mid"] = parts[2]
        return Footer(plen + 8, None, "STag", extra)

    if tail.endswith(b"QTag"):
        body = tail[:-4]
        payload, size_bytes = body[:-4], body[-4:]
        plen = int.from_bytes(size_bytes, "big")
        if len(payload) < plen:
            raise QmcError("QTag 声明的长度超过实际数据")
        parts = payload[len(payload) - plen :].decode("utf-8", "replace").split(",")
        if len(parts) != 3:
            raise QmcError("QTag 不是「ekey,song_id,extra」三段式")
        ekey, rid, extra2 = parts
        if not rid.isdigit():
            raise QmcError(f"QTag 的 song_id 不是数字：{rid}")
        # 第三段官方叫 rawMetaExtra2（atoi 读出来的整数），**不是**版本号；
        # 早先写成「必须等于 2」，遇到别的取值就把整个文件判死了。
        if not extra2.isdigit():
            raise QmcError(f"QTag 的第三段不是数字：{extra2}")
        if not ekey or not _is_b64(ekey.encode("latin-1", "replace")):
            raise QmcError("QTag 里的 EKey 不是 base64")
        return Footer(plen + 8, ekey, "QTag", {"resource_id": int(rid), "extra2": int(extra2)})

    if tail.endswith(b"musicex\x00"):
        body = tail[:-8]
        if len(body) < 8:
            raise QmcError("MusicEx 尾部过短")
        data, ver_bytes = body[:-4], body[-4:]
        if int.from_bytes(ver_bytes, "little") != 1:
            raise QmcError("MusicEx 版本不是 1")
        head, len_bytes = data[:-4], data[-4:]
        plen = int.from_bytes(len_bytes, "little")
        # 官方只要求 ≥ 0xC0（未来客户端可能加长），不是「必须等于 0xC0」
        if plen < 0xC0:
            raise QmcError(f"MusicEx 的载荷长度小于 0xC0（实际 0x{plen:X}）")
        if plen > len(tail):
            raise QmcError("MusicEx 声明的长度超过实际数据")
        inner = head[len(head) - (plen - 0x10) :]
        if len(inner) < 0x8C:
            raise QmcError("MusicEx 定长字段不完整")
        mid = _read_utf16le(inner[0x0C : 0x0C + 60])
        filename = _read_utf16le(inner[0x48 : 0x48 + 100])
        return Footer(plen, None, "PcV2MusicEx", {"mid": mid, "media_filename": filename})

    body, size_bytes = tail[:-4], tail[-4:]
    plen = int.from_bytes(size_bytes, "little")
    if plen > _MAX_EKEY_LEN or plen == 0:
        return None  # 绝大多数普通音频会走到这里，属于正常情况
    if len(body) < plen:
        raise QmcError("PC 版尾部声明的 ekey 长度超过实际数据")
    raw = body[len(body) - plen :]
    cut = raw.find(b"\x00")
    if cut != -1:
        raw = raw[:cut]
    if not raw or not _is_b64(raw):
        # 不像密钥就当成「没有尾部」。这里宁可回落到静态密钥再靠嗅探报错，
        # 也不要凭空按一个随机 u32 把音频尾巴裁掉。
        return None
    return Footer(plen + 4, raw.decode("latin-1"), "PcV1Legacy")


# --------------------------------------------------------------------------- #
# 输出格式嗅探
# --------------------------------------------------------------------------- #

# ncm.guess_format 只认 ncm 会产出的四种，QMC 还可能解出 m4a / ape / wma / aac
_EXTRA_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MAC ", "ape"),
    (b"\x30\x26\xb2\x75", "wma"),  # ASF 头
    (b"\xff\xf1", "aac"),  # ADTS
    (b"\xff\xf9", "aac"),
)


def sniff_format(head: bytes) -> str | None:
    """从解出来的头 4 KiB 判断容器格式。"""
    found = guess_format(head)
    if found:
        return found
    if head[4:8] == b"ftyp":
        return "m4a"
    for magic, name in _EXTRA_MAGIC:
        if head.startswith(magic):
            return name
    return None


# --------------------------------------------------------------------------- #
# 对外：解密 / 探测
# --------------------------------------------------------------------------- #


def probe(src: str | Path) -> QmcInfo:
    """只看尾部与后缀，不解音频 —— 用来判断「这个文件能不能离线解」。"""
    src = Path(src)
    if not src.is_file():
        raise QmcError(f"文件不存在：{src}")
    size = src.stat().st_size
    if size == 0:
        raise QmcError(f"文件是空的：{src}")
    with src.open("rb") as fp:
        fp.seek(max(0, size - _TAIL_PROBE))
        footer = parse_footer(fp.read())
    info = QmcInfo(file=src.name, ext=src.suffix.lstrip(".").lower())
    if footer is None:
        info.footer = "none(v1)"
        info.cipher = "static"
        info.audio_size = size
        return info
    info.footer = footer.kind
    info.ekey_len = len(footer.ekey or "")
    info.audio_size = size - footer.size
    info.needs_ekey = footer.ekey is None
    info.cipher = "?" if footer.ekey is None else "embedded"
    return info


def decrypt(
    src: str | Path,
    out_dir: str | Path | None = None,
    *,
    ekey: str | None = None,
    overwrite: bool = True,
) -> tuple[Path, QmcInfo]:
    """解出一个 QMC 文件，返回 (输出路径, 信息)。

    ``ekey`` 只在「尾部没有内嵌密钥」（``STag`` / ``MusicEx``）时才需要 —— 那种文件的
    密钥在腾讯服务器上，离线拿不到。给了它也不会覆盖文件内嵌的密钥（内嵌的更可信）。
    """
    src = Path(src)
    if not src.is_file():
        raise QmcError(f"文件不存在：{src}")

    size = src.stat().st_size
    if size == 0:
        raise QmcError(f"文件是空的：{src}")

    info = QmcInfo(file=src.name, ext=src.suffix.lstrip(".").lower())

    with src.open("rb") as fp:
        fp.seek(max(0, size - _TAIL_PROBE))
        footer: Footer | None
        footer_error: str | None = None
        try:
            footer = parse_footer(fp.read())
        except QmcError as exc:
            # 尾部坏了就当没有：下面还有 v1 兜底，不至于整盘拒掉。
            # 但要把原始原因记下来 —— 否则 v1 兜底解出噪声时，
            # 用户只会看到「密钥可能不对」，而真凶其实是尾部解析失败。
            footer = None
            footer_error = str(exc)

        audio_len = size - (footer.size if footer else 0)
        if audio_len <= 0:
            raise QmcError("尾部长度比文件还大，可能不是 QMC 文件")
        info.audio_size = audio_len

        cipher = _pick_cipher(footer, ekey, info)

        # 先用头 1 MiB 嗅探格式：QMC 没有魔数，只有解完才知道里面是什么
        fp.seek(0)
        head_len = min(_COPY_BLOCK, audio_len)
        raw_head = fp.read(head_len)
        plain_head = cipher.decrypt(raw_head, 0) if cipher else qmc1_transform(raw_head, 0)
        fmt = sniff_format(plain_head)
        if fmt is None:
            hint = _EXT_HINT.get(info.ext)
            raise QmcError(
                "解密结果不像任何一种音频"
                + (f"（按后缀猜是 {hint}，但这不可信）" if hint else "")
                + "：密钥可能不对，或文件本身已损坏"
                + (f"\n尾部也解析失败：{footer_error}" if footer_error else "")
            )
        info.fmt = fmt

        out_dir = Path(out_dir) if out_dir else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{src.stem}.{fmt}"
        tmp = out.with_name(out.name + ".part")
        if out.exists() and not overwrite:
            raise QmcError(f"输出已存在：{out}")

        fp.seek(0)
        pos = 0
        try:
            with tmp.open("wb") as dst:
                while pos < audio_len:
                    chunk = fp.read(min(_COPY_BLOCK, audio_len - pos))
                    if not chunk:
                        raise QmcError("读取源文件时提前结束")
                    dst.write(cipher.decrypt(chunk, pos) if cipher else qmc1_transform(chunk, pos))
                    pos += len(chunk)
            if pos != audio_len:
                raise QmcError(f"解出 {pos} 字节，与预期的 {audio_len} 不一致")
            tmp.replace(out)  # 先写 .part 再原子改名，失败不留半截文件
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    return out, info


def _pick_cipher(
    footer: Footer | None, ekey: str | None, info: QmcInfo
) -> _MapCipher | _RC4Cipher | None:
    """决定用哪个密码；返回 ``None`` 表示走 v1 静态密钥。"""
    if footer is None:
        info.footer = "none(v1)"
        info.cipher = "static"
        return None

    info.footer = footer.kind
    key_str = footer.ekey or ekey
    info.ekey_len = len(footer.ekey or "")
    if key_str is None:
        info.needs_ekey = True
        raise QmcError(
            f"这个文件（{footer.kind}）没有内嵌 EKey，密钥在服务器上，离线解不了。\n"
            "如果你有密钥，用 --ekey <base64> 指定。"
        )
    master = ekey_decrypt(key_str)
    info.key_len = len(master)
    cipher = _make_cipher(master)
    info.cipher = cipher.name
    return cipher
