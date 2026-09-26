#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包PDF盖章工具为EXE文件 - 财务专用版
支持 Windows/Linux/Mac

说明:
  - 本脚本是 PyInstaller 打包参数的单一来源（single source of truth）。
    仓库根目录的 `PDF盖章工具.spec` 与本脚本参数保持一致，仅供手动
    `python -m PyInstaller "PDF盖章工具.spec"` 调试使用；改参数请改这里。
  - 不使用 --optimize/-OO：PyInstaller 官方不建议开启优化，剥离 assert
    与 docstring 可能改变被打包代码的行为。
  - 不清空整个 build/ 与 dist/ 目录，只清理本脚本自己生成的目标产物。
"""

import os
import shutil
import subprocess
import sys

APP_NAME = "PDF盖章工具"
ENTRY_SCRIPT = "pdf_stamper.py"
ICON_PATH = os.path.join("assets", "stamp.ico")

# 打包时排除的重包：本工具的代码路径完全用不到，却被 PyInstaller 的分析钩子
# 顺手扫进 PYZ（scipy / pandas / sqlalchemy / numpy / setuptools / matplotlib / pytest
# 合计上千个模块，是 EXE 体积的主因）。排除后必须重新验证：
#   python build_exe.py  ->  python -m unittest tests.test_packaging -v  ->  启动冒烟
EXCLUDE_MODULES = [
    "scipy",
    "pandas",
    "numpy",
    "matplotlib",
    "sqlalchemy",
    "setuptools",
    "pkg_resources",
    "pytest",
    "_pytest",
    "lxml",
    "pygments",
    "IPython",
    "openpyxl",
    "jinja2",
    "dateutil",
    "pytz",
]


def output_name_for_platform():
    """按平台返回 dist 下的产物名（Windows: .exe / Mac: .app / Linux: 无后缀）。"""
    if sys.platform == "win32":
        return f"{APP_NAME}.exe"
    elif sys.platform == "darwin":
        return f"{APP_NAME}.app"
    else:
        return APP_NAME


def remove_own_artifact(path):
    """只删除本脚本生成的特定产物，带异常保护；删不掉只警告不中断。"""
    if not os.path.exists(path):
        return
    try:
        print(f"清理旧产物: {path}")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        print(f"警告: 清理 {path} 失败（跳过，不影响打包）: {e}")


def cleanup():
    """清理上一构建遗留的本工具产物，不触碰 build/、dist/ 里其他人的文件。"""
    remove_own_artifact(os.path.join("build", APP_NAME))
    remove_own_artifact(os.path.join("dist", output_name_for_platform()))


def ensure_pyinstaller():
    try:
        import PyInstaller
        print(f"PyInstaller版本: {PyInstaller.__version__}")
        return True
    except ImportError:
        print("正在安装PyInstaller...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"❌ PyInstaller 安装失败: {e}")
            print("   请手动执行: python -m pip install pyinstaller")
            return False
        return True


def resolve_icon_arg():
    """
    返回要追加的 --icon 参数（可能为空列表）。
    只有 Windows 使用 .ico；macOS 需要 .icns、Linux 不支持图标，均跳过以保证跨平台。
    """
    if sys.platform != "win32":
        print("提示: 当前平台不使用 .ico 图标（仅 Windows 打包附加 --icon），跳过。")
        return []
    if not os.path.isfile(ICON_PATH):
        print(f"❌ 找不到图标文件: {os.path.abspath(ICON_PATH)}")
        print(f"   请先执行: python {os.path.join('assets', 'make_icon.py')} 生成图标。")
        return None  # None 表示致命错误
    return [f"--icon={os.path.abspath(ICON_PATH)}"]


def main():
    print("=" * 60)
    print("PDF盖章工具 - EXE打包脚本")
    print("=" * 60)

    cleanup()

    if not ensure_pyinstaller():
        return 1

    icon_args = resolve_icon_arg()
    if icon_args is None:
        return 1

    output_name = output_name_for_platform()
    print(f"当前平台: {sys.platform}")
    print(f"输出文件: {output_name}")

    # PyInstaller参数（注意: 不启用 --optimize，PyInstaller 官方不建议）
    cmd = [
        sys.executable, "-m", "PyInstaller",
        f"--name={APP_NAME}",
        "--windowed",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--specpath=build",
        "--hidden-import=PIL._tkinter_finder",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageTk",
    ]
    cmd += icon_args
    cmd += [f"--exclude-module={name}" for name in EXCLUDE_MODULES]
    cmd.append(ENTRY_SCRIPT)

    print("\n开始打包...")
    print("命令:", " ".join(cmd))
    print()

    try:
        result = subprocess.run(cmd)
    except OSError as e:
        print(f"\n❌ 无法启动 PyInstaller: {e}")
        return 1

    if result.returncode != 0:
        print(f"\n❌ 打包失败: PyInstaller 退出码 {result.returncode}")
        print(f"   可查看详细日志: {os.path.join('build', APP_NAME)}")
        return result.returncode or 1

    # 检查结果
    exe_path = os.path.abspath(os.path.join("dist", output_name))
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print("\n" + "=" * 60)
        print("✅ 打包成功！")
        print(f"📦 文件: {exe_path}")
        print(f"📊 大小: {size_mb:.1f} MB")
        print("=" * 60)

        if sys.platform == "win32":
            print("\n💡 Windows EXE 使用说明:")
            print("1. 双击 'PDF盖章工具.exe' 即可运行")
            print("2. 无需安装Python环境")
            print("3. 可以发送给其他Windows电脑使用")
        elif sys.platform == "darwin":
            print("\n💡 Mac APP 使用说明:")
            print("1. 双击 'PDF盖章工具.app' 即可运行")
            print("2. 如提示无法打开，请到系统偏好设置→安全性与隐私→仍要打开")
        else:
            print("\n💡 Linux 可执行文件使用说明:")
            print("1. 运行: ./PDF盖章工具")
            print("2. 或双击运行（需要执行权限）")
            print("3. 如需制作Windows EXE，请在Windows环境下运行此脚本")
        return 0
    else:
        print(f"\n❌ 打包失败：未找到生成的文件 {exe_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
