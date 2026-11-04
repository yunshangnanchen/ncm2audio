# -*- coding: utf-8 -*-
"""ncm_core —— 网易云音乐 NCM 解密核心（纯标准库实现，零第三方依赖）

NCM 容器结构（依据开源项目 ncmdump 系列逆向结论）::

    8B   魔数 "CTENFDAM"
    2B   占位
    4B   密钥长度 n（小端）
    nB   密钥块：逐字节 XOR 0x64 -> AES-128-ECB(CORE_KEY) 解密 -> PKCS#7 去填充
         -> 去掉前 17 字节 "neteasecloudmusic"，得到 RC4 密钥
    2B   占位
    4B   元数据长度 m（小端；0 表示没有元数据）
    mB   元数据块：逐字节 XOR 0x63 -> 去掉 22 字节前缀 "163 key(Don't modify):"
         -> Base64 解码 -> AES-128-ECB(META_KEY) 解密 -> 去掉 6 字节 "music:"
         -> 得到 JSON（歌曲名/歌手/专辑/码率/时长/格式）
    4B   CRC32
    1B   封面版本
    4B   封面帧长度 cover_frame_len
    4B   封面数据长度 k
    kB   封面数据（JPEG / PNG）
    ...  音频数据：与 256 字节循环密钥流逐字节 XOR 即得原始音频

音频密钥流由 RC4 的 KSA 生成 256 字节置换后，按固定公式展开成 256 字节
循环模式（这就是 NCM 被诟病的“伪加密”所在）。
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "NcmError",
    "NcmFile",
    "ConvertResult",
    "AUDIO_EXTS",
    "MAGIC",
    "parse_ncm",
    "decrypt_audio_bytes",
    "decrypt_audio_to_file",
    "peek_audio",
    "guess_format",
    "convert",
    "convert_many",
    "collect_ncm_files",
    "collect_media_files",
    "NcmStream",
    "meta_title",
    "meta_artists",
    "meta_album",
    "format_duration",
    "human_size",
    "safe_filename",
    "read_audio_meta",
    "selftest",
]

MAGIC = b"CTENFDAM"
CORE_KEY = b"hzHRAmso5kInbaxW"
META_KEY = b"#14ljk_!\\]&0U<'("
KEY_XOR = 0x64
META_XOR = 0x63
KEY_PREFIX = b"neteasecloudmusic"
META_PREFIX = b"music:"
META_PLAIN_PREFIX = b"163 key(Don't modify):"
_MAX_SEGMENT = 64 << 20          # 单个段最大 64MB，防止损坏文件导致超大分配
_CHUNK = 1 << 16                 # 64KB，且必须是 256 的整数倍

# 可识别的音频扩展名（按优先级）
AUDIO_EXTS = ("flac", "mp3", "m4a", "wav", "ogg", "aac", "ape", "wma")


class NcmError(Exception):
    """NCM 解析/解密失败"""


# --------------------------------------------------------------------------
# AES-128（ECB）纯 Python 实现
# --------------------------------------------------------------------------
def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，不可约多项式 0x11B"""
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


def _build_aes_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _gmul(x, 3)  # 以生成元 3 遍历 GF(2^8) 乘法群
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    inv = [0] * 256
    for b in range(1, 256):
        inv[b] = exp[255 - log[b]]
    sbox = [0] * 256
    inv_sbox = [0] * 256
    for b in range(256):
        s = inv[b]
        t = s
        for k in range(1, 5):
            t ^= ((s << k) | (s >> (8 - k))) & 0xFF
        sbox[b] = t ^ 0x63
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_aes_tables()


def _expand_key(key: bytes) -> list:
    if len(key) != 16:
        raise NcmError("AES 密钥长度必须为 16 字节")
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]                      # RotWord
            t = [_SBOX[b] for b in t]              # SubWord
            t[0] ^= rcon
            rcon = _gmul(rcon, 2)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [b for word in w for b in word]         # 176 字节轮密钥


