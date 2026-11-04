# -*- coding: utf-8 -*-
"""三方案播放基准：A 惰性解密流 / B BytesIO 全量内存 / C 临时文件

- 实时播放基准：用目录里真实的 .ncm，静音（音量 0）实测
  准备耗时、load 耗时、播放、中段 seek、末尾 seek 落地、循环稳定性、内存
- 数据层基准：不依赖 pygame，测解密读吞吐 / 随机读延迟 / 全量解密时间与内存 / 写盘时间
  可用 --big N 额外构造 N MB 合成大文件压测（默认 64）

用法：
    venv/Scripts/python.exe bench_playback.py
    venv/Scripts/python.exe bench_playback.py --big 128
"""
import argparse
import base64
import ctypes
import gc
import io
import json
import os
import random
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import ncm_core as c

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402


# --------------------------------------------------------------------------
# 内存采样（Windows 进程级）
# --------------------------------------------------------------------------
class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_PSAPI = ctypes.WinDLL("psapi") if os.name == "nt" else None
if _PSAPI:
    _PSAPI.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_uint32]
    _PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int


def mem_now():
    """(工作集, 工作集峰值, 私有提交字节)"""
    if os.name != "nt":
        return (0, 0, 0)
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    ok = _PSAPI.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    if not ok:
        return (0, 0, 0)
    return (pmc.WorkingSetSize, pmc.PeakWorkingSetSize, pmc.PagefileUsage)


def mb(n):
    return f"{n / 1048576:.1f}MB" if n else "-"


def ms(t):
    return f"{t * 1000:.1f}ms"


def settle():
    gc.collect()
    time.sleep(0.15)


# --------------------------------------------------------------------------
# 三个方案的“准备”实现
# --------------------------------------------------------------------------
def read_ciphertext(ncm):
    with open(ncm.path, "rb") as f:
        f.seek(ncm.audio_offset)
        return f.read(ncm.audio_size)


def prep_a(ncm):
    """A 惰性解密流：BufferedReader(NcmStream)"""
    return io.BufferedReader(c.NcmStream(ncm), 1 << 18), None


def prep_b(ncm):
    """B BytesIO：一次性解密进内存"""
    data = c.decrypt_audio_bytes(ncm, read_ciphertext(ncm), 0)
    return io.BytesIO(data), None


def prep_c(ncm, ext="mp3"):
    """C 临时文件：解密写入 %TEMP%"""
    fd, tmp = tempfile.mkstemp(suffix="." + ext)
    os.close(fd)
    with open(tmp, "wb") as f:
        f.write(c.decrypt_audio_bytes(ncm, read_ciphertext(ncm), 0))

    def cleanup():
        try:
            os.remove(tmp)
        except OSError:
            pass
    return tmp, cleanup


