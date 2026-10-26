"""QMC 解密自测：静态变换边界、EKey、两种流密码、四种尾部、拒绝非 QMC。

约定和 `test_ncm.py` 一样：**不存样本文件**，样本按算法现场造出来。
这里自带一个 TEA 加密器（qmc.py 只带解密），用来把 master key 捏成 EKey；
它是解密器的严格逆过程，往返对得上才算数。
"""

from __future__ import annotations

import base64
import hashlib
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge import qmc  # noqa: E402

SALT = bytes(range(0x40, 0x4A))  # 10 字节
KEY = qmc.QMC1_KEY


def _tea_encrypt_block(block: int, k: tuple[int, int, int, int]) -> int:
    y = (block >> 32) & 0xFFFFFFFF
    z = block & 0xFFFFFFFF
    s = 0
    for _ in range(16):
        s = (s + 0x9E3779B9) & 0xFFFFFFFF
        y = (y + qmc._tea_round(z, s, k[0], k[1])) & 0xFFFFFFFF
        z = (z + qmc._tea_round(y, s, k[2], k[3])) & 0xFFFFFFFF
    return (y << 32) | z


def tea_encrypt(plain: bytes, key16: bytes, salt: bytes = SALT) -> bytes:
    """对应 qmc._tea_cbc_decrypt 的逆：pad(1) + salt(2) + body + 尾部 7 个 0。"""
    k = struct.unpack(">IIII", key16)
    out_len = 10 + len(plain)
    pad = (8 - (out_len & 7)) & 7
    header_len = 1 + pad + 2
    out_len += pad

    header = bytearray(16)
    header[:header_len] = salt[:header_len]
    header[0] = (header[0] & ~7) | pad
    take = min(16 - header_len, len(plain))
    header[header_len : header_len + take] = plain[:take]
    rest = plain[take:]

    iv1 = iv2 = 0

    def enc(block: bytes) -> bytes:
        nonlocal iv1, iv2
        b = int.from_bytes(block, "big")
        iv2_next = (b ^ iv1) & 0xFFFFFFFFFFFFFFFF
        c = (_tea_encrypt_block(iv2_next, k) ^ iv2) & 0xFFFFFFFFFFFFFFFF
        iv1 = c
        iv2 = iv2_next
        return c.to_bytes(8, "big")

    buf = bytearray(out_len)
    buf[0:8] = enc(bytes(header[0:8]))
    buf[8:16] = enc(bytes(header[8:16]))
    pos = 16
    while len(rest) >= 8:
        buf[pos : pos + 8] = enc(rest[:8])
        rest = rest[8:]
        pos += 8
    if rest:
        buf[pos : pos + 8] = enc(rest + b"\x00" * (8 - len(rest)))
    return bytes(buf[:out_len])


def make_ekey(master: bytes, *, v2: bool = False, salt: bytes = SALT) -> str:
    """把 master key 包成 QQ 音乐的 EKey 字符串（V1 形式，可选再套两层 V2）。"""
    header, body = master[:8], master[8:]
    tea_key = bytearray()
    for sk, hk in zip(qmc._SIMPLE_KEY, header):
        tea_key.append(sk)
        tea_key.append(hk)
    inner = base64.b64encode(header + tea_encrypt(body, bytes(tea_key), salt))
    if not v2:
        return inner.decode("ascii")
    payload = tea_encrypt(inner, qmc._EKEY_V2_KEY2, salt)
    payload = tea_encrypt(payload, qmc._EKEY_V2_KEY1, salt)
    return (qmc._EKEY_V2_PREFIX + base64.b64encode(payload)).decode("ascii")


def make_key(size: int) -> bytes:
    """确定性、无 0 字节的假 master key（0 会被 RC4 的 hash 跳过，不适合当样本）。"""
    return bytes(((i * 37) % 255) + 1 for i in range(size))


def make_plain(size: int = 4096, magic: bytes = b"fLaC") -> bytes:
    rnd = random.Random(20261026)
    return magic + bytes(rnd.randrange(256) for _ in range(max(0, size - len(magic))))


def pcv1_footer(ekey: str) -> bytes:
    raw = ekey.encode("ascii")
    return raw + len(raw).to_bytes(4, "little")


