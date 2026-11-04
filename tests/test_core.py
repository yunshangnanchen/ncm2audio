# -*- coding: utf-8 -*-
"""ncm_core 单元测试：算法向量、容器解析、命名、批量转换、标签"""

import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ncm_core as c


# --------------------------------------------------------------------------
# 测试用 NCM 生成器（熵源全部来自被测实现，仅用于验证解析/解密闭环）
# --------------------------------------------------------------------------
def build_ncm(audio: bytes, meta=None, cover: bytes = b"", key: bytes = b"unit-test-key-256",
              frame_padding: int = 0) -> bytes:
    key_plain = c.KEY_PREFIX + key
    key_blob = c.aes_ecb_encrypt(c.CORE_KEY, c.pkcs7_pad(key_plain))
    key_seg = bytes(b ^ c.KEY_XOR for b in key_blob)

    out = bytearray()
    out += c.MAGIC
    out += b"\x01\x70"
    out += struct.pack("<I", len(key_seg)) + key_seg

    if meta is None:
        out += struct.pack("<I", 0)
    else:
        raw = (c.META_PREFIX + json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        enc = c.aes_ecb_encrypt(c.META_KEY, c.pkcs7_pad(raw))
        b64 = base64.b64encode(enc)
        seg = bytes(b ^ c.META_XOR for b in (c.META_PLAIN_PREFIX + b64))
        out += struct.pack("<I", len(seg)) + seg

    out += b"\x00\x00\x00\x00"                       # CRC32
    out += b"\x00"                                    # 封面版本
    frame_len = len(cover) + frame_padding
    out += struct.pack("<I", frame_len)
    out += struct.pack("<I", len(cover))
    out += cover
    out += b"\x00" * frame_padding
    out += c.xor_pattern(audio, c.keystream_pattern(key))
    return bytes(out)


META = {
    "musicName": "测试歌曲",
    "artist": [["测试歌手", "1"], ["合作歌手", "2"]],
    "album": "测试专辑",
    "bitrate": 320000,
    "duration": 223440,
    "format": "flac",
}


def make_minimal_flac(seconds: float = 10.0) -> bytes:
    """构造一个只含 STREAMINFO 的最小合法 FLAC，供标签写入测试使用"""
    sr, ch, bps = 44100, 2, 16
    total = int(sr * seconds)
    x = (sr << 44) | ((ch - 1) << 41) | ((bps - 1) << 36) | total
    si = (struct.pack(">HH", 4096, 4096) + b"\x00\x00\x00" + b"\x00\x00\x00"
          + x.to_bytes(8, "big") + b"\x00" * 16)
    return b"fLaC" + b"\x80" + b"\x00\x00\x22" + si


class CoreTest(unittest.TestCase):
    def test_selftest_vectors(self):
        self.assertTrue(c.selftest())

    def test_parse_and_roundtrip(self):
        audio = b"fLaC" + bytes(range(256)) * 40
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
        blob = build_ncm(audio, META, cover, key=b"k" * 32, frame_padding=0)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.ncm"
            p.write_bytes(blob)
            ncm = c.parse_ncm(p)
            self.assertEqual(ncm.audio_size, len(audio))
            self.assertEqual(ncm.audio_offset, len(blob) - len(audio))
            self.assertEqual(ncm.meta["musicName"], "测试歌曲")
            self.assertEqual(ncm.artists(), ["测试歌手", "合作歌手"])
            self.assertEqual(ncm.cover_ext(), "jpg")
            self.assertEqual(c.peek_audio(ncm, 4), b"fLaC")
            out = Path(td) / "out.flac"
            n = c.decrypt_audio_to_file(ncm, out)
            self.assertEqual(n, len(audio))
            self.assertEqual(out.read_bytes(), audio)

    def test_cover_frame_padding(self):
        """NetEase 3.x 预留的封面帧填充必须被跳过（否则音频错位成噪声）"""
        audio = b"ID3\x03\x00\x00" + b"\x22" * 5000
        blob = build_ncm(audio, META, b"\xff\xd8\xff\xdbcover", frame_padding=97)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pad.ncm"
            p.write_bytes(blob)
            ncm = c.parse_ncm(p)
            self.assertEqual(ncm.audio_size, len(audio))
            self.assertEqual(c.peek_audio(ncm, 3), b"ID3")

    def test_no_metadata(self):
        audio = b"OggS" + b"\x00" * 300
        blob = build_ncm(audio, None, b"", key=b"z" * 16)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "no-meta.ncm"
            p.write_bytes(blob)
            ncm = c.parse_ncm(p)
            self.assertIsNone(ncm.meta)
            self.assertEqual(c.guess_format(c.peek_audio(ncm, 8)), "ogg")

    def test_bad_magic(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.ncm"
            p.write_bytes(b"NOPE" + b"\x00" * 64)
            with self.assertRaises(c.NcmError):
                c.parse_ncm(p)

    def test_truncated(self):
        audio = b"fLaC" + b"\x00" * 100
        blob = build_ncm(audio, META, b"")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trunc.ncm"
            p.write_bytes(blob[:20])
            with self.assertRaises(c.NcmError):
                c.parse_ncm(p)

    def test_convert_naming_and_tags(self):
        audio = b"fLaC" + b"\x00" * 4000
        blob = build_ncm(audio, META, b"\x89PNG\r\n\x1a\n" + b"\x00" * 80)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "随便的名字.ncm"
            src.write_bytes(blob)
            res = c.convert(src)
            self.assertFalse(res.error, res.error)
            self.assertEqual(res.output.name, "测试歌手 & 合作歌手 - 测试歌曲.flac")
            self.assertTrue(res.output.exists())
            # 再转一次应跳过
            res2 = c.convert(src)
            self.assertTrue(res2.skipped)

    def test_batch_collision_gets_suffix(self):
        audio = b"fLaC" + b"\x00" * 1000
        blob = build_ncm(audio, META, b"")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "one.ncm").write_bytes(blob)
            (td / "two.ncm").write_bytes(blob)
            res = c.convert_many(c.collect_ncm_files([td]))
            names = [r.output.name for r in res]
            self.assertEqual(len(set(names)), 2, names)

    def test_safe_filename(self):
        self.assertEqual(c.safe_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(c.safe_filename("歌曲 - 名字"), "歌曲 - 名字")
        self.assertEqual(c.safe_filename("  ...  "), "未命名")
        self.assertEqual(c.safe_filename("CON"), "_CON")
        self.assertEqual(c.safe_filename("a\x00b"), "a_b")

    def test_format_helpers(self):
        self.assertEqual(c.format_duration(223.44), "03:43")
        self.assertEqual(c.format_duration(3725), "1:02:05")
        self.assertEqual(c.human_size(9179038), "8.75 MB")
        self.assertEqual(c.guess_format(b'ftypM4A'), "bin")
        self.assertEqual(c.guess_format(b'\x00\x00\x00\x20ftypM4A '), "m4a")
        self.assertEqual(c.guess_format(b"RIFF\x00\x00\x00\x00WAVE"), "wav")

    def test_collect(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "sub").mkdir()
            (td / "a.ncm").write_bytes(b"x")
            (td / "sub" / "B.NCM").write_bytes(b"x")
            (td / "c.mp3").write_bytes(b"x")
            files = c.collect_ncm_files([td])
            self.assertEqual(sorted(p.name.lower() for p in files), ["a.ncm", "b.ncm"])

    def test_real_ncm_if_present(self):
        real = [p for p in ROOT.glob("*.ncm")]
        if not real:
            self.skipTest("目录中没有真实 NCM 样本")
        ncm = c.parse_ncm(real[0])
        self.assertGreater(len(ncm.key), 8)
        head = c.peek_audio(ncm, 16)
        self.assertIn(c.guess_format(head), ("mp3", "flac", "m4a", "wav", "ogg", "bin"))

    def test_write_tags(self):
        try:
            import mutagen  # noqa: F401
        except ImportError:
            self.skipTest("未安装 mutagen")
        audio = make_minimal_flac()
        blob = build_ncm(audio, META, b"", key=b"tag-key-0000000000")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "tag.ncm"
            src.write_bytes(blob)
            res = c.convert(src)
            self.assertTrue(res.output.exists())
            from mutagen.flac import FLAC
            f = FLAC(str(res.output))
            self.assertEqual(str(f["title"][0]), "测试歌曲")
            self.assertEqual(f["artist"], ["测试歌手", "合作歌手"])
            self.assertEqual(str(f["album"][0]), "测试专辑")

    def test_ncm_stream(self):
        """惰性解密流：任意偏移读出的字节必须与全量解密一致"""
        import io as _io
        audio = bytes(range(256)) * 64
        blob = build_ncm(audio, META, b"")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.ncm"
            p.write_bytes(blob)
            ncm = c.parse_ncm(p)
            full = audio  # 解密的期望输出就是明文
            s = c.NcmStream(ncm)
            self.assertEqual(s.read(1000), full[:1000])
            self.assertEqual(s.tell(), 1000)
            s.seek(5000)
            self.assertEqual(s.read(777), full[5000:5777])
            s.seek(-100, 2)
            self.assertEqual(s.read(), full[-100:])
            self.assertEqual(s.tell(), len(audio))
            s.seek(1000, 0)
            s.seek(234, 1)
            self.assertEqual(s.read(10), full[1234:1244])
            s.seek(0)
            self.assertEqual(s.read(), full)
            s.close()
            br = _io.BufferedReader(c.NcmStream(ncm), 4096)
            self.assertEqual(br.read(512), full[:512])
            br.seek(4096 * 3)
            self.assertEqual(br.read(512), full[4096 * 3:4096 * 3 + 512])
            br.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
