#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""Win7 档打包：把 UCRT 一起塞进 onefile 产物，并在打完后**校验它真的在里面**。

为什么要单独一个脚本，而不是把 pyinstaller 命令行抄进 CI：

1. **UCRT 是 Win7 上唯一真正的"起不来"原因**。Python 3.8 的 exe 依赖
   `api-ms-win-crt-*.dll` / `ucrtbase.dll`：这些在 Win10 是系统组件，在**没打补丁的
   Win7 SP1** 上并不存在，用户会看到
   `api-ms-win-crt-runtime-l1-1-0.dll 缺失`（PyInstaller issue #1588）。
   微软允许把 UCRT 按"应用本地部署"随应用分发，所以这里优先从 Windows SDK 的
   `Redist\\ucrt\\DLLs\\<arch>` 取，取不到再退回 Python 安装目录与 System32。
2. **打完立刻校验**：否则 CI 会"绿着产出一个在 Win7 上起不来的包" —— 这是本项目最贵的
   一类假绿灯（我们没有 Win7 真机，只能靠这层机械检查）。
3. **PyInstaller 版本固定**：6.x 的官方支持矩阵只到 Windows 8+（其 README 原话：
   "should work on Windows 7 or newer, but we only officially support Windows 8+"）。

用法（在 Windows + Python 3.8 下）：``python tools/build_win7_exe.py``
    --dry-run   只打印将要执行的命令（Linux 上也能跑，用于检查参数）
    --list-ucrt 只列出找到的 UCRT DLL