def qtag_footer(ekey: str, rid: int = 987654) -> bytes:
    csv = f"{ekey},{rid},2".encode("ascii")
    return csv + len(csv).to_bytes(4, "big") + b"QTag"


def stag_footer(rid: int = 987654, mid: str = "001abc") -> bytes:
    csv = f"{rid},2,{mid}".encode("ascii")
    return csv + len(csv).to_bytes(4, "big") + b"STag"


def musicex_footer() -> bytes:
    inner = bytearray(0xC0 - 0x10)
    inner[12 : 12 + 6] = b"m\x00i\x00d\x00"
    inner[72 : 72 + 12] = b"a\x00.\x00f\x00l\x00a\x00c\x00"
    return bytes(inner) + (0xC0).to_bytes(4, "little") + (1).to_bytes(4, "little") + b"musicex\x00"


def encrypt_payload(plain: bytes, master: bytes) -> bytes:
    """按 master key 选密码并加密（流密码 XOR，同一个函数即可逆）。"""
    return qmc._make_cipher(master).decrypt(plain, 0)


# --------------------------------------------------------------------------- #
# 静态变换
# --------------------------------------------------------------------------- #


class TestTransform(unittest.TestCase):
    def test_is_involution(self):
        # 异或对合：同一个函数既加密也解密，这是整条链路能自洽的前提
        blob = make_plain(3000)
        self.assertEqual(qmc.qmc1_transform(qmc.qmc1_transform(blob, 0), 0), blob)

    def test_offset_zero_uses_key_in_order(self):
        self.assertEqual(qmc.qmc1_transform(b"\x00" * 128, 0), KEY)

    def test_boundary_semantics_at_0x7fff(self):
        # 偏移 0x7FFF 用 key[127]；偏移 0x8000 回绕到 key[1]（是 0x7FFF 不是 0x8000）
        self.assertEqual(qmc.qmc1_transform(b"\x00", 0x7FFF), bytes([KEY[127]]))
        self.assertEqual(qmc.qmc1_transform(b"\x00", 0x8000), bytes([KEY[1]]))
        self.assertNotEqual(qmc.qmc1_transform(b"\x00", 0x8000), bytes([KEY[0]]))

    def test_static_matches_official_vectors(self):
        """黄金向量来自官方 cipher_static.go 的 ``box[(o*o+27)&0xFF]``。

        本地实现是「128 字节表 + 0x7FFF 回绕」，官方是「平方取模查 256 表」，
        两种写法必须逐字节等价 —— 这里的 sha256 覆盖 0..0x1FFFF（含两次回绕）。
        """
        got = qmc.qmc1_transform(bytes(0x20000), 0)
        self.assertEqual(
            hashlib.sha256(got).hexdigest(),
            "b0ee2fec3f755f6a459683c70cd84a5401fab07a8f5d13717076d71237617481",
        )
        for off, want in (
            (0x7FFE, 0xD6),
            (0x7FFF, 0x4A),
            (0x8000, 0x4A),
            (0x8001, 0xD6),
            (0xFFFF, 0x4A),
            (0x10000, 0xD6),
        ):
            self.assertEqual(qmc.qmc1_transform(b"\x00", off), bytes([want]), f"offset={off:#x}")

    def test_stream_rotates_with_offset(self):
        self.assertEqual(qmc.qmc1_transform(b"\x00" * 128, 1), KEY[1:] + KEY[:1])
        # 0x7FFF 处**不是**简单轮转：下标从 127 跳回 1（跳过 key[0]），
        # 因为 0x7FFF 不是 128 的整数倍。这条是这个格式最容易写错的地方。
        self.assertEqual(
            qmc.qmc1_transform(b"\x00" * 4, 0x7FFF),
            bytes([KEY[127], KEY[1], KEY[2], KEY[3]]),
        )

    def test_chunked_equals_oneshot(self):
        # 流式写盘的等价性：任意切块位置都必须与一次性变换一致
        blob = make_plain(0x9000)
        whole = qmc.qmc1_transform(blob, 0)
        for step in (1, 7, 128, 1000, 0x7FFF):
            parts = b"".join(
                qmc.qmc1_transform(blob[i : i + step], i) for i in range(0, len(blob), step)
            )
            self.assertEqual(parts, whole, f"step={step}")

    def test_nonzero_start_offset(self):
        # 跨 0x7FFF 边界的切块：从 0x7FF0 起、切 32 字节，必须与整段解的同一区间一致
        blob = make_plain(0x8100)
        whole = qmc.qmc1_transform(blob, 0)
        for start in (0x7FF0, 0x7FFF, 0x8000):
            self.assertEqual(
                qmc.qmc1_transform(blob[start : start + 32], start),
                whole[start : start + 32],
                f"start={start:#x}",
            )


