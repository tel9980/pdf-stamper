#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF盖章工具 v3.3 - 专业版
支持：多公章、撤销/重做、旋转、批量盖章、骑缝章、透明度调节

v3.3 变化
---------
- 导出移出 UI 线程：ExportJob（线程 + queue.Queue 事件 + 密码回执），主线程 root.after 轮询
  实测 1000 页 × 3 章：调用方阻塞 3553.8 ms -> 2.42 ms（主线程 99.9% 时间空闲）
- 新增「取消导出」按钮：检查点布在每章/每页/每次插图；取消发生在 os.replace 之前，目标文件不受损
- snapshot_stamps() 克隆公章，导出用「点按钮那一刻」的快照；worker 自开 fitz.Document

v3.2 变化
---------
- 骑缝章上下位置可拖动（cross_fold_slice_rect(y_pt=) / cross_fold_top_pt()，预览导出同源）
- 图像处理管线三级缓存（opacity -> rotate -> resize 各自 LRU）+ Image.reduce() 预降采样
- 导出兜底检测改 clip= 局部渲染 + 廉价预筛；位图缓存改像素预算（96 MB）
- ExportOptions 收口导出参数；STAMP_EXT 统一图片来源清单

v3.1 结构说明（重要）
--------------------
为了让核心逻辑可以在无 GUI / 无 Tk 主循环的环境下被 import 和测试，本文件分成两层：

1. 纯逻辑层（模块级函数 / 无 Tk 依赖的类）
   - process_stamp_image()      : 唯一的公章图像处理管线 opacity -> rotate(expand) -> scale（带缓存）
   - stamp_export_geometry()    : 画布像素坐标 -> PDF point 坐标 + 目标矩形
   - split_cross_fold_images()  : 骑缝章按页横向切片
   - cross_fold_slice_rect()    : 骑缝章某一刀的目标矩形（PDF point）
   - cross_fold_geometry()      : 骑缝章全部页的几何（预览 / 导出共用）
   - make_history_state() / restore_stamps() : 基于唯一 id 的历史快照与恢复
   - open_pdf_document()        : 打开 PDF（含加密 PDF 认证），返回 (doc, status)
   - export_pdf_with_stamps()   : 导出盖章结果（每个公章只编码一次并复用 xref）
   - RenderCore                 : 页面位图缓存 + 公章位图缓存，供 GUI 渲染调用
   - StampConfig / HistoryManager / DocumentSession
2. GUI 层（PDFStamper）：只做 Tkinter 交互，所有计算委托给纯逻辑层。