def _add_round_key(s, rk, r):
    base = r << 4
    for i in range(16):
        s[i] ^= rk[base + i]


def _sub_bytes(s):
    for i in range(16):
        s[i] = _SBOX[s[i]]


def _inv_sub_bytes(s):
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]


def _shift_rows(s):
    for r in range(1, 4):
        row = [s[r + 4 * c] for c in range(4)]
        for c in range(4):
            s[r + 4 * c] = row[(c + r) & 3]


def _inv_shift_rows(s):
    for r in range(1, 4):
        row = [s[r + 4 * c] for c in range(4)]
        for c in range(4):
            s[r + 4 * c] = row[(c - r) & 3]


def _mix_columns(s):
    for c in range(4):
        i = c << 2
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        s[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        s[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        s[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)


def _inv_mix_columns(s):
    for c in range(4):
        i = c << 2
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        s[i + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        s[i + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        s[i + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def encrypt_block(rk, block: bytes) -> bytes:
    s = list(block)
    _add_round_key(s, rk, 0)
    for r in range(1, 10):
        _sub_bytes(s)
        _shift_rows(s)
        _mix_columns(s)
        _add_round_key(s, rk, r)
    _sub_bytes(s)
    _shift_rows(s)
    _add_round_key(s, rk, 10)
    return bytes(s)


def decrypt_block(rk, block: bytes) -> bytes:
    s = list(block)
    _add_round_key(s, rk, 10)
    for r in range(9, 0, -1):
        _inv_shift_rows(s)
        _inv_sub_bytes(s)
        _add_round_key(s, rk, r)
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    _inv_sub_bytes(s)
    _add_round_key(s, rk, 0)
    return bytes(s)


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 解密（不做去填充，要求 data 长度为 16 的倍数）"""
    if len(data) % 16:
        raise NcmError("AES 数据长度不是 16 的整数倍")
    rk = _expand_key(key)
    out = bytearray(len(data))
    for off in range(0, len(data), 16):
        out[off:off + 16] = decrypt_block(rk, data[off:off + 16])
    return bytes(out)


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 加密（仅用于自测/生成测试样本）"""
    if len(data) % 16:
        raise NcmError("AES 数据长度不是 16 的整数倍")
    rk = _expand_key(key)
    out = bytearray(len(data))
    for off in range(0, len(data), 16):
        out[off:off + 16] = encrypt_block(rk, data[off:off + 16])
    return bytes(out)


def pkcs7_unpad(data: bytes) -> bytes:
    """按 ncmdump 的宽松规则去掉 PKCS#7 填充：末字节 >16 或 0 时视为无填充"""
    if not data:
        return data
    pad = data[-1]
    if pad == 0 or pad > 16 or pad > len(data):
        return data
    return data[:-pad]


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


# --------------------------------------------------------------------------
# NCM 音频密钥流
# --------------------------------------------------------------------------
def build_keybox(key: bytes) -> bytes:
    """RC4 的 KSA：由密钥生成 256 字节置换表"""
    if not key:
        raise NcmError("空密钥")
    kb = bytearray(range(256))
    last = 0
    off = 0
    n = len(key)
    for i in range(256):
        swap = kb[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= n:
            off = 0
        kb[i] = kb[c]
        kb[c] = swap
        last = c
    return bytes(kb)


def keystream_pattern(key: bytes) -> bytes:
    """由密钥生成 256 字节循环密钥流"""
    kb = build_keybox(key)
    out = bytearray(256)
    for p in range(256):
        j = (p + 1) & 0xFF
        kj = kb[j]
        out[p] = kb[(kj + kb[(kj + j) & 0xFF]) & 0xFF]
    return bytes(out)


def _pattern_int(pattern: bytes, size: int) -> int:
    reps = (size + len(pattern) - 1) // len(pattern)
    return int.from_bytes((pattern * reps)[:size], "little")


def xor_pattern(data: bytes, pattern: bytes) -> bytes:
    """用 256 字节循环模式异或数据（bytes 长度任意）"""
    if not data:
        return b""
    n = len(data)
    if n >= 4096:
        pi = _pattern_int(pattern, n & ~0xFF or 256)  # 只对 256 对齐部分做整型异或
        head_len = n & ~0xFF
        out = bytearray(n)
        if head_len:
            out[:head_len] = (int.from_bytes(data[:head_len], "little") ^ pi).to_bytes(head_len, "little")
        rest = n - head_len
        if rest:
            tail_pat = _pattern_int(pattern, rest)
            out[head_len:] = (int.from_bytes(data[head_len:], "little") ^ tail_pat).to_bytes(rest, "little")
        return bytes(out)
    out = bytearray(n)
    for i, b in enumerate(data):
        out[i] = b ^ pattern[i & 0xFF]
    return bytes(out)


# --------------------------------------------------------------------------
# 容器解析
# --------------------------------------------------------------------------
@dataclass
class NcmFile:
    path: Path
    key: bytes                 # 音频密钥（已去掉 17 字节前缀）
    meta: Optional[dict]       # 元数据 JSON（可能为 None）
    cover: bytes               # 封面原始字节（JPEG/PNG），可能为空
    audio_offset: int          # 音频数据起始偏移
    size: int                  # 文件总大小
    _pattern: Optional[bytes] = field(default=None, repr=False)

    @property
    def audio_size(self) -> int:
        return max(0, self.size - self.audio_offset)

    @property
    def pattern(self) -> bytes:
        if self._pattern is None:
            self._pattern = keystream_pattern(self.key)
        return self._pattern

    def cover_ext(self) -> str:
        if self.cover.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if self.cover.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if self.cover.startswith(b"GIF8"):
            return "gif"
        return "jpg"

    def title(self) -> str:
        return meta_title(self.meta, self.path)

    def artists(self) -> list:
        return meta_artists(self.meta)

    def album(self) -> str:
        return meta_album(self.meta)

    def format_hint(self) -> str:
        fmt = (self.meta or {}).get("format")
        if isinstance(fmt, str) and fmt.lower() in AUDIO_EXTS:
            return fmt.lower()
        return ""

    def duration(self) -> float:
        d = (self.meta or {}).get("duration")
        try:
            return float(d) / 1000.0 if d else 0.0
        except (TypeError, ValueError):
            return 0.0

    def bitrate(self) -> int:
        b = (self.meta or {}).get("bitrate")
        try:
            return int(b)
        except (TypeError, ValueError):
            return 0


def _read_exact(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise NcmError("文件被截断或长度字段异常")
    return b


def parse_ncm(path) -> NcmFile:
    """只解析头部（密钥/元数据/封面），不读音频数据"""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise NcmError(f"无法读取文件：{exc}") from exc

    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != MAGIC:
            if len(magic) < 8:
                raise NcmError("文件太小，不是 NCM 文件")
            raise NcmError("不是 NCM 文件（缺少 CTENFDAM 头）")

        f.seek(2, 1)                                  # 跳过 2 字节占位
        n = struct.unpack("<I", _read_exact(f, 4))[0]
        if n == 0 or n % 16 or n > _MAX_SEGMENT:
            raise NcmError("密钥段损坏（长度字段异常）")
        blob = bytes(b ^ KEY_XOR for b in _read_exact(f, n))
        plain = pkcs7_unpad(aes_ecb_decrypt(CORE_KEY, blob))
        if not plain.startswith(KEY_PREFIX):
            raise NcmError("密钥解密失败：可能不是有效的 NCM 文件")
        key = plain[len(KEY_PREFIX):]
        if not key:
            raise NcmError("密钥为空")

        n = struct.unpack("<I", _read_exact(f, 4))[0]
        meta = None
        if 0 < n <= _MAX_SEGMENT:
            data = _read_exact(f, n)
            dec = bytes(b ^ META_XOR for b in data)
            try:
                if dec.startswith(META_PLAIN_PREFIX):
                    dec = dec[len(META_PLAIN_PREFIX):]
                raw = base64.b64decode(dec + b"=" * (-len(dec) % 4))
                txt = pkcs7_unpad(aes_ecb_decrypt(META_KEY, raw))
                if txt.startswith(META_PREFIX):
                    txt = txt[len(META_PREFIX):]
                meta = json.loads(txt.decode("utf-8", "replace"))
                if not isinstance(meta, dict):
                    meta = None
            except (ValueError, NcmError, UnicodeDecodeError):
                meta = None

        f.seek(5, 1)                                  # CRC32(4) + 封面版本(1)
        frame_len, img_len = struct.unpack("<II", _read_exact(f, 8))
        cover = b""
        if 0 < img_len <= _MAX_SEGMENT:
            try:
                cover = _read_exact(f, img_len)
            except NcmError:
                cover = b""
        if frame_len > img_len:
            f.seek(frame_len - img_len, 1)
        audio_offset = f.tell()

    if audio_offset > size:
        raise NcmError("音频偏移超出文件大小")

    return NcmFile(
        path=path, key=key, meta=meta, cover=cover,
        audio_offset=audio_offset, size=size,
    )


def decrypt_audio_bytes(ncm: NcmFile, data: bytes, offset: int = 0) -> bytes:
    """解密一段音频数据；offset 为该段在音频流中的相对偏移（须为 256 对齐或提供错位处理）"""
    pat = ncm.pattern
    if offset % 256 == 0:
        rotated = pat
    else:
        k = offset % 256
        rotated = pat[k:] + pat[:k]
    return xor_pattern(data, rotated)


def peek_audio(ncm: NcmFile, n: int = 32) -> bytes:
    """只解密音频开头 n 字节，用于识别真实格式"""
    with open(ncm.path, "rb") as f:
        f.seek(ncm.audio_offset)
        head = f.read(n)
    return decrypt_audio_bytes(ncm, head, 0)


def decrypt_audio_to_file(ncm: NcmFile, out_path, chunk: int = _CHUNK) -> int:
    """流式解密音频写入 out_path，返回写入字节数"""
    out_path = Path(out_path)
    written = 0
    pat = ncm.pattern
    with open(ncm.path, "rb") as src, open(out_path, "wb") as dst:
        src.seek(ncm.audio_offset)
        while True:
            data = src.read(chunk)
            if not data:
                break
            dst.write(xor_pattern(data, pat))
            written += len(data)
    return written


# --------------------------------------------------------------------------
# 惰性解密流：把 .ncm 的音频段包装成可读、可 seek 的文件对象（内存播放用）
# --------------------------------------------------------------------------
class NcmStream(io.RawIOBase):
    """可随机读取的 NCM 音频解密流

    - read/seek 语义与普通文件一致：密钥流按当前位置旋转，任意偏移读出的
      字节与 decrypt_audio_bytes 全量解密结果完全一致
    - 供 pygame 等播放器“边读边解”，内存占用恒定（约等于读写缓冲）
    """

    def __init__(self, ncm: "NcmFile"):
        super().__init__()
        self._ncm = ncm
        self._pos = 0
        self._fh = open(ncm.path, "rb")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = min(len(b), self._ncm.audio_size - self._pos)
        if n <= 0:
            return 0
        self._fh.seek(self._ncm.audio_offset + self._pos)
        data = self._fh.read(n)
        out = decrypt_audio_bytes(self._ncm, data, self._pos)
        n = len(out)
        b[:n] = out
        self._pos += n
        return n

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = max(0, self._ncm.audio_size - self._pos)
        buf = bytearray(min(n, self._ncm.audio_size - self._pos))
        got = self.readinto(buf)
        return bytes(buf[:got])

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._ncm.audio_size + offset
        else:
            raise ValueError(f"无效的 whence: {whence!r}")
        self._pos = max(0, min(self._pos, self._ncm.audio_size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass
        super().close()


def guess_format(head: bytes) -> str:
    """根据解密后的音频头判断扩展名"""
    if not head:
        return "bin"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"MAC ":
        return "ape"
    return "bin"


# --------------------------------------------------------------------------
# 元数据/命名工具
# --------------------------------------------------------------------------
def meta_title(meta: Optional[dict], fallback: Optional[Path] = None) -> str:
    if meta:
        for k in ("musicName", "name", "title"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if fallback is not None:
        return Path(fallback).stem
    return "未知歌曲"


def meta_artists(meta: Optional[dict]) -> list:
    if not meta:
        return []
    out = []
    for a in meta.get("artist") or []:
        if isinstance(a, (list, tuple)) and a:
            out.append(str(a[0]))
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def meta_album(meta: Optional[dict]) -> str:
    if not meta:
        return ""
    v = meta.get("album")
    return v.strip() if isinstance(v, str) else ""


def format_duration(seconds: float) -> str:
    try:
        s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "--:--"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_size(n: int) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


_BAD_CHARS = set('<>:"/\\|?*')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, max_len: int = 150) -> str:
    """清理成 Windows 合法文件名（保留 - _ 空格等常用字符）"""
    out = []
    for c in name:
        o = ord(c)
        if c in _BAD_CHARS or o < 32 or o == 127:
            out.append("_")
        else:
            out.append(c)
    name = "".join(out)
    name = name.replace("\u3000", " ")
    name = " ".join(name.split())
    name = name.strip(" .")
    if not name:
        name = "未命名"
    if name.upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def _unique_path(path: Path, taken: Optional[set] = None) -> Path:
    """若目标已存在（或本次运行已占用），追加 (2)(3)..."""
    if not path.exists() and not (taken and str(path).lower() in taken):
        return path
    stem, suffix = path.stem, path.suffix
    idx = 2
    while True:
        cand = path.with_name(f"{stem} ({idx}){suffix}")
        if not cand.exists() and not (taken and str(cand).lower() in taken):
            return cand
        idx += 1


# --------------------------------------------------------------------------
# 转换
# --------------------------------------------------------------------------
@dataclass
class ConvertResult:
    source: Path
    output: Optional[Path]
    meta: Optional[dict] = None
    ext: str = ""
    cover_path: Optional[Path] = None
    skipped: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def convert(
    ncm_path,
    out_dir=None,
    *,
    overwrite: bool = False,
    save_cover: bool = False,
    write_tag: bool = True,
    taken: Optional[set] = None,
    progress=None,
) -> ConvertResult:
    """把单个 .ncm 转成音频文件

    - 输出目录默认为源文件所在目录
    - 文件名优先使用元数据「歌手 - 歌曲名」
    - 默认不覆盖：已存在则跳过（skipped=True）
    """
    ncm_path = Path(ncm_path)
    try:
        ncm = parse_ncm(ncm_path)
    except NcmError as exc:
        return ConvertResult(source=ncm_path, output=None, error=str(exc))
    except OSError as exc:
        return ConvertResult(source=ncm_path, output=None, error=f"无法打开文件：{exc}")

    ext = guess_format(peek_audio(ncm, 64)) or ncm.format_hint() or "bin"
    meta = ncm.meta
    base = f"{meta_title(meta, ncm_path)}"
    artists = meta_artists(meta)
    if artists:
        base = f"{' & '.join(artists)} - {base}"
    base = safe_filename(base, 120) or ncm_path.stem

    out_dir = Path(out_dir) if out_dir else ncm_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{base}.{ext}"

    if target.exists() and not overwrite:
        if taken is not None and str(target).lower() in taken:
            # 本次批量转换里已经生成过同名文件（不同歌曲撞名），加序号区分
            target = _unique_path(target, taken)
        else:
            return ConvertResult(source=ncm_path, output=target, meta=meta, ext=ext, skipped=True)

    if not overwrite:
        target = _unique_path(target, taken)
    if taken is not None:
        taken.add(str(target).lower())

    try:
        n = decrypt_audio_to_file(ncm, target)
    except (NcmError, OSError) as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return ConvertResult(source=ncm_path, output=None, meta=meta, ext=ext, error=str(exc))

    if n <= 0:
        target.unlink(missing_ok=True)
        return ConvertResult(source=ncm_path, output=None, meta=meta, ext=ext, error="音频数据为空")

    if write_tag:
        write_tags(target, meta, ncm.cover)

    if progress:
        progress(0.0, 1.0)

    cover_path = None
    if save_cover and ncm.cover:
        cext = ncm.cover_ext()
        cover_path = target.with_name(f"{target.stem}.cover.{cext}")
        try:
            cover_path.write_bytes(ncm.cover)
        except OSError:
            cover_path = None

    return ConvertResult(source=ncm_path, output=target, meta=meta, ext=ext, cover_path=cover_path)


def collect_ncm_files(inputs: Iterable) -> list:
    """把文件/文件夹列表展开成 NCM 文件列表（文件夹递归扫描，大小写不敏感）"""
    found = []
    seen = set()

    def add(p: Path):
        key = str(p.resolve()).lower() if p.exists() else str(p).lower()
        if key not in seen:
            seen.add(key)
            found.append(p)

    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                try:
                    if child.is_file() and child.suffix.lower() == ".ncm":
                        add(child)
                except OSError:
                    continue
        elif p.is_file():
            add(p)
    return found


def collect_media_files(directory, recursive: bool = True) -> list:
    """扫描目录下的 .ncm 与普通音频文件（用于播放器列表）"""
    directory = Path(directory)
    files = []
    exts = {f".{e}" for e in AUDIO_EXTS} | {".ncm"}
    iterator = directory.rglob("*") if recursive else directory.glob("*")
    for child in iterator:
        try:
            if child.is_file() and child.suffix.lower() in exts:
                files.append(child)
        except OSError:
            continue
    files.sort(key=lambda p: (p.name.lower(), str(p).lower()))
    return files


def convert_many(ncm_files, out_dir=None, *, overwrite=False, save_cover=False, on_result=None):
    taken: set = set()
    results = []
    for p in ncm_files:
        res = convert(p, out_dir, overwrite=overwrite, save_cover=save_cover, taken=taken)
        results.append(res)
        if on_result:
            on_result(res)
    return results


# --------------------------------------------------------------------------
# 普通音频标签读取（可选依赖 mutagen，缺失时优雅降级）
# --------------------------------------------------------------------------
def read_audio_meta(path) -> dict:
    """读取普通音频文件的标签信息；无 mutagen 时返回基础信息"""
    path = Path(path)
    base = {
        "title": path.stem,
        "artists": [],
        "album": "",
        "duration": 0.0,
        "bitrate": 0,
        "cover": b"",
        "format": path.suffix.lower().lstrip("."),
    }
    try:
        import mutagen  # type: ignore
    except ImportError:
        return base
    try:
        f = mutagen.File(str(path))
    except Exception:
        return base
    if f is None:
        return base
    try:
        info = getattr(f, "info", None)
        if info is not None:
            base["duration"] = float(getattr(info, "length", 0.0) or 0.0)
            base["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
            if not base["format"]:
                base["format"] = type(f).__name__.lower()
        tags = f.tags or {}

        def tag(*keys):
            for k in keys:
                try:
                    v = tags.get(k)
                except Exception:
                    v = None
                if isinstance(v, list):
                    v = v[0] if v else None
                if v:
                    return str(v)
            return None

        t = tag("title", "TIT2", "\xa9nam")
        if t:
            base["title"] = t
        ar = tag("artist", "TPE1", "\xa9ART")
        if ar:
            base["artists"] = [x.strip() for x in ar.replace("/", ";").split(";") if x.strip()]
        al = tag("album", "TALB", "\xa9alb")
        if al:
            base["album"] = al

        cover = b""
        try:
            pics = getattr(f, "pictures", None)
            if pics:
                cover = bytes(pics[0].data)
            elif tags:
                for k, v in list(tags.items()):
                    if k.startswith("APIC"):
                        cover = bytes(getattr(v, "data", b"") or b"")
                        break
                    if k == "covr" and v:
                        cover = bytes(v[0])
                        break
                    if k == "metadata_block_picture" and v:
                        from mutagen.flac import Picture as _Picture
                        cover = _Picture(base64.b64decode(v[0])).data
                        break
        except Exception:
            cover = b""
        base["cover"] = cover or b""
    except Exception:
        return base
    return base


# --------------------------------------------------------------------------
# 把歌曲信息 / 封面写入转换后的音频文件（可选依赖 mutagen，失败静默跳过）
# --------------------------------------------------------------------------
def write_tags(path, meta: Optional[dict], cover: bytes = b"", artists=None) -> bool:
    """写入标题/歌手/专辑/封面到 mp3 / flac / m4a / ogg 文件"""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return False
    path = Path(path)
    suffix = path.suffix.lower()
    title = meta_title(meta) if meta else ""
    artist_list = list(artists) if artists else meta_artists(meta)
    album = meta_album(meta)
    png = cover[:8] == b"\x89PNG\r\n\x1a\n"
    try:
        if suffix == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(str(path))
            f["title"] = [title]
            if artist_list:
                f["artist"] = artist_list
            if album:
                f["album"] = [album]
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/png" if png else "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover
                f.add_picture(pic)
            f.save()
            return True
        if suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC
            try:
                tags = ID3(str(path))
            except ID3NoHeaderError:
                tags = ID3()
            if title:
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=[title]))
            if artist_list:
                tags.delall("TPE1")
                tags.add(TPE1(encoding=3, text=artist_list))
            if album:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=[album]))
            if cover:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/png" if png else "image/jpeg",
                              type=3, desc="Cover", data=cover))
            tags.save(str(path), v2_version=3)
            return True
        if suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(str(path))
            f["\xa9nam"] = [title]
            if artist_list:
                f["\xa9ART"] = artist_list
            if album:
                f["\xa9alb"] = [album]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if png else MP4Cover.FORMAT_JPEG
                f["covr"] = [MP4Cover(cover, imageformat=fmt)]
            f.save()
            return True
        if suffix == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            f = OggVorbis(str(path))
            f["title"] = [title]
            if artist_list:
                f["artist"] = artist_list
            if album:
                f["album"] = [album]
            f.save()
            return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
def selftest() -> bool:
    """核心算法自测：AES FIPS-197 向量 + 密钥流往返测试"""
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'OK' if good else 'FAIL'}] {name}")

    # FIPS-197 C.1
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    rk = _expand_key(key)
    check("AES-128 加密向量", encrypt_block(rk, pt), ct)
    check("AES-128 解密向量", decrypt_block(rk, ct), pt)

    # NIST SP 800-38A ECB 向量
    k2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    p2 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    c2 = bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97")
    check("AES-128 ECB 向量", aes_ecb_encrypt(k2, p2), c2)
    check("AES-128 ECB 回解", aes_ecb_decrypt(k2, c2), p2)

    # PKCS#7
    check("PKCS#7 去填充", pkcs7_unpad(pkcs7_pad(b"hello world")), b"hello world")

    # 密钥流往返
    pat = keystream_pattern(CORE_KEY)
    check("密钥流长度", len(pat), 256)
    payload = bytes(range(256)) * 3 + b"tail!"
    check("异或往返(小)", xor_pattern(xor_pattern(payload, pat), pat), payload)
    big = os.urandom(70000)
    check("异或往返(大/整型路径)", xor_pattern(xor_pattern(big, pat), pat), big)
    shifted = xor_pattern(xor_pattern(big, pat[7:] + pat[:7]), pat[7:] + pat[:7])
    check("异或往返(错位)", shifted, big)
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
