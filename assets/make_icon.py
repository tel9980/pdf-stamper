#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成PDF盖章工具的应用图标（公章风格：红色圆环 + 中心五角星，透明背景，无文字）。

产物（与本脚本同目录）:
  - stamp.ico        多尺寸 ICO (16/32/48/256)，供 PyInstaller --icon 使用
  - stamp_icon.png   256x256 PNG 预览

可重复执行: python assets/make_icon.py
仅依赖 Pillow。
"""

import math
import os

from PIL import Image, ImageDraw

# 参考尺寸与超采样倍数：在 1024x1024 上以掩码方式绘制，再缩小抗锯齿
REF = 256
SUPERSAMPLE = 4
CANVAS = REF * SUPERSAMPLE

# 印章红 (RGB)
SEAL_RED = (200, 30, 30, 255)

# 几何参数（均以 REF=256 为基准，绘制时乘以 SUPERSAMPLE）
RING_OUTER_R = 122    # 圆环外半径（留 4px 边距，小尺寸下不至于被裁）
RING_INNER_R = 100    # 圆环内半径（环宽 22/256，缩到 16px 仍有约 1.4px 可见）
STAR_RADIUS = 78      # 五角星外接圆半径


def _scale(v):
    return v * SUPERSAMPLE


def _star_points(cx, cy, radius):
    """五角星 10 个顶点（外/内交替），一个角尖朝上。"""
    inner = radius * math.sin(math.radians(18)) / math.sin(math.radians(54))
    pts = []
    for k in range(5):
        outer_angle = math.radians(-90 + k * 72)
        inner_angle = math.radians(-90 + 36 + k * 72)
        pts.append((cx + radius * math.cos(outer_angle),
                    cy + radius * math.sin(outer_angle)))
        pts.append((cx + inner * math.cos(inner_angle),
                    cy + inner * math.sin(inner_angle)))
    return pts


def build_icon(size=REF):
    """绘制 RGBA 图标，返回指定尺寸的图像（LANCZOS 缩放保证小尺寸清晰）。"""
    img = Image.new("L", (CANVAS, CANVAS), 0)
    draw = ImageDraw.Draw(img)
    c = CANVAS // 2

    # 圆环：先画实心大圆，再挖掉内圆
    ro, ri = _scale(RING_OUTER_R), _scale(RING_INNER_R)
    draw.ellipse([c - ro, c - ro, c + ro, c + ro], fill=255)
    draw.ellipse([c - ri, c - ri, c + ri, c + ri], fill=0)

    # 中心五角星
    draw.polygon(_star_points(c, c, _scale(STAR_RADIUS)), fill=255)

    # 上色：纯色 + 掩码作为 alpha
    out = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    red = Image.new("RGBA", (CANVAS, CANVAS), SEAL_RED)
    out.paste(red, (0, 0), img)

    if size != CANVAS:
        out = out.resize((size, size), Image.LANCZOS)
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    icon256 = build_icon(REF)

    ico_path = os.path.join(here, "stamp.ico")
    png_path = os.path.join(here, "stamp_icon.png")

    # 多尺寸 ICO：以 256 为基准，Pillow 会按 sizes 内缩生成
    icon256.save(ico_path, format="ICO",
                 sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    icon256.save(png_path, format="PNG")

    print(f"已生成: {ico_path}")
    print(f"已生成: {png_path}")


if __name__ == "__main__":
    main()