`import pdf_stamper` 不会创建任何窗口（只有 main() 才 new Tk root）。
"""

import io
import json
import os
import queue
import time
import uuid
import dataclasses
import tempfile
import threading
from collections import OrderedDict, deque

try:  # PyMuPDF >= 1.24 提供 pymupdf 别名；用别名可避免 fitz 的弃用警告
    import pymupdf as fitz
except ImportError:  # 旧版本（requirements 下限 1.23）只有 fitz
    import fitz
from PIL import Image, ImageDraw, ImageTk

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# ==================== 常量 ====================

DEFAULT_RENDER_DPI = 150
OPEN_OK = "ok"
OPEN_NEEDS_PASSWORD = "needs_password"
OPEN_BAD_PASSWORD = "bad_password"
OPEN_CANCELLED = "cancelled"
OPEN_FAILED = "failed"

# 图章图片来源支持的扩展名（文件对话框与读取校验共用同一份清单）
STAMP_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
STAMP_FILE_DIALOG_TYPES = (
    ("图片文件", " ".join("*" + ext for ext in STAMP_EXT)),
    ("所有文件", "*.*"),
)

# 可编辑 / 会吞键的控件类型名（用于快捷键分发，见 shortcut_blocked）
EDITABLE_WIDGET_NAMES = frozenset({
    "Entry", "TEntry", "Text", "Listbox", "Spinbox", "TSpinbox",
    "Combobox", "TCombobox", "Treeview", "Notebook", "TNotebook", "Editor",
})

_stamp_image_cache = OrderedDict()
STAMP_IMAGE_CACHE_MAX = 96

# 分级流水线缓存：opacity -> rotate -> scale 各自一级。
# 拖滑块时只有被改的那一级会失效，后级复用前级结果（见 process_stamp_image）。
_OPACITY_CACHE = OrderedDict()
_ROTATE_CACHE = OrderedDict()
_DISPLAY_SCALE_CACHE = OrderedDict()
OPACITY_CACHE_MAX = 32
ROTATE_CACHE_MAX = 32
DISPLAY_SCALE_CACHE_MAX = 32

# 缓存统计（原先 stamp_image_cache_info() 的 hits 恒为 None，无法验证优化效果）
_CACHE_STATS = {"opacity_hit": 0, "opacity_miss": 0,
                "rotate_hit": 0, "rotate_miss": 0,
                "display_hit": 0, "display_miss": 0,
                "chain_hit": 0, "chain_miss": 0}

# 线程安全说明（后台导出 worker 与主线程会并发读写上面几个缓存）：
#   * 每个条目的值是 (源图, 结果) —— **强引用持有源图**，所以源图的 id() 在条目存活期间
#     不会被回收复用，`(id(img), size, mode, ...)` 作为键不会误命中别的图像；
#   * OrderedDict 的 get / __setitem__ / move_to_end / popitem 各自都是 GIL 原子操作，
#     交错执行最多导致「多淘汰一个条目」（下次 miss 重算），不会破坏结构；
#   * _CACHE_STATS 的 += 非原子，并发下可能少记几次 —— 只影响观测计数，不影响正确性。
# 因此这里**故意不加锁**：拖滑块是热路径，加锁的代价大于上述可忽略的副作用。


def _image_identity(img):
    """图像身份指纹：id + 尺寸 + mode。缓存条目强引用原图，故 id() 不会被复用。"""
    return (id(img), img.size, img.mode)


def _cache_put(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


# ==================== 纯逻辑：坐标 / 几何 ====================

def canvas_scale(render_dpi=DEFAULT_RENDER_DPI):
    """画布像素 / PDF point 的比例。"""
    return float(render_dpi) / 72.0


def new_stamp_id():
    """全局唯一且不变的公章 id。"""
    return uuid.uuid4().hex


def rect_center(rect):
    """(x0, y0, x1, y1) 的中心。"""
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def pdf_rect_from_canvas(x, y, width_px, height_px, scale_factor):
    """
    画布像素矩形（左上角 (x, y)，尺寸 width_px x height_px，单位=画布像素）
    -> PDF point 矩形 (x0, y0, x1, y1)。
    width_px / height_px 必须是**处理后**（已应用透明度/旋转/缩放）图像的真实尺寸，
    这样预览看到的中心与导出矩形中心严格一致（修复 G）。
    """
    sf = float(scale_factor)
    if sf <= 0:
        raise ValueError("scale_factor must be positive")
    return (x / sf, y / sf, (x + width_px) / sf, (y + height_px) / sf)


def canvas_rect_from_pdf(rect_pt, scale_factor):
    """pdf_rect_from_canvas 的逆运算（PDF point -> 画布像素）。"""
    sf = float(scale_factor)
    return (rect_pt[0] * sf, rect_pt[1] * sf, rect_pt[2] * sf, rect_pt[3] * sf)


def stamp_export_geometry(stamp, scale_factor, page_index=None):
    """
    单个（非骑缝）公章的导出/预览几何。预览与导出共用这一个函数。

    返回 dict:
      image_px      : 处理后图像尺寸 (w, h)（画布像素 == 位图像素）
      rect_px       : 画布像素矩形
      center_px     : 画布中心
      rect_pt       : PDF point 矩形（导出目标矩形）
      center_pt     : PDF point 中心
    """
    img = get_processed_image(stamp)
    w, h = img.size
    x, y = stamp.position_for_page(page_index)
    rect_px = (x, y, x + w, y + h)
    rect_pt = pdf_rect_from_canvas(x, y, w, h, scale_factor)
    return {
        "image_px": (w, h),
        "rect_px": rect_px,
        "center_px": rect_center(rect_px),
        "rect_pt": rect_pt,
        "center_pt": rect_center(rect_pt),
    }


def clip_rect_to_page(rect_pt, page_rect, size_pt):
    """把矩形平移进页面内（保持尺寸不变）。返回 (rect_pt, shifted_bool)。"""
    w, h = size_pt
    x0, y0, x1, y1 = rect_pt
    dx = dy = 0.0
    if x0 < 0:
        dx = -x0
    elif x1 > page_rect.width:
        dx = page_rect.width - x1
    if y0 < 0:
        dy = -y0
    elif y1 > page_rect.height:
        dy = page_rect.height - y1
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return rect_pt, False
    return (x0 + dx, y0 + dy, x1 + dx, y1 + dy), True


def stamp_applies_to_page(stamp, page_index, page_count):
    """判断公章是否应盖在指定页；旧历史数据默认覆盖全部页面。"""
    scope = getattr(stamp, "page_scope", "all")
    if scope == "none":
        return False
    if scope == "first":
        return int(page_index) == 0
    if scope == "last":
        return int(page_index) == max(0, int(page_count) - 1)
    return True


# ==================== 纯逻辑：骑缝章（真正的半章/多刀） ====================

def slice_bounds(total_px, num_slices):
    """把 total_px 像素连续切成 num_slices 刀，返回 [(start, end), ...]（和严格等于 total）。"""
    n = max(1, int(num_slices))
    total = int(total_px)
    if n == 1:
        return [(0, total)]
    base, rem = divmod(total, n)
    out = []
    start = 0
    for i in range(n):
        width = base + (1 if i < rem else 0)
        out.append((start, start + width))
        start += width
    return out


def split_cross_fold_images(stamp_img, num_pages, offset=0.5):
    """
    骑缝章切片：N 页文档上每页只显示该章的 1/N（按页序横向连续切片）。
    所有页拼合起来才是完整一枚章。

    参数
      stamp_img : PIL.Image。传入**处理后**的图像（已含旋转/缩放），保证与导出尺寸一致。
      num_pages : 切片数量（= 文档页数），最少 1。
      offset    : 跨页位置偏移，只影响放置矩形（见 cross_fold_slice_rect），
                  不影响切片本身的内容划分；保留该形参以与放置函数保持同一套签名。

    返回 list[PIL.Image]，长度 == max(1, num_pages)，各片宽度之和 == stamp_img.width。
    """
    n = max(1, int(num_pages))
    w, h = stamp_img.size
    images = []
    for start, end in slice_bounds(w, n):
        images.append(stamp_img.crop((start, 0, end, h)))
    return images


def cross_fold_slice_rect(page_width_pt, page_height_pt, full_width_pt, full_height_pt,
                          slice_index, num_pages, offset=0.5, slice_width_pt=None,
                          nominal_width_pt=None, y_pt=None):
    """
    骑缝章第 slice_index 刀在某一页上的目标矩形（PDF point）。

    放置约定（每页同一位置、内容不同 -> 错页拼合后为完整一枚章）：
      - 垂直方向：y_pt 给出整章顶边（PDF point）；省略时居中。
      - 水平方向：以「右边缘」为基准锚定（与旧版方向一致：offset 越大越向左）：
          right = page_width - offset * (full_width - nominal_slice_width)
          x0    = right - slice_width
        nominal_slice_width = full_width / num_pages，各页共用同一个名义宽度，
        因此所有页的右边缘严格对齐（像素取整只让宽度相差 <=1px，不会造成错位）。
      - offset=0 时切片贴住页面右边缘；offset=1 时整枚章完全收进页面内。
      - 结果 clamp 在页面内，不会被裁到页外。

    slice_width_pt     : 该刀真实宽度（默认取名义宽度）
    nominal_width_pt   : 放置用的名义宽度（默认 full_width / num_pages）
    y_pt               : 整章顶边（PDF point）；None 表示垂直居中（旧行为）
    """
    n = max(1, int(num_pages))
    idx = min(max(0, int(slice_index)), n - 1)
    nominal = float(nominal_width_pt if nominal_width_pt is not None else full_width_pt / n)
    actual = float(slice_width_pt if slice_width_pt is not None else nominal)
    right = page_width_pt - float(offset) * max(0.0, full_width_pt - nominal)
    right = min(right, page_width_pt)
    x0 = max(0.0, right - actual)
    if y_pt is None:
        y0 = (page_height_pt - full_height_pt) / 2.0
    else:
        y0 = float(y_pt)
    # 垂直方向允许整章略超出页面（骑缝章本就常压在页边），但仍限制在合理范围内，
    # 避免拖动过程中章飞出页面后无法用鼠标找回。
    y0 = min(max(y0, -full_height_pt * 0.5), page_height_pt - full_height_pt * 0.5)
    return (x0, y0, x0 + actual, y0 + full_height_pt)


def cross_fold_top_pt(stamp, page_index, scale_factor):
    """
    骑缝章在某页的整章顶边（PDF point）。

    骑行章与普通章共用同一套「按页位置」(`position_for_page`)，因此垂直位置可以像
    普通章一样拖动；这里把画布像素 y 换算成 PDF point。
    """
    _x, y_px = stamp.position_for_page(page_index)
    return float(y_px) / float(scale_factor)


def cross_fold_geometry(stamp, page_rect, num_pages, offset=0.5, scale_factor=None,
                        page_index=None):
    """
    骑缝章在某一页上的预览/导出共用几何。

    返回 dict:
      processed_px    : 处理后整章尺寸 (W, H)（像素）
      slice_index     : 该页显示第几刀（= 页序）
      slice_image_px  : 该刀图像尺寸
      rect_pt         : 该刀在页面上的 PDF point 矩形
      center_pt       : 该刀中心（PDF point）
      slices_pt       : 所有页矩形的列表（导出可直接用）
    """
    sf = float(scale_factor if scale_factor is not None else canvas_scale())
    img = get_processed_image(stamp)
    w_px, h_px = img.size
    if page_index is None:  # 未显式指定时退回stamp上记录的页序（兼容旧接口）
        page_index = getattr(stamp, "page_index", 0) or 0
    page_index = min(max(0, int(page_index)), max(0, num_pages - 1))
    bounds = slice_bounds(w_px, num_pages)
    start, end = bounds[page_index]
    slice_w_pt = (end - start) / sf
    full_w_pt = w_px / sf
    full_h_pt = h_px / sf
    top_pt = cross_fold_top_pt(stamp, page_index, sf)
    rect_pt = cross_fold_slice_rect(
        page_rect.width, page_rect.height, full_w_pt, full_h_pt,
        page_index, num_pages, offset, slice_width_pt=slice_w_pt, y_pt=top_pt,
    )
    slices_pt = []
    for i, (s, e) in enumerate(bounds):
        slices_pt.append(cross_fold_slice_rect(
            page_rect.width, page_rect.height, full_w_pt, full_h_pt,
            i, num_pages, offset, slice_width_pt=(e - s) / sf,
            y_pt=cross_fold_top_pt(stamp, i, sf),
        ))
    return {
        "processed_px": (w_px, h_px),
        "slice_index": page_index,
        "slice_bounds": (start, end),
        "slice_image_px": (end - start, h_px),
        "rect_pt": rect_pt,
        "center_pt": rect_center(rect_pt),
        "slices_pt": slices_pt,
        "scale_factor": sf,
    }


def cross_fold_rect_px(stamp, page_rect, num_pages, offset=0.5, scale_factor=None,
                       page_index=None):
    """骑缝章某页矩形的画布像素版本（预览用，与导出同源，只乘 scale_factor）。"""
    geo = cross_fold_geometry(stamp, page_rect, num_pages, offset, scale_factor, page_index)
    sf = geo["scale_factor"]
    return canvas_rect_from_pdf(geo["rect_pt"], sf)


# ==================== 纯逻辑：图像处理管线（唯一入口） ====================

def apply_opacity(img, opacity):
    """应用透明度（保留原 alpha 形状，等比缩小 alpha）。"""
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        opacity = 1.0
    if opacity >= 1.0:
        return img
    if opacity < 0:
        opacity = 0.0
    img_rgba = img.convert("RGBA")
    r, g, b, a = img_rgba.split()
    a = a.point(lambda p: int(p * opacity))
    return Image.merge("RGBA", (r, g, b, a))


def resize_lanczos(img, size):
    """
    缩放到目标尺寸，缩小时先用 reduce() 取整数倍降采样再 Lanczos 收尾。

    PIL 官方推荐的多步缩放做法：直接一步 Lanczos 缩小大图，参与计算的源像素远多于
    目标像素，代价随源尺寸增长；reduce() 用盒式滤波先丢掉多余像素，代价骤降。
    实测 800px -> 80/200/400px 时快 3.8~4.6 倍；放大或接近 1:1 时退回单步。
    """
    target_w = max(1, int(round(size[0])))
    target_h = max(1, int(round(size[1])))
    if (target_w, target_h) == img.size:
        return img
    if target_w < img.width and target_h < img.height:
        factor = min(img.width // target_w, img.height // target_h)
        if factor > 1:
            img = img.reduce(factor)
    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)


def process_stamp_image(img, opacity=1.0, rotation=0, scale=1.0, use_cache=True):
    """
    唯一的公章图像处理管线（修复 A）：opacity -> rotate(expand=True) -> scale。
    预览与导出都走这里，因此「画布上看到的尺寸/中心」与「导出的尺寸/中心」必然一致。

    带模块级缓存（修复 E 的一部分）：key 由图像身份 + 尺寸 + mode + 参数组成，
    参数不变时不重复做 opacity/rotate/resize。缓存条目强引用原图，避免 id() 复用。
    """
    try:
        rotation_i = int(rotation) % 360
    except (TypeError, ValueError):
        rotation_i = 0
    try:
        scale_f = max(0.01, float(scale))
    except (TypeError, ValueError):
        scale_f = 1.0
    try:
        opacity_f = round(float(opacity), 4)
    except (TypeError, ValueError):
        opacity_f = 1.0

    scale_key = round(scale_f, 4)
    key = (id(img), img.size, img.mode, opacity_f, rotation_i, scale_key)
    if use_cache:
        hit = _stamp_image_cache.get(key)
        if hit is not None:
            _stamp_image_cache.move_to_end(key)
            _CACHE_STATS["chain_hit"] += 1
            return hit[1]
        _CACHE_STATS["chain_miss"] += 1

    out = _apply_opacity_cached(img, opacity_f, use_cache)
    out = _rotate_cached(out, rotation_i, use_cache)
    if abs(scale_f - 1.0) > 1e-9:
        w, h = out.size
        out = resize_lanczos(out, (w * scale_f, h * scale_f))

    if use_cache:
        _stamp_image_cache[key] = (img, out)
        while len(_stamp_image_cache) > STAMP_IMAGE_CACHE_MAX:
            _stamp_image_cache.popitem(last=False)
    return out


def _apply_opacity_cached(img, opacity_f, use_cache=True):
    """
    管线第 1 级：只做透明度。拖 scale/rotation 滑块时这一级稳定命中。

    apply_opacity 对 opacity>=1 是零成本短路，因此不透明章不产生缓存条目。
    """
    if opacity_f >= 1.0:
        return img
    if not use_cache:
        return apply_opacity(img, opacity_f)
    key = (_image_identity(img), opacity_f)
    hit = _OPACITY_CACHE.get(key)
    if hit is not None:
        _OPACITY_CACHE.move_to_end(key)
        _CACHE_STATS["opacity_hit"] += 1
        return hit[1]
    _CACHE_STATS["opacity_miss"] += 1
    out = apply_opacity(img, opacity_f)
    _cache_put(_OPACITY_CACHE, key, (img, out), OPACITY_CACHE_MAX)
    return out


def _rotate_cached(img, rotation_i, use_cache=True):
    """管线第 2 级：只做旋转（expand=True）。拖 scale 滑块时这一级稳定命中。"""
    if not rotation_i:
        return img
    if not use_cache:
        return img.rotate(rotation_i, expand=True, resample=Image.Resampling.BICUBIC)
    key = (_image_identity(img), rotation_i)
    hit = _ROTATE_CACHE.get(key)
    if hit is not None:
        _ROTATE_CACHE.move_to_end(key)
        _CACHE_STATS["rotate_hit"] += 1
        return hit[1]
    _CACHE_STATS["rotate_miss"] += 1
    out = img.rotate(rotation_i, expand=True, resample=Image.Resampling.BICUBIC)
    _cache_put(_ROTATE_CACHE, key, (img, out), ROTATE_CACHE_MAX)
    return out


def display_scale_cache(img, factor, use_cache=True):
    """
    预览专用的「一步缩放到显示尺寸」缓存。

    预览显示尺寸 = 处理后尺寸 × 画布缩放；本函数把「按 scale 处理」与「按 zoom 显示」
    两步合成一次 resize，避免 zoom != 1 时的两次 Lanczos，并缓存结果供 PhotoImage 复用。
    """
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        factor = 1.0
    if abs(factor - 1.0) < 1e-9:
        return img
    if factor <= 0:
        factor = 1.0
    if not use_cache:
        return resize_lanczos(img, (img.width * factor, img.height * factor))
    key = (_image_identity(img), round(factor, 4))
    hit = _DISPLAY_SCALE_CACHE.get(key)
    if hit is not None:
        _DISPLAY_SCALE_CACHE.move_to_end(key)
        _CACHE_STATS["display_hit"] += 1
        return hit[1]
    _CACHE_STATS["display_miss"] += 1
    out = resize_lanczos(img, (img.width * factor, img.height * factor))
    _cache_put(_DISPLAY_SCALE_CACHE, key, (img, out), DISPLAY_SCALE_CACHE_MAX)
    return out


def remove_white_background(img, threshold=245):
    """把图片边缘连通的近白色背景变为透明，保留章内部的白色细节。"""
    rgba = img.convert("RGBA")
    width, height = rgba.size
    if not width or not height:
        return rgba

    pixels = rgba.load()
    threshold = max(0, min(255, int(threshold)))
    visited = bytearray(width * height)
    queue = deque()
    changed = False

    def is_background(x, y):
        r, g, b, alpha = pixels[x, y]
        return alpha > 0 and min(r, g, b) >= threshold

    for x in range(width):
        if is_background(x, 0):
            queue.append((x, 0))
        if height > 1 and is_background(x, height - 1):
            queue.append((x, height - 1))
    for y in range(1, height - 1):
        if is_background(0, y):
            queue.append((0, y))
        if width > 1 and is_background(width - 1, y):
            queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        pos = y * width + x
        if visited[pos] or not is_background(x, y):
            continue
        visited[pos] = 1
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        changed = True
        for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= next_x < width and 0 <= next_y < height:
                next_pos = next_y * width + next_x
                if not visited[next_pos]:
                    queue.append((next_x, next_y))
    return rgba if changed or img.mode != "RGBA" else img


def get_processed_image(stamp):
    """StampConfig（或任何带 img/opacity/rotation/scale 的对象）的处理后图像。"""
    return process_stamp_image(stamp.img, stamp.opacity, stamp.rotation, stamp.scale)


def stamp_image_cache_clear():
    _stamp_image_cache.clear()
    _OPACITY_CACHE.clear()
    _ROTATE_CACHE.clear()
    _DISPLAY_SCALE_CACHE.clear()


def stamp_image_cache_info():
    return {
        "entries": len(_stamp_image_cache),
        "max": STAMP_IMAGE_CACHE_MAX,
        "opacity_entries": len(_OPACITY_CACHE),
        "rotate_entries": len(_ROTATE_CACHE),
        "display_entries": len(_DISPLAY_SCALE_CACHE),
        "hits": dict(_CACHE_STATS),
    }


def stamp_image_cache_stats_reset():
    """清零命中统计（基准脚本用，便于分段测量）。"""
    for name in _CACHE_STATS:
        _CACHE_STATS[name] = 0


def image_to_png_bytes(img):
    buf = io.BytesIO()
    out = img if img.mode in ("RGB", "RGBA", "L", "LA") else img.convert("RGBA")
    if out.mode == "LA":
        out = out.convert("RGBA")
    out.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 快捷键分发（纯逻辑，可断言） ====================

def widget_class_names(widget):
    """控件的类名链（Tcl `winfo class` 风格 + Python MRO 名），拿不到时返回空列表。"""
    names = []
    if widget is None:
        return names
    try:
        cls = widget.winfo_class()
        if cls:
            names.append(str(cls))
    except Exception:
        pass
    try:
        names.extend(k.__name__ for k in type(widget).__mro__)
    except Exception:
        pass
    return names


def is_editable_widget(widget):
    """光标是否落在可编辑/会吞键的控件里（Entry / Text / Listbox / Combobox / ...）。"""
    if widget is None:
        return False
    try:
        if isinstance(widget, (tk.Entry, tk.Text, tk.Listbox, tk.Spinbox)):
            return True
    except Exception:
        pass
    try:
        if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Treeview, ttk.Notebook)):
            return True
    except Exception:
        pass
    return bool(EDITABLE_WIDGET_NAMES.intersection(widget_class_names(widget)))


def shortcut_blocked(root):
    """root.focus_get() 在输入类控件里时，全局快捷键不应生效（修复 D）。"""
    if root is None:
        return False
    try:
        focused = root.focus_get()
    except Exception:
        focused = None
    return is_editable_widget(focused)


# ==================== 画布图元命中 / 拖拽判定（纯逻辑，可断言） ====================

def stamp_index_from_tags(tags, stamp_count):
    """从 canvas 图元的 tags 里取出公章索引；取不到返回 None。"""
    if not tags:
        return None
    for tag in tags:
        if tag.startswith("stamp_") and tag != "stamp_item":
            try:
                idx = int(tag.split("_")[1])
            except (IndexError, ValueError):
                continue
            if 0 <= idx < int(stamp_count):
                return idx
    return None


def drag_allowed_for_tags(tags):
    """
    该图元能否被拖拽移动。

    骑缝章现在**可以上下拖动**（垂直位置与普通章共用同一套按页位置），但水平位置
    仍由「页序 + 偏移滑块」算出，所以拖动时只应用垂直分量（见 on_mouse_drag）。
    """
    if not tags:
        return False
    return "stamp_item" in tags


# ==================== StampConfig ====================

class StampConfig:
    """公章配置类（含不变唯一 id，供历史恢复按 id 找回图片引用）。"""

    def __init__(self, img, name="公章", stamp_id=None):
        self.img = img  # PIL Image
        self.name = name
        self.stamp_id = stamp_id or new_stamp_id()
        self.x = 100.0
        self.y = 100.0
        self.scale = 1.0
        self.opacity = 1.0
        self.rotation = 0  # 旋转角度（度）
        self.page_index = 0
        self.page_scope = "all"
        self.page_positions = {}
        self.is_cross_fold = False
        self.cross_fold_offset = 0.5
        self.source_path = None

    def position_for_page(self, page_index=None):
        if page_index is not None:
            position = self.page_positions.get(int(page_index))
            if position is not None:
                return position
        return self.x, self.y

    def set_position_for_page(self, page_index, x, y):
        page_index = int(page_index)
        if page_index == 0:
            self.x, self.y = float(x), float(y)
        self.page_positions[page_index] = (float(x), float(y))

    # ---- 图像 ----
    def get_processed_img(self):
        """opacity -> rotate(expand) -> scale 之后的真实显示/导出图像（带缓存）。"""
        return process_stamp_image(self.img, self.opacity, self.rotation, self.scale)

    def get_scaled_img(self):
        """兼容旧接口：现在返回**完整处理管线**之后的图像（修复 A）。"""
        return self.get_processed_img()

    def get_display_size(self):
        return self.get_processed_img().size

    def get_tk_img(self, tk_cache=None):
        """
        画布用 PhotoImage。旧版 bug：算完 opacity/rotate 却返回未处理的缩放图（修复 A）。
        tk_cache 为 dict 时按 (stamp_id, id(processed_image)) 复用 PhotoImage（修复 E）。
        """
        processed = self.get_processed_img()
        if tk_cache is None:
            return ImageTk.PhotoImage(processed)
        key = ("stamp", self.stamp_id, id(processed))
        hit = tk_cache.get(key)
        if hit is not None and hit[0] is processed:
            return hit[1]
        photo = ImageTk.PhotoImage(processed)
        # 控制缓存规模：同一公章的旧参数项丢掉
        for k in [k for k in tk_cache if isinstance(k, tuple) and k[0] == "stamp" and k[1] == self.stamp_id]:
            if k != key:
                tk_cache.pop(k, None)
        tk_cache[key] = (processed, photo)
        return photo

    # ---- 几何 ----
    def export_geometry(self, scale_factor):
        return stamp_export_geometry(self, scale_factor)

    def cross_fold_geometry(self, page_rect, num_pages, offset=0.5, scale_factor=None):
        return cross_fold_geometry(self, page_rect, num_pages, offset, scale_factor)

    # ---- 序列化 ----
    def to_dict(self):
        return {
            "id": self.stamp_id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "scale": self.scale,
            "opacity": self.opacity,
            "rotation": self.rotation,
            "page_scope": self.page_scope,
            "page_positions": {
                str(page): [position[0], position[1]]
                for page, position in self.page_positions.items()
            },
            "is_cross_fold": self.is_cross_fold,
            "cross_fold_offset": self.cross_fold_offset,
            "source_path": self.source_path,
        }

    def apply_dict(self, data):
        self.name = data.get("name", self.name)
        self.x = data.get("x", self.x)
        self.y = data.get("y", self.y)
        self.scale = data.get("scale", self.scale)
        self.opacity = data.get("opacity", self.opacity)
        self.rotation = data.get("rotation", self.rotation)
        self.page_scope = data.get("page_scope", self.page_scope)
        self.source_path = data.get("source_path", self.source_path)
        positions = data.get("page_positions", {}) or {}
        self.page_positions = {
            int(page): (float(position[0]), float(position[1]))
            for page, position in positions.items()
            if isinstance(position, (list, tuple)) and len(position) == 2
        }
        self.is_cross_fold = bool(data.get("is_cross_fold", self.is_cross_fold))
        self.cross_fold_offset = min(1.0, max(0.0, float(
            data.get("cross_fold_offset", self.cross_fold_offset))))
        return self

    @classmethod
    def from_dict(cls, data, img, stamp_id=None):
        config = cls(img, data.get("name", "公章"),
                     stamp_id=stamp_id or data.get("id") or new_stamp_id())
        return config.apply_dict(data)


# ==================== 历史（按 id 找回，可断言） ====================

class HistoryManager:
    """历史记录管理器（撤销/重做）。状态里的每一项都带公章 id。"""

    def __init__(self, max_history=50):
        self.undo_stack = []
        self.redo_stack = []
        self.max_history = max_history

    def save_state(self, state):
        self.undo_stack.append(state)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return None
        state = self.undo_stack.pop()
        self.redo_stack.append(state)
        return self.undo_stack[-1] if self.undo_stack else None

    def redo(self):
        if not self.redo_stack:
            return None
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        return state

    def can_undo(self):
        return len(self.undo_stack) > 1

    def can_redo(self):
        return len(self.redo_stack) > 0

    def clear(self):
        self.undo_stack.clear()
        self.redo_stack.clear()


def make_history_state(stamps, current_page=0, extra=None):
    """快照：每个公章带 id + 全部可恢复属性。"""
    state = {
        "stamps": [s.to_dict() for s in stamps],
        "current_page": current_page,
    }
    if extra:
        state.update(extra)
    return state


def build_image_pool(stamps, existing=None):
    """{stamp_id: PIL.Image}；把当前公章注册进池子，池子只增不删（否则 undo 找不回图）。"""
    pool = dict(existing) if existing else {}
    for s in stamps:
        pool[s.stamp_id] = s.img
    return pool


def restore_stamps(stamps_data, image_pool):
    """
    按**唯一 id**从图片池找回图片引用（修复 B）。

    返回 (restored_list, missing)：
      restored_list : list[StampConfig]，顺序与 stamps_data 一致，id 保持不变
      missing       : list[dict]，无法找回的项 {'id', 'name', 'reason'} —— 调用方必须提示，
                      不再静默丢章。
    """
    restored = []
    missing = []
    used = set()
    for data in stamps_data or []:
        sid = data.get("id")
        img = image_pool.get(sid) if sid else None
        if img is None:
            missing.append({
                "id": sid,
                "name": data.get("name", "(未命名)"),
                "reason": "no_id" if not sid else "image_lost",
            })
            continue
        if sid in used:  # 同一 id 重复出现（异常状态）：克隆一份，不复用对象
            clone = StampConfig(img, data.get("name", "公章"), stamp_id=new_stamp_id())
            clone.apply_dict(data)
            restored.append(clone)
            continue
        used.add(sid)
        stamp = StampConfig(img, data.get("name", "公章"), stamp_id=sid)
        stamp.apply_dict(data)
        restored.append(stamp)
    return restored, missing


# ==================== 打开 PDF（含加密认证，纯逻辑） ====================

def open_pdf_document(filepath, password=None):
    """
    打开 PDF。加密文档（doc.needs_pass）时：
      password is None -> (None, OPEN_NEEDS_PASSWORD)  调用方去弹密码框
      认证失败         -> (None, OPEN_BAD_PASSWORD)
    成功返回 (doc, OPEN_OK)。任何失败路径都会 close 已打开的文档，不留句柄（修复 C）。
    """
    doc = None
    try:
        doc = fitz.open(filepath)
        if getattr(doc, "needs_pass", 0):
            if password is None:
                doc.close()
                return None, OPEN_NEEDS_PASSWORD
            ok = doc.authenticate(password)
            if not ok:
                doc.close()
                return None, OPEN_BAD_PASSWORD
        if doc.page_count <= 0:
            doc.close()
            return None, OPEN_FAILED
        return doc, OPEN_OK
    except Exception as exc:  # 文件损坏 / 非 PDF
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        return exc, OPEN_FAILED


def describe_open_status(status, filename=""):
    if status == OPEN_OK:
        return "已打开: %s" % filename
    if status == OPEN_NEEDS_PASSWORD:
        return "该 PDF 已加密，需要密码"
    if status == OPEN_BAD_PASSWORD:
        return "密码错误，已拒绝打开: %s" % filename
    if status == OPEN_CANCELLED:
        return "已取消打开: %s" % filename
    return "打开失败: %s" % filename


# ==================== 导出（每章一次编码 + xref 复用，纯逻辑） ====================

def _check_cancel(cancel_check):
    """可中断循环点的统一出口：被取消就抛 ExportCancelled（目标文件尚未替换）。"""
    if cancel_check is not None and cancel_check():
        raise ExportCancelled("用户取消了导出")


def _prepare_stamp_payloads(stamps, doc, scale_factor, cross_fold_mode,
                            cross_fold_stamp_index, cross_fold_offset,
                            cancel_check=None):
    """
    为每个公章预处理一次图像并只编码一次 PNG（修复 F / G / H）。
    返回 (payloads, encode_count)；payload 描述每页该插什么。
    """
    num_pages = doc.page_count
    payloads = []
    encode_count = 0
    for idx, stamp in enumerate(stamps):
        _check_cancel(cancel_check)
        processed = get_processed_image(stamp)
        stamp_is_cross_fold = getattr(stamp, "is_cross_fold", False)
        if stamp_is_cross_fold or (cross_fold_mode and idx == cross_fold_stamp_index):
            stamp_offset = getattr(stamp, "cross_fold_offset", cross_fold_offset)
            # 与预览共用 cross_fold_geometry（同一套切片 + 同一套坐标）
            bounds_all = slice_bounds(processed.size[0], num_pages)
            slices = split_cross_fold_images(processed, num_pages, stamp_offset)
            per_page_geo = [
                cross_fold_geometry(stamp, fitz.Rect(0, 0, *_page_size(doc, i)),
                                    num_pages, stamp_offset, scale_factor, page_index=i)
                for i in range(num_pages)
            ]
            for i, sl in enumerate(slices):
                if not stamp_applies_to_page(stamp, i, num_pages):
                    continue
                _check_cancel(cancel_check)
                payload = {
                    "kind": "cross_fold_slice",
                    "stamp_id": stamp.stamp_id,
                    "page": i,
                    "rect_pt": per_page_geo[i]["rect_pt"],
                    "size_px": (bounds_all[i][1] - bounds_all[i][0], processed.size[1]),
                    "bytes": image_to_png_bytes(sl),
                }
                encode_count += 1
                payloads.append(payload)
        else:
            png = image_to_png_bytes(processed)
            encode_count += 1
            w_px, h_px = processed.size
            size_pt = (w_px / scale_factor, h_px / scale_factor)
            for i in range(num_pages):
                if not stamp_applies_to_page(stamp, i, num_pages):
                    continue
                _check_cancel(cancel_check)
                rect_pt0 = stamp_export_geometry(stamp, scale_factor, page_index=i)["rect_pt"]
                page_w, page_h = _page_size(doc, i)
                rect_pt, shifted = clip_rect_to_page(
                    rect_pt0, fitz.Rect(0, 0, page_w, page_h), size_pt)
                payloads.append({
                    "kind": "stamp",
                    "stamp_id": stamp.stamp_id,
                    "page": i,
                    "rect_pt": rect_pt,
                    "size_px": (w_px, h_px),
                    "bytes": png,  # 同一份 bytes 复用（修复 F）
                    "shifted_into_page": shifted,
                })
    return payloads, encode_count


def _page_size(doc, page_index):
    page = doc[min(max(0, page_index), doc.page_count - 1)]
    return page.rect.width, page.rect.height


def _copy_page_links(src_doc, dst_doc):
    """复制页面链接注释；insert_pdf 不保证保留原文档的链接对象。"""
    for page_index in range(min(src_doc.page_count, dst_doc.page_count)):
        source_page = src_doc[page_index]
        target_page = dst_doc[page_index]
        for link in source_page.get_links():
            link = dict(link)
            link["xref"] = 0
            target_page.insert_link(link)


def batch_output_path(input_path, output_dir):
    """生成批量导出文件名，避免覆盖输入 PDF。"""
    base_name = os.path.basename(input_path)
    stem, extension = os.path.splitext(base_name)
    extension = extension or ".pdf"
    return os.path.join(output_dir, "%s_盖章%s" % (stem, extension))


def batch_output_paths(input_paths, output_dir):
    """为一批输入生成不重复的输出路径。"""
    paths = []
    used = set()
    for input_path in input_paths:
        candidate = batch_output_path(input_path, output_dir)
        stem, extension = os.path.splitext(candidate)
        index = 2
        while candidate.casefold() in used:
            candidate = "%s_%d%s" % (stem, index, extension)
            index += 1
        used.add(candidate.casefold())
        paths.append(candidate)
    return paths


@dataclasses.dataclass
class ExportOptions:
    """
    导出选项：把散落在各处的骑缝章/底层开关收成一个对象，避免长参数列表传错位。

    全部字段都有默认值，因此 `ExportOptions()` 等价于旧版默认行为。
    """
    cross_fold_mode: bool = False
    cross_fold_stamp_index: int = None
    cross_fold_offset: float = 0.5
    underlay: bool = True

    def normalised(self):
        """
        夹紧数值范围，保证传入脏数据也不会画到页外或产生负偏移。

        cross_fold_stamp_index 允许为 None —— 语义是「按每个章自己的 is_cross_fold 判断」，
        这是当前 GUI 的默认用法，不能强行转成 0（会误伤第 0 枚普通章）。
        """
        offset = min(1.0, max(0.0, float(self.cross_fold_offset)))
        index = None if self.cross_fold_stamp_index is None else max(
            0, int(self.cross_fold_stamp_index))
        return ExportOptions(bool(self.cross_fold_mode), index, offset, bool(self.underlay))

    @classmethod
    def from_namespace(cls, **kwargs):
        """从关键字参数构造（忽略未知键），便于旧调用点平滑迁移。"""
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in kwargs.items() if k in fields})


class ExportCancelled(Exception):
    """
    用户主动取消导出。

    抛出它时导出函数尚未执行 `os.replace()`，因此目标文件保持原样；
    临时文件由 export_pdf_with_stamps 的 finally 清理。
    """


def snapshot_stamps(stamps):
    """
    为后台线程克隆一份公章（图像也复制），使 worker 与主线程不共享可变状态。

    为什么要克隆：
      - worker 里 get_processed_img() 会写模块级缓存；与主线程的 StampConfig 隔离后，
        主线程随时改参数都不会影响正在跑的导出（导出用的是"点下按钮那一刻"的快照）；
      - 克隆体由 job 持有引用直到结束，因此 id() 在任务期间稳定，不会被回收后复用。
    """
    clones = []
    for stamp in stamps:
        clone = StampConfig(stamp.img.copy(), stamp.name, stamp_id=stamp.stamp_id)
        clone.scale = stamp.scale
        clone.opacity = stamp.opacity
        clone.rotation = stamp.rotation
        clone.x, clone.y = stamp.x, stamp.y
        clone.page_scope = stamp.page_scope
        clone.page_positions = dict(stamp.page_positions)
        clone.is_cross_fold = stamp.is_cross_fold
        clone.cross_fold_offset = stamp.cross_fold_offset
        clones.append(clone)
    return clones


def export_pdf_with_stamps(src_doc, stamps, out_path=None, scale_factor=None,
                           cross_fold_mode=False, cross_fold_stamp_index=0,
                           cross_fold_offset=0.5, underlay=True, options=None,
                           cancel_check=None):
    """
    导出盖章后的 PDF（预览/导出共用同一套几何函数）。

    参数
      src_doc    : 已打开的 fitz.Document（不会被修改、不会被 close）
      stamps     : list[StampConfig]
      out_path   : 目标路径；None 时只计算并写进临时文件（便于测试），返回 report['output_path']
      scale_factor : 画布像素 -> PDF point（默认 canvas_scale()）
      options    : ExportOptions；给定时覆盖下面四个骑缝章/底层参数（推荐用法）
      cross_fold_mode / cross_fold_stamp_index / cross_fold_offset / underlay :
                   旧的散装参数，保持向后兼容；显式传入 options 时以 options 为准。
      cancel_check : 可选的无参回调，返回真值表示用户已请求取消；在每个可中断的
                   循环点被调用（每枚章、每页、每次插图）。取消时抛 ExportCancelled，
                   此时目标文件尚未被替换，原有文件不受影响。
    """
    if options is None:
        options = ExportOptions(cross_fold_mode, cross_fold_stamp_index,
                                cross_fold_offset, underlay)
    options = options.normalised()
    cross_fold_mode = options.cross_fold_mode
    cross_fold_stamp_index = options.cross_fold_stamp_index
    cross_fold_offset = options.cross_fold_offset
    underlay = options.underlay
    sf = float(scale_factor if scale_factor is not None else canvas_scale())
    own_tmp = out_path is None
    requested_path = out_path
    if own_tmp:
        fd, out_path = tempfile.mkstemp(prefix="pdf_stamper_", suffix=".pdf")
        os.close(fd)
    else:
        out_path = os.path.abspath(out_path)
        source_path = getattr(src_doc, "name", None)
        if source_path and os.path.abspath(source_path) == out_path:
            raise ValueError("输出路径不能与输入 PDF 相同")
    target_dir = os.path.dirname(os.path.abspath(out_path)) or os.getcwd()
    os.makedirs(target_dir, exist_ok=True)
    atomic_path = None
    fd, atomic_path = tempfile.mkstemp(prefix=".pdf_stamper_", suffix=".tmp", dir=target_dir)
    os.close(fd)
    try:
        new_doc = fitz.open()
        try:
            new_doc.insert_pdf(src_doc)
            _copy_page_links(src_doc, new_doc)
            payloads, encode_count = _prepare_stamp_payloads(
                stamps, new_doc, sf, cross_fold_mode, cross_fold_stamp_index,
                cross_fold_offset, cancel_check=cancel_check)
            embedded = []
            xref_by_bytes = {}
            for payload in payloads:
                _check_cancel(cancel_check)
                page = new_doc[payload["page"]]
                rect = fitz.Rect(*payload["rect_pt"])
                xref = xref_by_bytes.get(id(payload["bytes"]))
                if xref:
                    page.insert_image(rect, xref=xref, overlay=not underlay)
                else:
                    xref = page.insert_image(rect, stream=payload["bytes"],
                                             overlay=not underlay) or None
                    if xref:
                        xref_by_bytes[id(payload["bytes"])] = xref
                embedded.append({
                    "page": payload["page"],
                    "xref": xref,
                    "rect": tuple(rect),
                    "size_px": payload["size_px"],
                    "center_pt": rect_center(tuple(rect)),
                    "kind": payload["kind"],
                    "stamp_id": payload.get("stamp_id"),
                    "shifted_into_page": payload.get("shifted_into_page", False),
                })
            underlay_fallback_pages = []
            if underlay and payloads:
                underlay_fallback_pages = _apply_underlay_fallback(
                    src_doc, new_doc, payloads, sf, cancel_check=cancel_check)
            _check_cancel(cancel_check)
            new_doc.save(atomic_path, garbage=3, deflate=True)
            per_page = [len(new_doc[i].get_images(full=True)) for i in range(new_doc.page_count)]
            page_count = new_doc.page_count
        finally:
            new_doc.close()
        os.replace(atomic_path, out_path)
        atomic_path = None
        return {
            "output_path": requested_path or out_path,
            "page_count": page_count,
            "embedded": embedded,
            "per_page_image_counts": per_page,
            "png_encode_count": encode_count,
            "scale_factor": sf,
            "flattened_pages": [],
            "underlay_fallback_pages": underlay_fallback_pages,
        }
    finally:
        for path in (atomic_path, out_path if own_tmp else None):
            if path:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

def _apply_underlay_fallback(src_doc, new_doc, payloads, scale_factor, cancel_check=None):
    """
    底层模式的安全网：检测「整页不透明背景把公章压住」的页面并改用合成画法。

    性能要点（实测）：
      - 只在印章矩形内渲染比对，而不是整页：整页 11.26 ms/次 vs 矩形 2.32 ms/次。
        30 页 × 2 次渲染从 ~675 ms 降到 ~140 ms。
      - 先用页面图像对象做廉价预筛：没有「覆盖印章的大图」就直接跳过渲染。
        绝大多数 PDF 在此短路，能力与旧实现等价（旧实现渲染后也会判定为「未遮挡」）。
    """
    if not payloads:
        return []
    dpi = max(1, int(round(scale_factor * 72.0)))
    pixels_per_point = dpi / 72.0
    payloads_by_page = {}
    for payload in payloads:
        payloads_by_page.setdefault(payload["page"], []).append(payload)

    fallback_pages = []
    for page_index, page_payloads in payloads_by_page.items():
        _check_cancel(cancel_check)
        source_page = src_doc[page_index]
        # 需要比对的区域 = 本页所有印章矩形并集（转成 pixmap 整数边界）
        rects = [payload["rect_pt"] for payload in page_payloads]
        detect_rect = fitz.Rect(
            min(rect[0] for rect in rects), min(rect[1] for rect in rects),
            max(rect[2] for rect in rects), max(rect[3] for rect in rects))
        if detect_rect.is_empty:
            continue
        if not _may_hide_underlay(source_page, detect_rect):
            continue
        matrix = fitz.Matrix(pixels_per_point, pixels_per_point)
        base_pixmap = source_page.get_pixmap(
            matrix=matrix, clip=detect_rect, colorspace=fitz.csRGB, alpha=False)
        stamped_pixmap = new_doc[page_index].get_pixmap(
            matrix=matrix, clip=detect_rect, colorspace=fitz.csRGB, alpha=False)
        hidden = [
            payload for payload in page_payloads
            if _same_rendered_region(base_pixmap, stamped_pixmap,
                                     payload["rect_pt"], pixels_per_point)
        ]
        if not hidden:
            continue
        fallback_pages.append(page_index)
        _rebuild_page_with_composited_underlay(
            source_page, new_doc[page_index], page_payloads,
            pixels_per_point, base_pixmap)
    return fallback_pages


def _may_hide_underlay(page, detect_rect=None):
    """
    廉价预筛：页面是否存在可能遮住底层印章的**不透明内容**。

    只把「确实没有可疑内容」判为 False 以跳过渲染比对；任何判断不了的情况一律返回
    True（保守，宁可多渲染也不漏兜底）。

    判定只用两个便宜调用（实测各约 0.1 ms）：
      1. 页面有无图像对象 —— 有就必须渲染比对（可能是整页扫描件 / 白底图）；
      2. 页面绘制指令中有无「填充矩形」—— 整页纯色背景常由矢量填充实现。

    不要用 get_image_rects() 去量图像尺寸：实测约 3.7 ms/次，比它想省下的那次
    局部渲染（约 2.3 ms）还贵，属于净亏损。
    """
    try:
        if page.get_images(full=True):
            return True
        return _page_draws_filled_rect(page)
    except Exception:
        return True


def _page_draws_filled_rect(page):
    """
    页面是否含填充矩形绘制指令（用于识别整页纯色背景）。

    用 get_drawings() 比解析内容流稳妥；取不到时返回 True（保守，不跳过检测）。
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return True
    for drawing in drawings:
        if drawing.get("fill") is None:
            continue
        rect = drawing.get("rect")
        if rect is None or rect.is_empty:
            continue
        return True
    return False


