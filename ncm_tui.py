# -*- coding: utf-8 -*-
"""ncm_tui —— NCM 音频终端的播放器界面

布局（左 → 右）：
    左侧   播放列表（↑↓ 选择，Enter 播放/转换，空格暂停…）
    中间   竖线分隔
    右侧   封面（终端图形协议自适应：kitty / iTerm2 / Sixel / 半块字符）
           封面下方为歌曲信息、播放进度、歌词与频谱

运行环境：Windows Terminal / WezTerm / kitty / iTerm2 / 传统控制台均可，
不支持图形协议时自动降级为 24 位真彩半块字符画。
"""

from __future__ import annotations

import base64
import io
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ncm_core as core

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# --------------------------------------------------------------------------
# 主题配色
# --------------------------------------------------------------------------
BG = (12, 14, 18)          # 主背景
BG_ALT = (18, 21, 27)      # 顶栏 / 底栏
BG_SEL = (0, 92, 100)      # 选中行
BG_PANEL = (16, 19, 24)
FG = (226, 233, 239)
FG_DIM = (132, 143, 155)
FG_FAINT = (88, 99, 111)
ACCENT = (0, 214, 200)     # 主强调色（青）
ACCENT_DIM = (0, 128, 122)
ACCENT2 = (255, 194, 96)   # 次强调（金）
ACCENT3 = (168, 132, 255)  # 频谱高段（紫）
BORDER = (52, 61, 72)
BAR_BG = (43, 50, 59)
OK = (126, 222, 146)
ERR = (255, 122, 112)
WARN = (255, 200, 100)


