#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF盖章工具 v3.1 - 专业版
支持：多公章、撤销/重做、旋转、批量盖章、骑缝章、透明度调节

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
import os
import uuid
import tempfile
from collections import OrderedDict

import fitz  # PyMuPDF（该版本会打印 deprecation 警告，属正常）
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

# 可编辑 / 会吞键的控件类型名（用于快捷键分发，见 shortcut_blocked）
EDITABLE_WIDGET_NAMES = frozenset({
    "Entry", "TEntry", "Text", "Listbox", "Spinbox", "TSpinbox",
    "Combobox", "TCombobox", "Treeview", "Notebook", "TNotebook", "Editor",
})

_stamp_image_cache = OrderedDict()
_stamp_image_cache_lock_depth = 0
STAMP_IMAGE_CACHE_MAX = 96


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


def stamp_export_geometry(stamp, scale_factor):
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
    rect_px = (stamp.x, stamp.y, stamp.x + w, stamp.y + h)
    rect_pt = pdf_rect_from_canvas(stamp.x, stamp.y, w, h, scale_factor)
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
                          nominal_width_pt=None):
    """
    骑缝章第 slice_index 刀在某一页上的目标矩形（PDF point）。

    放置约定（每页同一位置、内容不同 -> 错页拼合后为完整一枚章）：
      - 垂直方向：页面居中。
      - 水平方向：以「右边缘」为基准锚定（与旧版方向一致：offset 越大越向左）：
          right = page_width - offset * (full_width - nominal_slice_width)
          x0    = right - slice_width
        nominal_slice_width = full_width / num_pages，各页共用同一个名义宽度，
        因此所有页的右边缘严格对齐（像素取整只让宽度相差 <=1px，不会造成错位）。
      - offset=0 时切片贴住页面右边缘；offset=1 时整枚章完全收进页面内。
      - 结果 clamp 在页面内，不会被裁到页外。

    slice_width_pt     : 该刀真实宽度（默认取名义宽度）
    nominal_width_pt   : 放置用的名义宽度（默认 full_width / num_pages）
    """
    n = max(1, int(num_pages))
    idx = min(max(0, int(slice_index)), n - 1)
    nominal = float(nominal_width_pt if nominal_width_pt is not None else full_width_pt / n)
    actual = float(slice_width_pt if slice_width_pt is not None else nominal)
    right = page_width_pt - float(offset) * max(0.0, full_width_pt - nominal)
    right = min(right, page_width_pt)
    x0 = max(0.0, right - actual)
    y0 = max(0.0, (page_height_pt - full_height_pt) / 2.0)
    return (x0, y0, x0 + actual, y0 + full_height_pt)


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
    rect_pt = cross_fold_slice_rect(
        page_rect.width, page_rect.height, full_w_pt, full_h_pt,
        page_index, num_pages, offset, slice_width_pt=slice_w_pt,
    )
    slices_pt = []
    for i, (s, e) in enumerate(bounds):
        slices_pt.append(cross_fold_slice_rect(
            page_rect.width, page_rect.height, full_w_pt, full_h_pt,
            i, num_pages, offset, slice_width_pt=(e - s) / sf,
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


def process_stamp_image(img, opacity=1.0, rotation=0, scale=1.0, use_cache=True):
    """
    唯一的公章图像处理管线（修复 A）：opacity -> rotate(expand=True) -> scale。
    预览与导出都走这里，因此「画布上看到的尺寸/中心」与「导出的尺寸/中心」必然一致。

    带模块级缓存（修复 E 的一部分）：key 由图像身份 + 尺寸 + mode + 参数组成，
    参数不变时不重复做 opacity/rotate/resize。缓存持有原图强引用，避免 id() 复用。
    """
    global _stamp_image_cache_lock_depth
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

    key = (id(img), img.size, img.mode, opacity_f, rotation_i, round(scale_f, 4))
    if use_cache:
        hit = _stamp_image_cache.get(key)
        if hit is not None:
            _stamp_image_cache.move_to_end(key)
            return hit[1]

    out = apply_opacity(img, opacity_f)
    if rotation_i:
        out = out.rotate(rotation_i, expand=True, resample=Image.Resampling.BICUBIC)
    if abs(scale_f - 1.0) > 1e-9:
        w, h = out.size
        out = out.resize((max(1, int(round(w * scale_f))), max(1, int(round(h * scale_f)))),
                         Image.Resampling.LANCZOS)

    if use_cache:
        _stamp_image_cache[key] = (img, out)
        while len(_stamp_image_cache) > STAMP_IMAGE_CACHE_MAX:
            _stamp_image_cache.popitem(last=False)
    return out


def get_processed_image(stamp):
    """StampConfig（或任何带 img/opacity/rotation/scale 的对象）的处理后图像。"""
    return process_stamp_image(stamp.img, stamp.opacity, stamp.rotation, stamp.scale)


def stamp_image_cache_clear():
    _stamp_image_cache.clear()


def stamp_image_cache_info():
    return {"entries": len(_stamp_image_cache), "max": STAMP_IMAGE_CACHE_MAX, "hits": None}


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
    该图元能否被拖拽移动。骑缝章切片的位置由「页序 + 偏移滑块」算出（不可拖），
    普通公章按画布坐标自由拖动。
    """
    if not tags:
        return False
    if "stamp_item" not in tags:
        return False
    return "cross_fold" not in tags


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
        }

    def apply_dict(self, data):
        self.name = data.get("name", self.name)
        self.x = data.get("x", self.x)
        self.y = data.get("y", self.y)
        self.scale = data.get("scale", self.scale)
        self.opacity = data.get("opacity", self.opacity)
        self.rotation = data.get("rotation", self.rotation)
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

def _prepare_stamp_payloads(stamps, doc, scale_factor, cross_fold_mode,
                            cross_fold_stamp_index, cross_fold_offset):
    """
    为每个公章预处理一次图像并只编码一次 PNG（修复 F / G / H）。
    返回 (payloads, encode_count)；payload 描述每页该插什么。
    """
    num_pages = doc.page_count
    payloads = []
    encode_count = 0
    for idx, stamp in enumerate(stamps):
        processed = get_processed_image(stamp)
        if cross_fold_mode and idx == cross_fold_stamp_index:
            # 与预览共用 cross_fold_geometry（同一套切片 + 同一套坐标）
            bounds_all = slice_bounds(processed.size[0], num_pages)
            slices = split_cross_fold_images(processed, num_pages, cross_fold_offset)
            per_page_geo = [
                cross_fold_geometry(stamp, fitz.Rect(0, 0, *_page_size(doc, i)),
                                    num_pages, cross_fold_offset, scale_factor, page_index=i)
                for i in range(num_pages)
            ]
            for i, sl in enumerate(slices):
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
            rect_pt0 = stamp_export_geometry(stamp, scale_factor)["rect_pt"]
            for i in range(num_pages):
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


def export_pdf_with_stamps(src_doc, stamps, out_path=None, scale_factor=None,
                           cross_fold_mode=False, cross_fold_stamp_index=0,
                           cross_fold_offset=0.5, underlay=True):
    """
    导出盖章后的 PDF（预览/导出共用同一套几何函数）。

    参数
      src_doc    : 已打开的 fitz.Document（不会被修改、不会被 close）
      stamps     : list[StampConfig]
      out_path   : 目标路径；None 时只计算并写进临时文件（便于测试），返回 report['output_path']
      scale_factor : 画布像素 -> PDF point（默认 canvas_scale()）
      cross_fold_mode / cross_fold_stamp_index / cross_fold_offset : 骑缝章
      underlay   : True -> 公章在文字下面（用 insert_image(overlay=False) 实现；
                   旧版调用了本版本 PyMuPDF 已不存在的 page.send_to_back，会抛异常）

    返回 report dict:
      page_count, embedded: [{'page','xref','rect','size_px','center_pt','kind','shifted_into_page'}],
      per_page_image_counts: [int], png_encode_count, output_path
    """
    sf = float(scale_factor if scale_factor is not None else canvas_scale())
    own_tmp = out_path is None
    if own_tmp:
        fd, out_path = tempfile.mkstemp(prefix="pdf_stamper_", suffix=".pdf")
        os.close(fd)
    try:
        new_doc = fitz.open()
        try:
            new_doc.insert_pdf(src_doc)
            payloads, encode_count = _prepare_stamp_payloads(
                stamps, new_doc, sf, cross_fold_mode, cross_fold_stamp_index, cross_fold_offset)
            embedded = []
            xref_by_bytes = {}
            for payload in payloads:
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
            new_doc.save(out_path, garbage=3, deflate=True)
            per_page = [len(new_doc[i].get_images(full=True)) for i in range(new_doc.page_count)]
            page_count = new_doc.page_count
        finally:
            new_doc.close()
        return {
            "output_path": out_path,
            "page_count": page_count,
            "embedded": embedded,
            "per_page_image_counts": per_page,
            "png_encode_count": encode_count,
            "scale_factor": sf,
        }
    finally:
        if own_tmp:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass


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
        self.page_configs = {}
        self.history = HistoryManager()
        self.image_pool = {}          # {stamp_id: PIL.Image}，只增不删，供 undo 找回
        self.active_stamp_idx = 0
        self.selected_stamp = None    # 画布上被选中的公章索引
        self.cross_fold_mode = False
        self.cross_fold_offset = 0.5
        self.stamp_underlay = True
        self.render_dpi = float(render_dpi)
        self.scale_factor = canvas_scale(self.render_dpi)
        self.last_restore_missing = []

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
        self.page_configs = {}
        self.image_pool = {}
        self.active_stamp_idx = 0
        self.selected_stamp = None
        self.cross_fold_mode = False
        self.history = HistoryManager()
        self.last_restore_missing = []

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
        return OPEN_OK, describe_open_status(OPEN_OK, os.path.basename(filepath))

    # ---- 公章 ----
    def add_stamp(self, img, name="公章"):
        stamp = StampConfig(img, name)
        self.stamps.append(stamp)
        self.image_pool[stamp.stamp_id] = img
        self.active_stamp_idx = len(self.stamps) - 1
        return stamp

    def remove_stamp_by_index(self, idx):
        if 0 <= idx < len(self.stamps):
            stamp = self.stamps.pop(idx)
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
                                  extra={"selected": selected.stamp_id if selected else None})

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
        self.current_page = min(max(0, int(state.get("current_page", self.current_page) or 0)),
                                max(0, self.total_pages - 1)) if self.total_pages else 0
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

    def __init__(self, dpi=DEFAULT_RENDER_DPI, max_page_cache=8):
        self.dpi = float(dpi)
        self.scale_factor = canvas_scale(self.dpi)
        self.max_page_cache = int(max_page_cache)
        self._page_cache = OrderedDict()   # key -> PIL.Image
        self._doc_token = None
        self.stats = {"page_hit": 0, "page_miss": 0, "stamp_hit": 0, "stamp_miss": 0}

    # ---- 缓存管理 ----
    def invalidate(self):
        self._page_cache.clear()
        self._doc_token = None

    def _check_doc(self, doc):
        token = (id(doc), doc.page_count, getattr(doc, "is_encrypted", None), str(doc.name))
        if token != self._doc_token:
            self.invalidate()
            self._doc_token = token

    def cache_info(self):
        return {"cached_pages": len(self._page_cache), "stats": dict(self.stats),
                "stamp_cache": stamp_image_cache_info()}

    # ---- 页面位图 ----
    def page_bitmap_key(self, doc, page_index):
        return (id(doc), int(page_index), self.dpi)

    def get_page_bitmap(self, doc, page_index):
        """渲染（或命中缓存）某页的位图。"""
        self._check_doc(doc)
        key = self.page_bitmap_key(doc, page_index)
        hit = self._page_cache.get(key)
        if hit is not None:
            self._page_cache.move_to_end(key)
            self.stats["page_hit"] += 1
            return hit
        self.stats["page_miss"] += 1
        page = doc[min(max(0, int(page_index)), doc.page_count - 1)]
        pix = page.get_pixmap(dpi=int(round(self.dpi)), colorspace=fitz.csRGB, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self._page_cache[key] = img
        while len(self._page_cache) > self.max_page_cache:
            self._page_cache.popitem(last=False)
        return img

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
                      cross_fold_stamp_index=None, cross_fold_offset=0.5):
        """
        返回 {'page_bitmap', 'stamp_items': [{'tag','stamp_id','x','y','image','size','cross_fold'}]}
        x/y 为画布像素左上角；骑缝章时坐标由 cross_fold_geometry 算出（与导出同源）。
        """
        self._check_doc(doc)
        page_index = min(max(0, int(page_index)), doc.page_count - 1)
        page = doc[page_index]
        items = []
        for idx, stamp in enumerate(stamps):
            image = self.get_stamp_bitmap(stamp)
            if cross_fold_mode and idx == cross_fold_stamp_index:
                geo = cross_fold_geometry(stamp, page.rect, doc.page_count,
                                          cross_fold_offset, self.scale_factor,
                                          page_index=page_index)
                start, end = geo["slice_bounds"]
                image = image.crop((start, 0, end, image.size[1]))
                rect_px = cross_fold_rect_px(stamp, page.rect, doc.page_count,
                                             cross_fold_offset, self.scale_factor,
                                             page_index=page_index)
                x, y = rect_px[0], rect_px[1]
                kind = "cross_fold"
            else:
                x, y = stamp.x, stamp.y
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


# ==================== GUI ====================

class PDFStamper:
    def __init__(self, root, render_dpi=DEFAULT_RENDER_DPI):
        self.root = root
        self.root.title("PDF盖章工具 v3.1")
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

        self._tk_img_cache = {}

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
    def page_configs(self):
        return self.session.page_configs

    @page_configs.setter
    def page_configs(self, value):
        self.session.page_configs = dict(value)

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

        ttk.Button(toolbar, text="打开PDF", command=self.open_pdf).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="加载公章", command=self.load_stamp).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="导出PDF", command=self.export_pdf).pack(side=tk.LEFT, padx=2)

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
        self.rotation_label = ttk.Label(toolbar, text="0°")
        self.rotation_label.pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)

        self.cross_fold_btn = ttk.Button(toolbar, text="骑缝章: 关", command=self.toggle_cross_fold)
        self.cross_fold_btn.pack(side=tk.LEFT, padx=2)

        self.layer_btn = ttk.Button(toolbar, text="公章: 底层", command=self.toggle_layer)
        self.layer_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="删除选中", command=self.delete_selected_stamp).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="重置所有", command=self.reset_all).pack(side=tk.LEFT, padx=2)

    def create_side_panel(self):
        side_panel = ttk.Frame(self.root, width=250)
        side_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)

        ttk.Label(side_panel, text="公章列表", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(0, 5))

        self.stamp_listbox = tk.Listbox(side_panel, height=8, selectmode=tk.SINGLE)
        self.stamp_listbox.pack(fill=tk.X, pady=(0, 5))
        self.stamp_listbox.bind('<<ListboxSelect>>', self.on_stamp_select)

        ttk.Button(side_panel, text="添加公章", command=self.load_stamp).pack(fill=tk.X, pady=(0, 5))
        ttk.Button(side_panel, text="删除公章", command=self.delete_stamp).pack(fill=tk.X, pady=(0, 10))

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
        self.offset_var = tk.DoubleVar(value=0.5)
        self.offset_slider = ttk.Scale(cross_frame, from_=0.0, to=1.0, variable=self.offset_var,
                                       command=self.on_offset_change, length=120)
        self.offset_slider.pack(side=tk.LEFT, padx=5)
        self.offset_label = ttk.Label(cross_frame, text="50%")
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
                filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"), ("所有文件", "*.*")]
            )
        if not filepath:
            return None
        try:
            img = Image.open(filepath).convert("RGBA")
        except Exception as e:
            messagebox.showerror("错误", f"加载失败: {str(e)}")
            return None
        stamp = self.add_stamp_image(img, os.path.basename(filepath))
        return stamp

    def add_stamp_image(self, img, name="公章"):
        stamp = self.session.add_stamp(img, name)
        self.sync_sliders_to_active()
        self.update_stamp_list()
        self.render_page()
        self.save_history()
        self.set_status("已加载: %s" % stamp.name)
        return stamp

    def delete_stamp(self):
        selection = self.stamp_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要删除的公章")
            return False
        idx = selection[0]
        name = self.stamps[idx].name if idx < len(self.stamps) else "?"
        if not messagebox.askyesno("确认", f"确定删除公章 '{name}'？"):
            return False
        return self.delete_stamp_at(idx)

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

    def update_stamp_list(self):
        self.stamp_listbox.delete(0, tk.END)
        for i, stamp in enumerate(self.stamps):
            marker = "► " if i == self.active_stamp_idx else "  "
            self.stamp_listbox.insert(tk.END, "%s%s" % (marker, stamp.name))
        if self.stamps and 0 <= self.active_stamp_idx < len(self.stamps):
            self.stamp_listbox.selection_set(self.active_stamp_idx)

    # ==================== 渲染 ====================

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
            self.pdf_doc, self.current_page, self.stamps,
            cross_fold_mode=self.cross_fold_mode,
            cross_fold_stamp_index=self.active_stamp_idx,
            cross_fold_offset=self.cross_fold_offset)

        bitmap = payload["page_bitmap"]
        self.page_img = self._tk_photo("page", id(bitmap), bitmap)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.page_img, tags="page")

        for item in payload["stamp_items"]:
            image = item["image"]
            photo = self._tk_photo("stamp", id(image), image)
            tags = [item["tag"], "stamp_item", item["kind"]]
            if item["index"] == self.active_stamp_idx:
                tags.append("active")
            if item["index"] == self.selected_stamp:
                tags.append("selected")
            self.canvas.create_image(item["x"], item["y"], anchor=tk.NW,
                                     image=photo, tags=tags)
        self.page_label.config(text="%d / %d" % (self.current_page + 1, self.total_pages))
        self.update_undo_redo_buttons()
        return payload

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
        items = self.canvas.find_overlapping(event.x, event.y, event.x + 1, event.y + 1)
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
            # 该图元是骑缝章切片：位置由页序 + 偏移滑块决定 -> 明确提示，而不是拖了没反应
            self.is_dragging = False
            self.drag_item_tag = None
            self.set_status("骑缝章位置由页序自动决定，请用「骑缝章配置 - 偏移」调整位置")
            return
        self.is_dragging = True
        self.drag_item_tag = "stamp_%d" % stamp_idx
        self.drag_start_x = event.x - stamp.x
        self.drag_start_y = event.y - stamp.y
        self.drag_last_x, self.drag_last_y = event.x, event.y
        self.drag_moved = False
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
        stamp.x = event.x - self.drag_start_x
        stamp.y = event.y - self.drag_start_y
        dx = event.x - self.drag_last_x
        dy = event.y - self.drag_last_y
        if dx or dy:
            self.canvas.move(self.drag_item_tag, dx, dy)
            self.drag_last_x, self.drag_last_y = event.x, event.y
            self.drag_moved = True

    def on_mouse_up(self, event):
        """松手才重绘 + 存历史（骑缝章位置由几何算出，需要重绘复位）。"""
        if not self.is_dragging:
            return
        self.is_dragging = False
        moved = self.drag_moved
        self.drag_item_tag = None
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
        self.update_stamp_list()
        self.sync_sliders_to_active()
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

    def on_offset_change(self, val):
        self.cross_fold_offset = float(val)
        self.offset_label.config(text="%d%%" % int(self.cross_fold_offset * 100))
        self.render_page()

    # ==================== 模式切换 ====================

    def toggle_cross_fold(self):
        self.cross_fold_mode = not self.cross_fold_mode
        self.cross_fold_btn.config(text="骑缝章: 开" if self.cross_fold_mode else "骑缝章: 关")
        self.render_page()
        self.set_status("骑缝章模式: %s" % ("开启" if self.cross_fold_mode else "关闭"))

    def toggle_layer(self):
        self.stamp_underlay = not self.stamp_underlay
        self.layer_btn.config(text="公章: 底层" if self.stamp_underlay else "公章: 上层")
        layer_name = "底层（文字下面）" if self.stamp_underlay else "上层（文字上面）"
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
        self.render_page()
        self.set_status("已全选（可进行批量操作）")

    # ==================== 导出 ====================

    def export_pdf(self, save_path=None):
        if not self.pdf_doc or not self.stamps:
            messagebox.showwarning("提示", "请先打开PDF并加载至少一个公章")
            return None
        if not save_path:
            save_path = filedialog.asksaveasfilename(
                title="保存盖章PDF",
                defaultextension=".pdf",
                filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
            )
        if not save_path:
            return None
        try:
            report = export_pdf_with_stamps(
                self.pdf_doc, self.stamps, save_path,
                scale_factor=self.scale_factor,
                cross_fold_mode=self.cross_fold_mode,
                cross_fold_stamp_index=self.active_stamp_idx,
                cross_fold_offset=self.cross_fold_offset,
                underlay=self.stamp_underlay)
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")
            return None
        self.set_status("已导出: %s" % os.path.basename(save_path))
        messagebox.showinfo("成功", "PDF已保存:\n%s" % save_path)
        return report

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