"""

import argparse
import os
import subprocess
import sys

#: UCRT 的 API set 前缀与基础库（应用本地部署需要一整套）
UCRT_PREFIX = "api-ms-win-crt-"
UCRT_BASE = "ucrtbase.dll"

DEFAULT_NAME = "easysub-helper"
#: 入口必须是**静态导入**的 launcher.py：用 easysub_helper/__main__.py 时 PyInstaller
#: 看不到运行期的 `from . import cli`，包里会没有 easysub_helper（实测崩溃）。
ENTRY = "launcher.py"
ICON = os.path.join("easysub_helper", "assets", "easysub.ico")
#: assets 必须显式 add-data：onefile 模式不会自动带上包内数据文件
ASSETS = "easysub_helper/assets" + os.pathsep + "easysub_helper/assets"


def soften_streams():
    """让这个脚本的 print 在 cp1252 控制台/CI 下也不崩。

    讽刺的是：CI 上 py3.8/windows-2022 的这份 job 就是这么挂的 —— 脚本自己打印中文
    （"将打入 N 个 UCRT DLL"）时抛 UnicodeEncodeError。优先复用包里的实现，装不上就自兜一份。
    """
    try:
        from easysub_helper.cli import _soften_console_encoding
    except Exception:  # noqa: BLE001 - 包没装也要能跑
        def _soften_console_encoding():
            for name in ("stdout", "stderr"):
                stream = getattr(sys, name, None)
                if stream is None:
                    continue
                reconfigure = getattr(stream, "reconfigure", None)
                if reconfigure is None:
                    continue
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
    _soften_console_encoding()


def find_ucrt_dlls(dirs):
    """在候选目录里找 UCRT 的 DLL，返回 ``{小写文件名: 绝对路径}``。

    纯函数（不碰全局状态），单测不需要 Windows。先出现的目录优先。
    """
    found = {}
    for directory in dirs:
        if not directory or not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            low = name.lower()
            if low == UCRT_BASE or (low.startswith(UCRT_PREFIX) and low.endswith(".dll")):
                if low not in found:
                    found[low] = os.path.join(directory, name)
    return found


def candidate_dirs(arch="x64", env=None):
    """UCRT 的候选来源，按"最正规 → 最将就"排序。"""
    env = os.environ if env is None else env
    root = env.get("SystemRoot") or r"C:\Windows"
    program_files_x86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    return [
        # ① Windows SDK 的应用本地部署（app-local）再分发目录 —— 官方推荐的来源
        os.path.join(program_files_x86, "Windows Kits", "10", "Redist", "ucrt", "DLLs", arch),
        os.path.join(r"C:\Program Files", "Windows Kits", "10", "Redist", "ucrt", "DLLs", arch),
        # ② CPython 安装目录本身带一份（保证 Python 能在未打补丁的 Win7 上跑）
        sys.base_prefix,
        # ③ 最后才用系统目录（Win10 上是系统组件；Win7 上可能根本没有）
        os.path.join(root, "System32"),
    ]


def build_command(python_exe, dlls, name=DEFAULT_NAME, icon=ICON, entry=ENTRY, assets=ASSETS):
    """构造 PyInstaller 的参数列表。纯函数，便于单测。"""
    argv = [
        python_exe, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",              # Windows 上不弹黑框（默认入口是 tkinter 窗口）
        "--name", name,
        "--icon", icon,
        "--add-data", assets,
        "--hidden-import", "soundcard",
        # 收集整个包与 soundcard 的子模块：有些模块是运行期才 import 的（后端选择、设备枚举），
        # 只靠静态分析容易漏，漏了就是运行时报 ModuleNotFoundError。
        "--collect-submodules", "easysub_helper",
        "--collect-submodules", "soundcard",
    ]
    for _name in sorted(dlls):
        argv += ["--add-binary", dlls[_name] + os.pathsep + "."]
    argv.append(entry)
    return argv


def archive_listing(python_exe, exe_path):
    """列出 onefile 产物里打包进去的内容（PyInstaller 自带 CLI）。"""
    out = subprocess.check_output(
        [python_exe, "-m", "PyInstaller.utils.cliutils.archive_viewer", "-l", exe_path],
        stderr=subprocess.STDOUT,
    )
    return out.decode("utf-8", "replace")


def verify_archive(python_exe, exe_path, dlls):
    """返回产物里**缺失**的 UCRT 文件名列表（空 = 校验通过）。"""
    listing = archive_listing(python_exe, exe_path).lower()
    return [name for name in sorted(dlls) if name not in listing]


def main(argv=None):
    soften_streams()
    parser = argparse.ArgumentParser(description="Win7 档打包（带 UCRT 并自校验）")
    parser.add_argument("--arch", default="x64", choices=("x64", "x86"))
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不真的打包")
    parser.add_argument("--list-ucrt", action="store_true", help="只列出找到的 UCRT DLL")
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args(argv)

    dlls = find_ucrt_dlls(candidate_dirs(args.arch))
    if args.list_ucrt:
        for name in sorted(dlls):
            print("{}\t{}".format(name, dlls[name]))
        if not dlls:
            print("（没找到 UCRT；在 Windows 上请确认装了 Windows SDK 或用的是 python.org 的 CPython）")
        return 0

    # --dry-run 要能在**任何**机器上核对参数（文档承诺"Linux 上也能跑"），
    # 所以它必须排在"找不到 UCRT 就报错"之前：没有 DLL 时也照样打印命令，只多一句提示。
    if args.dry_run:
        if not dlls:
            print("# 注意：本机没找到 UCRT（真实打包会被拒绝）；下面是去掉 --add-binary 后的命令骨架")
        print(" ".join(build_command(sys.executable, dlls, name=args.name)))
        return 0

    if not dlls:
        print("错误：找不到 UCRT 的 DLL（api-ms-win-crt-*.dll / ucrtbase.dll）。"
              "Win7 产物必须把它们一起打包，否则未打补丁的 Win7 会报 api-ms-win-crt-runtime 缺失。",
              file=sys.stderr)
        return 2

    cmd = build_command(sys.executable, dlls, name=args.name)

    if os.name != "nt":
        print("错误：真正打包只能在 Windows 上跑（当前 os.name={}）。"
              "Linux/macOS 上用 --dry-run 检查参数即可。".format(os.name), file=sys.stderr)
        return 2

    print("将打入 {} 个 UCRT DLL".format(len(dlls)))
    code = subprocess.call(cmd)
    if code != 0:
        return code

    exe_path = os.path.join("dist", args.name + (".exe" if os.name == "nt" else ""))
    missing = verify_archive(sys.executable, exe_path, dlls)
    if missing:
        print("::error::产物里缺少 UCRT：{}".format(", ".join(missing)), file=sys.stderr)
        return 3
    print("✓ 校验通过：产物已自带 UCRT（{} 个 DLL），未打补丁的 Win7 SP1 也能起".format(len(dlls)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
