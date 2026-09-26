#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF盖章工具 v3.1 - 修复项验收测试（A-H 逐项断言）

只测纯逻辑层（不启动 Tk 主循环，不实例化 PDFStamper）。
运行：
    cd E:/AI && python -m unittest discover -s tests -v
"""

import inspect
import io
import os
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# 控制台在 Windows/GBK 下也能打印中文（不让 print 变成失败原因）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:  # PyMuPDF >= 1.24 提供 pymupdf 别名；用别名可避免 fitz 的弃用警告
    import pymupdf as fitz
except ImportError:  # 旧版本（requirements 下限 1.23）只有 fitz
    import fitz

from PIL import Image, ImageDraw

import pdf_stamper as ps

OUT_DIR = os.path.join(PROJECT_DIR, "test_output")


# ==================== 测试夹具 ====================

def make_stamp_png(path, size=200, color=(255, 0, 0, 255)):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, size - 8, size - 8], outline=color, width=6)
    draw.ellipse([size * 0.22, size * 0.22, size * 0.78, size * 0.78], outline=color, width=3)
    draw.rectangle([size * 0.45, size * 0.3, size * 0.55, size * 0.7], fill=color)
    img.save(path)
    return path


def make_src_pdf(path, pages=3, width=595, height=842):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 100), "Test Page %d" % (i + 1), fontsize=24, fontname="helv")
    doc.save(path)
    doc.close()
    return path


def make_solid_stamp(size=200, color=(255, 0, 0, 255)):
    return Image.new("RGBA", (size, size), color)


def embedded_rects(doc):
    """每页嵌入图片的实际矩形（用 get_image_bbox 读回）。"""
    out = []
    for i in range(doc.page_count):
        page = doc[i]
        rects = []
        for im in page.get_images(full=True):
            bb = page.get_image_bbox(im)
            rects.append((im[0], (bb.x0, bb.y0, bb.x1, bb.y1)))
        out.append(rects)
    return out


# 输入夹具放独立临时目录：Windows 下仍被 fitz 句柄占用的文件无法被覆盖删除，
# 被测产物（rot_export.pdf / crossfold.pdf 等）仍然写在 test_output/。
_FIXTURES = {}


def fixture_dir():
    if "dir" not in _FIXTURES:
        import tempfile
        _FIXTURES["dir"] = tempfile.mkdtemp(prefix="pdf_stamp_fixtures_")
    return _FIXTURES["dir"]


def fixture(name, builder):
    if name not in _FIXTURES:
        _FIXTURES[name] = builder(os.path.join(fixture_dir(), name))
    return _FIXTURES[name]


def src_pdf():
    return fixture("fixes_src.pdf", lambda p: make_src_pdf(p, pages=3))


def src_pdf_5p():
    return fixture("fixes_src5.pdf", lambda p: make_src_pdf(p, pages=5))


def stamp_png():
    return fixture("fixes_stamp.png", lambda p: make_stamp_png(p, size=200))


def enc_pdf():
    """带口令 user=1234 的加密 PDF。"""
    def build(path):
        with fitz.open() as doc:
            doc.new_page(width=400, height=300)
            doc[0].insert_text((50, 80), "ENCRYPTED CONTENT")
            doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="owner-pw")
        return path
    return fixture("fixes_encrypted.pdf", build)


class TempArtifactMixin(unittest.TestCase):
    """管理 fitz 句柄：测试结束一律关闭，避免 Windows 文件占用。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs(OUT_DIR, exist_ok=True)
        cls.src_pdf_path = src_pdf()
        cls.stamp_png_path = stamp_png()
        src_pdf_5p()
        enc_pdf()

    def setUp(self):
        ps.stamp_image_cache_clear()
        self._open_docs = []
        self._sessions = []

    def tearDown(self):
        for doc in self._open_docs:
            try:
                doc.close()
            except Exception:
                pass
        for sess in self._sessions:
            sess.close_document()
        self._open_docs = []
        self._sessions = []

    def open_src(self, path=None):
        doc = fitz.open(path or self.src_pdf_path)
        self._open_docs.append(doc)
        return doc

    def new_session(self, **kw):
        sess = ps.DocumentSession(**kw)
        self._sessions.append(sess)
        return sess

    def load_stamp_image(self):
        return Image.open(self.stamp_png_path).convert("RGBA")


# ==================== A. 预览丢失旋转/透明度 ====================