def _rebuild_page_with_composited_underlay(source_page, target_page, page_payloads,
                                           pixels_per_point, base_pixmap):
    """把该页重画为「页面 + 公章」的合成补丁，绕过不透明背景的遮挡。"""
    base_image = Image.frombytes(
        "RGB", (base_pixmap.width, base_pixmap.height), base_pixmap.samples)
    origin_x, origin_y = base_pixmap.x, base_pixmap.y
    fallback_items = []
    for payload in page_payloads:
        with Image.open(io.BytesIO(payload["bytes"])) as stamp_image:
            stamp_image = stamp_image.convert("RGBA")
        bounds = _pdf_rect_to_pixmap_bounds(
            payload["rect_pt"], pixels_per_point, origin_x, origin_y)
        x0, y0, x1, y1 = bounds
        target_size = (max(1, x1 - x0), max(1, y1 - y0))
        if stamp_image.size != target_size:
            stamp_image = resize_lanczos(stamp_image, target_size)
        fallback_items.append({"x": x0, "y": y0, "image": stamp_image})
    composed = compose_underlay_fallback(base_image, fallback_items)
    rects = [payload["rect_pt"] for payload in page_payloads]
    patch_rect = fitz.Rect(
        min(rect[0] for rect in rects), min(rect[1] for rect in rects),
        max(rect[2] for rect in rects), max(rect[3] for rect in rects))
    patch_x0, patch_y0, patch_x1, patch_y1 = _pdf_rect_to_pixmap_bounds(
        tuple(patch_rect), pixels_per_point, origin_x, origin_y)
    patch_x0 = max(0, patch_x0)
    patch_y0 = max(0, patch_y0)
    patch_x1 = min(composed.width, patch_x1)
    patch_y1 = min(composed.height, patch_y1)
    if patch_x0 >= patch_x1 or patch_y0 >= patch_y1:
        return
    patch = composed.crop((patch_x0, patch_y0, patch_x1, patch_y1))
    target_page.insert_image(patch_rect, stream=image_to_png_bytes(patch), overlay=True)


