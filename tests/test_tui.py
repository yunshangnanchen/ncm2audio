# -*- coding: utf-8 -*-
"""ncm_tui 单元测试：屏幕模型、文本宽度、图形协议编码、应用状态与截图"""

import base64
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ncm_tui as tui
from test_core import META, build_ncm


def decode_sixel(seq: str, w: int, h: int):
    """测试用极简 Sixel 解码器，返回 {(x, y): (r, g, b)}"""
    assert seq.startswith("\x1bP"), "不是 DCS 序列"
    data = seq[seq.index("q") + 1:]
    palette = {}
    px = {}
    x = y = 0
    color = 0
    i = 0
    n = len(data)
    while i < n:
        ch = data[i]
        if ch == "\x1b":
            break
        if ch == '"':
            # 跳过光栅属性 "Pan;Pad;Ph;Pv
            j = i + 1
            while j < n and (data[j].isdigit() or data[j] == ";"):
                j += 1
            i = j
            continue
        if ch == "#":
            j = i + 1
            num = ""
            while j < n and data[j].isdigit():
                num += data[j]
                j += 1
            color = int(num)
            if j < n and data[j] == ";":
                k = j + 1
                raw = ""
                while k < n and (data[k].isdigit() or data[k] == ";"):
                    raw += data[k]
                    k += 1
                parts = [int(p) for p in raw.split(";") if p != ""]
                if len(parts) == 4 and parts[0] == 2:
                    palette[color] = (parts[1] * 255 // 100,
                                      parts[2] * 255 // 100,
                                      parts[3] * 255 // 100)
                j = k
            i = j
            continue
        if ch == "$":
            x = 0
            i += 1
            continue
        if ch == "-":
            y += 6
            x = 0
            i += 1
            continue
        if ch == "!":
            j = i + 1
            num = ""
            while j < n and data[j].isdigit():
                num += data[j]
                j += 1
            cnt = int(num)
            bits = ord(data[j]) - 63
            for _ in range(cnt):
                for r in range(6):
                    if (bits >> r) & 1:
                        px[(x, y + r)] = palette.get(color, (0, 0, 0))
                x += 1
            i = j + 1
            continue
        bits = ord(ch) - 63
        for r in range(6):
            if (bits >> r) & 1:
                px[(x, y + r)] = palette.get(color, (0, 0, 0))
        x += 1
        i += 1
    return px


class ScreenTest(unittest.TestCase):
    def test_char_width(self):
        self.assertEqual(tui.text_width("abc"), 3)
        self.assertEqual(tui.text_width("中文"), 4)
        self.assertEqual(tui.text_width("a中b"), 4)

    def test_put_wide_and_plain(self):
        s = tui.Screen(10, 2)
        s.put(0, 0, "中文AB")
        self.assertEqual(s.plain_lines()[0].strip(), "中文AB")
        ansi = s.to_ansi()
        self.assertIn("\x1b[", ansi)
        self.assertNotIn("\n", ansi)

    def test_truncate(self):
        self.assertEqual(tui.truncate("abcdef", 10), "abcdef")
        self.assertEqual(tui.truncate("abcdef", 4), "abc…")
        self.assertEqual(tui.truncate("中文字", 4), "中…")
        self.assertEqual(tui.truncate_left("C:\\music\\song.ncm", 8), "…ong.ncm")
        self.assertLessEqual(tui.text_width(tui.truncate_left("C:\\music\\song.ncm", 8)), 8)

    def test_colors(self):
        self.assertEqual(tui.mix((0, 0, 0), (255, 255, 255), 0.5), (127, 127, 127))
        self.assertGreaterEqual(tui._c256((255, 0, 0)), 16)


class GraphicsTest(unittest.TestCase):
    def test_sixel_roundtrip(self):
        from PIL import Image
        img = Image.new("RGB", (12, 6), (255, 0, 0))
        for xx in range(6, 12):
            for yy in range(6):
                img.putpixel((xx, yy), (0, 0, 255))
        seq = tui.sixel_payload(img, max_colors=8)
        px = decode_sixel(seq, 12, 6)
        self.assertEqual(px[(0, 0)], (255, 0, 0))
        self.assertEqual(px[(11, 5)], (0, 0, 255))
        self.assertEqual(px[(5, 3)], (255, 0, 0))

    def test_kitty_payload(self):
        from PIL import Image
        img = Image.new("RGB", (16, 16), (10, 200, 30))
        data = tui.png_bytes(img)
        payload = tui.kitty_payload(data, cols=4, rows=2)
        self.assertIn("a=T", payload)
        self.assertIn("f=100", payload)
        self.assertIn("c=4", payload)
        # 取第一段有效载荷并解码
        chunks = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", payload)
        b64 = "".join(p[1] for p in chunks if "a=T" in p[0])
        from PIL import Image as I
        out = I.open(io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(out.size, (16, 16))

    def test_iterm_payload(self):
        from PIL import Image
        img = Image.new("RGB", (8, 8), (1, 2, 3))
        payload = tui.iterm_payload(tui.png_bytes(img), cols=2, rows=1)
        self.assertTrue(payload.startswith("\x1b]1337;File=inline=1"))
        b64 = payload.split(":", 1)[1].rstrip("\a")
        self.assertEqual(base64.b64decode(b64)[:8], b"\x89PNG\r\n\x1a\n")

    def test_cover_blocks(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (200, 10, 10)).save(buf, format="PNG")
        s = tui.Screen(40, 20)
        res = tui.paint_cover(s, buf.getvalue(), tui.Rect(0, 0, 10, 5), "blocks")
        self.assertEqual(res.mode, "blocks")
        cells = [c for c in s.cells if c.ch == "▀"]
        self.assertTrue(cells)
        self.assertEqual(cells[0].fg, (200, 10, 10))

    def test_cover_placeholder(self):
        s = tui.Screen(20, 10)
        res = tui.paint_cover(s, b"", tui.Rect(0, 0, 8, 4), "blocks")
        self.assertTrue(any(c.ch == "▀" for c in s.cells))

    def test_detect_mode_env(self):
        import os
        old = {k: os.environ.get(k) for k in ("KITTY_WINDOW_ID", "TERM_PROGRAM", "NCM_COVER_MODE")}
        try:
            os.environ["NCM_COVER_MODE"] = "blocks"
            self.assertEqual(tui.detect_cover_mode(), "blocks")
            os.environ.pop("NCM_COVER_MODE")
            os.environ["KITTY_WINDOW_ID"] = "1"
            self.assertEqual(tui.detect_cover_mode(), "kitty")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class LyricTest(unittest.TestCase):
    def test_parse_lrc(self):
        text = ('{"t":0,"c":[{"tx":"作曲: "}]}\n'
                "[00:05.00]纯音乐，请欣赏\n"
                "[00:12.50]第二句\n"
                "[01:02.3]第三句\n")
        entries = tui.parse_lrc(text)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], (5.0, "纯音乐，请欣赏"))
        self.assertAlmostEqual(entries[1][0], 12.5)
        self.assertAlmostEqual(entries[2][0], 62.3)