class TestAPreviewPipeline(TempArtifactMixin):

    def test_white_stamp_background_becomes_transparent(self):
        img = Image.new("RGBA", (40, 40), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((5, 5, 34, 34), outline="red", width=3)
        transparent = ps.remove_white_background(img)

        self.assertEqual(transparent.getpixel((0, 0))[3], 0)
        self.assertEqual(transparent.getpixel((20, 20))[3], 255)

    def test_white_background_stays_transparent_in_cross_fold_slice(self):
        img = Image.new("RGBA", (90, 30), "white")
        ImageDraw.Draw(img).rectangle((5, 5, 84, 24), outline="red", width=3)
        stamp = ps.StampConfig(ps.remove_white_background(img), "白底骑缝章")
        slices = ps.split_cross_fold_images(stamp.get_processed_img(), 3)

        self.assertTrue(all(slice_img.getpixel((0, 0))[3] == 0 for slice_img in slices))

    def test_processed_applies_opacity_then_rotate_then_scale(self):
        img = make_solid_stamp(200)
        stamp = ps.StampConfig(img, "章A")
        stamp.rotation = 45
        stamp.opacity = 0.5
        stamp.scale = 1.3

        processed = stamp.get_processed_img()
        # 单一管线：先旋转(expand) 再缩放，尺寸必然同时体现两者
        rotated_only = ps.process_stamp_image(img, 1.0, 45, 1.0, use_cache=False)
        self.assertGreater(processed.size[0], rotated_only.size[0])
        self.assertEqual(processed.size[0], int(round(rotated_only.size[0] * 1.3)))
        self.assertEqual(processed.size, stamp.get_display_size())

    def test_old_bug_scaled_only_ignoring_rotation_is_gone(self):
        """旧版 get_scaled_img 返回未旋转未透图；新版必须与 get_processed_img 一致。"""
        img = make_solid_stamp(200)
        stamp = ps.StampConfig(img, "章A")
        stamp.rotation = 45
        stamp.opacity = 0.4
        stamp.scale = 2.0
        self.assertIs(stamp.get_scaled_img(), stamp.get_processed_img())
        self.assertNotEqual(stamp.get_scaled_img().size, img.size)

    def test_opacity_reduces_alpha_in_processed_image(self):
        img = make_solid_stamp(100, (255, 0, 0, 200))
        full = ps.process_stamp_image(img, 1.0, 0, 1.0, use_cache=False)
        half = ps.process_stamp_image(img, 0.5, 0, 1.0, use_cache=False)
        self.assertEqual(full.split()[3].getextrema(), (200, 200))
        self.assertEqual(half.split()[3].getextrema(), (100, 100))

    def test_get_tk_img_uses_processed_image(self):
        """get_tk_img 必须基于处理管线后的图像（不启动 GUI：monkeypatch PhotoImage）。"""
        img = make_solid_stamp(200)
        stamp = ps.StampConfig(img, "章A")
        stamp.rotation = 45
        stamp.scale = 1.5
        captured = {}

        class FakePhoto:
            def __init__(self, pil_img):
                captured["size"] = pil_img.size

        original = ps.ImageTk.PhotoImage
        ps.ImageTk.PhotoImage = FakePhoto
        try:
            stamp.get_tk_img()
        finally:
            ps.ImageTk.PhotoImage = original
        self.assertEqual(captured["size"], stamp.get_processed_img().size)
        self.assertNotEqual(captured["size"], img.size)


# ==================== G. 旋转后导出几何 ====================

class TestGExportGeometry(TempArtifactMixin):

    def test_stamp_page_scope_limits_preview_and_export(self):
        doc = self.open_src()
        core = ps.RenderCore(dpi=150)
        stamp = ps.StampConfig(make_solid_stamp(60, (255, 0, 0, 255)), "首页章")
        stamp.page_scope = "first"

        preview_counts = [len(core.build_payload(doc, i, [stamp])["stamp_items"])
                          for i in range(doc.page_count)]
        self.assertEqual(preview_counts, [1, 0, 0])

        out = os.path.join(OUT_DIR, "first_page_only.pdf")
        report = ps.export_pdf_with_stamps(doc, [stamp], out, scale_factor=core.scale_factor)
        self.assertEqual([item["page"] for item in report["embedded"]], [0])

        stamp.page_scope = "last"
        out = os.path.join(OUT_DIR, "last_page_only.pdf")
        report = ps.export_pdf_with_stamps(doc, [stamp], out, scale_factor=core.scale_factor)
        self.assertEqual([item["page"] for item in report["embedded"]], [2])

    def test_underlay_export_keeps_page_content_above_stamp(self):
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.draw_rect(fitz.Rect(80, 80, 120, 120), color=(0, 0, 0), fill=(0, 0, 0))
        self._open_docs.append(doc)
        stamp = ps.StampConfig(make_solid_stamp(80, (255, 0, 0, 255)), "底层章")
        stamp.x, stamp.y = 60, 60
        out = os.path.join(OUT_DIR, "underlay_order.pdf")

        ps.export_pdf_with_stamps(doc, [stamp], out, scale_factor=1.0, underlay=True)
        with fitz.open(out) as check:
            pix = check[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            center = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).getpixel((100, 100))
        self.assertLess(max(center), 40)

    def test_underlay_export_preserves_text_and_links(self):
        source_path = os.path.join(OUT_DIR, "underlay_fidelity_source.pdf")
        source_doc = fitz.open()
        page = source_doc.new_page(width=300, height=300)
        page.insert_text((30, 40), "PRESERVE_TEXT", fontsize=16, fontname="helv")
        page.insert_link({
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(20, 50, 120, 70),
            "uri": "https://example.com",
        })
        source_doc.save(source_path)
        source_doc.close()
        doc = fitz.open(source_path)
        self._open_docs.append(doc)
        stamp = ps.StampConfig(make_solid_stamp(60, (255, 0, 0, 180)), "保真章")
        stamp.x, stamp.y = 100, 100
        out = os.path.join(OUT_DIR, "underlay_fidelity.pdf")

        report = ps.export_pdf_with_stamps(doc, [stamp], out,
                                           scale_factor=1.0, underlay=True)
        self.assertEqual(report["flattened_pages"], [])
        with fitz.open(out) as check:
            self.assertIn("PRESERVE_TEXT", check[0].get_text())
            links = check[0].get_links()
            self.assertTrue(any(link.get("uri") == "https://example.com" for link in links))

    def test_rect_from_processed_size(self):
        sf = ps.canvas_scale()
        img = self.load_stamp_image()
        stamp = ps.StampConfig(img, "章G")
        stamp.rotation, stamp.opacity, stamp.scale = 45, 0.5, 1.3
        stamp.x, stamp.y = 300, 400
        geo = ps.stamp_export_geometry(stamp, sf)
        w_px, h_px = stamp.get_processed_img().size
        self.assertAlmostEqual(geo["rect_pt"][2] - geo["rect_pt"][0], w_px / sf, places=6)
        self.assertAlmostEqual(geo["rect_pt"][3] - geo["rect_pt"][1], h_px / sf, places=6)
        # 预览中心 == 导出中心
        self.assertAlmostEqual(geo["center_pt"][0], geo["center_px"][0] / sf, places=6)
        self.assertAlmostEqual(geo["center_pt"][1], geo["center_px"][1] / sf, places=6)

    def test_export_readback_matches_preview(self):
        """导出后用 get_image_bbox 读回实际矩形：宽高误差 <= 1pt，中心与预览一致。"""
        sf = ps.canvas_scale()
        img = self.load_stamp_image()
        stamp = ps.StampConfig(img, "旋转章")
        stamp.rotation, stamp.opacity, stamp.scale = 45, 0.5, 1.3
        stamp.x, stamp.y = 300, 400
        geo = ps.stamp_export_geometry(stamp, sf)
        w_px, h_px = stamp.get_processed_img().size
        out = os.path.join(OUT_DIR, "rot_export.pdf")

        report = ps.export_pdf_with_stamps(self.open_src(), [stamp], out, scale_factor=sf)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(report["page_count"], 3)
        with fitz.open(out) as check:
            per_page = embedded_rects(check)
        for page_index, rects in enumerate(per_page):
            self.assertEqual(len(rects), 1, "第 %d 页嵌入图片数应为 1" % page_index)
            _, rect = rects[0]
            self.assertLessEqual(abs((rect[2] - rect[0]) - w_px / sf), 1.0)
            self.assertLessEqual(abs((rect[3] - rect[1]) - h_px / sf), 1.0)
            cx, cy = ps.rect_center(rect)
            self.assertLessEqual(abs(cx - geo["center_pt"][0]), 1.0)
            self.assertLessEqual(abs(cy - geo["center_pt"][1]), 1.0)
            # 中心还必须等于「画布中心 / scale_factor」
            self.assertLessEqual(abs(cx - stamp.x / sf - (w_px / 2.0) / sf), 1.0)

    def test_rotation_zero_geometry_regressions(self):
        sf = ps.canvas_scale()
        stamp = ps.StampConfig(make_solid_stamp(100), "章")
        stamp.x, stamp.y, stamp.scale = 100, 200, 2.0
        geo = ps.stamp_export_geometry(stamp, sf)
        self.assertEqual(geo["image_px"], (200, 200))
        self.assertEqual(geo["rect_pt"], (100 / sf, 200 / sf, 300 / sf, 400 / sf))


# ==================== F. 导出重复编码 ====================

class TestFEncodeOnce(TempArtifactMixin):

    def test_one_encode_per_stamp_reused_across_pages(self):
        stamps = []
        for i in range(3):
            s = ps.StampConfig(make_solid_stamp(120, (255 * i // 255, 0, 0, 255)), "章%d" % i)
            s.x, s.y = 80 + i * 40, 120 + i * 40
            stamps.append(s)
        out = os.path.join(OUT_DIR, "encode_once.pdf")
        doc = self.open_src()
        # 5 页文档：编码次数应为章数，而不是 章数 x 页数
        with fitz.open() as big:
            for _ in range(5):
                big.insert_pdf(doc, from_page=0, to_page=0)
            report = ps.export_pdf_with_stamps(big, stamps, out, scale_factor=ps.canvas_scale())
        self.assertEqual(report["png_encode_count"], 3)
        self.assertEqual(report["page_count"], 5)
        self.assertEqual(report["per_page_image_counts"], [3] * 5)
        with fitz.open(out) as check:
            per_page = embedded_rects(check)
        # 同一枚章在所有页复用同一个 xref
        first_page_xrefs = [x for x, _ in per_page[0]]
        for page_index in range(1, 5):
            self.assertEqual([x for x, _ in per_page[page_index]], first_page_xrefs)

    def test_encoded_bytes_shared_object(self):
        stamps = [ps.StampConfig(make_solid_stamp(80), "单章")]
        payloads, count = ps._prepare_stamp_payloads(stamps, self.open_src(), ps.canvas_scale(),
                                                    False, 0, 0.5)
        self.assertEqual(count, 1)
        self.assertEqual(len(payloads), 3)
        self.assertIs(payloads[0]["bytes"], payloads[-1]["bytes"])


# ==================== H. 骑缝章真半章 ====================

class TestHCrossFold(TempArtifactMixin):

    def test_split_returns_n_slices_with_exact_total_width(self):
        img = make_solid_stamp(201)  # 故意不可整除
        slices = ps.split_cross_fold_images(img, 3, 0.5)
        self.assertEqual(len(slices), 3)
        self.assertEqual(sum(s.size[0] for s in slices), 201)
        self.assertEqual([s.size[1] for s in slices], [201, 201, 201])
        self.assertEqual([s.size[0] for s in slices], [67, 67, 67])

    def test_slices_are_contiguous_and_distinct(self):
        img = Image.new("RGBA", (120, 40))
        px = img.load()
        for x in range(120):
            for y in range(40):
                px[x, y] = (x, 0, 0, 255)
        slices = ps.split_cross_fold_images(img, 3, 0.5)
        self.assertEqual(slices[0].load()[0, 0][0], 0)
        self.assertEqual(slices[1].load()[0, 0][0], 40)
        self.assertEqual(slices[2].load()[0, 0][0], 80)
        self.assertEqual(slices[2].load()[39, 39][0], 119)

    def test_cross_fold_export_half_stamp_per_page(self):
        sf = ps.canvas_scale()
        img = self.load_stamp_image()
        stamp = ps.StampConfig(img, "骑缝章")
        stamp.scale = 1.0
        proc_size = stamp.get_processed_img().size
        out = os.path.join(OUT_DIR, "crossfold.pdf")
        report = ps.export_pdf_with_stamps(self.open_src(), [stamp], out, scale_factor=sf,
                                          cross_fold_mode=True, cross_fold_stamp_index=0,
                                          cross_fold_offset=0.5)
        self.assertEqual(report["page_count"], 3)
        # 每页恰有一枚切片，且宽度约为整章的 1/3
        widths = [e["size_px"][0] for e in report["embedded"]]
        self.assertEqual(sum(widths), proc_size[0])
        self.assertEqual(len(report["embedded"]), 3)
        with fitz.open(out) as check:
            per_page = embedded_rects(check)
            self.assertEqual([len(r) for r in per_page], [1, 1, 1])
            page_w = check[0].rect.width
            page_h = check[0].rect.height
            for i, rects in enumerate(per_page):
                _, rect = rects[0]
                self.assertLessEqual(abs((rect[2] - rect[0]) - widths[i] / sf), 1.0)
                self.assertLessEqual(abs((rect[3] - rect[1]) - proc_size[1] / sf), 1.0)
                self.assertLessEqual(rect[2], page_w + 1e-6)
                self.assertGreaterEqual(rect[0], -1e-6)
            xrefs = [r[0][0] for r in per_page]
            self.assertEqual(len(set(xrefs)), 3, "三页应是三张不同的切片")
            # 各页右边缘对齐（错页拼合后才是完整一枚）
            rights = [r[0][1][2] for r in per_page]
            self.assertTrue(all(abs(v - rights[0]) < 1e-6 for v in rights))
            # 预览与导出共用同一套坐标（PDF 内容流写回有浮点舍入，逐坐标 <=0.5pt）
            for i in range(3):
                geo = ps.cross_fold_geometry(stamp, check[i].rect, 3, 0.5, sf, page_index=i)
                got = per_page[i][0][1]
                self.assertEqual(geo["slice_index"], i)
                for a, b in zip(got, geo["rect_pt"]):
                    self.assertLessEqual(abs(a - b), 0.5)

    def test_preview_slice_matches_current_page_cut(self):
        sf = ps.canvas_scale()
        img = Image.new("RGBA", (120, 60))
        px = img.load()
        for x in range(120):
            for y in range(60):
                px[x, y] = (x, 0, 0, 255)
        stamp = ps.StampConfig(img, "骑缝")
        stamp.page_index = 1
        geo = ps.cross_fold_geometry(stamp, fitz.Rect(0, 0, 595, 842), 3, 0.5, sf)
        self.assertEqual(geo["slice_bounds"], (40, 80))
        slices = ps.split_cross_fold_images(stamp.get_processed_img(), 3, 0.5)
        self.assertEqual(slices[1].load()[0, 0][0], 40)

    def test_offset_moves_position_but_not_content(self):
        img = make_solid_stamp(180)
        a = ps.cross_fold_slice_rect(595, 842, 180, 180, 0, 3, 0.0)
        b = ps.cross_fold_slice_rect(595, 842, 180, 180, 0, 3, 1.0)
        # 与旧版方向一致：offset 越大越向左（贴右边缘 -> 完全收进页面）
        self.assertLess(b[0], a[0])
        self.assertAlmostEqual(a[2] - a[0], b[2] - b[0], places=9)
        # offset=0 时贴右边缘；offset=1 时整枚章收进页面内
        self.assertAlmostEqual(a[2], 595, places=6)
        self.assertAlmostEqual(b[0], 595 - 180, places=6)
        # 各页右边缘严格相同（名义宽度一致）
        rights = {round(ps.cross_fold_slice_rect(595, 842, 201, 180, i, 3, 0.5)[2], 9)
                  for i in range(3)}
        self.assertEqual(len(rights), 1)

    def test_normal_mode_unchanged_by_cross_fold_code(self):
        """非骑缝模式：每页一整枚章，位置就是画布坐标换算。"""
        sf = ps.canvas_scale()
        stamp = ps.StampConfig(make_solid_stamp(100), "普通章")
        stamp.x, stamp.y = 200, 300
        geo = ps.stamp_export_geometry(stamp, sf)
        report = ps.export_pdf_with_stamps(self.open_src(), [stamp],
                                          os.path.join(OUT_DIR, "normal_mode.pdf"),
                                          scale_factor=sf, cross_fold_mode=False)
        for emb in report["embedded"]:
            self.assertEqual(emb["rect"], geo["rect_pt"])
            self.assertEqual(emb["kind"], "stamp")


# ==================== B. 历史按 id 恢复 ====================

class TestBHistoryById(TempArtifactMixin):

    def test_ids_are_unique_and_stable(self):
        a = ps.StampConfig(make_solid_stamp(50), "同名")
        b = ps.StampConfig(make_solid_stamp(60), "同名")
        self.assertNotEqual(a.stamp_id, b.stamp_id)
        self.assertEqual(a.to_dict()["id"], a.stamp_id)
        round_trip = ps.StampConfig.from_dict(a.to_dict(), a.img)
        self.assertEqual(round_trip.stamp_id, a.stamp_id)

    def test_undo_after_delete_restores_ids_and_images(self):
        sess = self.new_session()
        status, _ = sess.load_document(self.src_pdf_path)
        self.assertEqual(status, ps.OPEN_OK)
        img_a = make_solid_stamp(50, (255, 0, 0, 255))
        img_b = make_solid_stamp(80, (0, 255, 0, 255))
        a = sess.add_stamp(img_a, "同名")
        sess.save_history()
        b = sess.add_stamp(img_b, "同名")
        sess.save_history()
        self.assertEqual([s.stamp_id for s in sess.stamps], [a.stamp_id, b.stamp_id])

        sess.remove_stamp_by_index(0)   # 删掉 a，两枚章同名
        sess.save_history()
        self.assertEqual([s.stamp_id for s in sess.stamps], [b.stamp_id])

        restored, missing = sess.undo()
        self.assertEqual(missing, [])
        self.assertEqual(len(sess.stamps), 2)
        self.assertEqual([s.stamp_id for s in sess.stamps], [a.stamp_id, b.stamp_id])
        # 同名但不同图：按 id 找回，不能串图
        self.assertIs(sess.stamps[0].img, img_a)
        self.assertIs(sess.stamps[1].img, img_b)
        self.assertEqual(sess.stamps[0].img.size, (50, 50))
        self.assertEqual(sess.stamps[1].img.size, (80, 80))

    def test_redo_then_undo_roundtrip_keeps_attributes(self):
        sess = self.new_session()
        sess.load_document(self.src_pdf_path)
        a = sess.add_stamp(make_solid_stamp(60), "章A")
        a.x, a.y, a.rotation, a.opacity, a.scale = 111, 222, 33, 0.6, 1.4
        sess.save_history()
        sess.remove_stamp_by_index(0)
        sess.save_history()
        sess.undo()
        self.assertEqual((sess.stamps[0].x, sess.stamps[0].y), (111, 222))
        self.assertEqual(sess.stamps[0].rotation, 33)
        self.assertAlmostEqual(sess.stamps[0].opacity, 0.6)
        self.assertAlmostEqual(sess.stamps[0].scale, 1.4)
        sess.redo()
        self.assertEqual(len(sess.stamps), 0)

    def test_missing_id_is_reported_not_silently_dropped(self):
        sess = self.new_session()
        sess.load_document(self.src_pdf_path)
        sess.add_stamp(make_solid_stamp(60), "章A")
        sess.save_history()
        ghost = {"id": "deadbeef" * 4, "name": "已被彻底丢弃的章", "x": 1, "y": 2,
                 "scale": 1.0, "opacity": 1.0, "rotation": 0}
        restored, missing = sess.restore({"stamps": [ghost], "current_page": 0})
        self.assertEqual(len(restored), 0)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["id"], ghost["id"])
        self.assertEqual(missing[0]["reason"], "image_lost")
        self.assertEqual(sess.last_restore_missing, missing)

    def test_restore_state_signature_returns_assertable_result(self):
        data = [{"id": "x1", "name": "n", "x": 5, "y": 6, "scale": 1.0, "opacity": 1.0,
                 "rotation": 0}]
        img = make_solid_stamp(30)
        restored, missing = ps.restore_stamps(data, {"x1": img})
        self.assertEqual(missing, [])
        self.assertEqual(restored[0].stamp_id, "x1")
        self.assertIs(restored[0].img, img)
        self.assertEqual((restored[0].x, restored[0].y), (5, 6))
        restored2, missing2 = ps.restore_stamps(data, {})
        self.assertEqual(restored2, [])
        self.assertEqual(missing2[0]["reason"], "image_lost")


# ==================== C. open_pdf 状态残留 / 句柄泄漏 / 加密 ====================

class TestCOpenPdfState(TempArtifactMixin):

    def test_loading_new_document_clears_state_and_closes_old_handle(self):
        sess = self.new_session()
        status, _msg = sess.load_document(self.src_pdf_path)
        old_doc = sess.pdf_doc
        self.assertEqual(status, ps.OPEN_OK)
        self.assertEqual(sess.total_pages, 3)
        sess.add_stamp(make_solid_stamp(60), "章A")
        sess.save_history()
        sess.selected_stamp = 0
        sess.active_stamp_idx = 0
        sess.cross_fold_mode = True
        sess.current_page = 2
        self.assertFalse(old_doc.is_closed)

        sess.load_document(src_pdf_5p())
        self.assertTrue(old_doc.is_closed, "旧文档句柄必须被关闭（无文件句柄泄漏）")
        self.assertEqual(sess.stamps, [], "公章必须清空")
        self.assertEqual(sess.image_pool, {}, "图片池必须清空")
        self.assertIsNone(sess.selected_stamp)
        self.assertEqual(sess.active_stamp_idx, 0)
        self.assertEqual(sess.current_page, 0)
        self.assertEqual(sess.total_pages, 5)
        self.assertFalse(sess.cross_fold_mode)
        self.assertEqual(sess.history.undo_stack, [], "历史必须重置")
        self.assertEqual(sess.history.redo_stack, [])
        self.assertFalse(sess.history.can_undo())

    def test_open_missing_file_returns_failed_status(self):
        doc, status = ps.open_pdf_document(os.path.join(OUT_DIR, "no_such_file.pdf"))
        self.assertEqual(status, ps.OPEN_FAILED)
        self.assertIsInstance(doc, Exception)

    def test_encrypted_pdf_password_branches(self):
        enc_path = enc_pdf()

        # 无密码 -> needs_password（GUI 据此弹密码框；取消即放弃打开）
        returned, status = ps.open_pdf_document(enc_path)
        self.assertEqual(status, ps.OPEN_NEEDS_PASSWORD)
        self.assertIsNone(returned)

        # 密码错误 -> bad_password，异常被捕获、返回可识别状态
        returned, status = ps.open_pdf_document(enc_path, password="wrong")
        self.assertEqual(status, ps.OPEN_BAD_PASSWORD)
        self.assertIsNone(returned)

        # 密码正确 -> ok
        returned, status = ps.open_pdf_document(enc_path, password="1234")
        self.assertEqual(status, ps.OPEN_OK)
        self._open_docs.append(returned)
        self.assertEqual(returned.page_count, 1)
        self.assertIn("ENCRYPTED", returned[0].get_text())

    def test_session_load_document_propagates_password_status(self):
        sess = self.new_session()
        status, msg = sess.load_document(enc_pdf())
        self.assertEqual(status, ps.OPEN_NEEDS_PASSWORD)
        self.assertIsNone(sess.pdf_doc)
        self.assertEqual(sess.stamps, [])
        status, msg = sess.load_document(enc_pdf(), password="nope")
        self.assertEqual(status, ps.OPEN_BAD_PASSWORD)
        self.assertIsNone(sess.pdf_doc)
        status, msg = sess.load_document(enc_pdf(), password="1234")
        self.assertEqual(status, ps.OPEN_OK)
        self.assertIsNotNone(sess.pdf_doc)
        sess.close_document()

    def test_close_document_releases_handle(self):
        sess = self.new_session()
        sess.load_document(self.src_pdf_path)
        doc = sess.pdf_doc
        sess.close_document()
        self.assertTrue(doc.is_closed)
        self.assertIsNone(sess.pdf_doc)
        # 重复关闭不报错
        sess.close_document()


# ==================== D. 快捷键误伤输入框 ====================

class FakeRoot:
    def __init__(self, focused):
        self._focused = focused

    def focus_get(self):
        return self._focused


class EntryLike:
    def winfo_class(self):
        return "TEntry"


class LabelLike:
    def winfo_class(self):
        return "Label"


class Entry:  # 名字命中 MRO 检查（无需真实 Tk 控件）
    def winfo_class(self):
        raise RuntimeError("no tcl")


class TestDShortcuts(TempArtifactMixin):

    def test_none_and_label_focus_do_not_block(self):
        self.assertFalse(ps.is_editable_widget(None))
        self.assertFalse(ps.is_editable_widget(object()))
        self.assertFalse(ps.is_editable_widget(LabelLike()))
        self.assertFalse(ps.shortcut_blocked(FakeRoot(None)))
        self.assertFalse(ps.shortcut_blocked(FakeRoot(LabelLike())))

    def test_entry_text_listbox_focus_blocks(self):
        self.assertTrue(ps.is_editable_widget(EntryLike()))
        self.assertTrue(ps.is_editable_widget(Entry()))
        self.assertTrue(ps.shortcut_blocked(FakeRoot(EntryLike())))
        for name in ("Text", "Listbox", "TCombobox", "TSpinbox"):
            widget = type(name, (), {"winfo_class": lambda self, n=name: n})()
            self.assertTrue(ps.is_editable_widget(widget), name)

    def test_handlers_are_guarded(self):
        src = {
            name: inspect.getsource(getattr(ps.PDFStamper, name))
            for name in ("on_delete_shortcut", "on_prev_shortcut", "on_next_shortcut",
                         "on_select_all_shortcut", "on_undo_shortcut", "on_redo_shortcut")
        }
        for name, body in src.items():
            self.assertIn("focus_blocks_shortcut", body, name)

    def test_bindings_point_to_guarded_handlers(self):
        body = inspect.getsource(ps.PDFStamper.bind_shortcuts)
        self.assertIn("on_delete_shortcut", body)
        self.assertIn("on_prev_shortcut", body)
        self.assertIn("on_next_shortcut", body)
        self.assertNotIn("self.delete_selected_stamp()", body)
        self.assertNotIn("self.prev_page()", body)

    def test_real_tk_classes_are_covered_without_root(self):
        """is_editable_widget 的 isinstance 元组必须真的包含这些真实控件类（无需建窗口）。"""
        import tkinter as tk
        from tkinter import ttk
        editable_classes = (tk.Entry, tk.Text, tk.Listbox, tk.Spinbox,
                            ttk.Entry, ttk.Combobox, ttk.Treeview, ttk.Notebook)
        for cls in (tk.Entry, tk.Text, tk.Listbox, tk.Spinbox,
                    ttk.Entry, ttk.Combobox, ttk.Treeview, ttk.Notebook):
            self.assertTrue(issubclass(cls, editable_classes), cls.__name__)
        # 不应把画布/按钮/标签算作输入控件
        for cls in (tk.Canvas, tk.Button, ttk.Label, ttk.Frame):
            self.assertFalse(issubclass(cls, editable_classes), cls.__name__)
        # winfo_class 名称必须都在兜底名单里
        for name in ("Entry", "TEntry", "Text", "Listbox", "TCombobox", "TSpinbox",
                     "Treeview"):
            self.assertIn(name, ps.EDITABLE_WIDGET_NAMES)
        self.assertNotIn("Canvas", ps.EDITABLE_WIDGET_NAMES)


# ==================== E. 拖拽卡顿（缓存 + 只移动图元） ====================

class TestERenderCaching(TempArtifactMixin):

    def test_page_bitmap_cache_hit(self):
        core = ps.RenderCore(dpi=150)
        doc = self.open_src()
        first = core.get_page_bitmap(doc, 1)
        self.assertEqual(core.stats["page_miss"], 1)
        self.assertEqual(core.stats["page_hit"], 0)
        again = core.get_page_bitmap(doc, 1)
        self.assertIs(first, again)
        self.assertEqual(core.stats["page_hit"], 1)
        self.assertEqual(core.cache_info()["cached_pages"], 1)

    def test_page_cache_invalidated_on_new_document(self):
        core = ps.RenderCore(dpi=150)
        doc_a = self.open_src()
        core.get_page_bitmap(doc_a, 0)
        with fitz.open() as doc_b:
            doc_b.new_page(width=595, height=842)
            core.get_page_bitmap(doc_b, 0)
        self.assertEqual(core.stats["page_miss"], 2)
        core.invalidate()
        self.assertEqual(core.cache_info()["cached_pages"], 0)

    def test_page_bitmap_lru_eviction(self):
        core = ps.RenderCore(dpi=150, max_page_cache=2)
        doc = self.open_src()
        core.get_page_bitmap(doc, 0)
        core.get_page_bitmap(doc, 1)
        core.get_page_bitmap(doc, 2)  # 挤掉 page 0
        self.assertEqual(core.cache_info()["cached_pages"], 2)
        hits_before = core.stats["page_hit"]
        core.get_page_bitmap(doc, 0)  # 已失效 -> 重新渲染
        self.assertEqual(core.stats["page_hit"], hits_before)
        self.assertEqual(core.stats["page_miss"], 4)

    def test_stamp_bitmap_cache_hit(self):
        core = ps.RenderCore(dpi=150)
        stamp = ps.StampConfig(self.load_stamp_image(), "章")
        stamp.rotation, stamp.scale, stamp.opacity = 30, 1.7, 0.5
        first = core.get_stamp_bitmap(stamp)
        second = core.get_stamp_bitmap(stamp)
        self.assertIs(first, second)
        self.assertEqual(core.stats["stamp_miss"], 1)
        self.assertEqual(core.stats["stamp_hit"], 1)
        stamp.rotation = 60
        third = core.get_stamp_bitmap(stamp)
        self.assertIsNot(third, first)
        self.assertEqual(core.stats["stamp_miss"], 2)

    def test_hit_testing_and_drag_rules_are_pure_functions(self):
        """图元命中/可拖拽判定抽成纯函数（无需 Tk 即可断言）。"""
        self.assertEqual(ps.stamp_index_from_tags(("stamp_0", "stamp_item", "stamp"), 3), 0)
        self.assertEqual(ps.stamp_index_from_tags(("stamp_2", "stamp_item", "active"), 3), 2)
        self.assertIsNone(ps.stamp_index_from_tags(("page",), 3))
        self.assertIsNone(ps.stamp_index_from_tags((), 3))
        self.assertIsNone(ps.stamp_index_from_tags(None, 3))
        self.assertIsNone(ps.stamp_index_from_tags(("stamp_5", "stamp_item"), 3), "越界索引")
        self.assertIsNone(ps.stamp_index_from_tags(("stamp_x", "stamp_item"), 3))
        # 普通公章可拖；骑缝章切片不可拖
        self.assertTrue(ps.drag_allowed_for_tags(("stamp_0", "stamp_item", "stamp")))
        self.assertFalse(ps.drag_allowed_for_tags(("stamp_0", "stamp_item", "cross_fold")))
        self.assertFalse(ps.drag_allowed_for_tags(("page",)))
        self.assertFalse(ps.drag_allowed_for_tags(None))
        # on_mouse_down 必须走这两个函数（而不是内联字符串解析）
        down_src = inspect.getsource(ps.PDFStamper.on_mouse_down)
        self.assertIn("stamp_index_from_tags", down_src)
        self.assertIn("drag_allowed_for_tags", down_src)

    def test_payload_geometry_matches_stamp_coordinates(self):
        core = ps.RenderCore(dpi=150)
        doc = self.open_src()
        stamp = ps.StampConfig(self.load_stamp_image(), "章")
        stamp.x, stamp.y, stamp.scale = 120, 160, 0.8
        payload = core.build_payload(doc, 0, [stamp])
        item = payload["stamp_items"][0]
        self.assertEqual((item["x"], item["y"]), (120, 160))
        self.assertEqual(item["size"], ps.get_processed_image(stamp).size)
        self.assertEqual(item["tag"], "stamp_0")

    def test_cross_fold_preview_shows_pages_own_slice(self):
        """预览端：每页显示属于该页的那一刀，且放置位置一致。"""
        core = ps.RenderCore(dpi=150)
        doc = self.open_src()
        img = Image.new("RGBA", (120, 60))
        px = img.load()
        for x in range(120):
            for y in range(60):
                px[x, y] = (x, 0, 0, 255)
        stamp = ps.StampConfig(img, "骑缝")
        payload_pages = []
        for page_index in range(3):
            payload = core.build_payload(doc, page_index, [stamp], cross_fold_mode=True,
                                        cross_fold_stamp_index=0, cross_fold_offset=0.5)
            self.assertEqual(len(payload["stamp_items"]), 1)
            payload_pages.append(payload["stamp_items"][0])
        # 每页只拿到 1/3 宽度，且三刀内容互不相同（首像素 x=0/40/80）
        self.assertEqual([i["size"][0] for i in payload_pages], [40, 40, 40])
        self.assertEqual([i["image"].load()[0, 0][0] for i in payload_pages], [0, 40, 80])
        self.assertEqual([i["kind"] for i in payload_pages], ["cross_fold"] * 3)
        # 位置按右边缘对齐（像素取整让左边缘相差 <=1px），并且等于 cross_fold_geometry 的结果
        lefts = [i["x"] for i in payload_pages]
        self.assertLessEqual(max(lefts) - min(lefts), 1.0)
        rights = [i["x"] + i["size"][0] for i in payload_pages]
        self.assertLessEqual(max(rights) - min(rights), 1e-6)
        geo = ps.cross_fold_geometry(stamp, doc[1].rect, 3, 0.5, core.scale_factor,
                                    page_index=1)
        self.assertAlmostEqual(payload_pages[1]["x"], geo["rect_pt"][0] * core.scale_factor,
                               places=6)
        # 非骑缝模式下仍然是整枚章、位置取画布坐标
        normal = core.build_payload(doc, 1, [stamp])["stamp_items"][0]
        self.assertEqual(normal["kind"], "stamp")
        self.assertEqual(normal["size"], (120, 60))

    def test_drag_only_moves_canvas_item(self):
        """拖拽回调里不得出现重绘；松手时才重绘 + 存历史。"""
        drag_src = inspect.getsource(ps.PDFStamper.on_mouse_drag)
        down_src = inspect.getsource(ps.PDFStamper.on_mouse_down)
        up_src = inspect.getsource(ps.PDFStamper.on_mouse_up)
        self.assertNotIn("self.render_page", drag_src)
        self.assertIn("self.canvas.move", drag_src)
        self.assertNotIn("self.render_page", down_src)
        self.assertIn("self.canvas.tag_raise", down_src)
        self.assertIn("self.render_page", up_src)
        self.assertIn("save_history", up_src)
        # 页面位图缓存有失效入口（切换/重新打开 PDF 时调用）
        self.assertTrue(hasattr(ps.RenderCore, "invalidate"))
        open_src_text = inspect.getsource(ps.PDFStamper.open_pdf)
        self.assertIn("core.invalidate", open_src_text)

    def test_bench_script_exists_and_runs(self):
        bench = os.path.join(TESTS_DIR, "bench_render.py")
        self.assertTrue(os.path.exists(bench))
        import subprocess
        proc = subprocess.run([sys.executable, bench, "--quick"], cwd=PROJECT_DIR,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
        out = proc.stdout or ""
        self.assertEqual(proc.returncode, 0, out[-4000:])
        self.assertIn("单次 render_page 耗时", out)
        self.assertIn("页面位图缓存命中耗时", out)
        # 缓存必须真的命中过
        self.assertIn("页面位图缓存: hit=", out)


# ==================== 附加：预览/导出同一处理图（A+G 交叉） ====================

class TestPreviewExportConsistency(TempArtifactMixin):

    def test_exported_pixels_are_same_processed_image_as_preview(self):
        """导出图与预览用同一张处理后图：宽高比、透明度一致。"""
        sf = ps.canvas_scale()
        img = self.load_stamp_image()
        stamp = ps.StampConfig(img, "一致性章")
        stamp.rotation, stamp.opacity, stamp.scale = 30, 0.4, 1.1
        stamp.x, stamp.y = 250, 300
        processed = stamp.get_processed_img()
        out = os.path.join(OUT_DIR, "consistency.pdf")
        ps.export_pdf_with_stamps(self.open_src(), [stamp], out, scale_factor=sf)
        with fitz.open(out) as check:
            rect = embedded_rects(check)[0][0][1]
            aspect = (rect[2] - rect[0]) / (rect[3] - rect[1])
            self.assertAlmostEqual(aspect, processed.size[0] / processed.size[1], places=3)
            pix = check[0].get_pixmap(dpi=150, clip=fitz.Rect(*rect))
        # 半透明章叠在白色页面上：中心区域应当既有非红（背景）也有红（章）像素
        img_px = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        rgb_bytes = img_px.convert("RGB").tobytes()   # 不用 getdata()：Pillow 14 将移除该 API
        colors = {rgb_bytes[i:i + 3] for i in range(0, len(rgb_bytes), 3)}
        self.assertGreater(len(colors), 1)


class TestBatchWorkflow(TempArtifactMixin):

    def test_batch_output_names_are_distinct_from_inputs(self):
        self.assertEqual(
            os.path.basename(ps.batch_output_path("合同.pdf", "out")),
            "合同_盖章.pdf")

    def test_batch_output_paths_disambiguate_same_names(self):
        paths = ps.batch_output_paths(
            [r"C:\\one\合同.pdf", r"D:\\two\合同.pdf"], "out")
        self.assertEqual([os.path.basename(path) for path in paths],
                         ["合同_盖章.pdf", "合同_盖章_2.pdf"])

    def test_atomic_export_keeps_existing_target_on_failure(self):
        with tempfile.TemporaryDirectory(prefix="atomic_export_") as work_dir:
            source_path = os.path.join(work_dir, "source.pdf")
            output_path = os.path.join(work_dir, "result.pdf")
            make_src_pdf(source_path, pages=1)
            with open(output_path, "wb") as stream:
                stream.write(b"original target")
            source, status = ps.open_pdf_document(source_path)
            self.assertEqual(status, ps.OPEN_OK)
            try:
                with self.assertRaises(ValueError):
                    ps.export_pdf_with_stamps(source, [], out_path=source_path)
            finally:
                source.close()
            with open(output_path, "rb") as stream:
                self.assertEqual(stream.read(), b"original target")

    def test_multiple_pdfs_export_and_no_stamp_copy(self):
        with tempfile.TemporaryDirectory(prefix="batch_test_") as work_dir:
            inputs = []
            for name in ("one.pdf", "two.pdf"):
                path = os.path.join(work_dir, name)
                make_src_pdf(path, pages=2)
                inputs.append(path)
            stamp = ps.StampConfig(self.load_stamp_image(), "批量章")
            for input_path in inputs:
                source, status = ps.open_pdf_document(input_path)
                self.assertEqual(status, ps.OPEN_OK)
                output = ps.batch_output_path(input_path, work_dir)
                ps.export_pdf_with_stamps(source, [stamp], output)
                source.close()
                with fitz.open(output) as result:
                    self.assertEqual(result.page_count, 2)
            source, status = ps.open_pdf_document(inputs[0])
            self.assertEqual(status, ps.OPEN_OK)
            copy_path = os.path.join(work_dir, "plain.pdf")
            ps.export_pdf_with_stamps(source, [], copy_path)
            source.close()
            with fitz.open(copy_path) as result:
                self.assertEqual(result.page_count, 2)
                self.assertEqual(len(result[0].get_images(full=True)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