# --------------------------------------------------------------------------
# 文本宽度工具（终端格子）
# --------------------------------------------------------------------------
def char_width(ch: str) -> int:
    if not ch:
        return 0
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def truncate(s: str, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超出时补省略号"""
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    out = []
    used = 0
    ew = char_width(ellipsis) if ellipsis else 0
    for ch in s:
        w = char_width(ch)
        if used + w > width - ew:
            break
        out.append(ch)
        used += w
    res = "".join(out)
    if ellipsis:
        res += ellipsis
    return res


def truncate_left(s: str, width: int, ellipsis: str = "…") -> str:
    """从左侧截断（保留尾部，适合显示路径）"""
    if text_width(s) <= width:
        return s
    out = []
    used = 0
    ew = char_width(ellipsis)
    for ch in reversed(s):
        w = char_width(ch)
        if used + w > width - ew:
            break
        out.append(ch)
        used += w
    return ellipsis + "".join(reversed(out))


def mix(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _c256(rgb) -> int:
    """近似映射到 xterm-256 调色板"""
    r, g, b = [min(255, max(0, int(v))) for v in rgb]
    best, bd = 16, 1 << 30
    for i in range(216):
        rr, gg, bb = (i // 36) * 51, ((i // 6) % 6) * 51, (i % 6) * 51
        d = (rr - r) ** 2 + (gg - g) ** 2 + (bb - b) ** 2
        if d < bd:
            bd, best = d, 16 + i
    for i in range(24):
        v = 8 + i * 10
        d = (v - r) ** 2 + (v - g) ** 2 + (v - b) ** 2
        if d < bd:
            bd, best = d, 232 + i
    return best


# --------------------------------------------------------------------------
# 屏幕单元格模型
# --------------------------------------------------------------------------
class Cell:
    __slots__ = ("ch", "fg", "bg", "bold", "dim", "underline")

    def __init__(self, ch=" ", fg=FG, bg=BG, bold=False, dim=False, underline=False):
        self.ch = ch
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.dim = dim
        self.underline = underline


class Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h


class Screen:
    def __init__(self, w: int, h: int):
        self.w = max(1, w)
        self.h = max(1, h)
        self.cells = [Cell() for _ in range(self.w * self.h)]

    def cell(self, x: int, y: int) -> Cell:
        return self.cells[y * self.w + x]

    def fill(self, bg=BG, fg=FG):
        for c in self.cells:
            c.ch = " "
            c.fg = fg
            c.bg = bg
            c.bold = False
            c.dim = False
            c.underline = False

    def fill_rect(self, x, y, w, h, bg=BG, fg=FG):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        for yy in range(y0, y1):
            base = yy * self.w
            for xx in range(x0, x1):
                c = self.cells[base + xx]
                c.ch = " "
                c.fg = fg
                c.bg = bg
                c.bold = False
                c.dim = False
                c.underline = False

    def put(self, x, y, text, fg=FG, bg=None, bold=False, dim=False, underline=False) -> int:
        """写入文本，返回结束列号；宽字符自动占两格"""
        if y < 0 or y >= self.h:
            return x
        cx = x
        base = y * self.w
        for ch in text:
            if ch == "\n":
                break
            w = char_width(ch)
            if w == 0:
                continue
            if cx + w > self.w:
                break
            if cx < 0:
                cx += w
                continue
            c = self.cells[base + cx]
            c.ch = ch
            c.fg = fg
            c.bg = BG if bg is None else bg
            c.bold = bold
            c.dim = dim
            c.underline = underline
            if w == 2:
                c2 = self.cells[base + cx + 1]
                c2.ch = ""
                c2.fg = fg
                c2.bg = c.bg
                c2.bold = bold
                c2.dim = dim
                c2.underline = underline
            cx += w
        return cx

    def hline(self, x, y, w, ch="─", fg=BORDER, bg=None):
        if bg is None:
            bg = self.cell(min(max(x, 0), self.w - 1), max(0, min(y, self.h - 1))).bg
        for i in range(w):
            if 0 <= x + i < self.w:
                self.put(x + i, y, ch, fg=fg, bg=bg)

    def vline(self, x, y, h, ch="│", fg=BORDER, bg=None):
        for i in range(h):
            if 0 <= y + i < self.h:
                self.put(x, y + i, ch, fg=fg, bg=bg)

    # ---------- 输出 ----------
    def _sgr(self, fg, bg, bold, dim, underline, truecolor: bool) -> str:
        parts = ["\x1b[0"]
        if fg is not None:
            if truecolor:
                parts.append(f"38;2;{fg[0]};{fg[1]};{fg[2]}")
            else:
                parts.append(f"38;5;{_c256(fg)}")
        if bg is not None:
            if truecolor:
                parts.append(f"48;2;{bg[0]};{bg[1]};{bg[2]}")
            else:
                parts.append(f"48;5;{_c256(bg)}")
        if bold:
            parts.append("1")
        if dim:
            parts.append("2")
        if underline:
            parts.append("4")
        return "\x1b[" + ";".join(parts) + "m"

    def to_ansi(self, truecolor: bool = True, skip: Optional["Rect"] = None) -> str:
        """生成整帧 ANSI；skip 矩形内不输出任何字符（留给终端图形协议叠放图片）"""
        out = []
        style = None

        def emit_run(y: int, x0: int, x1: int):
            nonlocal style
            if x1 <= x0:
                return
            out.append(f"\x1b[{y + 1};{x0 + 1}H")
            base = y * self.w
            for x in range(x0, x1):
                c = self.cells[base + x]
                if c.ch == "":
                    continue
                st = (c.fg, c.bg, c.bold, c.dim, c.underline)
                if st != style:
                    out.append(self._sgr(c.fg, c.bg, c.bold, c.dim, c.underline, truecolor))
                    style = st
                out.append(c.ch)

        for y in range(self.h):
            in_skip_row = skip is not None and skip.y <= y < skip.y + skip.h
            if in_skip_row and 0 < skip.x < self.w:
                emit_run(y, 0, min(skip.x, self.w))
                if skip.x + skip.w < self.w:
                    emit_run(y, skip.x + skip.w, self.w)
            else:
                emit_run(y, 0, self.w)
        out.append("\x1b[0m")
        return "".join(out)

    def plain_lines(self) -> list:
        """纯文本转储（宽字符只出现一次，便于调试/测试）"""
        lines = []
        for y in range(self.h):
            base = y * self.w
            chars = []
            for x in range(self.w):
                c = self.cells[base + x]
                if c.ch == "":
                    continue
                chars.append(c.ch)
            lines.append("".join(chars).rstrip())
        return lines


# --------------------------------------------------------------------------
# 终端图形协议（高清封面）
# --------------------------------------------------------------------------
MISSING = object()


def detect_cover_mode(probe: bool = False) -> str:
    """根据环境变量/终端类型自动选择封面渲染方式"""
    env = os.environ
    manual = env.get("NCM_COVER_MODE")
    if manual:
        m = manual.strip().lower()
        if m in ("auto", "kitty", "iterm", "sixel", "blocks", "none"):
            if m != "auto":
                return m
    term = env.get("TERM", "").lower()
    tp = env.get("TERM_PROGRAM", "").lower()
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or tp in ("wezterm", "ghostty", "kitty"):
        return "kitty"
    if tp == "iterm.app":
        return "iterm"
    if "sixel" in term or env.get("MLTERM"):
        return "sixel"
    if env.get("WT_SESSION") or env.get("WT_PROFILE_ID"):
        return "sixel" if (probe and probe_sixel()) else "blocks"
    if probe and probe_sixel():
        return "sixel"
    return "blocks"


def probe_sixel(timeout: float = 0.35) -> bool:
    """通过 DA1 设备属性查询判断终端是否支持 Sixel（仅 Windows 控制台）"""
    if os.name != "nt" or not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        import ctypes
        import msvcrt
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        old = mode.value
        # 输入改为 VT 模式并关闭行缓冲/回显
        k32.SetConsoleMode(h, (old | 0x0200) & ~0x0006)
        sys.stdout.write("\x1b[c")
        sys.stdout.flush()
        buf = ""
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                buf += ch
                if ch == "c":
                    break
            else:
                time.sleep(0.01)
        k32.SetConsoleMode(h, old)
        m = re.search(r"\?[0-9;]*", buf)
        if not m:
            return False
        attrs = m.group(0)[1:].split(";")
        return "4" in attrs
    except Exception:
        return False


def load_cover_image(data: bytes):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def placeholder_cover(w_px: int, h_px: int):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (max(1, w_px), max(1, h_px)), BG)
    d = ImageDraw.Draw(img)
    c1 = mix(ACCENT_DIM, BG, 0.35)
    for y in range(img.height):
        d.line([(0, y), (img.width, y)], fill=mix(c1, BG, y / max(1, img.height)))
    note = "♪"
    try:
        from PIL import ImageFont
        font = _load_font(None, max(12, min(img.width, img.height) // 3), bold=False, cjk=False)
        bbox = d.textbbox((0, 0), note, font=font)
        d.text(((img.width - bbox[2]) // 2, (img.height - bbox[3]) // 2), note,
               font=font, fill=mix(ACCENT, BG, 0.25))
    except Exception:
        pass
    return img


def fit_cover(img, box_w: int, box_h: int, bg=BG):
    from PIL import Image
    box_w, box_h = max(1, box_w), max(1, box_h)
    ratio = min(box_w / max(1, img.width), box_h / max(1, img.height))
    tw = max(1, int(img.width * ratio))
    th = max(1, int(img.height * ratio))
    im = img.resize((tw, th), Image.LANCZOS)
    canvas = Image.new("RGB", (box_w, box_h), bg)
    canvas.paste(im, ((box_w - tw) // 2, (box_h - th) // 2))
    return canvas


def kitty_payload(png: bytes, cols: int, rows: int, image_id: int = 1) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)] or [""]
    out = [f"\x1b_Ga=d,i={image_id},q=2\x1b\\"]
    for idx, chunk in enumerate(chunks):
        params = f"a=T,f=100,i={image_id},q=2"
        if idx == 0:
            params += f",c={cols},r={rows}"
        if idx < len(chunks) - 1:
            params += ",m=1"
        out.append(f"\x1b_G{params};{chunk}\x1b\\")
    return "".join(out)


def iterm_payload(png: bytes, cols: int, rows: int) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    return (f"\x1b]1337;File=inline=1;width={cols};height={rows};"
            f"preserveAspectRatio=1:{b64}\a")


def png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


_SIXEL_TRANS_CACHE: dict = {}


def sixel_payload(img, max_colors: int = 128) -> str:
    """把 PIL 图像编码为 Sixel 转义序列（纯 Python，一次编码后缓存）"""
    from PIL import Image
    q = img.quantize(colors=max_colors, method=Image.FASTOCTREE, dither=Image.Dither.NONE)
    w, h = q.size
    pal = q.getpalette() or [0] * 768
    data = q.tobytes()
    out = [f'\x1bP0;0;0q"1;1;{w};{h}']
    used = sorted(set(data))
    for i in used:
        r = pal[3 * i] if 3 * i < len(pal) else 0
        g = pal[3 * i + 1] if 3 * i + 1 < len(pal) else 0
        b = pal[3 * i + 2] if 3 * i + 2 < len(pal) else 0
        out.append(f"#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")
    add = int.from_bytes(b"\x3f" * w, "little")
    for y0 in range(0, h, 6):
        rows = []
        for r in range(6):
            y = y0 + r
            rows.append(data[y * w:(y + 1) * w] if y < h else bytes(w))
        present = set()
        for row in rows:
            present.update(row)
        for c in sorted(present):
            if c >= max_colors:
                continue
            trans = _SIXEL_TRANS_CACHE.get(c)
            if trans is None:
                trans = bytes(1 if x == c else 0 for x in range(256))
                _SIXEL_TRANS_CACHE[c] = trans
            val = 0
            for r in range(6):
                val |= int.from_bytes(rows[r].translate(trans), "little") << r
            s = (val + add).to_bytes(w, "little").decode("latin-1")
            s = re.sub(r"(.)\1{3,}", lambda m: "!%d%s" % (len(m.group(0)), m.group(1)), s)
            out.append(f"#{c}{s}$")
        out.append("-")
    out.append("\x1b\\")
    return "".join(out)


@dataclass
class CoverResult:
    mode: str = "blocks"
    payload: str = ""
    overlay: Optional[tuple] = None     # (Rect, PIL.Image) 供截图模拟
    cleared: bool = False


def paint_cover(screen: Screen, cover: bytes, rect: Rect, mode: str,
                cell_px=(8, 16), simulate_hd: bool = False, cache: Optional[dict] = None) -> CoverResult:
    """把封面画进 rect 区域；blocks 模式直接写入单元格，其他模式返回转义序列"""
    cell_px_w, cell_px_h = cell_px
    px_w, px_h = rect.w * cell_px_w, rect.h * cell_px_h
    img = None
    if cover:
        img = load_cover_image(cover)
    if img is None:
        img = placeholder_cover(px_w, px_h)

    if simulate_hd:
        # 截图模拟图形协议：清空区域，图像由截图程序叠加
        screen.fill_rect(rect.x, rect.y, rect.w, rect.h, bg=BG)
        fitted = fit_cover(img, px_w, px_h, bg=BG)
        return CoverResult(mode="blocks", overlay=(rect, fitted))
    if mode == "blocks":
        fitted = fit_cover(img, rect.w, rect.h * 2, bg=BG)
        px = fitted.load()
        for yy in range(rect.h):
            for xx in range(rect.w):
                top = px[xx, yy * 2]
                bottom = px[xx, yy * 2 + 1]
                screen.put(rect.x + xx, rect.y + yy, "▀", fg=top, bg=bottom)
        return CoverResult(mode="blocks")

    # 图像协议模式：该区域画空白背景，转发转义序列
    screen.fill_rect(rect.x, rect.y, rect.w, rect.h, bg=BG)
    key = None
    if cache is not None:
        key = (mode, id(cover), len(cover), rect.w, rect.h)
        cached = cache.get(key)
        if cached is not None:
            payload = cached
            return CoverResult(mode=mode, payload=payload)

    if mode == "kitty":
        payload = kitty_payload(png_bytes(fit_cover(img, px_w, px_h)), rect.w, rect.h)
    elif mode == "iterm":
        payload = iterm_payload(png_bytes(fit_cover(img, px_w, px_h)), rect.w, rect.h)
    elif mode == "sixel":
        payload = sixel_payload(fit_cover(img, px_w, px_h))
    else:
        payload = ""
    if cache is not None and key is not None:
        cache[key] = payload
        if len(cache) > 32:
            cache.pop(next(iter(cache)))
    return CoverResult(mode=mode, payload=payload)


# --------------------------------------------------------------------------
# 简易 PNG 渲染（用于截图验证 / 生成预览图）
# --------------------------------------------------------------------------
_FONT_DIRS = [
    r"C:\Windows\Fonts",
    "/usr/share/fonts/truetype/dejavu",
    "/System/Library/Fonts",
]
_MONO_NAMES = ["consola.ttf", "CascadiaMono.ttf", "consolab.ttf", "DejaVuSansMono.ttf"]
_MONO_BOLD_NAMES = ["consolab.ttf", "CascadiaMonoBold.ttf", "DejaVuSansMono-Bold.ttf"]
_CJK_NAMES = ["msyh.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc", "msyhbd.ttc", "simsun.ttc"]
_SYMBOL_NAMES = ["seguisym.ttf", "DejaVuSans.ttf", "NotoSansSymbols-Regular.ttf"]


def _is_symbol_char(ch: str) -> bool:
    o = ord(ch)
    return (0x2190 <= o <= 0x23FF) or (0x25A0 <= o <= 0x25FF) or \
           (0x2600 <= o <= 0x27BF) or (0x2B00 <= o <= 0x2BFF)


def _find_font(names) -> Optional[str]:
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def _load_font(path, size, bold=False, cjk=False):
    from PIL import ImageFont
    if path is None:
        if cjk:
            path = _find_font(_CJK_NAMES)
        else:
            path = _find_font(_MONO_BOLD_NAMES if bold else _MONO_NAMES)
            if path is None:
                path = _find_font(_MONO_NAMES)
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


class PNGRenderer:
    def __init__(self, cell_w=9, cell_h=18, scale=2):
        self.cw = cell_w * scale
        self.ch = cell_h * scale
        self.scale = scale
        self.fs = 15 * scale
        self.mono = _load_font(None, self.fs)
        self.mono_bold = _load_font(None, self.fs, bold=True)
        self.cjk = _load_font(None, int(self.fs * 0.98), cjk=True)
        self.cjk_bold = _load_font(None, int(self.fs * 0.98), bold=True, cjk=True)
        self.sym = None
        sym_path = _find_font(_SYMBOL_NAMES)
        if sym_path:
            try:
                from PIL import ImageFont
                self.sym = ImageFont.truetype(sym_path, self.fs)
            except Exception:
                self.sym = None

    def render(self, screen: Screen, path, overlay: Optional[tuple] = None):
        from PIL import Image, ImageDraw
        W, H = screen.w * self.cw, screen.h * self.ch
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        # 背景
        for y in range(screen.h):
            base = y * screen.w
            for x in range(screen.w):
                c = screen.cells[base + x]
                if c.bg != BG:
                    d.rectangle([x * self.cw, y * self.ch, (x + 1) * self.cw - 1,
                                 (y + 1) * self.ch - 1], fill=c.bg)
        # 文字
        for y in range(screen.h):
            base = y * screen.w
            for x in range(screen.w):
                c = screen.cells[base + x]
                if not c.ch or c.ch == "":
                    continue
                wide = char_width(c.ch) == 2
                o = ord(c.ch)
                if wide or 0x2150 <= o <= 0x218F:
                    font = self.cjk_bold if c.bold else self.cjk
                elif self.sym is not None and _is_symbol_char(c.ch):
                    font = self.sym
                else:
                    font = self.mono_bold if c.bold else self.mono
                color = c.fg
                if c.dim:
                    color = mix(c.fg, c.bg, 0.45)
                try:
                    bbox = d.textbbox((0, 0), c.ch, font=font)
                    top = bbox[1]
                    bottom = bbox[3]
                except Exception:
                    top, bottom = 0, self.fs
                ty = y * self.ch + (self.ch - (bottom - top)) // 2 - top
                d.text((x * self.cw, ty), c.ch, font=font, fill=color)
                if c.underline:
                    uy = y * self.ch + self.ch - 2
                    d.line([(x * self.cw, uy), (x * self.cw + (self.cw * 2 if wide else self.cw), uy)],
                           fill=color)
        if overlay:
            rect, cover_img = overlay
            x0 = rect.x * self.cw
            y0 = rect.y * self.ch
            w = rect.w * self.cw
            h = rect.h * self.ch
            if cover_img.size != (w, h):
                cover_img = cover_img.resize((w, h), Image.LANCZOS)
            img.paste(cover_img, (x0, y0))
        img.save(path)
        return path


# --------------------------------------------------------------------------
# 播放后端（pygame 优先，ffplay 兜底）
# --------------------------------------------------------------------------
class Player:
    def __init__(self, volume: float = 0.75):
        self.backend = "none"
        self.error = ""
        self.path: Optional[str] = None
        self.state = "stopped"        # stopped / playing / paused
        self.duration = 0.0
        self.volume = volume
        self._pg = None
        self._ff = None
        self._base = 0.0
        self._started = 0.0
        self._paused_acc = 0.0
        self._pause_wall = 0.0
        try:
            import pygame
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            pygame.mixer.music.set_volume(volume)
            self._pg = pygame
            self.backend = "pygame"
        except Exception as exc:
            self.error = str(exc)
            if shutil.which("ffplay"):
                self.backend = "ffplay"
            else:
                self.backend = "none"

    # ---- 控制 ----
    def load(self, path, duration: float = 0.0) -> bool:
        self.stop()
        self.path = str(path)
        self.duration = float(duration or 0.0)
        if self.backend == "pygame":
            try:
                self._pg.mixer.music.load(self.path)
            except Exception as exc:
                self.error = str(exc)
                return False
        return True

    def play(self):
        if not self.path:
            return
        if self.backend == "pygame":
            try:
                self._pg.mixer.music.play()
            except Exception as exc:
                self.error = str(exc)
                return
            self.state = "playing"
            self._base = 0.0
            self._started = time.time()
            self._paused_acc = 0.0
        elif self.backend == "ffplay":
            try:
                self._ff = subprocess.Popen(
                    [shutil.which("ffplay"), "-nodisp", "-autoexit", "-loglevel", "quiet",
                     "-volume", str(int(self.volume * 100)), self.path],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.state = "playing"
                self._base = 0.0
                self._started = time.time()
                self._paused_acc = 0.0
            except Exception as exc:
                self.error = str(exc)

    def stop(self):
        if self.backend == "pygame":
            try:
                self._pg.mixer.music.stop()
            except Exception:
                pass
        elif self.backend == "ffplay" and self._ff:
            try:
                self._ff.stdin.write(b"q")
                self._ff.stdin.flush()
            except Exception:
                pass
            try:
                self._ff.terminate()
            except Exception:
                pass
            self._ff = None
        self.state = "stopped"

    def toggle_pause(self):
        if self.state == "playing":
            if self.backend == "pygame":
                self._pg.mixer.music.pause()
            elif self._ff and self._ff.stdin:
                self._ff.stdin.write(b"p")
                self._ff.stdin.flush()
            self._pause_wall = time.time()
            self.state = "paused"
        elif self.state == "paused":
            if self.backend == "pygame":
                self._pg.mixer.music.unpause()
            elif self._ff and self._ff.stdin:
                self._ff.stdin.write(b"p")
                self._ff.stdin.flush()
            self._paused_acc += time.time() - self._pause_wall
            self.state = "playing"

    def seek(self, target: float):
        if not self.path or self.state not in ("playing", "paused"):
            return False
        target = max(0.0, target)
        if self.duration:
            target = min(target, max(0.0, self.duration - 0.3))
        if self.backend == "pygame":
            try:
                self._pg.mixer.music.set_pos(target)
            except Exception as exc:
                self.error = str(exc)
                return False
        elif self._ff and self._ff.stdin:
            # ffplay 方向键：左右各约 10 秒
            try:
                delta = target - self.position()
                key = b"\x1b[C" if delta > 0 else b"\x1b[D"
                for _ in range(max(1, int(abs(delta) // 10))):
                    self._ff.stdin.write(key)
                self._ff.stdin.flush()
            except Exception:
                return False
        self._base = target
        self._started = time.time()
        self._paused_acc = 0.0
        if self.state == "paused":
            self._pause_wall = time.time()
        return True

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))
        if self.backend == "pygame":
            self._pg.mixer.music.set_volume(self.volume)

    # ---- 状态 ----
    def position(self) -> float:
        if self.state == "stopped" or not self.path:
            return 0.0
        if self.state == "paused":
            return self._base + self._paused_acc
        return max(0.0, self._base + (time.time() - self._started) - self._paused_acc)

    def is_busy(self) -> bool:
        if self.backend == "pygame":
            try:
                return bool(self._pg.mixer.music.get_busy())
            except Exception:
                return False
        if self.backend == "ffplay":
            return bool(self._ff and self._ff.poll() is None)
        return False

    def ended(self) -> bool:
        if self.state != "playing":
            return False
        if time.time() - self._started < 0.8:
            return False
        return not self.is_busy()


# --------------------------------------------------------------------------
# 歌词
# --------------------------------------------------------------------------
_LRC_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str) -> list:
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("{"):
            continue
        stamps = _LRC_RE.findall(line)
        if not stamps:
            continue
        content = _LRC_RE.sub("", line).strip()
        for mm, ss, frac in stamps:
            t = int(mm) * 60 + int(ss)
            if frac:
                t += int(frac) / (10 ** len(frac))
            entries.append((t, content))
    entries.sort(key=lambda e: e[0])
    return entries


def read_text_any(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return ""
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 列表项
# --------------------------------------------------------------------------
class TrackItem:
    __slots__ = ("path", "kind", "size", "converted", "meta", "meta_loaded", "_cover")

    def __init__(self, path: Path, kind: str, size: int = 0):
        self.path = path
        self.kind = kind
        self.size = size
        self.converted: Optional[Path] = None
        self.meta: dict = {}
        self.meta_loaded = False
        self._cover: bytes = b""

    def display_name(self) -> str:
        if self.meta_loaded:
            title = self.meta.get("title") or self.path.stem
            artists = self.meta.get("artists") or []
            if artists and title:
                return f"{' / '.join(artists)} - {title}"
            return title
        return self.path.stem

    def tag(self) -> str:
        if self.kind == "audio":
            return self.path.suffix.upper().lstrip(".") or "AUDIO"
        return "NCM"


def make_item(path: Path) -> TrackItem:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    kind = "ncm" if path.suffix.lower() == ".ncm" else "audio"
    return TrackItem(path, kind, size)


# --------------------------------------------------------------------------
# 应用主体
# --------------------------------------------------------------------------
class App:
    def __init__(self, directory: Path, cover_mode: str = "auto", filter_mode: str = "all",
                 volume: float = 0.75):
        self.dir = Path(directory).resolve()
        self.items: list = []
        self.sel = 0
        self.scroll = 0
        self.filter_mode = filter_mode
        self.player = Player(volume)
        self.status = ""
        self.status_until = 0.0
        self.status_color = FG_DIM
        self.show_lyrics = True
        self.mode = cover_mode if cover_mode != "auto" else detect_cover_mode()
        self.truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or os.name == "nt"
        self.cover_cache: dict = {}
        self.lyric_cache: dict = {}
        self.playing_idx = -1
        self.running = True
        self.need_redraw = True
        self.size = (100, 32)
        self._meta_cache: dict = {}
        self._paste = ""
        self._last_char_t = 0.0
        self._overlay = None
        self._gfx = ""
        self._last_gfx_key = None
        self._cover_rect: Optional[Rect] = None
        self.scan()

    # ---------- 数据 ----------
    def scan(self):
        old = self.current_path()
        self.items = [make_item(p) for p in core.collect_media_files(self.dir, recursive=False)]
        self.sel = 0
        if old:
            for i, it in enumerate(self.filtered_items()):
                if it.path == old:
                    self.sel = i
                    break
        self.need_redraw = True
        self.set_status(f"已扫描：{len(self.items)} 个文件", FG_DIM, 2.0)

    def filtered_items(self) -> list:
        if self.filter_mode == "ncm":
            return [it for it in self.items if it.kind == "ncm"]
        if self.filter_mode == "audio":
            return [it for it in self.items if it.kind == "audio"]
        return self.items

    def current(self) -> Optional[TrackItem]:
        items = self.filtered_items()
        if not items:
            return None
        self.sel = max(0, min(self.sel, len(items) - 1))
        return items[self.sel]

    def current_path(self) -> Optional[Path]:
        it = self.current()
        return it.path if it else None

    def set_status(self, text: str, color=FG_DIM, seconds: float = 4.0):
        self.status = text
        self.status_color = color
        self.status_until = time.time() + seconds
        self.need_redraw = True

    def meta_for(self, item: TrackItem) -> dict:
        if item.meta_loaded:
            return item.meta
        key = str(item.path)
        m = self._meta_cache.get(key)
        if m is None:
            try:
                if item.kind == "ncm":
                    ncm = core.parse_ncm(item.path)
                    fmt = ncm.format_hint() or core.guess_format(core.peek_audio(ncm, 16))
                    m = {
                        "title": ncm.title(),
                        "artists": ncm.artists(),
                        "album": ncm.album(),
                        "duration": ncm.duration(),
                        "bitrate": ncm.bitrate(),
                        "cover": ncm.cover,
                        "fmt": fmt.upper(),
                        "size": ncm.size,
                    }
                else:
                    info = core.read_audio_meta(item.path)
                    m = {
                        "title": info.get("title") or item.path.stem,
                        "artists": info.get("artists") or [],
                        "album": info.get("album") or "",
                        "duration": info.get("duration") or 0.0,
                        "bitrate": info.get("bitrate") or 0,
                        "cover": info.get("cover") or b"",
                        "fmt": (info.get("format") or item.path.suffix).upper().lstrip("."),
                        "size": item.size,
                    }
            except Exception as exc:
                m = {"title": item.path.stem, "artists": [], "album": "", "duration": 0.0,
                     "bitrate": 0, "cover": b"", "fmt": "", "size": item.size,
                     "error": str(exc)}
            self._meta_cache[key] = m
        item.meta = m
        item.meta_loaded = True
        return m

    def lyrics_for(self, item: TrackItem) -> list:
        stem = item.path.stem
        for cand in (self.dir / f"{stem}.lrc", self.dir / f"{stem}.LRC"):
            key = str(cand)
            if key in self.lyric_cache:
                return self.lyric_cache[key]
            if cand.exists():
                entries = parse_lrc(read_text_any(cand))
                self.lyric_cache[key] = entries
                return entries
        self.lyric_cache[str(self.dir / f"{stem}.lrc")] = []
        return []

    # ---------- 播放 ----------
    def play_item(self, item: TrackItem, convert: bool = True):
        path = item.path
        if item.kind == "ncm":
            if item.converted and item.converted.exists():
                path = item.converted
            elif convert:
                conv = self.convert_item(item, quiet=True)
                if conv is None:
                    return
                path = conv
            else:
                return
        try:
            ok = self.player.load(path, self.meta_for(item).get("duration", 0.0))
        except Exception as exc:
            self.set_status(f"播放失败：{exc}", ERR)
            return
        if not ok:
            self.set_status(f"无法加载音频：{self.player.error}", ERR)
            return
        self.player.play()
        if self.player.state != "playing":
            self.set_status(f"播放失败：{self.player.error or '后端不可用'}", ERR)
            return
        self.playing_idx = self.filtered_items().index(item) if item in self.filtered_items() else -1
        fmt = item.tag()
        self.set_status(f"▶ 正在播放：{item.display_name()}", ACCENT, 2.0)
        self.need_redraw = True

    def toggle_pause(self):
        if self.player.state in ("playing", "paused"):
            self.player.toggle_pause()
            self.need_redraw = True
        else:
            it = self.current()
            if it:
                self.play_item(it)

    def seek(self, delta: float):
        if self.player.state in ("playing", "paused"):
            if self.player.seek(self.player.position() + delta):
                self.set_status(f"跳转到 {core.format_duration(self.player.position())}", FG_DIM, 1.5)
                self.need_redraw = True
            else:
                self.set_status("当前格式不支持跳转", WARN, 2.0)

    def next_track(self, auto: bool = False):
        items = self.filtered_items()
        if not items:
            return
        idx = (self.playing_idx if self.playing_idx >= 0 else self.sel) + 1
        if idx >= len(items):
            if auto:
                self.player.stop()
                self.playing_idx = -1
                self.set_status("播放结束", FG_DIM, 3)
                return
            idx = 0
        self.sel = idx
        self.play_item(items[idx])

    def prev_track(self):
        items = self.filtered_items()
        if not items:
            return
        idx = (self.playing_idx if self.playing_idx >= 0 else self.sel) - 1
        if idx < 0:
            idx = len(items) - 1
        self.sel = idx
        self.play_item(items[idx])

    def on_track_end(self):
        self.next_track(auto=True)

    # ---------- 转换 ----------
    def convert_item(self, item: TrackItem, quiet: bool = False) -> Optional[Path]:
        try:
            res = core.convert(item.path, overwrite=False)
        except Exception as exc:
            self.set_status(f"转换失败：{exc}", ERR)
            return None
        if res.error:
            self.set_status(f"转换失败：{res.error}", ERR)
            return None
        if res.output:
            item.converted = res.output
            if not quiet:
                if res.skipped:
                    self.set_status(f"已存在：{res.output.name}", FG_DIM)
                else:
                    self.set_status(f"✓ 已转换 → {res.output.name}", OK)
            return res.output
        return None

    def convert_selected(self, play: bool = False):
        it = self.current()
        if not it:
            return
        if it.kind != "ncm":
            self.set_status("该文件已经是音频", WARN)
            return
        out = self.convert_item(it)
        if out and play:
            self.play_item(it)

    def convert_all(self):
        ncm_items = [it for it in self.items if it.kind == "ncm"]
        if not ncm_items:
            self.set_status("没有需要转换的 NCM 文件", WARN)
            return
        ok = skip = fail = 0
        taken: set = set()
        for it in ncm_items:
            res = core.convert(it.path, overwrite=False, taken=taken)
            if res.error:
                fail += 1
            elif res.skipped:
                skip += 1
                it.converted = res.output
            else:
                ok += 1
                it.converted = res.output
        color = OK if not fail else WARN
        self.set_status(f"批量转换：成功 {ok}，已存在 {skip}，失败 {fail}", color, 6.0)

    # ---------- 交互 ----------
    def move(self, delta: int):
        items = self.filtered_items()
        if not items:
            return
        self.sel = max(0, min(self.sel + delta, len(items) - 1))
        self.need_redraw = True

    def cycle_filter(self):
        order = ["all", "ncm", "audio"]
        self.filter_mode = order[(order.index(self.filter_mode) + 1) % len(order)]
        self.sel = 0
        self.set_status({"all": "显示全部", "ncm": "只显示 NCM", "audio": "只显示音频"}[self.filter_mode],
                        FG_DIM, 1.5)

    def open_folder(self):
        it = self.current()
        target = it.path.parent if it else self.dir
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", str(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            self.set_status(f"已打开目录：{target}", FG_DIM, 2.0)
        except Exception as exc:
            self.set_status(f"无法打开目录：{exc}", ERR)

    def change_volume(self, delta: float):
        self.player.set_volume(self.player.volume + delta)
        self.set_status(f"音量 {int(self.player.volume * 100)}%", FG_DIM, 1.5)

    def handle_key(self, k: str) -> bool:
        now = time.time()
        if len(k) == 1 and k.isprintable():
            # 连续快速到达的字符视为“拖入路径”粘贴
            if now - self._last_char_t < 0.05:
                self._paste += k
                self._last_char_t = now
                return True
            self._paste = ""
            self._last_char_t = now
        if k in ("esc", "q", "Q"):
            return False
        if k == "up":
            self.move(-1)
        elif k == "down":
            self.move(1)
        elif k == "pgup":
            self.move(-10)
        elif k == "pgdn":
            self.move(10)
        elif k == "home":
            self.sel = 0
            self.need_redraw = True
        elif k == "end":
            self.sel = max(0, len(self.filtered_items()) - 1)
            self.need_redraw = True
        elif k == "enter":
            if len(self._paste) >= 3:
                self.drop_paths(self._paste)
                self._paste = ""
            else:
                it = self.current()
                if it:
                    self.play_item(it)
        elif k == "space":
            self.toggle_pause()
        elif k == "left":
            self.seek(-5)
        elif k == "right":
            self.seek(5)
        elif k in ("c", "C"):
            self.convert_selected()
        elif k in ("a", "A"):
            self.convert_all()
        elif k in ("r", "R"):
            self.scan()
        elif k in ("y", "Y"):
            self.show_lyrics = not self.show_lyrics
            self.set_status("歌词：开" if self.show_lyrics else "歌词：关", FG_DIM, 1.5)
        elif k == "tab":
            self.cycle_filter()
        elif k in ("n", "N"):
            self.next_track()
        elif k in ("p", "P"):
            self.prev_track()
        elif k in ("o", "O"):
            self.open_folder()
        elif k in ("+", "="):
            self.change_volume(0.05)
        elif k in ("-", "_"):
            self.change_volume(-0.05)
        self.need_redraw = True
        return True

    def drop_paths(self, raw: str):
        """处理拖进终端窗口的路径文本（cmd 会把路径粘贴为输入）"""
        paths = re.findall(r'"([^"]+)"|(\S+)', raw)
        files = []
        for a, b in paths:
            p = Path((a or b).strip())
            if p.exists():
                files.append(p)
        if not files:
            self.set_status("拖入路径未识别，请使用拖放启动批处理", WARN)
            return
        target_dir = None
        for p in files:
            if p.is_dir():
                target_dir = p
        n = 0
        for p in files:
            if p.is_dir():
                for f in core.collect_ncm_files([p]):
                    res = core.convert(f, overwrite=False)
                    if res.output:
                        n += 1
            elif p.suffix.lower() == ".ncm":
                res = core.convert(p, overwrite=False)
                if res.output:
                    n += 1
        self.scan()
        self.set_status(f"已处理 {n} 个文件", OK, 4.0)

    def tick(self):
        if self.player.state == "playing" and self.player.ended():
            self.on_track_end()
        # 回到播放项
        if self.player.state != "stopped" and self.playing_idx >= 0:
            items = self.filtered_items()
            if self.playing_idx < len(items) and items[self.playing_idx] is not self.current():
                pass

    def animating(self) -> bool:
        return self.player.state == "playing"

    # ---------- 渲染 ----------
    def render_frame(self, w: int, h: int, as_screen: bool = False, simulate_hd: bool = False):
        w, h = max(1, w), max(1, h)
        s = Screen(w, h)
        s.fill(BG)
        self._overlay = None
        self._gfx = ""
        self._cover_rect = None
        self._paint_header(s, w)
        body_top, body_bottom = 1, h - 2
        if h >= 12:
            self._paint_body(s, w, h, body_top, body_bottom, simulate_hd)
        self._paint_footer(s, w, h)
        return s

    # ---- 顶栏 ----
    def _paint_header(self, s: Screen, w: int):
        s.fill_rect(0, 0, w, 1, bg=BG_ALT)
        x = 1
        x = s.put(x, 0, " ♫ NCM 2 AUDIO ", fg=(6, 10, 12), bg=ACCENT, bold=True)
        x += 2
        right_txt = ""
        right_color = FG_DIM
        if self.status and time.time() < self.status_until:
            right_txt = self.status
            right_color = self.status_color
        else:
            n_ncm = sum(1 for it in self.items if it.kind == "ncm")
            n_aud = len(self.items) - n_ncm
            right_txt = f"NCM {n_ncm} · 音频 {n_aud}"
        rw_txt = text_width(right_txt)
        avail = w - x - rw_txt - 3
        if avail > 4:
            s.put(x, 0, truncate_left(str(self.dir), avail), fg=FG_DIM, bg=BG_ALT)
        if rw_txt < w - 4:
            s.put(w - rw_txt - 1, 0, right_txt, fg=right_color, bg=BG_ALT)

    # ---- 主体 ----
    def _paint_body(self, s: Screen, w: int, h: int, top: int, bottom: int, simulate_hd: bool):
        if bottom <= top:
            return
        n_rows = bottom - top + 1
        spec_rows = 4 if n_rows >= 30 else (3 if n_rows >= 22 else 0)
        item = self.current()
        lyric_rows = 0
        if item is not None and self.show_lyrics and self.lyrics_for(item):
            lyric_rows = 1
        # 封面尺寸先按可用高度算（正方形：列 = 2 × 行）
        cover_rows = (bottom - top) - spec_rows - 5 - lyric_rows
        cover_rows = max(3, min(cover_rows, 34))
        cover_cols = cover_rows * 2
        # 右侧列表至少保留 22 列，否则缩小封面
        max_cols = w - 1 - 22 - 2
        if cover_cols > max_cols:
            cover_cols = max(8, (max_cols // 2) * 2)
            cover_rows = max(3, cover_cols // 2)
        # 左栏宽度贴着封面走，剩余全部给列表
        left_w = min(max(cover_cols + 2, 22), w - 1 - 18)
        right_w = w - left_w - 1
        if left_w < 22 or right_w < 18:
            self._paint_list(s, 0, top, max(10, w), bottom)
            return
        divider_x = left_w
        right_x = left_w + 1
        for y in range(top, bottom + 1):
            s.put(divider_x, y, "│", fg=BORDER, bg=BG)
        self._paint_detail(s, 0, top, left_w, bottom, simulate_hd, cover_rows, cover_cols)
        self._paint_list(s, right_x, top, right_w, bottom)

    # ---- 左侧列表 ----
    def _paint_list(self, s: Screen, x0: int, top: int, width: int, bottom: int):
        items = self.filtered_items()
        s.put(x0 + 1, top, f"文件列表 ({len(items)})", fg=FG, bg=BG, bold=True)
        filt = {"all": "全部", "ncm": "NCM", "audio": "音频"}[self.filter_mode]
        fw = text_width(filt) + 2
        s.put(x0 + width - fw, top, f" {filt} ", fg=(8, 12, 14), bg=ACCENT_DIM)
        list_top = top + 1
        rows = bottom - list_top + 1
        if rows <= 0:
            return
        if not items:
            s.put(x0 + 2, list_top + 1, "（此目录没有 .ncm / 音频文件）", fg=FG_FAINT, bg=BG)
            return
        self.sel = max(0, min(self.sel, len(items) - 1))
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + rows:
            self.scroll = self.sel - rows + 1
        self.scroll = max(0, min(self.scroll, max(0, len(items) - rows)))
        playing_item = None
        fitems = self.filtered_items()
        if 0 <= self.playing_idx < len(fitems):
            playing_item = fitems[self.playing_idx]
        for i in range(rows):
            idx = self.scroll + i
            if idx >= len(items):
                break
            it = items[idx]
            y = list_top + i
            selected = idx == self.sel
            bg = BG_SEL if selected else BG
            s.fill_rect(x0, y, width, 1, bg=bg)
            # 播放标记
            mark = "▶" if it is playing_item else " "
            s.put(x0 + 1, y, mark, fg=ACCENT if selected else ACCENT2 if it is playing_item else FG_FAINT, bg=bg)
            # 图标
            icon = "♪" if it.kind == "audio" else "♫"
            s.put(x0 + 3, y, icon, fg=ACCENT if selected else FG_DIM if not selected else FG, bg=bg)
            # 右侧标签
            tag = it.tag()
            if it.converted:
                tag = "✓"
            meta_txt = f"{tag}  {core.human_size(it.size)}"
            meta_w = text_width(meta_txt)
            name_w = width - 6 - meta_w - 1
            name = truncate(it.display_name(), max(4, name_w))
            s.put(x0 + 5, y, name, fg=(238, 248, 248) if selected else FG, bg=bg, bold=selected)
            s.put(x0 + width - meta_w - 1, y, meta_txt, fg=(150, 225, 225) if selected else FG_FAINT, bg=bg)
            # 滚动条
            if len(items) > rows:
                bar_x = x0 + width - 1
                span_ratio = rows / len(items)
                thumb_len = max(1, int(rows * span_ratio))
                thumb_start = int(self.scroll / max(1, len(items) - rows) * (rows - thumb_len))
                if thumb_start <= i < thumb_start + thumb_len:
                    s.put(bar_x, y, "▐", fg=ACCENT_DIM, bg=bg)
                else:
                    s.put(bar_x, y, "▏", fg=BORDER, bg=bg)

    # ---- 右侧详情 ----
    def _paint_detail(self, s: Screen, x0: int, top: int, width: int, bottom: int,
                      simulate_hd: bool, cover_rows: Optional[int] = None,
                      cover_cols: Optional[int] = None):
        items = self.filtered_items()
        item = self.current()
        playing_item = items[self.playing_idx] if 0 <= self.playing_idx < len(items) else None
        label = "正在播放" if (item is not None and item is playing_item) else "已选中"
        inner_x = x0 + 1
        inner_w = max(8, width - 2)
        s.put(inner_x, top, label, fg=FG_DIM, bg=BG)
        if item is None:
            s.put(inner_x, top + 2, "列表为空", fg=FG_FAINT, bg=BG)
            return
        meta = self.meta_for(item)
        if item.converted:
            hint = "✓ 已转换"
            s.put(x0 + width - text_width(hint) - 1, top, hint, fg=OK, bg=BG)
        n_rows = bottom - top + 1
        if n_rows < 8:
            s.put(inner_x, top + 2, truncate(item.display_name(), inner_w), fg=FG, bg=BG)
            return
        spec_rows = 4 if n_rows >= 30 else (3 if n_rows >= 22 else 0)
        if cover_rows is None:
            entries0 = self.lyrics_for(item) if self.show_lyrics else []
            lyric_rows0 = 1 if entries0 else 0
            cover_rows = (bottom - top) - spec_rows - 5 - lyric_rows0
            cover_rows = min(cover_rows, inner_w // 2, 30)
            cover_rows = max(3, cover_rows)
        if cover_cols is None:
            cover_cols = cover_rows * 2
        cover_x = x0 + (width - cover_cols) // 2
        cover_y = top + 1
        cover_bytes = meta.get("cover", b"")
        res = paint_cover(s, cover_bytes, Rect(cover_x, cover_y, cover_cols, cover_rows),
                          self.mode, simulate_hd=simulate_hd, cache=self.cover_cache)
        if res.payload:
            key = (self.mode, id(cover_bytes), cover_cols, cover_rows, cover_x, cover_y)
            if key != self._last_gfx_key:
                self._gfx = f"\x1b[{cover_y + 1};{cover_x + 1}H" + res.payload
                self._last_gfx_key = key
            # 图形协议模式下，整帧重绘会跳过该矩形，避免把已绘制的图片抹掉
            self._cover_rect = Rect(cover_x, cover_y, cover_cols, cover_rows)
        if res.overlay:
            self._overlay = res.overlay

        # 信息区：标题 / 歌手·专辑 / 格式 / 歌词，紧贴封面下方
        info_y = cover_y + cover_rows + 1
        entries = self.lyrics_for(item) if self.show_lyrics else []
        s.put(inner_x, info_y, truncate(meta.get("title") or item.path.stem, inner_w),
              fg=ACCENT, bg=BG, bold=True)
        artists = " / ".join(meta.get("artists") or []) or "未知"
        album = meta.get("album") or ""
        line2 = artists if not album else f"{artists} · {album}"
        s.put(inner_x, info_y + 1, truncate(line2, inner_w), fg=FG_DIM, bg=BG)
        bits = []
        if meta.get("fmt"):
            bits.append(meta["fmt"])
        if meta.get("bitrate"):
            bits.append(f"{meta['bitrate'] // 1000} kbps")
        bits.append(core.human_size(meta.get("size") or item.size))
        s.put(inner_x, info_y + 2, truncate(" · ".join(bits), inner_w), fg=FG, bg=BG)
        if entries and info_y + 3 <= bottom - spec_rows - 1:
            cur = entries[0]
            pos = self.player.position()
            if self.player.state != "stopped":
                for t, txt in entries:
                    if t <= pos + 0.25:
                        cur = (t, txt)
                    else:
                        break
            s.put(inner_x, info_y + 3, truncate(cur[1], inner_w), fg=ACCENT2, bg=BG)
        # 进度条（底部，频谱之上）
        bar_y = bottom - spec_rows
        if bar_y > info_y + 2:
            self._paint_progress(s, inner_x, bar_y, inner_w)
        # 频谱
        if spec_rows:
            self._paint_spectrum(s, inner_x, bottom - spec_rows + 1, inner_w, spec_rows)

    def _paint_progress(self, s: Screen, x: int, y: int, w: int):
        st = self.player.state
        pos = self.player.position() if st != "stopped" else 0.0
        dur = self.player.duration or 0.0
        icon = {"playing": "▶", "paused": "||"}.get(st, "■")
        left = f"{icon} {core.format_duration(pos)}"
        right = core.format_duration(dur) if dur else "--:--"
        lw = text_width(left)
        rw = text_width(right)
        bar_x = x + lw + 1
        bar_w = w - lw - rw - 3
        if bar_w < 4:
            bar_w = max(4, w - lw - 3)
        s.put(x, y, left, fg=ACCENT if st != "stopped" else FG_DIM, bg=BG, bold=st == "playing")
        frac = (pos / dur) if dur > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        filled = int(frac * bar_w)
        if bar_w > 0:
            s.put(bar_x, y, "━" * filled, fg=ACCENT, bg=BG)
            rest = bar_w - filled
            if rest > 0:
                s.put(bar_x + filled, y, "━" * rest, fg=BAR_BG, bg=BG)
            knob = min(bar_w - 1, filled)
            s.put(bar_x + knob, y, "●", fg=ACCENT, bg=BG)
        s.put(x + w - rw, y, right, fg=FG_DIM, bg=BG)

    def _paint_spectrum(self, s: Screen, x: int, y: int, w: int, rows: int):
        n = max(4, (w - 1) // 2)
        t = time.time()
        playing = self.player.state == "playing"
        seed = 0.5
        for i in range(n):
            phase = i * 0.9 + seed
            if playing:
                v = (math.sin(t * 2.4 + phase) * 0.5 + 0.5) * 0.55 + \
                    (math.sin(t * 5.7 + phase * 1.7) * 0.5 + 0.5) * 0.3 + \
                    (math.sin(t * 9.3 + phase * 2.3) * 0.5 + 0.5) * 0.15
            else:
                v = max(0.0, 0.35 - i * 0.004)
            hgt = int(round(v * rows))
            for k in range(hgt):
                yy = y + rows - 1 - k
                frac = (k + 1) / max(1, rows)
                color = mix(ACCENT, ACCENT3, frac)
                if not playing:
                    color = mix(color, BG, 0.45)
                s.put(x + i * 2, yy, "█", fg=color, bg=BG)

    # ---- 底栏 ----
    def _paint_footer(self, s: Screen, w: int, h: int):
        y = h - 1
        s.fill_rect(0, y, w, 1, bg=BG_ALT)
        help_txt = ("↑↓ 选择  Enter 播放/转换  空格 暂停  ←→ 跳转  "
                    "C 转换  A 全部  R 刷新  Y 歌词  Tab 筛选  Esc 退出")
        if text_width(help_txt) > w - 2:
            help_txt = "↑↓ Enter 空格 ←→ C A R Y Tab Esc"
        s.put(1, y, truncate(help_txt, w - 2), fg=FG_DIM, bg=BG_ALT)
        st = self.player.state
        if st in ("playing", "paused") and self.player.path:
            icon = "▶" if st == "playing" else "||"
            txt = f"{icon} {core.format_duration(self.player.position())} / " \
                  f"{core.format_duration(self.player.duration)}"
            tw = text_width(txt)
            if tw < w - text_width(help_txt) - 4:
                s.put(w - tw - 1, y, txt, fg=ACCENT, bg=BG_ALT)


# --------------------------------------------------------------------------
# 终端控制
# --------------------------------------------------------------------------
class Terminal:
    def __init__(self):
        self.win = os.name == "nt"
        self.old_mode = None
        self._h_out = None
        self._h_in = None

    def size(self):
        try:
            sz = os.get_terminal_size()
            return max(40, sz.columns), max(12, sz.lines)
        except OSError:
            return 100, 32

    def write(self, text: str):
        sys.stdout.write(text)
        sys.stdout.flush()

    def enter(self):
        if self.win:
            import ctypes
            k32 = ctypes.windll.kernel32
            self._h_out = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(self._h_out, ctypes.byref(mode)):
                self.old_mode = mode.value
                k32.SetConsoleMode(self._h_out, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        try:
            sys.stdin.reconfigure(errors="replace")
        except Exception:
            pass

    def leave(self):
        try:
            self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        except Exception:
            pass
        if self.win and self.old_mode is not None:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(self._h_out, self.old_mode)

    def read_keys(self) -> list:
        keys = []
        if self.win:
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    key = {"H": "up", "P": "down", "M": "right", "K": "left",
                           "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(code)
                    if key:
                        keys.append(key)
                    continue
                keys.append(self._map_char(ch))
        else:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                data = os.read(sys.stdin.fileno(), 64)
                keys.extend(self._parse_posix(data))
        return keys

    @staticmethod
    def _map_char(ch: str) -> str:
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\t":
            return "tab"
        if ch == "\x08":
            return "backspace"
        if ch == " ":
            return "space"
        return ch

    @staticmethod
    def _parse_posix(data) -> list:
        keys = []
        if isinstance(data, (bytes, bytearray)):
            text = bytes(data).decode("utf-8", "replace")
        else:
            text = str(data)
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                if text[i:i + 3] in ("\x1b[A", "\x1bOA"):
                    keys.append("up")
                    i += 3
                    continue
                if text[i:i + 3] in ("\x1b[B", "\x1bOB"):
                    keys.append("down")
                    i += 3
                    continue
                if text[i:i + 3] in ("\x1b[C", "\x1bOC"):
                    keys.append("right")
                    i += 3
                    continue
                if text[i:i + 3] in ("\x1b[D", "\x1bOD"):
                    keys.append("left")
                    i += 3
                    continue
                keys.append("esc")
                i += 1
                continue
            keys.append(Terminal._map_char(ch))
            i += 1
        return keys


def run_tui(directory: Path, cover_mode: str = "auto", filter_mode: str = "all") -> int:
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        print("播放器界面需要在终端窗口中运行。")
        print("用法：ncm2audio                        # 扫描当前目录")
        print("      ncm2audio --dir D:\\music")
        return 1
    if cover_mode == "auto":
        cover_mode = detect_cover_mode(probe=True)
    app = App(directory, cover_mode=cover_mode, filter_mode=filter_mode)
    term = Terminal()
    term.enter()
    last_draw = 0.0
    try:
        while app.running:
            for k in term.read_keys():
                if not app.handle_key(k):
                    app.running = False
                    break
            app.tick()
            w, h = term.size()
            if (w, h) != app.size:
                app.size = (w, h)
                app.need_redraw = True
                app._last_gfx_key = None     # 尺寸变了，封面需要重新叠图
                term.write("\x1b[2J")
            now = time.time()
            interval = 0.05
            redraw = (app.need_redraw or
                      (app.animating() and now - last_draw >= interval) or
                      now - last_draw > 0.5)
            if redraw:
                s = app.render_frame(w, h)
                # 图形协议模式：跳过封面矩形，避免文字重绘抹掉已显示的图片
                frame = s.to_ansi(app.truecolor, skip=app._cover_rect)
                term.write(frame + app._gfx)
                app.need_redraw = False
                last_draw = now
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        app.player.stop()
        term.leave()
    return 0


# --------------------------------------------------------------------------
# 截图（用于预览图与自动化验证）
# --------------------------------------------------------------------------
def screenshot(directory: Path, out_path: Path, size=(120, 36), hd: bool = False,
               cover_mode: str = "blocks") -> Path:
    app = App(directory, cover_mode=cover_mode)
    app.size = size
    items = app.filtered_items()
    target = None
    for it in items:
        if app.meta_for(it).get("cover"):
            target = it
            break
    if target is None and items:
        target = items[0]
    if target is not None:
        app.sel = items.index(target)
        meta = app.meta_for(target)
        app.playing_idx = app.sel
        app.player.state = "playing"
        app.player.path = str(target.path)
        app.player.duration = float(meta.get("duration") or 0.0)
        pos = min(84.0, app.player.duration * 0.38) if app.player.duration else 0.0
        app.player._base = pos
        app.player._started = time.time()
        app.set_status(f"▶ 正在播放：{target.display_name()}", ACCENT, 30.0)
    s = app.render_frame(size[0], size[1], simulate_hd=hd)
    PNGRenderer(scale=2).render(s, out_path, overlay=app._overlay if hd else None)
    return out_path