# --------------------------------------------------------------------------
# 实时播放基准
# --------------------------------------------------------------------------
def bench_realtime(label, ncm, prep, cleanup_always, play_secs=2.0, seek_to=120.0, cycles=8):
    res = {"label": label}
    try:
        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
    except Exception:
        pass
    settle()
    m0 = mem_now()

    t = time.perf_counter()
    try:
        handle, cleanup = prep(ncm)
    except Exception as exc:
        res["error"] = f"准备失败: {exc!r}"
        return res
    res["setup"] = time.perf_counter() - t
    m1 = mem_now()

    load_err = None
    t = time.perf_counter()
    try:
        pygame.mixer.music.load(handle)
    except Exception as exc:
        load_err = repr(exc)
    res["load"] = time.perf_counter() - t
    m2 = mem_now()
    res["load_err"] = load_err

    played = False
    if load_err is None:
        pygame.mixer.music.set_volume(0.0)
        pygame.mixer.music.play()
        wall0 = time.time()
        peak_ws = m2[0]
        while time.time() - wall0 < play_secs:
            time.sleep(0.2)
            peak_ws = max(peak_ws, mem_now()[0])
        res["peak_ws_play"] = max(0, peak_ws - m0[0])
        played = pygame.mixer.music.get_busy()
    res["play_ok"] = played

    # 中段 seek
    seek_err = None
    if played:
        try:
            pygame.mixer.music.set_pos(seek_to)
        except Exception as exc:
            seek_err = repr(exc)
        time.sleep(0.4)
        res["busy_after_seek"] = pygame.mixer.music.get_busy()
    res["seek_err"] = seek_err

    # 末尾 seek 落地检查：跳到最后 3.5 秒，应在 8 秒内自然结束
    end_ok = None
    if played and ncm.duration() > 10:
        try:
            pygame.mixer.music.set_pos(ncm.duration() - 3.5)
        except Exception as exc:
            end_ok = f"ERR {exc!r}"
        else:
            t0 = time.time()
            ended = False
            while time.time() - t0 < 8:
                if not pygame.mixer.music.get_busy():
                    ended = True
                    break
                time.sleep(0.2)
            end_ok = ended
    res["end_seek_ok"] = end_ok

    try:
        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass
    if cleanup_always:
        cleanup()
    settle()
    m3 = mem_now()
    res["ws_setup"] = m1[0] - m0[0]
    res["ws_after_load"] = m2[0] - m0[0]
    res["page_after_load"] = m2[2] - m0[2]
    res["ws_after_cleanup"] = m3[0] - m0[0]

    # 循环稳定性：反复 载入→播放→停止
    fails = 0
    for _ in range(cycles):
        try:
            h2, cl2 = prep(ncm)
            pygame.mixer.music.load(h2)
            pygame.mixer.music.play()
            time.sleep(0.12)
            if not pygame.mixer.music.get_busy():
                fails += 1
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            try:
                h2.close()
            except Exception:
                pass
            if cl2:
                cl2()
        except Exception:
            fails += 1
    res["cycle_fails"] = fails
    res["cycles"] = cycles
    return res


# --------------------------------------------------------------------------
# 数据层基准（不依赖 pygame）
# --------------------------------------------------------------------------
def data_level(ncm, label, seeks=128, chunk=1 << 16):
    size = ncm.audio_size
    row = {"label": label, "size": size}

    # A：顺序吞吐
    br = io.BufferedReader(c.NcmStream(ncm), 1 << 20)
    t0 = time.perf_counter()
    total = 0
    while True:
        d = br.read(chunk)
        if not d:
            break
        total += len(d)
    dt = time.perf_counter() - t0
    row["a_seq_mbps"] = total / dt / 1048576
    # A：随机读（裸流 seek + 读，测真实延迟；不经 BufferedReader 以免缓冲回填干扰）
    rng = random.Random(1)
    positions = [rng.randrange(0, max(1, size - chunk)) for _ in range(seeks)]
    raw = c.NcmStream(ncm)
    t0 = time.perf_counter()
    for p in positions:
        raw.seek(p)
        raw.read(chunk)
    dt2 = time.perf_counter() - t0
    row["a_rand_mbps"] = seeks * chunk / dt2 / 1048576
    row["a_rand_lat_ms"] = dt2 / seeks * 1000
    raw.close()

    # B：全量解密
    settle()
    m0 = mem_now()
    t0 = time.perf_counter()
    obj, _ = prep_b(ncm)
    dt3 = time.perf_counter() - t0
    m1 = mem_now()
    row["b_time"] = dt3
    row["b_ws"] = m1[0] - m0[0]
    row["b_page"] = m1[2] - m0[2]

    # C：解密+写临时文件
    t0 = time.perf_counter()
    tmp, cleanup = prep_c(ncm, ext=c.guess_format(c.peek_audio(ncm, 16)))
    dt4 = time.perf_counter() - t0
    row["c_time"] = dt4
    cleanup()
    return row