def _pdf_rect_to_pixmap_bounds(rect_pt, pixels_per_point, origin_x=0, origin_y=0):
    """
    PDF point 矩形 -> 位图像素整数边界 (x0, y0, x1, y1)。

    point ↔ 像素 的换算原来在导出检测与合成两处各写了一遍，这里收敛成单一来源，
    避免以后改 render_dpi 时只改一处。
    """
    x0 = int(round(rect_pt[0] * pixels_per_point)) - origin_x
    y0 = int(round(rect_pt[1] * pixels_per_point)) - origin_y
    x1 = int(round(rect_pt[2] * pixels_per_point)) - origin_x
    y1 = int(round(rect_pt[3] * pixels_per_point)) - origin_y
    return (x0, y0, x1, y1)


def _same_rendered_region(base_pixmap, stamped_pixmap, rect_pt, pixels_per_point):
    """
    判断插入底层印章后，其目标区域是否仍与原页完全相同（即被背景遮挡）。

    base/stamped 可以是整页位图，也可以是「按 rect 裁剪过的局部位图」。局部位图时
    pixmap.x / pixmap.y 已经是**缩放后的像素原点**（实测：clip=Rect(336,576,…) 在
    matrix=2.0833 下得 .x=700, .y=1200），所以这里减去它即可把页面坐标的 rect_pt
    换算到局部位图坐标，不需要再乘一次 pixels_per_point。
    """
    if (base_pixmap.width != stamped_pixmap.width
            or base_pixmap.height != stamped_pixmap.height
            or base_pixmap.x != stamped_pixmap.x
            or base_pixmap.y != stamped_pixmap.y):
        return False
    base = Image.frombytes("RGB", (base_pixmap.width, base_pixmap.height), base_pixmap.samples)
    stamped = Image.frombytes("RGB", (stamped_pixmap.width, stamped_pixmap.height),
                              stamped_pixmap.samples)
    x0, y0, x1, y1 = _pdf_rect_to_pixmap_bounds(
        rect_pt, pixels_per_point, base_pixmap.x, base_pixmap.y)
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(base.width, x1)
    y1 = min(base.height, y1)
    if x0 >= x1 or y0 >= y1:
        return False
    bounds = (x0, y0, x1, y1)
    return base.crop(bounds).tobytes() == stamped.crop(bounds).tobytes()


def compose_underlay_fallback(base, stamp_items):
    """印章被不透明页面背景遮挡时，按预览规则合成印章并恢复深色前景。"""
    composed = base.convert("RGBA")
    for item in stamp_items:
        composed.alpha_composite(item["image"], (int(round(item["x"])),
                                                   int(round(item["y"]))))
    for item in stamp_items:
        x0 = max(0, int(round(item["x"])))
        y0 = max(0, int(round(item["y"])))
        x1 = min(base.width, x0 + item["image"].width)
        y1 = min(base.height, y0 + item["image"].height)
        if x0 >= x1 or y0 >= y1:
            continue
        crop = base.crop((x0, y0, x1, y1)).convert("RGB")
        mask = crop.convert("L").point(lambda value: 255 if value < 245 else 0)
        composed.paste(crop, (x0, y0), mask)
    return composed.convert("RGB")


# ==================== 会话状态（非 GUI，可断言；修复 C） ====================

class DocumentSession:
    """
    一次「文档 + 公章」会话的全部非界面状态。open_pdf / close / 历史 都在这里，
    GUI 只是它的客户端，所以测试可以直接断言状态清理与历史恢复。
    """

    def __init__(self, render_dpi=DEFAULT_RENDER_DPI):
        self.pdf_doc = None
        self.pdf_path = None
        self.total_pages = 0
        self.current_page = 0
        self.stamps = []
        self.history = HistoryManager()
        self.image_pool = {}          # {stamp_id: PIL.Image}，只增不删，供 undo 找回
        self.active_stamp_idx = 0
        self.cross_fold_stamp_index = None
        self.selected_stamp = None    # 画布上被选中的公章索引
        self.cross_fold_mode = False
        self.cross_fold_offset = 0.5
        self.stamp_underlay = True
        self.seal_mode = "全部页面加印章"
        self.stamp_mode_enabled = True
        self.render_dpi = float(render_dpi)
        self.scale_factor = canvas_scale(self.render_dpi)
        self.last_restore_missing = []
        self.password = None    # 当前文档成功打开时用过的密码（仅内存）

    # ---- 文档 ----
    def close_document(self):
        """关闭当前 PDF 句柄（防止文件句柄泄漏）。"""
        doc, self.pdf_doc = self.pdf_doc, None
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    def reset_for_new_document(self):
        """打开新 PDF 前彻底清理状态（修复 C 的状态残留）。"""
        self.close_document()
        self.pdf_path = None
        self.total_pages = 0
        self.current_page = 0
        self.stamps = []
        self.image_pool = {}
        self.active_stamp_idx = 0
        self.cross_fold_stamp_index = None
        self.selected_stamp = None
        self.cross_fold_mode = False
        self.cross_fold_offset = 0.5
        self.stamp_underlay = True
        self.seal_mode = "全部页面加印章"
        self.stamp_mode_enabled = True
        self.history = HistoryManager()
        self.last_restore_missing = []
        self.password = None

    def load_document(self, filepath, password=None):
        """
        打开新文档：先清理旧状态 + 关闭旧句柄，再打开。
        返回 (status, message)；status 见 OPEN_* 常量。
        """
        self.reset_for_new_document()
        result, status = open_pdf_document(filepath, password)
        if status != OPEN_OK:
            msg = describe_open_status(status, os.path.basename(filepath))
            if status == OPEN_FAILED and isinstance(result, Exception):
                msg = "%s (%s)" % (msg, result)
            return status, msg
        self.pdf_doc = result
        self.pdf_path = filepath
        self.total_pages = result.page_count
        self.current_page = 0
        # 记住本次成功使用的密码：后台导出要自己重开一份文档（fitz 非线程安全），
        # 有了它就不必让用户为同一份加密文件再输一次。仅存内存，不写进配置文件。
        self.password = password
        return OPEN_OK, describe_open_status(OPEN_OK, os.path.basename(filepath))

    # ---- 公章 ----
    def add_stamp(self, img, name="公章"):
        img = remove_white_background(img)
        stamp = StampConfig(img, name)
        self.stamps.append(stamp)
        self.image_pool[stamp.stamp_id] = img
        self.active_stamp_idx = len(self.stamps) - 1
        return stamp

    def remove_stamp_by_index(self, idx):
        if 0 <= idx < len(self.stamps):
            stamp = self.stamps.pop(idx)
            if self.cross_fold_stamp_index is not None:
                if self.cross_fold_stamp_index == idx:
                    self.cross_fold_stamp_index = None
                elif self.cross_fold_stamp_index > idx:
                    self.cross_fold_stamp_index -= 1
            if self.active_stamp_idx >= len(self.stamps):
                self.active_stamp_idx = max(0, len(self.stamps) - 1)
            if self.selected_stamp is not None and self.selected_stamp >= len(self.stamps):
                self.selected_stamp = None
            return stamp
        return None

    def get_stamp(self, idx):
        if 0 <= idx < len(self.stamps):
            return self.stamps[idx]
        return None

    @property
    def active_stamp(self):
        return self.get_stamp(self.active_stamp_idx)

    # ---- 历史 ----
    def snapshot(self):
        selected = self.get_stamp(self.selected_stamp) if self.selected_stamp is not None else None
        return make_history_state(self.stamps, self.current_page,
                                  extra={
                                      "selected": selected.stamp_id if selected else None,
                                      "cross_fold_offset": self.cross_fold_offset,
                                      "cross_fold_mode": self.cross_fold_mode,
                                      "cross_fold_stamp_index": self.cross_fold_stamp_index,
                                      "stamp_underlay": self.stamp_underlay,
                                      "seal_mode": self.seal_mode,
                                      "stamp_mode_enabled": self.stamp_mode_enabled,
                                  })

    def save_history(self):
        self.history.save_state(self.snapshot())
        return self.history

    def restore(self, state):
        """按 id 恢复；找回失败的项记录在 last_restore_missing（不再静默丢章）。"""
        if not state:
            return [], []
        stamps_data = state.get("stamps", [])
        # 防御：把当前公章注册进池（image_pool 只增不删，保证 undo 能找回被删的章）
        self.image_pool = build_image_pool(self.stamps, self.image_pool)
        restored, missing = restore_stamps(stamps_data, self.image_pool)
        self.stamps = restored
        self.last_restore_missing = missing
        if (self.cross_fold_stamp_index is not None
                and self.cross_fold_stamp_index >= len(self.stamps)):
            self.cross_fold_stamp_index = None
        self.current_page = min(max(0, int(state.get("current_page", self.current_page) or 0)),
                                max(0, self.total_pages - 1)) if self.total_pages else 0
        if "cross_fold_offset" in state:
            self.cross_fold_offset = min(1.0, max(0.0, float(state["cross_fold_offset"])))
        if "cross_fold_mode" in state:
            self.cross_fold_mode = bool(state["cross_fold_mode"])
        if "cross_fold_stamp_index" in state:
            value = state["cross_fold_stamp_index"]
            self.cross_fold_stamp_index = None if value is None else int(value)
        if "stamp_underlay" in state:
            self.stamp_underlay = bool(state["stamp_underlay"])
        if "seal_mode" in state:
            self.seal_mode = str(state["seal_mode"])
        if "stamp_mode_enabled" in state:
            self.stamp_mode_enabled = bool(state["stamp_mode_enabled"])
        if self.active_stamp_idx >= len(self.stamps):
            self.active_stamp_idx = max(0, len(self.stamps) - 1)
        sel_id = state.get("selected")
        self.selected_stamp = None
        if sel_id:
            for i, s in enumerate(self.stamps):
                if s.stamp_id == sel_id:
                    self.selected_stamp = i
                    break
        return restored, missing

    def undo(self):
        if not self.history.can_undo():
            return None
        return self.restore(self.history.undo())

    def redo(self):
        if not self.history.can_redo():
            return None
        return self.restore(self.history.redo())

    # ---- 页面 ----
    def goto_page_index(self, page_index):
        page_index = int(page_index)
        if not self.pdf_doc or self.total_pages <= 0:
            return False
        if not (0 <= page_index < self.total_pages):
            return False
        self.current_page = page_index
        return True


# ==================== 渲染核心（缓存；修复 E） ====================