# --------------------------------------------------------------------------- #
# EKey 与流密码
# --------------------------------------------------------------------------- #


class TestEkey(unittest.TestCase):
    def test_simple_key_shape(self):
        self.assertEqual(len(qmc._SIMPLE_KEY), 8)
        # 与官方 simpleMakeKey(106, 8) 逐字节一致
        self.assertEqual(qmc._SIMPLE_KEY.hex(), "695646382b20150b")

    def test_tencent_tea_official_vector(self):
        """tc_tea crate 0.1.4 的官方测试向量（外部锚点，不是自证）。

        明文 01..08 + key "12345678ABCDEFGH" → 24 字节密文；
        解完应剥掉 pad/salt/尾部 0，只剩那 8 字节。
        """
        ct = bytes.fromhex("91095162e3f5b6dc6b414b50d1a5b84ec50d0c1b1196fd3c")
        self.assertEqual(qmc._tea_cbc_decrypt(ct, b"12345678ABCDEFGH"), bytes(range(1, 9)))

    def test_v1_roundtrip(self):
        for size in (16, 128, 300):
            master = make_key(size)
            self.assertEqual(qmc.ekey_decrypt(make_ekey(master)), master)

    def test_v2_roundtrip(self):
        # V2 = 在 V1 的 base64 文本外面再套两层固定密钥的 TEA
        for size in (16, 128):
            master = make_key(size)
            ekey = make_ekey(master, v2=True)
            self.assertTrue(ekey.startswith(qmc._EKEY_V2_PREFIX.decode("ascii")))
            self.assertEqual(qmc.ekey_decrypt(ekey), master)

    def test_accepts_bytes(self):
        master = bytes(range(1, 33))
        self.assertEqual(qmc.ekey_decrypt(make_ekey(master).encode("ascii")), master)

    def test_too_short_rejected(self):
        with self.assertRaises(qmc.QmcError):
            qmc.ekey_decrypt("QUJD")

    def test_garbage_rejected(self):
        with self.assertRaises(qmc.QmcError):
            qmc.ekey_decrypt("abcdefghijklmnopqrstuvwxyz012345")


