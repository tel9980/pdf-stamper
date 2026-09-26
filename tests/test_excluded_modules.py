# -*- coding: utf-8 -*-
"""
EXE 瘦身回归测试：在子进程里把 build_exe.py 的 EXCLUDE_MODULES 全部设为「不可导入」，
再跑一遍 tests.test_fixes 套件，模拟这些包没被打包进 EXE 时的运行状态。
子进程失败 = 排除清单里混进了功能必需的包，必须把它从 EXCLUDE_MODULES 里去掉。

运行: cd E:/AI && python -m unittest tests.test_excluded_modules -v
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from build_exe import EXCLUDE_MODULES  # 打包参数的单一来源

BLOCKED_RUNNER = """
import sys
BLOCKED = set({blocked!r})


class _BlockedFinder:
    \"\"\"让 BLOCKED 里的包在本子进程中一律 import 失败（等价于没被打包进 EXE）。\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in BLOCKED:
            raise ModuleNotFoundError('blocked by test: ' + name)
        return None


sys.meta_path.insert(0, _BlockedFinder())

import unittest
suite = unittest.TestLoader().loadTestsFromName('tests.test_fixes')
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


class TestExcludedModulesAreSafe(unittest.TestCase):

    def test_fixes_suite_passes_without_excluded_modules(self):
        code = BLOCKED_RUNNER.format(blocked=sorted(EXCLUDE_MODULES))
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=300)
        self.assertEqual(
            proc.returncode, 0,
            "排除模块后 test_fixes 失败（EXCLUDE_MODULES 里有功能必需的包）:\n"
            "--- stdout ---\n%s\n--- stderr ---\n%s"
            % (proc.stdout[-4000:], proc.stderr[-4000:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)