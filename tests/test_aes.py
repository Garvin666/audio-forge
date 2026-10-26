"""AES-128 实现自测：FIPS-197 官方向量 + 补位边界。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audioforge.aes import (  # noqa: E402
    AES128ECB,
    SBOX,
    pkcs7_pad,
    pkcs7_unpad,
)

# FIPS-197 附录 C.1
FIPS_C1_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_C1_PT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_C1_CT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

# FIPS-197 附录 B（示例向量）
FIPS_B_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
FIPS_B_PT = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
FIPS_B_CT = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")


class TestSbox(unittest.TestCase):
    def test_known_entries(self):
        # 抽查 FIPS-197 Table 4 的几个位置，覆盖生成算法各分支
        for idx, want in ((0x00, 0x63), (0x01, 0x7C), (0x53, 0xED), (0xFF, 0x16)):
            self.assertEqual(SBOX[idx], want, f"SBOX[{idx:#04x}]")

    def test_bijective(self):
        self.assertEqual(len(set(SBOX)), 256)


class TestVectors(unittest.TestCase):
    def test_decrypt_fips_c1(self):
        self.assertEqual(AES128ECB(FIPS_C1_KEY).decrypt(FIPS_C1_CT), FIPS_C1_PT)

    def test_decrypt_fips_b(self):
        self.assertEqual(AES128ECB(FIPS_B_KEY).decrypt(FIPS_B_CT), FIPS_B_PT)

    def test_encrypt_matches_vector(self):
        self.assertEqual(AES128ECB(FIPS_C1_KEY).encrypt(FIPS_C1_PT), FIPS_C1_CT)

    def test_roundtrip_multiblock(self):
        payload = bytes(range(256)) * 3
        aes = AES128ECB(FIPS_B_KEY)
        self.assertEqual(aes.decrypt(aes.encrypt(payload)), payload)

    def test_bad_key_length(self):
        with self.assertRaises(ValueError):
            AES128ECB(b"short")

    def test_bad_block_length(self):
        aes = AES128ECB(FIPS_C1_KEY)
        with self.assertRaises(ValueError):
            aes.decrypt(b"\x00" * 15)


class TestPadding(unittest.TestCase):
    def test_pad_unpad_roundtrip(self):
        for n in (0, 1, 15, 16, 17, 100):
            data = b"x" * n
            padded = pkcs7_pad(data)
            self.assertEqual(len(padded) % 16, 0)
            self.assertEqual(pkcs7_unpad(padded), data)

    def test_full_block_gets_extra_block(self):
        padded = pkcs7_pad(b"y" * 16)
        self.assertEqual(len(padded), 32)
        self.assertEqual(padded[-1], 16)

    def test_lenient_mode_keeps_nonstandard_tail(self):
        # NCM 实测出现过非标准补位，宽松模式必须原样返回而不是抛错
        weird = b"abc\x00\x00"
        self.assertEqual(pkcs7_unpad(weird), weird)

    def test_strict_mode_raises(self):
        with self.assertRaises(ValueError):
            pkcs7_unpad(b"abc\x00\x00", strict=True)


if __name__ == "__main__":
    unittest.main()