class TestCiphers(unittest.TestCase):
    def test_short_key_picks_map(self):
        self.assertEqual(qmc._make_cipher(b"\x01" * 300).name, "map")

    def test_long_key_picks_rc4(self):
        self.assertEqual(qmc._make_cipher(b"\x01" * 301).name, "rc4")

    def test_empty_key_rejected(self):
        with self.assertRaises(qmc.QmcError):
            qmc._make_cipher(b"")

    def test_map_is_involution(self):
        cipher = qmc._MapCipher(bytes(range(1, 65)))
        blob = make_plain(5000)
        self.assertEqual(cipher.decrypt(cipher.decrypt(blob, 0), 0), blob)

    def test_map_matches_official_vectors(self):
        """黄金向量来自官方 cipher_map.go：idx=(o*o+71214)%len，o 超过 0x7FFF 才取模。

        key 长度特意取 **96**（不能整除 256）。早先的实现先把 key 压成 128 字节，
        对 128/256 长度恰好等价、对 96/200/300 从偏移 128 起全错 ——
        而真实样本多半是 128/256，所以这个 bug 能一路「绿灯」到线上。
        """
        key = bytes((i * 37 + 11) & 0xFF for i in range(96))
        cipher = qmc._MapCipher(key)
        self.assertEqual(
            hashlib.sha256(cipher.decrypt(bytes(0x10000))).hexdigest(),
            "9477177c638fda8f8736ace0ad80e8332a87f968f5c6370c7a2d05523c305cd9",
        )
        cases = {
            0x0000: 0x54,
            0x007F: 0xBE,
            0x0080: 0xEC,
            0x0081: 0xB6,
            0x00FF: 0xB6,
            0x7FFE: 0x42,
            0x7FFF: 0xBE,
            0x8000: 0xBE,
            0x8001: 0x43,
        }
        for off, want in cases.items():
            self.assertEqual(cipher.decrypt(b"\x00", off), bytes([want]), f"offset={off:#x}")

    def test_rc4_is_involution(self):
        cipher = qmc._RC4Cipher(bytes(range(1, 200)) * 3)
        blob = make_plain(0x3000)
        self.assertEqual(cipher.decrypt(cipher.decrypt(blob, 0), 0), blob)

    def test_rc4_matches_official_vectors(self):
        """黄金向量来自官方 cipher_rc4.go：skip = idx % len(key)，**不是** idx & 0x1FF。

        key 长度取 384（不是 512）。``& 0x1FF`` 只在长度为 2 的幂时与 ``% n`` 等价，
        所以 512 字节的真样本永远暴露不了这个错 —— 必须用非 2 的幂的长度锁住。
        """
        key = bytes((i * 91 + 7) & 0xFF or 1 for i in range(384))
        got = qmc._RC4Cipher(key).decrypt(bytes(0x4000))
        self.assertEqual(
            hashlib.sha256(got).hexdigest(),
            "9c2da8edeb7be6ba16ef823d67fa267242555fe1e6f91b149da6358fa99d8d70",
        )
        self.assertEqual(got[:16].hex(), "d3db92324367e5a49ace69ea9f0bbe16")
        # 第二个 5120 字节段的开头：skip 一错，这里立刻变样
        self.assertEqual(got[0x1400:0x1408].hex(), "99631d25c9630e15")

        key301 = bytes((i * 53 + 3) & 0xFF or 2 for i in range(301))
        got301 = qmc._RC4Cipher(key301).decrypt(bytes(0x1400))
        self.assertEqual(
            hashlib.sha256(got301).hexdigest(),
            "f4ce68e4a96f9802205defff9b6f9bf5f1184d7960ba3167a45075dfc9fa08e1",
        )

    def test_rc4_chunked_equals_oneshot(self):
        # RC4 的分段逻辑最容易切错：必须在 0x80 / 0x1400 边界附近都对
        cipher = qmc._RC4Cipher(bytes(range(1, 200)) * 3)
        blob = make_plain(0x5000)
        whole = cipher.decrypt(blob, 0)
        for step in (0x40, 0x80, 0x1400, 777):
            parts = b"".join(
                cipher.decrypt(blob[i : i + step], i) for i in range(0, len(blob), step)
            )
            self.assertEqual(parts, whole, f"step={step}")


# --------------------------------------------------------------------------- #
# 尾部解析
# --------------------------------------------------------------------------- #


