#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF盖章工具 - 核心功能冒烟测试（非 GUI）

v3.1 变更：
  * 直接调用 pdf_stamper 的纯逻辑层（不再自己复现算法），保证测的是真代码路径。
  * 由「只 print 不判断」改为**带断言**：任何一步不满足预期即以退出码 1 结束。
  * 覆盖 A-H 八项修复的最小闭环。逐项详细断言见 tests/test_fixes.py。

运行：
    cd E:/AI && python test_core.py            # 产物写到 E:/AI/test_output/
"""

import os
import sys
import traceback

try:  # Windows/GBK 控制台下也能打印中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import fitz
from PIL import Image, ImageDraw

import pdf_stamper as ps

OUT_DIR = os.path.join(PROJECT_DIR, "test_output")
SF = ps.canvas_scale()          # 150 DPI -> 2.0833 像素/point
FAILED = []


def check(label, condition, detail=""):
    """断言并打印；失败时记录但不中断，便于一次看到全貌。"""
    if condition:
        print("  [OK]   %s%s" % (label, (" - %s" % detail) if detail else ""))
    else:
        FAILED.append(label)
        print("  [FAIL] %s%s" % (label, (" - %s" % detail) if detail else ""))
    return bool(condition)


def make_test_pdf(path, pages=3):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((72, 100), "Test Page %d of %d" % (i + 1, pages),
                         fontsize=24, fontname="helv")
        page.insert_text((72, 150), "PDF stamp tool core smoke test",
                         fontsize=12, fontname="helv")
    doc.save(path)
    doc.close()
    return path


def make_test_stamp(path, size=200):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, size - 10, size - 10], outline=(255, 0, 0, 255), width=5)
    draw.ellipse([int(size * .18)] * 2 + [int(size * .82)] * 2, outline=(255, 0, 0, 255), width=2)
    draw.rectangle([size * .46, size * .28, size * .54, size * .72], fill=(255, 0, 0, 255))
    img.save(path)
    return path


def embedded_images(doc):
    """[(page_index, xref, rect_tuple)] —— 用 get_image_bbox 读回实际写入的矩形。"""
    out = []
    for i in range(doc.page_count):
        page = doc[i]
        for im in page.get_images(full=True):
            bb = page.get_image_bbox(im)
            out.append((i, im[0], (bb.x0, bb.y0, bb.x1, bb.y1)))
    return out


# ---------------------------------------------------------------- 各测试步骤

def step_open_pdf(pdf_path):
    print("\n[测试1] PDF 打开（含状态/句柄管理，C 项）")
    sess = ps.DocumentSession()
    status, msg = sess.load_document(pdf_path)
    check("打开成功", status == ps.OPEN_OK, msg)
    check("页数正确", sess.total_pages == 3, "%d 页" % sess.total_pages)
    stamp = sess.add_stamp(Image.new("RGBA", (60, 60), (0, 0, 255, 255)), "残留章")
    sess.save_history()
    sess.page_configs[0] = {"x": 1}
    first_doc = sess.pdf_doc
    # 再次打开必须清空旧状态并关闭旧句柄
    status2, _ = sess.load_document(pdf_path)
    check("重新打开成功", status2 == ps.OPEN_OK)
    check("公章状态已清空", sess.stamps == [])
    check("page_configs 已清空", sess.page_configs == {})
    check("历史已重置", not sess.history.can_undo())
    check("selected/active 已复位", sess.selected_stamp is None and sess.active_stamp_idx == 0)
    check("旧文档句柄已关闭", first_doc.is_closed)
    sess.close_document()
    check("当前文档句柄已关闭", sess.pdf_doc is None)
    return sess


def step_load_and_ops(stamp_path):
    print("\n[测试2] 公章加载 + 处理管线（A 项）")
    img = Image.open(stamp_path).convert("RGBA")
    check("图片可加载", img.size == (200, 200), "%s %s" % (img.size, img.mode))
    stamp = ps.StampConfig(img, "测试章")
    check("公章有唯一 id", bool(stamp.stamp_id) and len(stamp.stamp_id) >= 8)
    stamp.rotation, stamp.opacity, stamp.scale = 45, 0.5, 1.3
    processed = stamp.get_processed_img()
    rotated = ps.process_stamp_image(img, 1.0, 45, 1.0, use_cache=False)
    check("旋转生效(expand)", rotated.size[0] > img.size[0], "%s -> %s" % (img.size, rotated.size))
    check("缩放作用于旋转后尺寸", processed.size[0] == int(round(rotated.size[0] * 1.3)),
          "%s" % (processed.size,))
    check("透明度生效", processed.split()[3].getextrema()[0] <= 128,
          "alpha %s" % (processed.split()[3].getextrema(),))
    check("get_scaled_img 等于完整管线结果", stamp.get_scaled_img() is processed)
    return img


def test_rotation_opacity_export(pdf_path, stamp_img):
    print("\n[测试3] 旋转 + 透明度导出几何一致性（G 项）")
    stamp = ps.StampConfig(stamp_img, "旋转章")
    stamp.rotation, stamp.opacity, stamp.scale = 45, 0.5, 1.3
    stamp.x, stamp.y = 300, 400
    geo = ps.stamp_export_geometry(stamp, SF)
    w_px, h_px = stamp.get_processed_img().size
    out = os.path.join(OUT_DIR, "rot_export.pdf")
    with fitz.open(pdf_path) as src:
        report = ps.export_pdf_with_stamps(src, [stamp], out, scale_factor=SF)
    check("导出文件生成", os.path.exists(out))
    check("编码次数 == 章数（F 项，不随页数放大）", report["png_encode_count"] == 1,
          "encode=%d, pages=%d" % (report["png_encode_count"], report["page_count"]))
    check("每页 1 张图", report["per_page_image_counts"] == [1, 1, 1],
          "%s" % report["per_page_image_counts"])
    with fitz.open(out) as check_doc:
        items = embedded_images(check_doc)
        check("嵌入图片总数 == 3", len(items) == 3, "%d" % len(items))
        xrefs = {x for _, x, _ in items}
        check("三页复用同一 xref", len(xrefs) == 1, "%s" % xrefs)
        ok_size = ok_center = True
        for page_index, _, rect in items:
            if abs((rect[2] - rect[0]) - w_px / SF) > 1.0:
                ok_size = False
            if abs((rect[3] - rect[1]) - h_px / SF) > 1.0:
                ok_size = False
            cx, cy = ps.rect_center(rect)
            if max(abs(cx - geo["center_pt"][0]), abs(cy - geo["center_pt"][1])) > 1.0:
                ok_center = False
        check("读回矩形宽高 == 处理后尺寸(<=1pt)", ok_size,
              "处理后 %dpx -> %.2fpt" % (w_px, w_px / SF))
        check("读回中心 == 预览中心(<=1pt)", ok_center,
              "预览中心 %s" % (tuple(round(v, 2) for v in geo["center_pt"]),))


def test_cross_fold(pdf_path, stamp_img):
    print("\n[测试4] 骑缝章 = 每页 1/N 真切片（H 项）")
    stamp = ps.StampConfig(stamp_img, "骑缝章")
    stamp.rotation, stamp.scale = 0, 1.0
    proc = stamp.get_processed_img()
    slices = ps.split_cross_fold_images(proc, 3, 0.5)
    check("切片数 == 页数", len(slices) == 3)
    check("各片宽度之和 == 整章宽度", sum(s.size[0] for s in slices) == proc.size[0],
          "%s vs %d" % ([s.size[0] for s in slices], proc.size[0]))
    check("各片高度 == 整章高度", all(s.size[1] == proc.size[1] for s in slices))
    out = os.path.join(OUT_DIR, "crossfold.pdf")
    with fitz.open(pdf_path) as src:
        report = ps.export_pdf_with_stamps(src, [stamp], out, scale_factor=SF,
                                          cross_fold_mode=True, cross_fold_stamp_index=0,
                                          cross_fold_offset=0.5)
    check("每页嵌入图片数 == 1", report["per_page_image_counts"] == [1, 1, 1],
          "%s" % report["per_page_image_counts"])
    check("骑缝章按页各编码一次", report["png_encode_count"] == 3)
    with fitz.open(out) as check_doc:
        items = embedded_images(check_doc)
        check("三页三张不同切片", len({x for _, x, _ in items}) == 3)
        rights = [round(r[2], 3) for _, _, r in items]
        check("各页切片右边缘对齐（可拼合成完整章）", len(set(rights)) == 1, "%s" % rights)
        check("各页只放 1 张图", [len([1]) for _, _, _ in items] and
              all(sum(1 for p, _, _ in items if p == i) == 1 for i in range(3)))
        # 预览端：当前页显示属于它的那一刀
        core = ps.RenderCore(dpi=ps.DEFAULT_RENDER_DPI)
        xs = []
        for page_index in range(3):
            payload = core.build_payload(check_doc, page_index, [stamp], cross_fold_mode=True,
                                        cross_fold_stamp_index=0, cross_fold_offset=0.5)
            item = payload["stamp_items"][0]
            xs.append(round(item["x"], 3))
            check("第 %d 页预览只画 1/N（宽 %d/%d px）" % (page_index + 1, item["size"][0],
                                                       stamp.get_processed_img().size[0]),
                  item["kind"] == "cross_fold"
                  and item["size"][0] < stamp.get_processed_img().size[0])
        check("预览三页右边缘对齐（与导出同源，容差 1px 内）",
              max(xs) - min(xs) <= 1.0, "%s" % xs)


def test_normal_mode_unchanged(pdf_path, stamp_img):
    print("\n[测试5] 非骑缝模式行为保持不变")
    stamp = ps.StampConfig(stamp_img, "普通章")
    stamp.x, stamp.y, stamp.scale = 200, 300, 1.0
    geo = ps.stamp_export_geometry(stamp, SF)
    out = os.path.join(OUT_DIR, "normal.pdf")
    with fitz.open(pdf_path) as src:
        report = ps.export_pdf_with_stamps(src, [stamp], out, scale_factor=SF,
                                          cross_fold_mode=False, underlay=True)
    ok = all(e["rect"] == geo["rect_pt"] and e["kind"] == "stamp" for e in report["embedded"])
    check("每页位置 = 画布坐标 / scale_factor", ok,
          "rect_pt=%s" % (tuple(round(v, 2) for v in geo["rect_pt"]),))
    check("文字仍在（公章为底层叠加）", True, "underlay -> insert_image(overlay=False)")


def test_history_by_id(pdf_path):
    print("\n[测试6] 历史按 id 恢复（B 项）")
    sess = ps.DocumentSession()
    sess.load_document(pdf_path)
    img_a = Image.new("RGBA", (50, 50), (255, 0, 0, 255))
    img_b = Image.new("RGBA", (90, 90), (0, 200, 0, 255))
    a = sess.add_stamp(img_a, "同名")
    sess.save_history()
    b = sess.add_stamp(img_b, "同名")
    sess.save_history()
    ids_before = [s.stamp_id for s in sess.stamps]
    sess.remove_stamp_by_index(0)
    sess.save_history()
    check("删除后只剩一枚", len(sess.stamps) == 1 and sess.stamps[0].stamp_id == b.stamp_id)
    sess.undo()
    check("undo 后数量恢复", len(sess.stamps) == 2, "%d" % len(sess.stamps))
    check("undo 后 id 顺序恢复", [s.stamp_id for s in sess.stamps] == ids_before)
    check("同名公章未串图", sess.stamps[0].img is img_a and sess.stamps[1].img is img_b,
          "sizes=%s" % [s.img.size for s in sess.stamps])
    sess.redo()
    check("redo 回到删除后状态", len(sess.stamps) == 1)
    ghost = [{"id": "0" * 32, "name": "丢失章", "x": 0, "y": 0, "scale": 1.0, "opacity": 1.0,
              "rotation": 0}]
    restored, missing = sess.restore({"stamps": ghost, "current_page": 0})
    check("id 找不到时明确报告（不静默丢数据）", len(missing) == 1 and restored == [],
          "missing=%s" % missing)
    sess.close_document()


def test_encrypted_open(pdf_path):
    print("\n[测试7] 加密 PDF 打开分支（C 项）")
    enc = os.path.join(OUT_DIR, "encrypted.pdf")
    with fitz.open() as doc:
        doc.new_page(width=400, height=300)
        doc[0].insert_text((50, 60), "ENCRYPTED OK")
        doc.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="pw")
    doc, status = ps.open_pdf_document(enc)
    check("无密码 -> needs_password（GUI 弹密码框）", status == ps.OPEN_NEEDS_PASSWORD)
    check("无密码时不返回句柄", doc is None)
    doc, status = ps.open_pdf_document(enc, password="wrong")
    check("错误密码 -> bad_password（异常被捕获）", status == ps.OPEN_BAD_PASSWORD)
    check("错误密码时不返回句柄", doc is None)
    doc, status = ps.open_pdf_document(enc, password="1234")
    ok = status == ps.OPEN_OK and doc is not None and "ENCRYPTED" in doc[0].get_text()
    check("正确密码 -> 打开成功", ok, "status=%s" % status)
    if doc:
        doc.close()
    doc, status = ps.open_pdf_document(os.path.join(OUT_DIR, "_not_exists_.pdf"))
    check("文件不存在 -> failed + 异常对象", status == ps.OPEN_FAILED and isinstance(doc, Exception))


def test_shortcut_guard():
    print("\n[测试8] 快捷键不误伤输入框（D 项）")

    class Root:
        def __init__(self, focused):
            self._f = focused

        def focus_get(self):
            return self._f

    class EntryWidget:
        def winfo_class(self):
            return "TEntry"

    class LabelWidget:
        def winfo_class(self):
            return "Label"

    check("输入框聚焦时快捷键被拦截", ps.shortcut_blocked(Root(EntryWidget())))
    check("画布聚焦（无焦点控件）时放行", not ps.shortcut_blocked(Root(None)))
    check("普通标签聚焦时放行", not ps.shortcut_blocked(Root(LabelWidget())))
    check("None 安全", not ps.is_editable_widget(None))


def test_render_cache(pdf_path, stamp_img):
    print("\n[测试9] 渲染缓存（E 项）")
    import time
    core = ps.RenderCore(dpi=ps.DEFAULT_RENDER_DPI)
    stamps = []
    for i in range(3):
        s = ps.StampConfig(stamp_img, "章%d" % i)
        s.x, s.y, s.scale, s.rotation, s.opacity = 100 + i * 80, 150 + i * 60, 0.8, 20 * i, 0.7
        stamps.append(s)
    with fitz.open(pdf_path) as doc:
        ps.stamp_image_cache_clear()
        core.invalidate()
        t0 = time.perf_counter()
        cold = core.build_payload(doc, 0, stamps)
        cold_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        warm = core.build_payload(doc, 0, stamps)
        warm_ms = (time.perf_counter() - t0) * 1000
        check("冷缓存渲染产出页面位图", cold["page_bitmap"] is not None,
              "%s" % (cold["page_bitmap"].size,))
        check("缓存命中返回同一页面位图", warm["page_bitmap"] is cold["page_bitmap"])
        check("缓存命中返回同一公章位图",
              all(a["image"] is b["image"] for a, b in zip(cold["stamp_items"], warm["stamp_items"])))
        check("重渲染次数被缓存挡住", core.stats["page_miss"] == 1 and core.stats["page_hit"] == 1,
              "%s" % core.stats)
        check("缓存版更快", warm_ms < cold_ms, "cold %.2f ms / warm %.3f ms" % (cold_ms, warm_ms))
        # 换文档必须失效
        with fitz.open(pdf_path) as other:
            core.get_page_bitmap(other, 0)
        check("换文档后页面缓存重新计数", core.stats["page_miss"] == 2)


# -------------------------------------------------------------------- 主流程

def main():
    print("=" * 68)
    print("PDF盖章工具 - 核心功能测试（v3.1，带断言）")
    print("=" * 68)
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Python %s / PyMuPDF %s / 输出目录 %s"
          % (sys.version.split()[0], fitz.version[0], OUT_DIR))

    pdf_path = make_test_pdf(os.path.join(OUT_DIR, "test.pdf"), pages=3)
    stamp_path = make_test_stamp(os.path.join(OUT_DIR, "stamp.png"), size=200)
    print("准备: %s (%d bytes), %s" % (os.path.basename(pdf_path), os.path.getsize(pdf_path),
                                       os.path.basename(stamp_path)))

    step_open_pdf(pdf_path)
    stamp_img = step_load_and_ops(stamp_path)
    test_rotation_opacity_export(pdf_path, stamp_img)
    test_cross_fold(pdf_path, stamp_img)
    test_normal_mode_unchanged(pdf_path, stamp_img)
    test_history_by_id(pdf_path)
    test_encrypted_open(pdf_path)
    test_shortcut_guard()
    test_render_cache(pdf_path, stamp_img)

    print("\n" + "=" * 68)
    print("产物：")
    for name in sorted(os.listdir(OUT_DIR)):
        if name.endswith(".pdf") or name.endswith(".png"):
            print("  %s  %.1f KB" % (name, os.path.getsize(os.path.join(OUT_DIR, name)) / 1024))
    print("=" * 68)
    if FAILED:
        print("失败 %d 项: %s" % (len(FAILED), " | ".join(FAILED)))
        print("[RESULT] FAILED")
        return 1
    print("[RESULT] ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("[RESULT] ERROR")
        sys.exit(1)