class RenderCore:
    """
    页面位图缓存 + 公章位图缓存。GUI 的 render_page 只调用它，不自己算。

    - get_page_bitmap : {页面: PIL 位图} LRU；换文档 / 改 dpi 时 invalidate()
    - payload         : 一次拿到「页面位图 + 每个公章要画的图像和坐标」，
                        拖拽时 GUI 只移动 canvas 图元，不重新调用 payload
    """

    def __init__(self, dpi=DEFAULT_RENDER_DPI, max_page_cache=8,
                 max_page_bytes=96 * 1024 * 1024):
        self.dpi = float(dpi)
        self.scale_factor = canvas_scale(self.dpi)
        # 预先算好 Matrix：比每次传 dpi= 少构造一次 Matrix 对象
        # （实测收益在噪声内，但写法更直接，也让 get_pixmap 参数与下面保持一致）
        self._matrix = fitz.Matrix(self.scale_factor, self.scale_factor)
        self.max_page_cache = int(max_page_cache)
        self.max_page_bytes = int(max_page_bytes)
        self._page_cache = OrderedDict()   # key -> PIL.Image
        self._page_bytes = 0
        self._doc_token = None
        self.last_underlay_fallback = False
        self.stats = {"page_hit": 0, "page_miss": 0, "stamp_hit": 0, "stamp_miss": 0}

    # ---- 缓存管理 ----
    def invalidate(self):
        self._page_cache.clear()
        self._page_bytes = 0
        self._doc_token = None

    def _check_doc(self, doc):
        token = (id(doc), doc.page_count, getattr(doc, "is_encrypted", None), str(doc.name))
        if token != self._doc_token:
            self.invalidate()
            self._doc_token = token

    def cache_info(self):
        return {"cached_pages": len(self._page_cache),
                "cached_page_bytes": self._page_bytes,
                "max_page_bytes": self.max_page_bytes,
                "stats": dict(self.stats),
                "stamp_cache": stamp_image_cache_info()}

    # ---- 页面位图 ----
    def page_bitmap_key(self, doc, page_index):
        return (id(doc), int(page_index), self.dpi)

    def get_page_bitmap(self, doc, page_index):
        """
        渲染（或命中缓存）某页的位图。

        淘汰按「像素预算」（max_page_bytes）而不是固定页数：A4@150dpi 单页约 6.2 MB，
        而 A0/A1 大幅面单页可达数十 MB，按页数淘汰会让内存失控。

        注：曾尝试缓存 DisplayList 以省去内容流解析，但实测解析只占渲染耗时的约 6.5%
        （解析 0.14 ms vs 光栅化 2.04 ms），冷渲染 30 页三种写法中位数都在 230 ms 上下，
        差异在噪声内，因此不引入这层额外复杂度。
        """
        self._check_doc(doc)
        key = self.page_bitmap_key(doc, page_index)
        hit = self._page_cache.get(key)
        if hit is not None:
            self._page_cache.move_to_end(key)
            self.stats["page_hit"] += 1
            return hit
        self.stats["page_miss"] += 1
        page = doc[min(max(0, int(page_index)), doc.page_count - 1)]
        pix = page.get_pixmap(matrix=self._matrix, colorspace=fitz.csRGB, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self._page_cache[key] = img
        self._page_bytes += pix.width * pix.height * 3
        self._evict_page_cache()
        return img

    def _evict_page_cache(self):
        """按像素预算 + 页数上限双重淘汰（至少保留 1 页，避免刚放进就被踢掉）。"""
        while len(self._page_cache) > 1 and (
                len(self._page_cache) > self.max_page_cache
                or self._page_bytes > self.max_page_bytes):
            _, evicted = self._page_cache.popitem(last=False)
            try:
                self._page_bytes -= evicted.width * evicted.height * 3
            except Exception:
                self._page_bytes = 0
        if not self._page_cache:
            self._page_bytes = 0

    # ---- 公章位图（走全局 process 缓存，顺便统计命中） ----
    def get_stamp_bitmap(self, stamp):
        img = stamp.img
        key = (id(img), img.size, img.mode, round(float(stamp.opacity), 4),
               int(stamp.rotation) % 360, round(float(stamp.scale), 4))
        cached = key in _stamp_image_cache
        processed = get_processed_image(stamp)
        self.stats["stamp_hit" if cached else "stamp_miss"] += 1
        return processed

    # ---- 一次渲染的完整载荷 ----
    def build_payload(self, doc, page_index, stamps, cross_fold_mode=False,
                      cross_fold_stamp_index=None, cross_fold_offset=0.5,
                      options=None):
        """
        返回 {'page_bitmap', 'stamp_items': [{'tag','stamp_id','x','y','image','size','cross_fold'}]}
        x/y 为画布像素左上角；骑缝章时坐标由 cross_fold_geometry 算出（与导出同源）。

        options 给定时覆盖三个骑缝章参数（与 export_pdf_with_stamps 共用同一套语义）。
        """
        if options is not None:
            opts = options.normalised()
            cross_fold_mode = opts.cross_fold_mode
            cross_fold_stamp_index = opts.cross_fold_stamp_index
            cross_fold_offset = opts.cross_fold_offset
        self._check_doc(doc)
        page_index = min(max(0, int(page_index)), doc.page_count - 1)
        page = doc[page_index]
        items = []
        for idx, stamp in enumerate(stamps):
            if not stamp_applies_to_page(stamp, page_index, doc.page_count):
                continue
            image = self.get_stamp_bitmap(stamp)
            stamp_is_cross_fold = getattr(stamp, "is_cross_fold", False)
            if stamp_is_cross_fold or (cross_fold_mode and idx == cross_fold_stamp_index):
                stamp_offset = getattr(stamp, "cross_fold_offset", cross_fold_offset)
                geo = cross_fold_geometry(stamp, page.rect, doc.page_count,
                                          stamp_offset, self.scale_factor,
                                          page_index=page_index)
                start, end = geo["slice_bounds"]
                image = image.crop((start, 0, end, image.size[1]))
                rect_px = cross_fold_rect_px(stamp, page.rect, doc.page_count,
                                             stamp_offset, self.scale_factor,
                                             page_index=page_index)
                x, y = rect_px[0], rect_px[1]
                kind = "cross_fold"
            else:
                x, y = stamp.position_for_page(page_index)
                kind = "stamp"
            items.append({
                "tag": "stamp_%s" % idx,
                "stamp_id": stamp.stamp_id,
                "index": idx,
                "x": x,
                "y": y,
                "image": image,
                "size": image.size,
                "kind": kind,
            })
        return {
            "page_bitmap": self.get_page_bitmap(doc, page_index),
            "page_index": page_index,
            "stamp_items": items,
        }

    def build_underlay_bitmap(self, doc, payload):
        """把公章以 PDF 底层顺序插入临时页面后再渲染，确保文字压在公章上。"""
        self.last_underlay_fallback = False
        temp_doc = fitz.open()
        try:
            temp_doc.insert_pdf(doc, from_page=payload["page_index"],
                                to_page=payload["page_index"])
            page = temp_doc[0]
            for item in payload["stamp_items"]:
                image = item["image"]
                rect = fitz.Rect(
                    item["x"] / self.scale_factor,
                    item["y"] / self.scale_factor,
                    (item["x"] + image.width) / self.scale_factor,
                    (item["y"] + image.height) / self.scale_factor,
                )
                page.insert_image(rect, stream=image_to_png_bytes(image), overlay=False)
            pix = page.get_pixmap(matrix=self._matrix, colorspace=fitz.csRGB, alpha=False)
            underlay = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            base = payload["page_bitmap"]
            hidden_stamp = False
            for item in payload["stamp_items"]:
                x0 = max(0, int(round(item["x"])))
                y0 = max(0, int(round(item["y"])))
                x1 = min(base.width, x0 + item["image"].width)
                y1 = min(base.height, y0 + item["image"].height)
                if x0 < x1 and y0 < y1:
                    if underlay.crop((x0, y0, x1, y1)) == base.crop((x0, y0, x1, y1)):
                        hidden_stamp = True
                        break
            if not hidden_stamp:
                return underlay

            # 某些 PDF 用整页白色背景对象覆盖底层图片；用页面深色内容恢复文字。
            self.last_underlay_fallback = True
            return compose_underlay_fallback(base, payload["stamp_items"])
        finally:
            temp_doc.close()


# ==================== 后台导出任务 ====================

class ExportJob:
    """
    在后台线程跑一次导出，主线程用 `root.after()` 轮询事件队列。

    线程安全约定（三条，缺一不可）：
      1. **worker 不碰 GUI 状态**：不读 `self.pdf_doc`，不调 Tk。需要文档时自己
         `fitz.open()` 一份；公章用 `snapshot_stamps()` 克隆后独占。
      2. **所有 Tk 调用只在主线程**：worker 把事件压进 `self.events`（queue.Queue），
         主线程在 `poll()` 里出队并应用（进度条 / 状态栏 / 弹窗）。
      3. **需要主线程弹窗时必须等回执**：worker 发 `ask_password` 请求并阻塞在
         `threading.Event` 上，主线程处理完写入 reply 再 set，worker 才继续。

    取消：`cancel()` 只 set 一个 Event，真正的退出发生在导出函数的下一个
    `_check_cancel()` 检查点（每枚章 / 每页 / 每次插图）。因为 `os.replace()`
    尚未执行，目标文件保持原样。

    可测试性：`poll()` 不依赖 Tk —— 测试可以自己循环 drain 事件、对 `ask_password`
    直接给出回复，从而在无 GUI 环境下验证整条链路。
    """

    def __init__(self, worker, poll_ms=60):
        """
        worker: 接受 (job) 一个参数的可调用对象；job 提供 `cancelled` 属性与
                `ask_password(filepath)` 方法。返回值作为 done 事件的结果。
        """
        self.worker = worker
        self.poll_ms = poll_ms
        self.events = queue.Queue()
        self._cancel_event = threading.Event()
        self._thread = None
        self._password_reply = None
        self._password_wait = None
        self.done = False
        self.result = None
        self.error = None
        self.cancelled_flag = False

    # ---- worker 侧 ----

    @property
    def cancelled(self):
        return self._cancel_event.is_set()

    def cancel(self):
        """请求取消；worker 在下一个检查点退出。"""
        self._cancel_event.set()

    def post(self, kind, **payload):
        """worker 侧：发一个事件给主线程。"""
        self.events.put((kind, payload))

    def ask_password(self, filepath):
        """
        worker 侧：请主线程弹密码框并阻塞等待结果。

        用户取消（或主线程尚未响应就被要求退出）时返回 None。
        """
        reply = {"value": None}
        wait = threading.Event()
        self._password_reply = reply
        self._password_wait = wait
        self.post("ask_password", filepath=filepath)
        wait.wait()
        return reply["value"]

    # ---- 主线程侧 ----

    def start(self):
        self._thread = threading.Thread(target=self._run, name="pdf-export", daemon=True)
        self._thread.start()
        return self._thread

    def _run(self):
        try:
            self.result = self.worker(self)
        except ExportCancelled:
            self.cancelled_flag = True
        except BaseException as exc:            # noqa: BLE001 - 必须带回主线程再弹窗
            self.error = exc
        finally:
            self.done = True

    def answer_password(self, password):
        """主线程侧：回复 worker 的密码请求。"""
        if self._password_reply is None or self._password_wait is None:
            return
        self._password_reply["value"] = password
        self._password_wait.set()
        self._password_reply = None
        self._password_wait = None

    def poll(self):
        """
        主线程侧：取出当前所有事件。

        返回 (events, finished)：events 是 [(kind, payload), ...]，
        finished 表示 worker 已结束（此时再调一次可拿到 done 事件）。
        """
        drained = []
        while True:
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                break
        return drained, self.done


# ==================== GUI ====================

class PDFStamper:
    def __init__(self, root, render_dpi=DEFAULT_RENDER_DPI):
        self.root = root
        self.root.title("PDF盖章工具 v3.3")
        self.root.geometry("1400x900")

        # 非界面状态全部放在 DocumentSession（可被测试直接驱动）
        self.session = DocumentSession(render_dpi=render_dpi)
        self.core = RenderCore(dpi=render_dpi)
        self.render_dpi = self.session.render_dpi
        self.scale_factor = self.session.scale_factor

        # 拖拽状态（拖拽期间只 move 图元，不重绘 —— 修复 E）
        self.is_dragging = False
        self.drag_item_tag = None
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.drag_last_x = 0
        self.drag_last_y = 0
        self.drag_moved = False
        self.drag_vertical_only = False

        self._tk_img_cache = {}
        self.file_mode = "单文件模式"
        self.batch_paths = []
        self.seal_mode = "全部页面加印章"
        self.stamp_mode_enabled = True
        self._parameter_drag_state = None
        self._offset_drag_state = None
        self._batch_password = None
        self.view_zoom = 1.0

        # 后台导出任务（见 ExportJob）：同一时刻只允许一个
        self._export_job = None
        self._export_on_done = None

        self.setup_ui()

    # ---- session 代理（保持既有属性写法可用） ----
    @property
    def pdf_doc(self):
        return self.session.pdf_doc

    @pdf_doc.setter
    def pdf_doc(self, value):
        self.session.pdf_doc = value

    @property
    def pdf_path(self):
        return self.session.pdf_path

    @property
    def stamps(self):
        return self.session.stamps

    @stamps.setter
    def stamps(self, value):
        self.session.stamps = list(value)

    @property
    def current_page(self):
        return self.session.current_page

    @current_page.setter
    def current_page(self, value):
        self.session.current_page = int(value)

    @property
    def total_pages(self):
        return self.session.total_pages

    @total_pages.setter
    def total_pages(self, value):
        self.session.total_pages = int(value)

    @property
    def history(self):
        return self.session.history

    @property
    def selected_stamp(self):
        return self.session.selected_stamp

    @selected_stamp.setter
    def selected_stamp(self, value):
        self.session.selected_stamp = value

    @property
    def active_stamp_idx(self):
        return self.session.active_stamp_idx

    @active_stamp_idx.setter
    def active_stamp_idx(self, value):
        self.session.active_stamp_idx = int(value)

    @property
    def cross_fold_mode(self):
        return self.session.cross_fold_mode

    @cross_fold_mode.setter
    def cross_fold_mode(self, value):
        self.session.cross_fold_mode = bool(value)

    @property
    def cross_fold_stamp_index(self):
        return self.session.cross_fold_stamp_index

    @cross_fold_stamp_index.setter
    def cross_fold_stamp_index(self, value):
        self.session.cross_fold_stamp_index = None if value is None else int(value)

    @property
    def cross_fold_offset(self):
        return self.session.cross_fold_offset

    @cross_fold_offset.setter
    def cross_fold_offset(self, value):
        self.session.cross_fold_offset = float(value)

    @property
    def stamp_underlay(self):
        return self.session.stamp_underlay

    @stamp_underlay.setter
    def stamp_underlay(self, value):
        self.session.stamp_underlay = bool(value)

    # ------------------------------------------------------------------ UI
    def setup_ui(self):
        self.create_toolbar()
        self.create_side_panel()
        self.create_canvas()
        self.create_status_bar()
        self.bind_shortcuts()

    def create_toolbar(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        mode_frame = ttk.Frame(toolbar)
        mode_frame.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(mode_frame, text="文件模式").pack(side=tk.LEFT, padx=(0, 2))
        self.file_mode_var = tk.StringVar(value=self.file_mode)
        self.file_mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.file_mode_var,
            values=("单文件模式", "批量文件模式"),
            state="readonly", width=10)
        self.file_mode_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.file_mode_combo.bind("<<ComboboxSelected>>", self.on_file_mode_change)

        ttk.Label(mode_frame, text="盖章模式").pack(side=tk.LEFT, padx=(0, 2))
        self.cross_fold_mode_var = tk.StringVar(value="普通盖章")
        self.cross_fold_mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.cross_fold_mode_var,
            values=("普通盖章", "加盖骑缝章"),
            state="readonly", width=10)
        self.cross_fold_mode_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.cross_fold_mode_combo.bind("<<ComboboxSelected>>", self.on_cross_fold_mode_change)

        ttk.Label(mode_frame, text="印章模式").pack(side=tk.LEFT, padx=(0, 2))
        self.seal_mode_var = tk.StringVar(value=self.seal_mode)
        self.seal_mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.seal_mode_var,
            values=("不加印章", "首页加印章", "尾页加印章", "全部页面加印章"),
            state="readonly", width=12)
        self.seal_mode_combo.pack(side=tk.LEFT)
        self.seal_mode_combo.bind("<<ComboboxSelected>>", self.on_seal_mode_change)

        ttk.Button(toolbar, text="打开PDF", command=self.open_pdf).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="加载公章", command=self.load_stamp).pack(side=tk.LEFT, padx=2)
        self.stamp_export_btn = ttk.Button(
            toolbar, text="盖章并导出PDF", command=self.export_pdf)
        self.stamp_export_btn.pack(side=tk.LEFT, padx=2)
        self.cancel_export_btn = ttk.Button(
            toolbar, text="取消导出", command=self.cancel_export, state=tk.DISABLED)
        self.cancel_export_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="保存配置", command=self.save_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="加载配置", command=self.load_config).pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)

        self.undo_btn = ttk.Button(toolbar, text="撤销", command=self.undo, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=2)
        self.redo_btn = ttk.Button(toolbar, text="重做", command=self.redo, state=tk.DISABLED)
        self.redo_btn.pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)

        ttk.Label(toolbar, text="大小:").pack(side=tk.LEFT, padx=2)
        self.scale_var = tk.DoubleVar(value=1.0)
        self.scale_slider = ttk.Scale(toolbar, from_=0.1, to=3.0, variable=self.scale_var,
                                      command=self.on_scale_change, length=100)
        self.scale_slider.pack(side=tk.LEFT, padx=2)
        self.scale_label = ttk.Label(toolbar, text="100%")
        self.scale_label.pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="透明度:").pack(side=tk.LEFT, padx=2)
        self.opacity_var = tk.DoubleVar(value=1.0)
        self.opacity_slider = ttk.Scale(toolbar, from_=0.0, to=1.0, variable=self.opacity_var,
                                        command=self.on_opacity_change, length=100)
        self.opacity_slider.pack(side=tk.LEFT, padx=2)
        self.opacity_label = ttk.Label(toolbar, text="100%")
        self.opacity_label.pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="旋转:").pack(side=tk.LEFT, padx=2)
        self.rotation_var = tk.IntVar(value=0)
        self.rotation_slider = ttk.Scale(toolbar, from_=0, to=359, variable=self.rotation_var,
                                         command=self.on_rotation_change, length=100)
        self.rotation_slider.pack(side=tk.LEFT, padx=2)
        for slider in (self.scale_slider, self.opacity_slider, self.rotation_slider):
            slider.bind("<ButtonPress-1>", self.on_parameter_press)
            slider.bind("<ButtonRelease-1>", self.on_parameter_release)
        self.rotation_label = ttk.Label(toolbar, text="0°")
        self.rotation_label.pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="预览缩放:").pack(side=tk.LEFT, padx=(8, 2))
        self.view_zoom_var = tk.DoubleVar(value=self.view_zoom)
        self.view_zoom_slider = ttk.Scale(
            toolbar, from_=0.5, to=2.5, variable=self.view_zoom_var,
            command=self.on_view_zoom_change, length=100)
        self.view_zoom_slider.pack(side=tk.LEFT, padx=2)
        self.view_zoom_label = ttk.Label(toolbar, text="100%")
        self.view_zoom_label.pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)

        self.cross_fold_btn = ttk.Button(toolbar, text="骑缝章: 关", command=self.toggle_cross_fold)
        self.cross_fold_btn.pack(side=tk.LEFT, padx=2)
        self.copy_cross_fold_btn = ttk.Button(
            toolbar, text="复制为骑缝章", command=self.add_cross_fold_copy)
        self.copy_cross_fold_btn.pack(side=tk.LEFT, padx=2)
        self.confirm_cross_fold_btn = ttk.Button(
            toolbar, text="确认骑缝位置", command=self.confirm_cross_fold)
        self.confirm_cross_fold_btn.pack(side=tk.LEFT, padx=2)

        self.layer_btn = ttk.Button(toolbar, text="公章: 底层", command=self.toggle_layer)
        self.layer_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="删除选中", command=self.delete_selected_stamp).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="重置所有", command=self.reset_all).pack(side=tk.LEFT, padx=2)

    def create_side_panel(self):
        side_panel = ttk.Frame(self.root, width=250)
        side_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)

        ttk.Label(side_panel, text="公章列表", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(0, 5))

        self.stamp_listbox = tk.Listbox(side_panel, height=8, selectmode=tk.EXTENDED)
        self.stamp_listbox.pack(fill=tk.X, pady=(0, 5))
        self.stamp_listbox.bind('<<ListboxSelect>>', self.on_stamp_select)

        ttk.Button(side_panel, text="添加公章", command=self.load_stamp).pack(fill=tk.X, pady=(0, 5))
        ttk.Button(side_panel, text="删除公章", command=self.delete_stamp).pack(fill=tk.X, pady=(0, 10))

        scope_frame = ttk.Frame(side_panel)
        scope_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(scope_frame, text="盖章页:").pack(side=tk.LEFT)
        self.page_scope_var = tk.StringVar(value="全部页面")
        self.page_scope_combo = ttk.Combobox(
            scope_frame, textvariable=self.page_scope_var,
            values=("全部页面", "首页", "尾页", "不加印章"), state="readonly", width=10)
        self.page_scope_combo.pack(side=tk.LEFT, padx=5)
        self.page_scope_combo.bind("<<ComboboxSelected>>", self.on_page_scope_change)

        ttk.Label(side_panel, text="页面导航", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(0, 5))

        nav_frame = ttk.Frame(side_panel)
        nav_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(nav_frame, text="◀", command=self.prev_page, width=5).pack(side=tk.LEFT, padx=2)
        self.page_label = ttk.Label(nav_frame, text="1 / 1", width=10, anchor=tk.CENTER)
        self.page_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(nav_frame, text="▶", command=self.next_page, width=5).pack(side=tk.LEFT, padx=2)

        goto_frame = ttk.Frame(side_panel)
        goto_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(goto_frame, text="跳转到:").pack(side=tk.LEFT)
        self.goto_entry = ttk.Entry(goto_frame, width=8)
        self.goto_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(goto_frame, text="Go", command=self.goto_page, width=5).pack(side=tk.LEFT)

        ttk.Label(side_panel, text="骑缝章配置", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(10, 5))

        cross_frame = ttk.Frame(side_panel)
        cross_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(cross_frame, text="偏移:").pack(side=tk.LEFT)
        self.offset_var = tk.DoubleVar(value=self.cross_fold_offset)
        self.offset_slider = ttk.Scale(cross_frame, from_=0.0, to=1.0, variable=self.offset_var,
                                       command=self.on_offset_change, length=120)
        self.offset_slider.pack(side=tk.LEFT, padx=5)
        self.offset_slider.bind("<ButtonPress-1>", self.on_offset_press)
        self.offset_slider.bind("<ButtonRelease-1>", self.on_offset_release)
        self.offset_label = ttk.Label(cross_frame,
                          text="%d%%" % int(self.cross_fold_offset * 100))
        self.offset_label.pack(side=tk.LEFT)

        ttk.Label(side_panel, text="操作提示", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(10, 5))

        tips_text = """• 鼠标拖拽：移动公章
• 滚轮/←→：翻页（输入框内不拦截）
• Ctrl+Z：撤销
• Ctrl+Y：重做
• Delete：删除选中公章
• Ctrl+A：全选公章"""

        tips_label = ttk.Label(side_panel, text=tips_text, justify=tk.LEFT, foreground='gray')
        tips_label.pack(anchor=tk.W, pady=(0, 10))

    def create_canvas(self):
        self.canvas_frame = ttk.Frame(self.root)
        self.canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.canvas = tk.Canvas(self.canvas_frame, bg='#1e1e1e', cursor="hand2")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        h_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        v_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        self.canvas.bind("<Button-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel)

    def create_status_bar(self):
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            self.root, variable=self.progress_var, maximum=1.0,
            length=180, mode="determinate")
        self.progress_bar.pack(side=tk.RIGHT, padx=5, pady=2)
        self.status_bar = ttk.Label(self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def set_status(self, text):
        try:
            self.status_bar.config(text=text)
        except Exception:
            pass

    def bind_shortcuts(self):
        """快捷键先判断焦点控件类型，输入框内不劫持（修复 D）。"""
        self.root.bind('<Control-z>', self.on_undo_shortcut)
        self.root.bind('<Control-y>', self.on_redo_shortcut)
        self.root.bind('<Delete>', self.on_delete_shortcut)
        self.root.bind('<BackSpace>', self.on_delete_shortcut)
        self.root.bind('<Left>', self.on_prev_shortcut)
        self.root.bind('<Right>', self.on_next_shortcut)
        self.root.bind('<Control-a>', self.on_select_all_shortcut)

    def focus_blocks_shortcut(self):
        return shortcut_blocked(self.root)

    def on_undo_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.undo()

    def on_redo_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.redo()

    def on_delete_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.delete_selected_stamp()

    def on_prev_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.prev_page()

    def on_next_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.next_page()

    def on_select_all_shortcut(self, event=None):
        if self.focus_blocks_shortcut():
            return
        self.select_all_stamps()

    # ==================== PDF操作 ====================

    def open_pdf(self, filepath=None):
        """
        打开新 PDF：先关旧句柄并清空状态（修复 C）；加密 PDF 弹密码框，取消则放弃。
        传入 filepath 时不弹文件选择框（便于脚本调用/测试）。
        """
        if not filepath:
            if self.file_mode == "批量文件模式":
                paths = filedialog.askopenfilenames(
                    title="选择要批量盖章的PDF文件",
                    filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
                )
                if not paths:
                    return OPEN_CANCELLED
                self.batch_paths = list(paths)
                filepath = self.batch_paths[0]
            else:
                filepath = filedialog.askopenfilename(
                    title="选择PDF文件",
                    filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
                )
        if not filepath:
            return OPEN_CANCELLED
        status, message = self.session.load_document(filepath, password=None)
        if status == OPEN_NEEDS_PASSWORD:
            password = self.ask_password(filepath)
            if password is None:
                self.core.invalidate()
                self.set_status(describe_open_status(OPEN_CANCELLED, os.path.basename(filepath)))
                return OPEN_CANCELLED
            status, message = self.session.load_document(filepath, password=password)
            if status == OPEN_BAD_PASSWORD:
                messagebox.showerror("密码错误", "密码不正确，已放弃打开：\n%s" % filepath)
                self.set_status(message)
                return status
        if status != OPEN_OK:
            if status != OPEN_CANCELLED:
                messagebox.showerror("错误", "打开PDF失败: %s" % message)
            self.set_status(message)
            return status
        self.core.invalidate()
        self._tk_img_cache.clear()
        self.sync_mode_controls()
        self.update_stamp_list()
        self.render_page()
        self.set_status(message)
        return status

    def ask_password(self, filepath):
        """密码输入框；用户取消返回 None。"""
        return ask_password_string(
            "PDF 密码", "该 PDF 已加密，请输入密码：\n%s" % os.path.basename(filepath),
            parent=self.root, show="*")

    def close_pdf(self):
        self.session.reset_for_new_document()
        self.core.invalidate()
        self._tk_img_cache.clear()
        self.update_stamp_list()
        self.render_page()

    # ==================== 公章操作 ====================

    def load_stamp(self, filepath=None):
        if not filepath:
            filepath = filedialog.askopenfilename(
                title="选择公章图片",
                filetypes=list(STAMP_FILE_DIALOG_TYPES))
        if not filepath:
            return None
        try:
            img = Image.open(filepath).convert("RGBA")
        except Exception as e:
            messagebox.showerror("错误", f"加载失败: {str(e)}")
            return None
        stamp = self.add_stamp_image(img, os.path.basename(filepath))
        stamp.source_path = os.path.abspath(filepath)
        return stamp

    def add_stamp_image(self, img, name="公章"):
        stamp = self.session.add_stamp(img, name)
        self.sync_sliders_to_active()
        self.update_stamp_list()
        self.render_page()
        self.save_history()
        self.set_status("已加载: %s" % stamp.name)
        return stamp

    def add_cross_fold_copy(self):
        """从当前公章创建独立骑缝章副本，不改变原章的位置或参数。"""
        source = self.active_stamp()
        if source is None:
            messagebox.showwarning("提示", "请先选择要复制的公章")
            return None
        copy = self.session.add_stamp(source.img.copy(), "%s（骑缝章）" % source.name)
        copy.scale = source.scale
        copy.opacity = source.opacity
        copy.rotation = source.rotation
        copy.page_scope = "all"
        copy.source_path = source.source_path
        copy.is_cross_fold = True
        copy.cross_fold_offset = 0.5
        self.cross_fold_mode = True
        self.cross_fold_stamp_index = self.active_stamp_idx
        self.session.cross_fold_mode = True
        self.session.cross_fold_stamp_index = self.active_stamp_idx
        self.sync_sliders_to_active()
        self.sync_cross_fold_controls()
        self.update_stamp_list()
        self.render_page()
        self.save_history()
        self.set_status("已创建独立骑缝章，可单独调整偏移后盖章")
        return copy

    def delete_stamp(self):
        selection = self.stamp_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要删除的公章")
            return False
        indices = [idx for idx in selection if idx < len(self.stamps)]
        names = "、".join(self.stamps[idx].name for idx in indices)
        prompt = "确定删除选中的 %d 个公章？\n%s" % (len(indices), names)
        if not messagebox.askyesno("确认", prompt):
            return False
        for idx in reversed(indices):
            self.session.remove_stamp_by_index(idx)
        self.update_stamp_list()
        self.render_page()
        self.save_history()
        self.set_status("已删除 %d 个公章" % len(indices))
        return True

    def delete_stamp_at(self, idx):
        removed = self.session.remove_stamp_by_index(idx)
        if removed is None:
            return False
        self.update_stamp_list()
        self.render_page()
        self.save_history()
        self.set_status("已删除公章: %s" % removed.name)
        return True

    def delete_selected_stamp(self):
        """删除当前页选中的公章（图片引用仍留在 session.image_pool 里，undo 能找回）。"""
        if self.selected_stamp is None:
            return False
        return self.delete_stamp_at(self.selected_stamp)

    def on_stamp_select(self, event):
        selection = self.stamp_listbox.curselection()
        if selection:
            self.active_stamp_idx = selection[0]
            stamp = self.get_stamp(self.active_stamp_idx)
            if stamp is not None:
                stamp.page_index = self.current_page
                self.sync_sliders_to_active()
                self.sync_cross_fold_controls()
            self.render_page()

    def get_stamp(self, idx):
        return self.session.get_stamp(idx)

    def sync_sliders_to_active(self):
        stamp = self.get_stamp(self.active_stamp_idx)
        if stamp is None:
            return
        self.scale_var.set(stamp.scale)
        self.scale_label.config(text="%d%%" % int(stamp.scale * 100))
        self.opacity_var.set(stamp.opacity)
        self.opacity_label.config(text="%d%%" % int(stamp.opacity * 100))
        self.rotation_var.set(stamp.rotation)
        self.rotation_label.config(text="%d°" % stamp.rotation)
        scope_labels = {"all": "全部页面", "first": "首页", "last": "尾页",
                "none": "不加印章"}
        self.page_scope_var.set(scope_labels.get(stamp.page_scope, "全部页面"))

    def sync_cross_fold_controls(self):
        stamp = self.active_stamp()
        enabled = bool(stamp is not None and getattr(stamp, "is_cross_fold", False))
        self.cross_fold_mode = enabled
        self.cross_fold_stamp_index = self.active_stamp_idx if enabled else None
        self.session.cross_fold_mode = enabled
        self.session.cross_fold_stamp_index = self.cross_fold_stamp_index
        self.cross_fold_mode_var.set("加盖骑缝章" if enabled else "普通盖章")
        self.cross_fold_btn.config(text="骑缝章: 开" if enabled else "骑缝章: 关")
        self.confirm_cross_fold_btn.config(
            state=tk.NORMAL if stamp is not None and enabled else tk.DISABLED)
        if stamp is not None:
            self.cross_fold_offset = stamp.cross_fold_offset
            self.offset_var.set(stamp.cross_fold_offset)
            self.offset_label.config(text="%d%%" % int(stamp.cross_fold_offset * 100))
            scope_modes = {"all": "全部页面加印章", "first": "首页加印章",
                           "last": "尾页加印章", "none": "不加印章"}
            self.seal_mode = scope_modes.get(stamp.page_scope, "全部页面加印章")
            self.seal_mode_var.set(self.seal_mode)
        self.stamp_mode_enabled = True
        self.session.stamp_mode_enabled = True

    def on_page_scope_change(self, event=None):
        stamp = self.active_stamp()
        if stamp is None:
            return
        scope_values = {"全部页面": "all", "首页": "first", "尾页": "last",
                "不加印章": "none"}
        stamp.page_scope = scope_values.get(self.page_scope_var.get(), "all")
        self.sync_cross_fold_controls()
        self.render_page()
        self.save_history()

    def update_stamp_list(self):
        self.stamp_listbox.delete(0, tk.END)
        for i, stamp in enumerate(self.stamps):
            marker = "► " if i == self.active_stamp_idx else "  "
            kind = "骑缝章" if stamp.is_cross_fold else "公章"
            self.stamp_listbox.insert(tk.END, "%s[%s] %s" % (marker, kind, stamp.name))
        if self.stamps and 0 <= self.active_stamp_idx < len(self.stamps):
            self.stamp_listbox.selection_set(self.active_stamp_idx)

    # ==================== 渲染 ====================

    def export_options(self):
        """当前预览/导出共用的一组选项（预览与导出必然同源，避免两处参数漂移）。"""
        return ExportOptions(
            cross_fold_mode=self.cross_fold_mode,
            cross_fold_stamp_index=self.cross_fold_stamp_index,
            cross_fold_offset=self.cross_fold_offset,
            underlay=self.stamp_underlay)

    def render_page(self):
        """
        重绘当前页。页面位图与公章位图都有缓存，因此参数不变时只做 Tk 图层操作（修复 E）。
        拖拽过程中不调用本方法（见 on_mouse_drag / on_mouse_up）。
        """
        if not self.pdf_doc:
            if hasattr(self, "canvas"):
                self.canvas.delete("all")
            return None
        payload = self.core.build_payload(
            self.pdf_doc, self.current_page,
            self.stamps,
            options=self.export_options())

        zoom = self.view_zoom
        bitmap = payload["page_bitmap"]
        display_bitmap = (self.core.build_underlay_bitmap(self.pdf_doc, payload)
                          if self.stamp_underlay else bitmap)
        display_bitmap = self._display_image(display_bitmap, zoom)
        self.page_img = self._tk_photo(
            "page", (id(bitmap), self.stamp_underlay,
                     zoom, tuple(id(i["image"]) for i in payload["stamp_items"])), display_bitmap)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.page_img, tags="page")

        for item in payload["stamp_items"]:
            image = item["image"]
            if self.stamp_underlay:
                image = Image.new("RGBA", item["size"], (0, 0, 0, 0))
            # 一步缩放到显示尺寸（payload 里的图已含 stamp.scale；这里只叠画布缩放）
            image = display_scale_cache(image, zoom)
            photo = self._tk_photo("stamp", id(image), image)
            tags = [item["tag"], "stamp_item", item["kind"]]
            if item["index"] == self.active_stamp_idx:
                tags.append("active")
            if item["index"] == self.selected_stamp:
                tags.append("selected")
            self.canvas.create_image(item["x"] * zoom, item["y"] * zoom, anchor=tk.NW,
                                     image=photo, tags=tags)
        bounds = self.canvas.bbox("all")
        self.canvas.configure(scrollregion=bounds or (0, 0, 0, 0))
        self.page_label.config(text="%d / %d" % (self.current_page + 1, self.total_pages))
        self.update_undo_redo_buttons()
        return payload

    @staticmethod
    def _display_image(image, zoom):
        """按预览倍率调整图像，模型尺寸仍保持渲染基准像素。"""
        if abs(float(zoom) - 1.0) < 1e-9:
            return image
        width = max(1, int(round(image.width * zoom)))
        height = max(1, int(round(image.height * zoom)))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        return image.resize((width, height), resampling)

    def _tk_photo(self, kind, sub_key, image):
        """PhotoImage 缓存：同一处理后位图对象不重复转换（修复 E）。"""
        key = (kind, sub_key)
        hit = self._tk_img_cache.get(key)
        if hit is not None and hit[0] is image:
            return hit[1]
        photo = ImageTk.PhotoImage(image)
        if len(self._tk_img_cache) > 64:
            self._tk_img_cache.clear()
        self._tk_img_cache[key] = (image, photo)
        return photo

    # ==================== 鼠标交互 ====================

    def on_mouse_down(self, event):
        if not self.stamps:
            return
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(canvas_x, canvas_y, canvas_x + 1, canvas_y + 1)
        clicked_tags = ()
        for item in reversed(items or ()):
            tags = self.canvas.gettags(item)
            if "stamp_item" in tags:
                clicked_tags = tags
                break
        stamp_idx = stamp_index_from_tags(clicked_tags, len(self.stamps))
        if stamp_idx is None:
            # 点空白：只清除选择标记（selected 标签无独立视觉元素，不需要整页重绘）
            self.selected_stamp = None
            return
        stamp = self.get_stamp(stamp_idx)
        if stamp is None:
            return
        self.selected_stamp = stamp_idx
        self.active_stamp_idx = stamp_idx
        self.update_stamp_list()
        self.sync_sliders_to_active()
        if not drag_allowed_for_tags(clicked_tags):
            self.is_dragging = False
            self.drag_item_tag = None
            self.set_status("该印章当前不可拖动")
            return
        self.is_dragging = True
        self.drag_item_tag = "stamp_%d" % stamp_idx
        # 骑缝章的水平位置由「页序 + 偏移」算出，拖动只改垂直位置
        self.drag_vertical_only = "cross_fold" in clicked_tags
        stamp_x, stamp_y = stamp.position_for_page(self.current_page)
        self.drag_start_x = canvas_x - stamp_x * self.view_zoom
        self.drag_start_y = canvas_y - stamp_y * self.view_zoom
        self.drag_last_x, self.drag_last_y = canvas_x, canvas_y
        self.drag_moved = False
        if self.drag_vertical_only:
            self.set_status("骑缝章：拖动调整上下位置，左右位置请用「骑缝章配置 - 偏移」")
        if self.stamp_underlay:
            item = self.canvas.find_withtag(self.drag_item_tag)
            if item:
                processed = stamp.get_processed_img()
                processed = self._display_image(processed, self.view_zoom)
                photo = self._tk_photo("stamp-drag", id(processed), processed)
                self.canvas.itemconfigure(item[0], image=photo)
        # 不重新渲染整页：只把该图元提到最上层
        try:
            self.canvas.tag_raise(self.drag_item_tag)
        except Exception:
            pass

    def on_mouse_drag(self, event):
        """拖拽期间只移动图元（修复 E 卡顿），不 render_page。"""
        if not (self.is_dragging and self.drag_item_tag):
            return
        stamp = self.get_stamp(self.active_stamp_idx)
        if stamp is None:
            return
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        stamp_x = (canvas_x - self.drag_start_x) / self.view_zoom
        stamp_y = (canvas_y - self.drag_start_y) / self.view_zoom
        if self.drag_vertical_only:
            # 骑缝章：只应用垂直分量，水平位置仍由「页序 + 偏移」决定
            stamp_x = stamp.position_for_page(self.current_page)[0]
        stamp.set_position_for_page(self.current_page, stamp_x, stamp_y)
        dx = canvas_x - self.drag_last_x
        dy = canvas_y - self.drag_last_y
        if dx or dy:
            self.canvas.move(self.drag_item_tag, dx, dy)
            self.drag_last_x, self.drag_last_y = canvas_x, canvas_y
            self.drag_moved = True

    def on_mouse_up(self, event):
        """松手才重绘 + 存历史（骑缝章的水平位置由几何算出，需要重绘复位）。"""
        if not self.is_dragging:
            return
        self.is_dragging = False
        moved = self.drag_moved
        self.drag_item_tag = None
        self.drag_vertical_only = False
        self.render_page()
        if moved:
            self.save_history()

    def on_mouse_wheel(self, event):
        if not self.pdf_doc:
            return
        delta = getattr(event, "delta", 0)
        if delta:
            if delta > 0:
                self.prev_page()
            elif delta < 0:
                self.next_page()
            return
        num = getattr(event, "num", None)
        if num == 4:
            self.prev_page()
        elif num == 5:
            self.next_page()

    # ==================== 页面导航 ====================

    def prev_page(self):
        if self.session.goto_page_index(self.current_page - 1):
            self.render_page()

    def next_page(self):
        if self.session.goto_page_index(self.current_page + 1):
            self.render_page()

    def goto_page(self):
        if not self.pdf_doc:
            return
        raw = self.goto_entry.get()
        try:
            page_num = int(str(raw).strip())
        except ValueError:
            messagebox.showwarning("提示", "请输入有效的页码")
            return
        if 1 <= page_num <= self.total_pages:
            self.current_page = page_num - 1
            self.render_page()
        else:
            messagebox.showwarning("提示", f"页码应在 1-{self.total_pages} 之间")

    # ==================== 历史记录 ====================

    def save_history(self):
        self.session.save_history()
        self.update_undo_redo_buttons()

    def undo(self):
        result = self.session.undo()
        if result is None:
            return False
        self.after_restore()
        return True

    def redo(self):
        result = self.session.redo()
        if result is None:
            return False
        self.after_restore()
        return True

    def after_restore(self):
        missing = self.session.last_restore_missing
        self.cross_fold_mode = self.session.cross_fold_mode
        self.cross_fold_stamp_index = self.session.cross_fold_stamp_index
        self.cross_fold_offset = self.session.cross_fold_offset
        self.stamp_underlay = self.session.stamp_underlay
        self.seal_mode = self.session.seal_mode
        self.stamp_mode_enabled = self.session.stamp_mode_enabled
        self.update_stamp_list()
        self.sync_sliders_to_active()
        self.offset_var.set(self.cross_fold_offset)
        self.offset_label.config(text="%d%%" % int(self.cross_fold_offset * 100))
        self.sync_mode_controls()
        self.layer_btn.config(text="公章: 底层" if self.stamp_underlay else "公章: 上层")
        self.render_page()
        self.update_undo_redo_buttons()
        if missing:
            names = "、".join(str(m.get("name") or m.get("id")) for m in missing)
            msg = "撤销/重做时有公章图片已丢失，未能恢复: %s" % names
            self.set_status(msg)
            messagebox.showwarning("历史恢复不完整", msg)

    def restore_state(self, state):
        """兼容旧接口：委托给 session.restore（按 id 找回，失败会提示）。"""
        restored, missing = self.session.restore(state)
        self.update_stamp_list()
        self.render_page()
        self.update_undo_redo_buttons()
        return restored, missing

    def update_undo_redo_buttons(self):
        self.undo_btn.config(state=tk.NORMAL if self.history.can_undo() else tk.DISABLED)
        self.redo_btn.config(state=tk.NORMAL if self.history.can_redo() else tk.DISABLED)

    # ==================== 参数调整 ====================

    def active_stamp(self):
        return self.get_stamp(self.active_stamp_idx)

    def on_parameter_press(self, event=None):
        stamp = self.active_stamp()
        self._parameter_drag_state = (
            stamp.stamp_id,
            stamp.scale,
            stamp.opacity,
            stamp.rotation,
        ) if stamp is not None else None

    def on_parameter_release(self, event=None):
        stamp = self.active_stamp()
        before = self._parameter_drag_state
        self._parameter_drag_state = None
        if stamp is None or before is None or before[0] != stamp.stamp_id:
            return
        after = (stamp.stamp_id, stamp.scale, stamp.opacity, stamp.rotation)
        if after != before:
            self.save_history()

    def on_scale_change(self, val):
        stamp = self.active_stamp()
        if stamp is not None:
            stamp.scale = float(val)
            self.scale_label.config(text="%d%%" % int(stamp.scale * 100))
            self.render_page()

    def on_opacity_change(self, val):
        stamp = self.active_stamp()
        if stamp is not None:
            stamp.opacity = float(val)
            self.opacity_label.config(text="%d%%" % int(stamp.opacity * 100))
            self.render_page()

    def on_rotation_change(self, val):
        stamp = self.active_stamp()
        if stamp is not None:
            stamp.rotation = int(float(val))
            self.rotation_label.config(text="%d°" % stamp.rotation)
            self.render_page()

    def on_view_zoom_change(self, val):
        self.view_zoom = max(0.5, min(2.5, float(val)))
        self.view_zoom_label.config(text="%d%%" % int(round(self.view_zoom * 100)))
        self.render_page()

    def on_offset_change(self, val):
        self.cross_fold_offset = float(val)
        stamp = self.active_stamp()
        if stamp is not None:
            stamp.cross_fold_offset = self.cross_fold_offset
        self.offset_label.config(text="%d%%" % int(self.cross_fold_offset * 100))
        self.render_page()

    def on_offset_press(self, event=None):
        self._offset_drag_state = self.cross_fold_offset

    def on_offset_release(self, event=None):
        before = self._offset_drag_state
        self._offset_drag_state = None
        if before is not None and abs(self.cross_fold_offset - before) > 1e-9:
            self.save_history()

    # ==================== 模式切换 ====================

    def on_file_mode_change(self, event=None):
        self.file_mode = self.file_mode_var.get()
        if self.file_mode == "批量文件模式":
            paths = filedialog.askopenfilenames(
                title="选择要批量盖章的PDF文件",
                filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
            )
            if not paths:
                self.file_mode = "单文件模式"
                self.file_mode_var.set(self.file_mode)
                return
            self.batch_paths = list(paths)
            self.open_pdf(self.batch_paths[0])
            self.set_status("已选择 %d 个PDF，可预览第一个文件" % len(self.batch_paths))
        else:
            self.batch_paths = []
            self.set_status("已选择%s" % self.file_mode)

    def sync_mode_controls(self):
        """同步模式下拉框与会话状态，避免切换文档后显示旧状态。"""
        self.sync_cross_fold_controls()
        self.seal_mode_var.set(self.seal_mode)

    def on_cross_fold_mode_change(self, event=None):
        stamp = self.active_stamp()
        if stamp is None:
            return
        stamp.is_cross_fold = self.cross_fold_mode_var.get() == "加盖骑缝章"
        self.cross_fold_mode = stamp.is_cross_fold
        self.session.cross_fold_mode = self.cross_fold_mode
        self.cross_fold_stamp_index = self.active_stamp_idx if self.cross_fold_mode else None
        self.cross_fold_btn.config(text="骑缝章: 开" if self.cross_fold_mode else "骑缝章: 关")
        self.render_page()
        self.save_history()
        self.set_status("骑缝章模式: %s" % ("开启" if self.cross_fold_mode else "关闭"))

    def on_seal_mode_change(self, event=None):
        mode = self.seal_mode_var.get()
        stamp = self.active_stamp()
        if stamp is None:
            self.seal_mode_var.set(self.seal_mode)
            return
        self.seal_mode = mode
        self.stamp_mode_enabled = True
        self.session.seal_mode = self.seal_mode
        self.session.stamp_mode_enabled = self.stamp_mode_enabled
        scope = {
            "首页加印章": "first",
            "尾页加印章": "last",
            "加盖印章": "all",  # 兼容旧配置
            "全部页面加印章": "all",
            "不加印章": "none",
        }.get(mode)
        if scope is not None:
            stamp.page_scope = scope
            self.page_scope_var.set({"all": "全部页面", "first": "首页",
                                     "last": "尾页", "none": "不加印章"}[scope])
        self.render_page()
        self.save_history()
        self.set_status("印章模式: %s" % mode)

    def toggle_cross_fold(self):
        stamp = self.active_stamp()
        if stamp is None:
            return
        stamp.is_cross_fold = not getattr(stamp, "is_cross_fold", False)
        self.cross_fold_mode = stamp.is_cross_fold
        self.cross_fold_stamp_index = self.active_stamp_idx if self.cross_fold_mode else None
        self.session.cross_fold_mode = self.cross_fold_mode
        self.session.cross_fold_stamp_index = self.cross_fold_stamp_index
        self.cross_fold_mode_var.set("加盖骑缝章" if self.cross_fold_mode else "普通盖章")
        self.cross_fold_btn.config(text="骑缝章: 开" if self.cross_fold_mode else "骑缝章: 关")
        self.render_page()
        self.save_history()
        self.set_status("骑缝章模式: %s" % ("开启" if self.cross_fold_mode else "关闭"))

    def confirm_cross_fold(self):
        """确认当前公章的骑缝位置，并将状态写入历史。"""
        stamp = self.active_stamp()
        if stamp is None:
            messagebox.showwarning("提示", "请先选择一个公章")
            return False
        if not getattr(stamp, "is_cross_fold", False):
            messagebox.showwarning("提示", "请先开启骑缝章并调整位置")
            return False
        stamp.cross_fold_offset = min(1.0, max(0.0, float(self.offset_var.get())))
        self.cross_fold_offset = stamp.cross_fold_offset
        self.cross_fold_mode = True
        self.cross_fold_stamp_index = self.active_stamp_idx
        self.session.cross_fold_mode = True
        self.session.cross_fold_stamp_index = self.active_stamp_idx
        self.sync_cross_fold_controls()
        self.render_page()
        self.save_history()
        self.set_status("已确认骑缝章位置，可导出PDF")
        return True

    def toggle_layer(self):
        self.stamp_underlay = not self.stamp_underlay
        self.session.stamp_underlay = self.stamp_underlay
        self.layer_btn.config(text="公章: 底层" if self.stamp_underlay else "公章: 上层")
        layer_name = "底层（文字下面）" if self.stamp_underlay else "上层（文字上面）"
        self.render_page()
        self.save_history()
        self.set_status("公章层级：%s" % layer_name)
        messagebox.showinfo("公章层级",
                            f"已切换为：{layer_name}\n\n"
                            "底层：公章在文字下面，适合水印效果\n"
                            "上层：公章在文字上面，适合正式盖章")

    def reset_all(self):
        if not self.stamps:
            return
        if not messagebox.askyesno("确认", "确定要重置所有公章到默认位置吗？"):
            return
        for stamp in self.stamps:
            stamp.x = 100
            stamp.y = 100
            stamp.page_positions = {}
            stamp.scale = 1.0
            stamp.opacity = 1.0
            stamp.rotation = 0
        self.scale_var.set(1.0)
        self.opacity_var.set(1.0)
        self.rotation_var.set(0)
        self.render_page()
        self.save_history()

    def select_all_stamps(self):
        """全选公章（用于批量操作）。"""
        self.selected_stamp = None
        self.stamp_listbox.selection_set(0, tk.END)
        self.render_page()
        self.set_status("已全选 %d 个公章，可进行批量删除" % len(self.stamps))

    # ==================== 导出 ====================

    def save_config(self):
        if not self.stamps:
            messagebox.showwarning("提示", "请先加载至少一个公章")
            return None
        path = filedialog.asksaveasfilename(
            title="保存盖章配置", defaultextension=".json",
            filetypes=[("JSON配置", "*.json"), ("所有文件", "*.*")])
        if not path:
            return None
        data = {
            "version": 1,
            "stamps": [stamp.to_dict() for stamp in self.stamps],
            "cross_fold_mode": self.cross_fold_mode,
            "cross_fold_stamp_index": self.cross_fold_stamp_index,
            "cross_fold_offset": self.cross_fold_offset,
            "stamp_underlay": self.stamp_underlay,
            "seal_mode": self.seal_mode,
            "stamp_mode_enabled": self.stamp_mode_enabled,
        }
        try:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("错误", "配置保存失败: %s" % exc)
            return None
        self.set_status("配置已保存: %s" % os.path.basename(path))
        return path

    def load_config(self):
        path = filedialog.askopenfilename(
            title="加载盖章配置",
            filetypes=[("JSON配置", "*.json"), ("所有文件", "*.*")])
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            stamp_data = data.get("stamps", [])
            if not isinstance(stamp_data, list) or not stamp_data:
                raise ValueError("配置中没有公章")
            image_pool = {stamp.stamp_id: stamp.img for stamp in self.stamps}
            config_dir = os.path.dirname(os.path.abspath(path))
            missing_paths = []
            for item in stamp_data:
                source_path = item.get("source_path")
                if source_path and not os.path.isabs(source_path):
                    source_path = os.path.join(config_dir, source_path)
                if source_path and os.path.exists(source_path):
                    try:
                        image_pool[item.get("id")] = remove_white_background(
                            Image.open(source_path).convert("RGBA"))
                        item["source_path"] = source_path
                    except (OSError, ValueError):
                        missing_paths.append(source_path)
                elif item.get("id") not in image_pool:
                    missing_paths.append(source_path or item.get("name", "(未命名)"))
            if missing_paths:
                raise ValueError("找不到公章图片: %s" % "、".join(map(str, missing_paths)))
            restored, missing = restore_stamps(stamp_data, image_pool)
            if missing:
                raise ValueError("配置引用的公章图片未加载，请先加载对应图片")
            self.session.stamps = restored
            self.session.image_pool = build_image_pool(restored, image_pool)
            self.cross_fold_mode = bool(data.get("cross_fold_mode", False))
            self.cross_fold_stamp_index = data.get("cross_fold_stamp_index")
            self.cross_fold_offset = float(data.get("cross_fold_offset", 0.5))
            # 兼容旧配置：旧版只保存全局骑缝索引和偏移，没有逐章字段。
            legacy_idx = self.cross_fold_stamp_index
            if self.cross_fold_mode and isinstance(legacy_idx, int):
                legacy_stamp = self.get_stamp(legacy_idx)
                if legacy_stamp is not None and not legacy_stamp.is_cross_fold:
                    legacy_stamp.is_cross_fold = True
                    legacy_stamp.cross_fold_offset = self.cross_fold_offset
            self.stamp_underlay = bool(data.get("stamp_underlay", True))
            self.seal_mode = str(data.get("seal_mode", "全部页面加印章"))
            legacy_stamps_disabled = not bool(data.get("stamp_mode_enabled", True))
            if legacy_stamps_disabled:
                for stamp in restored:
                    stamp.page_scope = "none"
            self.stamp_mode_enabled = True
            self.session.cross_fold_mode = self.cross_fold_mode
            self.session.cross_fold_stamp_index = self.cross_fold_stamp_index
            self.session.cross_fold_offset = self.cross_fold_offset
            self.session.stamp_underlay = self.stamp_underlay
            self.session.seal_mode = self.seal_mode
            self.session.stamp_mode_enabled = True
            self.update_stamp_list()
            self.sync_mode_controls()
            self.render_page()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("错误", "配置加载失败: %s" % exc)
            return None
        self.set_status("配置已加载: %s" % os.path.basename(path))
        return path

    # ==================== 导出（后台线程 + 可取消） ====================

    def _busy_exporting(self):
        return self._export_job is not None

    def export_pdf(self, save_path=None):
        """
        启动导出。默认**不阻塞界面**：导出在后台线程跑，主线程用 `root.after()` 收进度。

        返回 ExportJob（可用 `wait_for_export()` 等它结束）；参数校验失败返回 None。
        """
        if self._busy_exporting():
            messagebox.showinfo("提示", "已有导出任务在进行中，请等它结束或点「取消导出」")
            return None
        if not self.pdf_doc:
            messagebox.showwarning("提示", "请先打开PDF")
            return None
        if self.stamp_mode_enabled and not self.stamps:
            messagebox.showwarning("提示", "请先加载至少一个公章")
            return None
        if self.file_mode == "批量文件模式" and self.batch_paths:
            output_dir = save_path or filedialog.askdirectory(title="选择批量导出目录")
            if not output_dir:
                return None
            return self.export_batch_pdf(output_dir)
        if not save_path:
            save_path = filedialog.asksaveasfilename(
                title="保存盖章PDF",
                defaultextension=".pdf",
                filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
            )
        if not save_path:
            return None

        # 快照：点下按钮那一刻的路径 / 密码 / 公章 / 选项。
        # 之后用户再怎么改界面，都不会影响正在跑的这次导出。
        source_path = self.pdf_path
        password = self.session.password
        stamps = snapshot_stamps(self.stamps)
        options = ExportOptions(cross_fold_mode=False, cross_fold_stamp_index=None,
                                underlay=self.stamp_underlay)
        scale_factor = self.scale_factor
        base_name = os.path.basename(save_path)

        def worker(job):
            job.post("status", text="正在导出：%s" % base_name)
            job.post("progress", value=0.0, maximum=1.0)
            # fitz 非线程安全：worker 自己开一份，绝不共享 self.pdf_doc
            doc, status = open_pdf_document(source_path, password=password)
            if status == OPEN_NEEDS_PASSWORD:
                reply = job.ask_password(source_path)
                if reply is None:
                    raise ExportCancelled("未输入密码")
                doc, status = open_pdf_document(source_path, password=reply)
            if status != OPEN_OK:
                raise RuntimeError(describe_open_status(status, base_name))
            try:
                return export_pdf_with_stamps(
                    doc, stamps, save_path, scale_factor=scale_factor,
                    options=options, cancel_check=lambda: job.cancelled)
            finally:
                doc.close()

        return self._begin_export(worker, lambda job: self._report_single_done(job, save_path))

    def _report_single_done(self, job, save_path):
        if job.error is not None:
            self.set_status("导出失败")
            messagebox.showerror("错误", "导出失败: %s" % job.error)
            return
        if job.cancelled_flag:
            return
        self.set_status("已导出: %s" % os.path.basename(save_path))
        messagebox.showinfo("成功", "PDF已保存:\n%s" % save_path)

    def export_batch_pdf(self, output_dir):
        """
        批量导出（后台线程）。返回 ExportJob；确认框被拒绝或校验失败返回 None。
        """
        if self._busy_exporting():
            messagebox.showinfo("提示", "已有导出任务在进行中，请等它结束或点「取消导出」")
            return None
        paths = list(self.batch_paths)
        output_paths = batch_output_paths(paths, output_dir)
        existing_paths = [path for path in output_paths if os.path.exists(path)]
        duplicate_paths = len(output_paths) != len(set(output_paths))
        if existing_paths or duplicate_paths:
            message = "批量导出将覆盖已有文件，是否继续？"
            if duplicate_paths:
                message += "\n\n不同输入文件生成了同名输出文件。"
            if not messagebox.askyesno("确认批量导出", message):
                self.set_status("已取消批量导出")
                return None

        stamps = snapshot_stamps(self.stamps)
        options = ExportOptions(cross_fold_mode=False, cross_fold_stamp_index=None,
                                underlay=self.stamp_underlay)
        scale_factor = self.scale_factor
        total = len(paths)
        seed_password = self._batch_password

        def worker(job):
            reports = []
            failures = []
            last_password = seed_password
            for index, (filepath, output_path) in enumerate(zip(paths, output_paths), 1):
                if job.cancelled:
                    break
                job.post("status", text="正在处理第 %d/%d 个文件：%s" %
                         (index, total, os.path.basename(filepath)))
                job.post("progress", value=index - 1, maximum=max(1, total))
                doc, status = open_pdf_document(filepath)
                if status == OPEN_NEEDS_PASSWORD:
                    if last_password is not None:
                        doc, status = open_pdf_document(filepath, password=last_password)
                    if status != OPEN_OK:
                        # 弹密码框必须回主线程：ask_password 会阻塞等回执
                        last_password = job.ask_password(filepath)
                        if last_password is not None:
                            doc, status = open_pdf_document(filepath, password=last_password)
                    if last_password is None:
                        failures.append((filepath, "未输入密码"))
                        job.post("progress", value=index, maximum=max(1, total))
                        continue
                if status != OPEN_OK:
                    failures.append((filepath, describe_open_status(
                        status, os.path.basename(filepath))))
                    job.post("progress", value=index, maximum=max(1, total))
                    continue
                cancelled_here = False
                try:
                    reports.append(export_pdf_with_stamps(
                        doc, stamps, output_path, scale_factor=scale_factor,
                        options=options, cancel_check=lambda: job.cancelled))
                except ExportCancelled:
                    cancelled_here = True
                except Exception as exc:
                    failures.append((filepath, str(exc)))
                finally:
                    doc.close()
                if cancelled_here:
                    break
                job.post("progress", value=index, maximum=max(1, total))
            return {"reports": reports, "failures": failures, "output_dir": output_dir,
                    "password": last_password, "total": total}

        return self._begin_export(worker, self._report_batch_done)

    def _report_batch_done(self, job):
        if job.error is not None:
            self.set_status("批量导出失败")
            messagebox.showerror("错误", "批量导出失败: %s" % job.error)
            return
        if job.cancelled_flag:
            return
        result = job.result or {}
        reports = result.get("reports", [])
        failures = result.get("failures", [])
        # 把本次用过的有效密码留给下一次（跨文件复用，避免重复弹框）
        if result.get("password") is not None:
            self._batch_password = result["password"]
        if failures:
            self.set_status("批量导出完成：成功 %d 个，失败 %d 个" %
                            (len(reports), len(failures)))
            messagebox.showwarning(
                "批量导出完成",
                "成功 %d 个，失败 %d 个\n\n%s" % (
                    len(reports), len(failures),
                    "\n".join(os.path.basename(path) + "：" + reason
                              for path, reason in failures)))
        else:
            self.set_status("批量导出完成：%d 个文件" % len(reports))
            messagebox.showinfo("成功", "已批量导出 %d 个PDF" % len(reports))

    # ---- 任务调度 ----

    def _begin_export(self, worker, on_done):
        job = ExportJob(worker)
        self._export_job = job
        self._export_on_done = on_done
        self.stamp_export_btn.config(state=tk.DISABLED)
        self.cancel_export_btn.config(state=tk.NORMAL)
        self.progress_var.set(0.0)
        job.start()
        self.root.after(job.poll_ms, self._poll_export)
        return job

    def _poll_export(self):
        """主线程：抽干后台任务的事件并应用到 Tk（唯一允许碰控件的地方）。"""
        job = self._export_job
        if job is None:
            return
        events, finished = job.poll()
        for kind, payload in events:
            if kind == "progress":
                self.progress_bar.configure(maximum=payload["maximum"])
                self.progress_var.set(payload["value"])
            elif kind == "status":
                self.set_status(payload["text"])
            elif kind == "ask_password":
                # 阻塞在 worker 侧等这个回执，所以必须同步处理完再 set
                job.answer_password(self.ask_password(payload["filepath"]))
        if not finished:
            self.root.after(job.poll_ms, self._poll_export)
            return
        # 收尾：先复位控件，再回调（回调里可能弹窗）
        self._export_job = None
        self.stamp_export_btn.config(state=tk.NORMAL)
        self.cancel_export_btn.config(state=tk.DISABLED)
        self.progress_var.set(0.0)
        callback, self._export_on_done = self._export_on_done, None
        if job.cancelled_flag:
            self.set_status("已取消导出（目标文件未被修改）")
        elif callback is not None:
            callback(job)

    def cancel_export(self):
        """请求取消当前导出；worker 在下一个检查点退出，目标文件保持原样。"""
        job = self._export_job
        if job is None:
            return False
        job.cancel()
        self.set_status("正在取消导出…")
        self.cancel_export_btn.config(state=tk.DISABLED)
        return True

    def wait_for_export(self, job=None, timeout=180.0):
        """
        阻塞直到导出结束（供脚本与测试使用；GUI 主循环下不必调用）。

        通过反复 `root.update()` 泵出 after 回调，等价于短暂地跑一下事件循环。
        """
        job = job if job is not None else self._export_job
        if job is None:
            return None
        deadline = time.monotonic() + float(timeout)
        while self._export_job is not None and time.monotonic() < deadline:
            try:
                self.root.update()
            except tk.TclError:
                break
            time.sleep(0.005)
        return job

    def destroy(self):
        # 关窗口时先让后台任务停手，避免它还在写文件 / 已无处回报
        if self._export_job is not None:
            self._export_job.cancel()
            self.wait_for_export(self._export_job, timeout=5.0)
        self.session.close_document()

    def destroy(self):
        self.session.close_document()


def ask_password_string(title, prompt, parent=None, show="*"):
    """密码输入框封装（便于测试 monkeypatch）。取消返回 None。"""
    return simpledialog.askstring(title, prompt, parent=parent, show=show)


def make_test_pdf(path, pages=3, width=595, height=842):
    """生成测试/演示用 PDF（也供 tests 使用）。"""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 100), "Page %d of %d" % (i + 1, pages), fontsize=24, fontname="helv")
    doc.save(path)
    doc.close()
    return path


def make_test_stamp(path, size=200, color=(255, 0, 0, 255)):
    """生成测试用圆形红章 PNG（也供 tests 使用）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, size - 10, size - 10], outline=color, width=5)
    draw.ellipse([size * 0.1, size * 0.1, size * 0.9, size * 0.9], outline=color, width=2)
    img.save(path)
    return path


def main():
    root = tk.Tk()
    PDFStamper(root)
    root.mainloop()


if __name__ == "__main__":
    main()