class TestFooter(unittest.TestCase):
    def test_qtag(self):
        f = qmc.parse_footer((b"x" * 100) + qtag_footer("QUJD", 42))
        self.assertEqual(f.kind, "QTag")
        self.assertEqual(f.ekey, "QUJD")
        self.assertEqual(f.extra["resource_id"], 42)

    def test_stag_has_no_ekey(self):
        f = qmc.parse_footer(stag_footer())
        self.assertEqual(f.kind, "STag")
        self.assertIsNone(f.ekey)
        self.assertEqual(f.extra["media_mid"], "001abc")

    def test_musicex(self):
        f = qmc.parse_footer(musicex_footer())
        self.assertEqual(f.kind, "PcV2MusicEx")
        self.assertIsNone(f.ekey)
        self.assertEqual(f.extra["mid"], "mid")
        self.assertEqual(f.extra["media_filename"], "a.flac")

    def test_pc_legacy(self):
        f = qmc.parse_footer((b"x" * 50) + pcv1_footer("QUJDRA=="))
        self.assertEqual(f.kind, "PcV1Legacy")
        self.assertEqual(f.ekey, "QUJDRA==")
        self.assertEqual(f.size, len("QUJDRA==") + 4)

    def test_qtag_third_field_is_not_a_version(self):
        """官方把第三段叫 rawMetaExtra2（atoi 读的整数），**不是**版本号。

        早先的实现要求它必须等于 "2"，这是凭空加出来的约束：换一个客户端版本
        就整文件判死。这条用例锁住「别的取值也必须能解析」。
        """
        csv = b"QUJD,42,7"
        tail = csv + len(csv).to_bytes(4, "big") + b"QTag"
        f = qmc.parse_footer(tail)
        self.assertEqual((f.kind, f.ekey, f.extra["extra2"]), ("QTag", "QUJD", 7))

    def test_qtag_non_numeric_fields_rejected(self):
        for csv in (b"QUJD,x,2", b"QUJD,42,y"):
            tail = csv + len(csv).to_bytes(4, "big") + b"QTag"
            with self.assertRaises(qmc.QmcError):
                qmc.parse_footer(tail)

    def test_stag_payload_shape_not_enforced(self):
        """STag 里没有密钥，正文格式随客户端变 —— 只按长度裁，不做正文校验。"""
        csv = b"001abc,2"
        tail = csv + len(csv).to_bytes(4, "big") + b"STag"
        f = qmc.parse_footer(tail)
        self.assertEqual((f.kind, f.ekey), ("STag", None))
        self.assertEqual(f.size, len(csv) + 8)

    def test_musicex_larger_tag_accepted(self):
        # 官方只要求 TagSize >= 0xC0（未来可能加长），不是必须等于
        size = 0xC0 + 0x10
        inner = bytearray(size - 0x10)
        inner[12:18] = "mid".encode("utf-16-le")
        name = "a.mgg".encode("utf-16-le") + b"\x00\x00"
        inner[72 : 72 + len(name)] = name
        tail = (
            bytes(inner) + size.to_bytes(4, "little") + (1).to_bytes(4, "little") + b"musicex\x00"
        )
        f = qmc.parse_footer(tail)
        self.assertEqual(
            (f.kind, f.size, f.extra["media_filename"]), ("PcV2MusicEx", size, "a.mgg")
        )

    def test_plain_file_has_no_footer(self):
        # 普通 flac 的末尾 4 字节几乎必然不是合法长度，必须判成「无尾部」
        self.assertIsNone(qmc.parse_footer(make_plain(2048)[-1024:]))

    def test_non_base64_ekey_rejected(self):
        csv = "密钥不是base64,42,2".encode()
        tail = csv + len(csv).to_bytes(4, "big") + b"QTag"
        with self.assertRaises(qmc.QmcError):
            qmc.parse_footer(tail)


# --------------------------------------------------------------------------- #
# 整文件
# --------------------------------------------------------------------------- #


class TestDecrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.plain = make_plain(8192)

    def _write(self, name: str, body: bytes, footer: bytes = b"") -> Path:
        p = self.dir / name
        p.write_bytes(body + footer)
        return p

    def test_v1_static(self):
        # 没有尾部 = 老格式，整文件静态密钥
        p = self._write("a.qmcflac", qmc.qmc1_transform(self.plain, 0))
        out, info = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)
        self.assertEqual((info.footer, info.cipher, info.fmt), ("none(v1)", "static", "flac"))
        self.assertEqual(out.name, "a.flac")

    def test_v2_map_via_pc_footer(self):
        master = bytes(range(1, 129))
        body = encrypt_payload(self.plain, master)
        p = self._write("b.mflac", body, pcv1_footer(make_ekey(master)))
        out, info = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)
        self.assertEqual((info.footer, info.cipher, info.key_len), ("PcV1Legacy", "map", 128))

    def test_v2_rc4_via_qtag_footer(self):
        master = bytes(range(1, 200)) * 3  # 597 字节 -> rc4
        body = encrypt_payload(self.plain, master)
        p = self._write("c.mgg", body, qtag_footer(make_ekey(master)))
        out, info = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)
        self.assertEqual((info.footer, info.cipher), ("QTag", "rc4"))

    def test_v2_ekey_form(self):
        master = bytes(range(1, 65))
        body = encrypt_payload(self.plain, master)
        p = self._write("d.mflac", body, pcv1_footer(make_ekey(master, v2=True)))
        out, _ = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)

    def test_legacy_key_longer_than_old_cap(self):
        """PC 版旧格式的密钥长度上限是 0xFFFF，不是 0x500。

        旧实现写死 1280：超过就判成「没有尾部」，于是回落到静态密钥，
        解出整首噪音却照样 `[OK]` —— 这条用例锁住「长 key 必须真能解」。
        """
        master = bytes(((i * 71) % 255) + 1 for i in range(2000))
        ekey = make_ekey(master)
        self.assertGreater(len(ekey), 1280)
        body = encrypt_payload(self.plain, master)
        p = self._write("big.mflac", body, pcv1_footer(ekey))
        out, info = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)
        self.assertEqual((info.footer, info.ekey_len), ("PcV1Legacy", len(ekey)))

    def test_footer_is_trimmed(self):
        # 尾部字节不能混进输出
        master = bytes(range(1, 65))
        body = encrypt_payload(self.plain, master)
        p = self._write("e.mflac", body, pcv1_footer(make_ekey(master)))
        out, info = qmc.decrypt(p, self.out)
        self.assertEqual(len(out.read_bytes()), len(self.plain))
        self.assertEqual(info.audio_size, len(self.plain))

    def test_stag_needs_ekey(self):
        p = self._write("f.mflac", encrypt_payload(self.plain, bytes(range(1, 65))), stag_footer())
        with self.assertRaises(qmc.QmcError) as ctx:
            qmc.decrypt(p, self.out)
        self.assertIn("没有内嵌 EKey", str(ctx.exception))
        self.assertEqual(list(self.out.iterdir()) if self.out.exists() else [], [])

    def test_stag_with_manual_ekey(self):
        master = bytes(range(1, 65))
        p = self._write("g.mflac", encrypt_payload(self.plain, master), stag_footer())
        out, _ = qmc.decrypt(p, self.out, ekey=make_ekey(master))
        self.assertEqual(out.read_bytes(), self.plain)

    def test_non_audio_rejected(self):
        # 普通 flac 被当 QMC 硬解：结果一定不是音频，必须报错而不是写出垃圾
        p = self._write("h.qmc0", self.plain)
        with self.assertRaises(qmc.QmcError) as ctx:
            qmc.decrypt(p, self.out)
        self.assertIn("不像任何一种音频", str(ctx.exception))
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_wrong_ekey_rejected(self):
        master = bytes(range(1, 65))
        body = encrypt_payload(self.plain, master)
        p = self._write("i.mflac", body, pcv1_footer(make_ekey(bytes(range(2, 66)))))
        with self.assertRaises(qmc.QmcError):
            qmc.decrypt(p, self.out)

    def test_overwrite_false(self):
        p = self._write("j.qmcflac", qmc.qmc1_transform(self.plain, 0))
        qmc.decrypt(p, self.out)
        with self.assertRaises(qmc.QmcError) as ctx:
            qmc.decrypt(p, self.out, overwrite=False)
        self.assertIn("已存在", str(ctx.exception))

    def test_no_part_file_left(self):
        p = self._write("k.qmcflac", qmc.qmc1_transform(self.plain, 0))
        qmc.decrypt(p, self.out)
        self.assertEqual([x.name for x in self.out.iterdir()], ["k.flac"])

    def test_empty_file_rejected(self):
        p = self.dir / "empty.qmc0"
        p.write_bytes(b"")
        with self.assertRaises(qmc.QmcError):
            qmc.decrypt(p, self.out)

    def test_missing_file_rejected(self):
        with self.assertRaises(qmc.QmcError):
            qmc.decrypt(self.dir / "nope.mflac", self.out)

    def test_probe_does_not_decrypt(self):
        p = self._write(
            "l.mflac",
            encrypt_payload(self.plain, bytes(range(1, 65))),
            pcv1_footer(make_ekey(bytes(range(1, 65)))),
        )
        info = qmc.probe(p)
        self.assertEqual(info.footer, "PcV1Legacy")
        self.assertFalse(info.needs_ekey)
        self.assertTrue(info.ekey_len > 0)
        self.assertIsNone(info.fmt, "probe 不解音频，不该报格式")


