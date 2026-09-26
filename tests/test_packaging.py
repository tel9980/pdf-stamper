# -*- coding: utf-8 -*-
"""
打包配置测试（不执行 PyInstaller 本体，只做静态与产物校验）。

运行: cd E:/AI && python -m unittest tests.test_packaging -v
"""

import ast
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_EXE = os.path.join(ROOT, "build_exe.py")
SPEC_FILE = os.path.join(ROOT, "PDF盖章工具.spec")
ICO_FILE = os.path.join(ROOT, "assets", "stamp.ico")
PNG_FILE = os.path.join(ROOT, "assets", "stamp_icon.png")

REQUIRED_ICO_SIZES = {(16, 16), (32, 32), (48, 48), (256, 256)}


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _module_str_constants(tree):
    """收集模块级 `NAME = "字符串"` 常量，用于解析 cmd 列表里的变量引用。"""
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    consts[t.id] = node.value.value
    return consts


def _cmd_list_literals(tree):
    """提取 build_exe.py 中 `cmd = [...]` 列表里的所有字符串字面量
    （含 f-string 的字面片段、可解析的模块级常量名），即真正传给 PyInstaller 的固定参数。"""
    literals = []
    consts = _module_str_constants(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "cmd" for t in node.targets)
                and isinstance(node.value, ast.List)):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    literals.append(sub.value)
                elif isinstance(sub, ast.Name) and sub.id in consts:
                    literals.append(consts[sub.id])
        # cmd.append(ENTRY_SCRIPT) / cmd.append("xxx") 形式的追加项
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "cmd" and len(node.args) == 1):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literals.append(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                literals.append(consts[arg.id])
    return literals


def _non_docstring_strings(tree):
    """整棵语法树中的所有字符串常量，排除模块/函数/类的 docstring
    （文档中允许解释为何不用优化等，不算命令行参数）。"""
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_nodes.add(id(body[0].value))
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_nodes:
            strings.append(node.value)
    return strings


class TestIconAssets(unittest.TestCase):

    def test_ico_exists_and_valid(self):
        self.assertTrue(os.path.isfile(ICO_FILE), f"缺少图标: {ICO_FILE}")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow 未安装，跳过 ICO 校验")
        with Image.open(ICO_FILE) as img:
            self.assertEqual(img.format, "ICO", "stamp.ico 不是合法 ICO 格式")
            sizes = set(img.ico.sizes())
            self.assertTrue(REQUIRED_ICO_SIZES.issubset(sizes),
                            f"ICO 尺寸集合 {sorted(sizes)} 缺少 {sorted(REQUIRED_ICO_SIZES - sizes)}")
            # 透明背景: 256 版主图的左上角像素应为全透明
            img.ico.sizes()
            big = img  # IcoImageFile 打开后为当前帧
            big.load()
            if big.mode != "RGBA":
                big = big.convert("RGBA")
            self.assertEqual(big.getpixel((0, 0))[3], 0, "图标左上角应透明（透明背景）")

    def test_png_preview_exists(self):
        self.assertTrue(os.path.isfile(PNG_FILE), f"缺少 PNG 预览: {PNG_FILE}")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow 未安装，跳过 PNG 校验")
        with Image.open(PNG_FILE) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.size, (256, 256))

    def test_make_icon_script_present(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "make_icon.py")),
                        "缺少可重跑的图标生成脚本 assets/make_icon.py")


class TestBuildExeArgs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = _read(BUILD_EXE)
        cls.tree = ast.parse(cls.source)

    def test_no_optimize_flag(self):
        for s in _cmd_list_literals(self.tree):
            self.assertFalse(s.startswith("--optimize"),
                             f"PyInstaller 参数不应包含优化选项: {s}")
            self.assertNotIn("-OO", s)
        for s in _non_docstring_strings(self.tree):
            self.assertNotIn("--optimize", s,
                             "build_exe.py 不应在任何代码字符串中传入 --optimize")

    def test_no_self_add_data(self):
        for s in _non_docstring_strings(self.tree):
            self.assertFalse("--add-data" in s and "pdf_stamper.py" in s,
                             f"不应把主脚本自身作为 add-data 打入 bundle: {s}")

    def test_icon_flag_present(self):
        self.assertIn("--icon=", self.source, "缺少 --icon 参数")
        self.assertIn("stamp.ico", self.source, "--icon 应指向 assets/stamp.ico")

    def test_entry_script_still_bundled(self):
        literals = _cmd_list_literals(self.tree)
        self.assertIn("pdf_stamper.py", literals, "cmd 参数列表应包含入口脚本本身")

    def test_cross_platform_branches_kept(self):
        self.assertIn("win32", self.source)
        self.assertIn("darwin", self.source)
        self.assertIn(".app", self.source)

    def test_cleanup_not_wiping_build_and_dist(self):
        # 不允许再出现无条件删除整个 build/ dist/ 目录的写法
        self.assertNotIn("shutil.rmtree('build')", self.source)
        self.assertNotIn("shutil.rmtree(\"build\")", self.source)
        self.assertNotIn("'build', 'dist'", self.source)
        self.assertIn("except OSError", self.source, "清理逻辑应有异常保护")

    def test_py_compile(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", BUILD_EXE],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"py_compile 失败: {result.stdout}\n{result.stderr}")


class TestSpecConsistency(unittest.TestCase):
    """spec 与 build_exe.py 保持一致（build_exe.py 为单一来源）。"""

    @classmethod
    def setUpClass(cls):
        cls.source = _read(SPEC_FILE)
        ast.parse(cls.source)  # spec 必须是合法 Python

    def test_spec_has_no_optimize(self):
        self.assertNotIn("optimize", self.source, "spec 不应残留解释器优化配置")
        self.assertNotIn("'O'", self.source, "spec 不应残留 ('O', None, 'OPTION') 项")

    def test_spec_has_no_self_add_data(self):
        self.assertNotIn("('pdf_stamper.py', '.')", self.source.replace('"', "'"),
                         "spec 不应把主脚本自身作为 datas")

    def test_spec_declares_single_source(self):
        self.assertIn("build_exe.py", self.source, "spec 应注明单一来源是 build_exe.py")

    def test_spec_has_icon(self):
        self.assertIn("stamp.ico", self.source)
        self.assertIn("icon=", self.source)

    def test_spec_hiddenimports_match(self):
        for mod in ("PIL._tkinter_finder", "PIL.Image", "PIL.ImageTk"):
            self.assertIn(mod, self.source, f"spec 缺少 hiddenimport: {mod}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