# --------------------------------------------------------------------------
# 合成大文件
# --------------------------------------------------------------------------
def build_synthetic_ncm(size_mb: int, out_path: Path):
    audio = os.urandom(size_mb * 1048576)
    key = b"bench-key-0123456789ab"
    key_seg = bytes(x ^ c.KEY_XOR for x in
                    c.aes_ecb_encrypt(c.CORE_KEY, c.pkcs7_pad(c.KEY_PREFIX + key)))
    meta = {"musicName": "bench", "artist": [["bench", "1"]], "album": "bench",
            "bitrate": 1411200, "duration": 300000, "format": "flac"}
    raw = c.META_PREFIX + json.dumps(meta).encode()
    meta_seg = bytes(x ^ c.META_XOR for x in
                     (c.META_PLAIN_PREFIX +
                      base64.b64encode(c.aes_ecb_encrypt(c.META_KEY, c.pkcs7_pad(raw)))))
    head = c.MAGIC + b"\x01\x70" + struct.pack("<I", len(key_seg)) + key_seg
    head += struct.pack("<I", len(meta_seg)) + meta_seg
    head += b"\x00" * 4 + b"\x00" + struct.pack("<II", 0, 0)
    with open(out_path, "wb") as f:
        f.write(head)
        f.write(c.xor_pattern(audio, c.keystream_pattern(key)))
    return out_path


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--big", type=int, default=64, help="合成大文件压测大小 MB（0 关闭）")
    ap.add_argument("--cycles", type=int, default=8)
    args = ap.parse_args()

    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
    pygame.mixer.music.set_volume(0.0)

    ncm_files = c.collect_ncm_files([ROOT])
    if not ncm_files:
        print("目录里没有 .ncm 文件")
        return 2
    ncm = c.parse_ncm(ncm_files[0])
    ext = c.guess_format(c.peek_audio(ncm, 16))

    print(f"样本: {ncm.path.name}  音频 {c.human_size(ncm.audio_size)}  格式 {ext}"
          f"  时长 {c.format_duration(ncm.duration())}")
    print(f"环境: python {sys.version.split()[0]} | pygame-ce {pygame.version.ver}"
          f" | SDL_mixer {'.'.join(map(str, pygame.mixer.get_sdl_mixer_version()))}")
    print()

    print("== 实时播放基准（静音）==")
    header = f"{'方案':<4}{'准备':>10}{'load':>10}  {'播放':<6}{'中段seek':<10}{'末尾seek落地':<12}" \
             f"{'循环失败':>8}{'load后工作集':>14}{'播放峰值':>12}"
    print(header)
    rows = []
    for label, prep, cl in (("A流", prep_a, False), ("B内存", prep_b, False),
                            ("C临时", lambda n: prep_c(n, ext), True)):
        r = bench_realtime(label, ncm, prep, cl, cycles=args.cycles)
        rows.append(r)
        if "error" in r:
            print(f"{label:<4} {r['error']}")
            continue
        print(f"{label:<4}{ms(r['setup']):>10}{ms(r['load']):>10}  "
              f"{'✓' if r['play_ok'] else '✗':<6}"
              f"{('✓' if r.get('busy_after_seek') else '✗') if r['seek_err'] is None else 'ERR':<10}"
              f"{str(r.get('end_seek_ok')):<12}"
              f"{str(r['cycle_fails']) + '/' + str(r['cycles']):>8}"
              f"{mb(r['ws_after_load']):>14}{mb(r.get('peak_ws_play', 0)):>12}")
        if r["load_err"]:
            print(f"     └─ load 异常: {r['load_err']}")
        if r["seek_err"]:
            print(f"     └─ 中段 seek 异常: {r['seek_err']}")
    print()

    print("== 数据层基准（解密吞吐 / 内存）==")
    print(f"{'样本':<18}{'大小':>10}{'A顺序读':>12}{'A随机读':>12}{'随机延迟':>10}"
          f"{'B全量解密':>12}{'B内存增量':>12}{'C写盘':>10}")
    dl_rows = [data_level(ncm, "真实歌曲")]
    print(f"{'真实歌曲':<18}{mb(dl_rows[0]['size']):>10}"
          f"{dl_rows[0]['a_seq_mbps']:>10.0f}MB/s{dl_rows[0]['a_rand_mbps']:>10.0f}MB/s"
          f"{dl_rows[0]['a_rand_lat_ms']:>8.2f}ms"
          f"{ms(dl_rows[0]['b_time']):>12}{mb(max(0, dl_rows[0]['b_page'])):>12}"
          f"{ms(dl_rows[0]['c_time']):>10}")
    if args.big > 0:
        with tempfile.TemporaryDirectory() as td:
            big = build_synthetic_ncm(args.big, Path(td) / "big.ncm")
            bncm = c.parse_ncm(big)
            r = data_level(bncm, "合成大文件")
            dl_rows.append(r)
            print(f"{f'合成{args.big}MB':<18}{mb(r['size']):>10}"
                  f"{r['a_seq_mbps']:>10.0f}MB/s{r['a_rand_mbps']:>10.0f}MB/s"
                  f"{r['a_rand_lat_ms']:>8.2f}ms"
                  f"{ms(r['b_time']):>12}{mb(max(0, r['b_page'])):>12}"
                  f"{ms(r['c_time']):>10}")

    # 写报告
    report = ROOT / "播放方案测试报告.md"
    with open(report, "w", encoding="utf-8") as f:
        f.write("# 三方案播放基准测试报告\n\n")
        f.write(f"- 样本：`{ncm.path.name}`（{c.human_size(ncm.audio_size)}，{ext}，"
                f"{c.format_duration(ncm.duration())}）\n")
        f.write(f"- 环境：Python {sys.version.split()[0]}，pygame-ce {pygame.version.ver}，"
                f"SDL_mixer {'.'.join(map(str, pygame.mixer.get_sdl_mixer_version()))}，"
                f"静音（音量 0）运行\n")
        f.write(f"- 内存列均为进程工作集/私有提交的**增量**；循环稳定性 = 反复"
                f"「载入→播放→停止」{args.cycles} 次的失败数\n\n")
        f.write("## 实时播放基准\n\n")
        f.write("| 方案 | 准备 | load | 播放 | 中段seek | 末尾seek落地 | 循环失败 | load后工作集 | 播放峰值工作集 |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for r in rows:
            if "error" in r:
                f.write(f"| {r['label']} | — | — | — | — | — | — | — | {r['error']} |\n")
                continue
            seek_mid = "ERR" if r["seek_err"] else ("✓" if r.get("busy_after_seek") else "✗")
            f.write(f"| {r['label']} | {ms(r['setup'])} | {ms(r['load'])} "
                    f"| {'✓' if r['play_ok'] else '✗'} | {seek_mid} "
                    f"| {r.get('end_seek_ok')} | {r['cycle_fails']}/{r['cycles']} "
                    f"| {mb(r['ws_after_load'])} | {mb(r.get('peak_ws_play', 0))} |\n")
        f.write("\n## 数据层基准\n\n")
        f.write("| 样本 | 大小 | A 顺序读 | A 随机读 | 随机延迟 | B 全量解密 | B 内存增量 | C 解密+写盘 |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for r in dl_rows:
            f.write(f"| {r['label']} | {mb(r['size'])} | {r['a_seq_mbps']:.0f}MB/s "
                    f"| {r['a_rand_mbps']:.0f}MB/s | {r['a_rand_lat_ms']:.2f}ms "
                    f"| {ms(r['b_time'])} | {mb(max(0, r['b_page']))} | {ms(r['c_time'])} |\n")
        f.write("\n> B 内存增量按**私有提交字节**计（BytesIO 持有的解密数据）。\n")
    print(f"\n报告已写入: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
