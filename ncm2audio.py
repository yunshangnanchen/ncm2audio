#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ncm2audio —— 网易云音乐 NCM 解密工具 / 终端播放器

用法::

    ncm2audio                              # 打开播放器界面（扫描当前目录）
    ncm2audio a.ncm b.ncm                  # 批量解密文件
    ncm2audio D:\\music                     # 批量解密文件夹（递归）
    ncm2audio --dir D:\\music               # 播放器界面扫描指定目录

把 .ncm 文件或文件夹直接拖到「拖入即解密.bat」上也可以一键解密。

加入 PATH 后可在任意目录直接使用 ``ncm2audio`` 命令，默认扫描**运行命令时所在的
目录**（而不是程序安装目录），运行 ``安装到PATH.bat`` 即可完成配置。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ncm_core as core

VERSION = "1.1.0"


def default_scan_dir() -> Path:
    """全局调用时的默认扫描目录：当前工作目录。

    从 PATH 里调用 ``ncm2audio`` 时，用户期望扫描的是「我现在所在的目录」，
    而不是程序安装目录。cwd 取不到（目录被删除 / 无权限）时退回脚本目录。
    """
    try:
        cwd = Path.cwd()
    except OSError:
        return ROOT
    return cwd if cwd.is_dir() else ROOT


def _setup_console() -> None:
    """尽量让控制台输出 UTF-8，避免中文用户名/路径乱码"""
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream and getattr(stream, "reconfigure", None):
                enc = (stream.encoding or "").lower()
                if enc not in ("utf-8", "utf8", "cp65001"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _pause() -> None:
    try:
        input("\n按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


def batch_convert(paths, out_dir, overwrite: bool, cover: bool, write_tag: bool,
                  no_pause: bool) -> int:
    files = core.collect_ncm_files(paths)
    if not files:
        print("没有找到需要解密的 .ncm 文件。")
        if not no_pause:
            _pause()
        return 2
    print(f"NCM 解密 · 共 {len(files)} 个文件\n")
    ok = skip = fail = 0
    t0 = time.time()
    taken: set = set()
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}")
        try:
            res = core.convert(f, out_dir, overwrite=overwrite, save_cover=cover,
                               write_tag=write_tag, taken=taken)
        except Exception as exc:  # 兜底，避免批处理中断
            fail += 1
            print(f"      ✗ 未知错误：{exc}")
            continue
        if res.error:
            fail += 1
            print(f"      ✗ {res.error}")
        elif res.skipped:
            skip += 1
            print(f"      已存在，跳过 → {res.output}")
        else:
            ok += 1
            print(f"      ✓ → {res.output}")
            if res.cover_path:
                print(f"        封面 → {res.cover_path}")
    dt = time.time() - t0
    print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}，用时 {dt:.1f} 秒")
    if not no_pause:
        _pause()
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    _setup_console()
    parser = argparse.ArgumentParser(
        prog="ncm2audio",
        description="网易云音乐 NCM 解密工具 / 终端播放器（拖入文件或文件夹即可解密）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  ncm2audio                              打开播放器（扫描当前目录）\n"
               "  ncm2audio song.ncm                     解密单个文件\n"
               "  ncm2audio D:\\music -o D:\\out           解密整个文件夹到指定目录\n",
    )
    parser.add_argument("paths", nargs="*", help="要解密的 .ncm 文件或文件夹（可多个）")
    parser.add_argument("-o", "--out", metavar="目录", help="输出目录（默认与源文件同目录）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--cover", action="store_true", help="额外导出封面图片")
    parser.add_argument("--no-tag", action="store_true", help="不把歌曲信息/封面写入输出文件")
    parser.add_argument("--tui", action="store_true", help="强制打开播放器界面")
    parser.add_argument("--dir", metavar="目录", help="播放器界面扫描的目录（默认当前目录）")
    parser.add_argument("--filter", choices=["all", "ncm", "audio"], default="all",
                        help="播放器初始筛选")
    parser.add_argument("--cover-mode", choices=["auto", "kitty", "iterm", "sixel", "blocks", "none"],
                        default="auto", help="封面渲染协议（默认自动检测）")
    parser.add_argument("--volume", type=float, default=0.75, help="初始音量 0.0~1.0")
    parser.add_argument("--screenshot", metavar="PNG", help="生成界面截图（用于预览/调试）")
    parser.add_argument("--size", default="120x36", help="截图尺寸，如 120x36")
    parser.add_argument("--hd", action="store_true", help="截图中模拟高清封面（图形协议效果）")
    parser.add_argument("--self-test", action="store_true", help="运行核心算法自测")
    parser.add_argument("--no-pause", action="store_true", help="结束后不等待回车")
    parser.add_argument("-v", "--version", action="version", version=f"ncm2audio {VERSION}")
    args = parser.parse_args(argv)

    # 兜底：cmd 的 %~dp0 末尾带反斜杠时，"--dir \"...\\\"" 会让 \" 被当成转义引号，
    # 路径末尾多出一个引号；Windows 文件名不允许引号，直接去掉即可。
    def _clean_path(p):
        return p.replace('"', "").strip() if p else p

    if args.dir:
        args.dir = _clean_path(args.dir)
    if args.out:
        args.out = _clean_path(args.out)
    if args.paths:
        args.paths = [cp for cp in (_clean_path(p) for p in args.paths) if cp]

    if args.self_test:
        ok = core.selftest()
        print("自测通过" if ok else "自测失败")
        return 0 if ok else 1

    if args.screenshot:
        import ncm_tui
        scan_dir = Path(args.dir) if args.dir else (Path(args.paths[0]) if args.paths else default_scan_dir())
        if scan_dir.is_file():
            scan_dir = scan_dir.parent
        try:
            w, h = (int(x) for x in args.size.lower().split("x", 1))
        except ValueError:
            print("--size 格式应为 宽x高，例如 120x36")
            return 2
        out = Path(args.screenshot)
        ncm_tui.screenshot(scan_dir, out, size=(w, h), hd=args.hd, cover_mode=args.cover_mode)
        print(f"已生成截图：{out}")
        return 0

    if args.paths and not args.tui:
        return batch_convert(args.paths, args.out, args.overwrite, args.cover,
                             not args.no_tag, args.no_pause)

    # 播放器界面
    import ncm_tui
    scan_dir = Path(args.dir) if args.dir else default_scan_dir()
    if not scan_dir.is_dir():
        print(f"目录不存在：{scan_dir}")
        return 2
    rc = ncm_tui.run_tui(scan_dir, cover_mode=args.cover_mode, filter_mode=args.filter)
    if rc != 0 and not args.no_pause:
        _pause()
    return rc


if __name__ == "__main__":
    sys.exit(main())