class AppTest(unittest.TestCase):
    def _make_dir(self, td: Path):
        audio = b"fLaC" + b"\x00" * 4000
        (td / "b.ncm").write_bytes(build_ncm(audio, META, b"\x89PNG\r\n\x1a\n" + b"\x00" * 40))
        (td / "a.mp3").write_bytes(b"ID3\x03\x00" + b"\x00" * 2000)
        return td

    def test_scan_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._make_dir(Path(td))
            app = tui.App(td, cover_mode="blocks")
            self.assertEqual(len(app.items), 2)
            self.assertEqual(app.filtered_items()[0].path.name, "a.mp3")
            app.handle_key("tab")
            self.assertEqual(len(app.filtered_items()), 1)
            self.assertEqual(app.filtered_items()[0].path.name, "b.ncm")
            app.handle_key("tab")
            self.assertEqual(app.filter_mode, "audio")
            self.assertEqual(len(app.filtered_items()), 1)

    def test_selection_clamp_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._make_dir(Path(td))
            app = tui.App(td, cover_mode="blocks")
            app.handle_key("up")
            self.assertEqual(app.sel, 0)
            app.handle_key("end")
            self.assertEqual(app.sel, len(app.filtered_items()) - 1)
            ncm_item = [it for it in app.items if it.kind == "ncm"][0]
            meta = app.meta_for(ncm_item)
            self.assertEqual(meta["title"], "测试歌曲")
            self.assertTrue(meta["cover"].startswith(b"\x89PNG"))

    def test_quit_keys(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._make_dir(Path(td))
            app = tui.App(td, cover_mode="blocks")
            self.assertFalse(app.handle_key("esc"))

    def test_render_and_screenshot(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._make_dir(Path(td))
            app = tui.App(td, cover_mode="blocks")
            s = app.render_frame(120, 36)
            lines = s.plain_lines()
            self.assertEqual(len(lines), 36)
            self.assertIn("文件列表", lines[1])
            out = td / "shot.png"
            tui.screenshot(td, out, size=(100, 30), hd=True)
            from PIL import Image
            with Image.open(out) as im:
                self.assertEqual(im.size, (100 * 18, 30 * 36))

    def test_small_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            td = self._make_dir(Path(td))
            app = tui.App(td, cover_mode="blocks")
            s = app.render_frame(50, 14)
            self.assertEqual(len(s.plain_lines()), 14)

    def test_keys_map(self):
        self.assertEqual(tui.Terminal._map_char("\r"), "enter")
        self.assertEqual(tui.Terminal._map_char(" "), "space")
        keys = tui.Terminal._parse_posix("\x1b[A\x1b[B\x1b[C\x1b[D")
        self.assertEqual(keys, ["up", "down", "right", "left"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
