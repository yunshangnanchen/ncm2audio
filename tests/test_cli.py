# -*- coding: utf-8 -*-
"""tests/test_cli.py —— 全局调用（cwd 扫描）相关测试"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ncm2audio


class DefaultScanDirTest(unittest.TestCase):
    def test_default_scan_dir_is_cwd(self):
        """全局调用时默认扫描当前工作目录，而不是脚本所在目录"""
        with tempfile.TemporaryDirectory() as td:
            old = os.getcwd()
            os.chdir(td)
            try:
                got = ncm2audio.default_scan_dir()
            finally:
                os.chdir(old)
            self.assertEqual(got, Path(td).resolve())

    def test_default_scan_dir_falls_back_to_root(self):
        """cwd 不可用时退回脚本所在目录，保证不崩溃"""
        with mock.patch.object(Path, "cwd", side_effect=OSError("cwd deleted")):
            self.assertEqual(ncm2audio.default_scan_dir(), ncm2audio.ROOT)


if __name__ == "__main__":
    unittest.main()
