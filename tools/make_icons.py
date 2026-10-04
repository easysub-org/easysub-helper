#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hcz1017
"""把主项目的插件 logo 转成桌面端需要的图标资源（logo.jpg / *.png / *.ico / *.icns）。

为什么要这一步：桌面助手本体没有自己的品牌资产，**logo 一律以插件 logo 为准**
（主仓库 `logo.jpg` + `public/icons/icon{16,48,128}.png`）。浏览器窗口里的 favicon 由
Web 产物（`src/web/index.html` 的 `<link rel="icon" href="icons/icon48.png">`）负责；
这里补齐助手自己要用到的那几份：

  - `easysub_helper/assets/logo.jpg`   配对页头部的品牌图
  - `easysub_helper/assets/icon{16,48,128}.png`  页面/favicon（不依赖 Web 产物也能用）
  - `easysub_helper/assets/easysub.ico`  Windows 可执行文件图标（PyInstaller --icon）
  - `easysub_helper/assets/easysub.icns` macOS 应用图标（PyInstaller --icon）

用法（图标更新后重跑一次，产物要一并提交，构建机就不需要 Pillow）：
    python tools/make_icons.py                      # 默认从 ../ 主仓库取源
    python tools/make_icons.py --source /path/to/easysub
"""

import argparse
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "easysub_helper", "assets")

#: ICO 里要包含的尺寸（Windows 资源管理器/任务栏/Alt-Tab 各个场景都覆盖）
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: ICNS 的 PNG chunk 类型 → 边长（Apple 的 icp4/icp5/icp6/ic07/ic08/ic09）
ICNS_CHUNKS = (("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256), ("ic09", 512))


def _load_pillow():
    try:
        from PIL import Image  # noqa: WPS433
        return Image
    except Exception:  # noqa: BLE001 - 没装 Pillow 时走 ImageMagick 兜底
        return None


def _resize(Image, src, size, out):
    img = Image.open(src).convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    img.save(out, format="PNG", optimize=True)
    return img


def build_ico(Image, base_png, out):
    """多尺寸 ICO。Pillow 会按标准编码写：<256 用 BMP、256 用 PNG（Win7 都认）。"""
    base = Image.open(base_png).convert("RGBA")
    if base.size[0] < 256:
        # 源图不足 256 时用最大的一张放大（插件给的是 128），保证 256 档不缺失
        base = base.resize((256, 256), Image.LANCZOS)
    base.save(out, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return out


def build_icns(png_by_size, out):
    """手写 ICNS：'icns' + 总长度 + 若干 (type, length, PNG) chunk。

    自己拼是为了不引入 iconutil（只有 macOS 有）/png2icns 这类工具依赖。
    """
    body = b""
    for chunk_type, size in ICNS_CHUNKS:
        path = png_by_size.get(size)
        if not path or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        body += chunk_type.encode("ascii") + struct.pack(">I", len(data) + 8) + data
    header = b"icns" + struct.pack(">I", len(body) + 8)
    with open(out, "wb") as fh:
        fh.write(header + body)
    return out


def build_ico_with_imagemagick(png_paths, out):
    import subprocess

    sizes = ",".join(str(s) for s in ICO_SIZES)
    argv = ["convert"] + list(png_paths) + ["-define", "icon:auto-resize=" + sizes, out]
    subprocess.check_call(argv)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成桌面端图标资源（来源：插件 logo）")
    # 默认取"easysub-helper 的上级目录" = 主仓库根（开发期助手就住在主仓库里）
    parser.add_argument("--source", default=os.path.dirname(os.path.dirname(HERE)),
                        help="主仓库根目录（含 logo.jpg 与 public/icons/）")
    parser.add_argument("--out", default=ASSETS)
    args = parser.parse_args(argv)

    source_logo = os.path.join(args.source, "logo.jpg")
    source_icons = os.path.join(args.source, "public", "icons")
    for path in (source_logo, os.path.join(source_icons, "icon16.png"),
                 os.path.join(source_icons, "icon48.png"), os.path.join(source_icons, "icon128.png")):
        if not os.path.isfile(path):
            sys.stderr.write("找不到源文件: {}\n".format(path))
            return 2

    os.makedirs(args.out, exist_ok=True)
    shutil.copyfile(source_logo, os.path.join(args.out, "logo.jpg"))
    for name in ("icon16.png", "icon48.png", "icon128.png"):
        shutil.copyfile(os.path.join(source_icons, name), os.path.join(args.out, name))

    Image = _load_pillow()
    tmp_dir = os.path.join(args.out, ".tmp-sizes")
    os.makedirs(tmp_dir, exist_ok=True)
    png_by_size = {}
    try:
        for size in sorted(set(ICO_SIZES) | {s for _t, s in ICNS_CHUNKS}):
            out = os.path.join(tmp_dir, "icon{}.png".format(size))
            if Image is not None:
                _resize(Image, source_logo, size, out)
            else:
                import subprocess
                subprocess.check_call(["convert", source_logo, "-resize", "{}x{}".format(size, size), out])
            png_by_size[size] = out

        ico = os.path.join(args.out, "easysub.ico")
        icns = os.path.join(args.out, "easysub.icns")
        # 首选 ImageMagick 走 ICO：它给的是**每个尺寸都是 BMP(DIB)** 的传统编码，
        # Win7 的资源管理器/任务栏/Alt-Tab 全都稳妥（Pillow 会写成全 PNG 条目，
        # 虽然 Vista+ 认，但小尺寸上传统编码更保险）。没有 convert 时退回 Pillow。
        if shutil.which("convert"):
            build_ico_with_imagemagick([png_by_size[size] for size in ICO_SIZES], ico)
        elif Image is not None:
            build_ico(Image, os.path.join(args.out, "logo.jpg"), ico)
        else:
            sys.stderr.write("既没有 ImageMagick 也没有 Pillow，无法生成 .ico\n")
            return 2
        build_icns(png_by_size, icns)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    for name in ("logo.jpg", "icon16.png", "icon48.png", "icon128.png", "easysub.ico", "easysub.icns"):
        path = os.path.join(args.out, name)
        sys.stdout.write("{:>16}  {:>9} bytes\n".format(name, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