class TestLongTail(unittest.TestCase):
    """尾部探测窗口必须装得下最长可能的 ekey。

    回归自一个真 bug：``_TAIL_PROBE`` 原本是 1024，而 V2 encv2 形式的 ekey 可以
    长到一千多字符 —— 尾部被截断后 ``parse_footer`` 报「QTag 声明的长度超过实际
    数据」，``decrypt`` 又把该错误吞掉回落到 v1 静态密钥，最后给用户一句
    「密钥可能不对」，把「窗口太小」误诊成「密钥错」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.plain = make_plain(8192)

    def test_probe_window_covers_max_ekey(self):
        # 结构不变量：窗口至少要能放下「1280 字符 ekey + ",<id>,2" + 8 字节头尾」
        self.assertGreater(qmc._TAIL_PROBE, qmc._MAX_EKEY_LEN + 64)

    def test_v2_long_ekey_roundtrip(self):
        # 512 字节 master 的 V2 ekey 有 1008 字符 -> QTag 尾部 1026 字节，超过旧窗口
        master = make_key(512)
        ekey = make_ekey(master, v2=True)
        self.assertGreater(len(ekey), 1000, "样本要足够长，短了测不出这个 bug")
        footer = qtag_footer(ekey)
        self.assertGreater(len(footer), 1024, "尾部必须超过旧的 1024 窗口才有意义")
        p = self.dir / "long.mflac"
        p.write_bytes(encrypt_payload(self.plain, master) + footer)

        info = qmc.probe(p)
        self.assertEqual((info.footer, info.needs_ekey), ("QTag", False))
        self.assertEqual(info.ekey_len, len(ekey))

        out, _ = qmc.decrypt(p, self.out)
        self.assertEqual(out.read_bytes(), self.plain)

    def test_footer_error_surfaces_when_fallback_also_fails(self):
        # 尾部坏 + v1 兜底也解不出音频时，报错里必须带上尾部的真实原因，
        # 不能只丢一句「密钥可能不对」让人去猜。
        p = self.dir / "broken.mflac"
        # 声明 9999 字节的 CSV，但文件里只有 2048 字节 -> 尾部解析必然失败
        p.write_bytes(bytes(range(256)) * 8 + b"x,1,2" + (9999).to_bytes(4, "big") + b"QTag")
        with self.assertRaises(qmc.QmcError) as ctx:
            qmc.decrypt(p, self.out)
        self.assertIn("不像任何一种音频", str(ctx.exception))
        self.assertIn("QTag 声明的长度超过实际数据", str(ctx.exception))


class TestSniff(unittest.TestCase):
    def test_known_magics(self):
        self.assertEqual(qmc.sniff_format(b"fLaC" + b"\x00" * 8), "flac")
        self.assertEqual(qmc.sniff_format(b"OggS" + b"\x00" * 8), "ogg")
        self.assertEqual(qmc.sniff_format(b"ID3\x04" + b"\x00" * 8), "mp3")
        self.assertEqual(qmc.sniff_format(b"\xff\xfb\x90" + b"\x00" * 8), "mp3")
        self.assertEqual(qmc.sniff_format(b"RIFF" + b"\x00" * 8), "wav")

    def test_qmc_only_formats(self):
        self.assertEqual(qmc.sniff_format(b"\x00\x00\x00\x20ftypM4A "), "m4a")
        self.assertEqual(qmc.sniff_format(b"MAC " + b"\x00" * 8), "ape")
        self.assertEqual(qmc.sniff_format(b"\x30\x26\xb2\x75" + b"\x00" * 8), "wma")
        self.assertEqual(qmc.sniff_format(b"\xff\xf1\x50" + b"\x00" * 8), "aac")

    def test_unknown(self):
        self.assertIsNone(qmc.sniff_format(b"\x01\x02\x03\x04" * 4))


class TestNames(unittest.TestCase):
    def test_extension_set(self):
        for name in ("a.mflac", "a.MGG", "a.qmc0", "a.qmcflac", "a.tkm", "a.mgg1"):
            self.assertTrue(qmc.is_qmc_name(name), name)
        for name in ("a.mp3", "a.ncm", "a.flac"):
            self.assertFalse(qmc.is_qmc_name(name), name)

    def test_bare_extension(self):
        self.assertTrue(qmc.is_qmc_name(".mflac"))
        self.assertTrue(qmc.is_qmc_name("mflac"))


if __name__ == "__main__":
    unittest.main()
