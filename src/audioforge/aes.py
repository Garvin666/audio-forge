"""AES-128 ECB 解密（纯 Python，无第三方依赖）。

只用于解 NCM 容器里的两段小型加密块（key 块、meta 块，通常各数百字节），
所以这里可读性优先于吞吐：S-box、密钥扩展、逆向轮函数全部显式实现，
不依赖 pycryptodome / cryptography。

音频流本身不经过 AES —— 它用的是 256 字节周期的 keystream XOR，见 ``ncm.py``。
"""

from __future__ import annotations

__all__ = ["AES128ECB", "pkcs7_unpad", "pkcs7_pad"]


# --------------------------------------------------------------------------- #
# GF(2^8) 与 S-box
# --------------------------------------------------------------------------- #

def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，既约多项式 0x11B（x^8 + x^4 + x^3 + x + 1）。"""
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return r


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_tables() -> tuple[tuple[int, ...], tuple[int, ...]]:
    """由 GF(2^8) 逆元 + 仿射变换生成 S-box 与逆 S-box。

    生成元取 3，用 exp/log 表求逆元（比逐个试乘快两个数量级）。
    """
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _gmul(x, 3)
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    sbox = [0] * 256
    for v in range(256):
        # v 的乘法逆元：0 约定为 0
        inv = 0 if v == 0 else exp[255 - log[v]]
        sbox[v] = (
            inv
            ^ _rotl8(inv, 1)
            ^ _rotl8(inv, 2)
            ^ _rotl8(inv, 3)
            ^ _rotl8(inv, 4)
            ^ 0x63
        )

    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return tuple(sbox), tuple(inv_sbox)


SBOX, INV_SBOX = _build_tables()

# 逆 MixColumns 用的乘数矩阵行：[0e, 0b, 0d, 09] 循环右移
_MUL9 = tuple(_gmul(i, 0x09) for i in range(256))
_MUL11 = tuple(_gmul(i, 0x0B) for i in range(256))
_MUL13 = tuple(_gmul(i, 0x0D) for i in range(256))
_MUL14 = tuple(_gmul(i, 0x0E) for i in range(256))
# 正向 MixColumns 用 02/03
_MUL2 = tuple(_gmul(i, 0x02) for i in range(256))
_MUL3 = tuple(_gmul(i, 0x03) for i in range(256))

RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


# --------------------------------------------------------------------------- #
# 轮函数
# --------------------------------------------------------------------------- #

def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展，返回 11 组轮密钥，每组 16 字节（列主序展平）。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 需要 16 字节密钥，收到 {len(key)}")
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                       # RotWord
            temp = [SBOX[b] for b in temp]                   # SubWord
            temp[0] ^= RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    # 44 字 → 11 组 16 字节
    rounds = []
    for r in range(11):
        flat: list[int] = []
        for w in words[r * 4:r * 4 + 4]:
            flat.extend(w)
        rounds.append(flat)
    return rounds


def _add_round_key(state: list[int], rk: list[int]) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _inv_shift_rows(state: list[int]) -> None:
    """列主序下，第 r 行循环右移 r 个字节。"""
    for r in (1, 2, 3):
        row = [state[r + 4 * c] for c in range(4)]
        for c in range(4):
            state[r + 4 * ((c + r) & 3)] = row[c]


def _inv_sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = INV_SBOX[state[i]]


def _inv_mix_columns(state: list[int]) -> None:
    for c in range(4):
        a0, a1, a2, a3 = state[4 * c:4 * c + 4]
        state[4 * c + 0] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
        state[4 * c + 1] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
        state[4 * c + 2] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
        state[4 * c + 3] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    if len(block) != 16:
        raise ValueError("AES 分组长度必须是 16 字节")
    state = list(block)
    _add_round_key(state, round_keys[10])
    for rnd in range(9, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, round_keys[0])
    return bytes(state)


# --- 正向轮函数：只用于自测与构造 fixture，工具本身不加密任何文件 --- #

def _shift_rows(state: list[int]) -> None:
    for r in (1, 2, 3):
        row = [state[r + 4 * c] for c in range(4)]
        for c in range(4):
            state[r + 4 * ((c - r) & 3)] = row[c]


def _sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = SBOX[state[i]]


def _mix_columns(state: list[int]) -> None:
    for c in range(4):
        a0, a1, a2, a3 = state[4 * c:4 * c + 4]
        state[4 * c + 0] = _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3
        state[4 * c + 1] = a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3
        state[4 * c + 2] = a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3]
        state[4 * c + 3] = _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3]


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    if len(block) != 16:
        raise ValueError("AES 分组长度必须是 16 字节")
    state = list(block)
    _add_round_key(state, round_keys[0])
    for rnd in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, round_keys[10])
    return bytes(state)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #

def pkcs7_unpad(data: bytes, strict: bool = False) -> bytes:
    """去 PKCS#7 补位。

    NCM 实际样本里出现过非标准补位，默认 ``strict=False`` 时按「末字节落在
    1..16 且补位字节全部相同」判断，不满足就原样返回，避免误切真实数据。
    """
    if not data:
        return data
    pad = data[-1]
    if not 1 <= pad <= 16 or pad > len(data):
        if strict:
            raise ValueError(f"PKCS#7 补位非法：末字节 {pad}")
        return data
    if data[-pad:] != bytes([pad]) * pad:
        if strict:
            raise ValueError("PKCS#7 补位内容不一致")
        return data
    return data[:-pad]


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    """按 PKCS#7 补位（满块时补一整块，与 AES 的约定一致）。"""
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


class AES128ECB:
    """AES-128 ECB 解密器。ECB 无 IV、无链接模式，逐 16 字节分组独立解密。"""

    __slots__ = ("_round_keys",)

    def __init__(self, key: bytes) -> None:
        self._round_keys = _expand_key(key)

    def decrypt(self, data: bytes) -> bytes:
        if len(data) % 16:
            raise ValueError(f"密文长度必须是 16 的整数倍，收到 {len(data)}")
        out = bytearray(len(data))
        rks = self._round_keys
        for off in range(0, len(data), 16):
            out[off:off + 16] = _decrypt_block(data[off:off + 16], rks)
        return bytes(out)

    def decrypt_unpadded(self, data: bytes) -> bytes:
        """解密并去掉补位（宽松模式）。"""
        return pkcs7_unpad(self.decrypt(data))

    def encrypt(self, data: bytes) -> bytes:
        """仅供测试与构造 fixture 使用；本工具不加密任何用户文件。"""
        if len(data) % 16:
            raise ValueError(f"明文长度必须是 16 的整数倍，收到 {len(data)}")
        out = bytearray(len(data))
        rks = self._round_keys
        for off in range(0, len(data), 16):
            out[off:off + 16] = _encrypt_block(data[off:off + 16], rks)
        return bytes(out)

    def encrypt_padded(self, data: bytes) -> bytes:
        return self.encrypt(pkcs7_pad(data))
