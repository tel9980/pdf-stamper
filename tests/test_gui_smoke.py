#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI 装配冒烟测试（可选，默认跳过）

默认不运行：它会真的创建一个 Tk root（withdraw 隐藏，不进入 mainloop），
在无显示环境下会自动 SKIP。手动验证 GUI 层与纯逻辑层的接线是否正常：

    cd E:/AI
    PDF_STAMPER_GUI_SMOKE=1 python -m unittest tests.test_gui_smoke -v

检查点：
  * PDFStamper(root) 能在 v3.1 结构下构建全部控件（session 代理属性可读写）
  * render_page() 走真实 ImageTk.PhotoImage 路径 + 页面/公章位图缓存
  * 拖拽事件序列（Down -> Motion -> Up）只移动图元，坐标最终落到 StampConfig
  * 快捷键焦点保护在真实 ttk.Entry 聚焦时生效（D 项）
  * 历史 undo/redo 在 GUI 侧能恢复公章（B 项）
  * export_pdf 走真实导出（messagebox/filedialog 全部打桩，不弹窗）
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENABLED = os.environ.get("PDF_STAMPER_GUI_SMOKE") == "1"


class FakeEvent:
    def __init__(self, x=0, y=0, delta=0, num=None):
        self.x = x
        self.y = y
        self.delta = delta
        self.num = num


@unittest.skipUnless(ENABLED, "设置 PDF_STAMPER_GUI_SMOKE=1 才运行（会创建隐藏的 Tk 窗口）")
class TestGuiSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:  # PyMuPDF >= 1.24 提供 pymupdf 别名；用别名可避免 fitz 的弃用警告
            import pymupdf as fitz
        except ImportError:  # 旧版本（requirements 下限 1.23）只有 fitz
            import fitz
        from PIL import Image
        import pdf_stamper as ps
        import tkinter as tk
        cls.ps = ps
        cls.fitz = fitz
        cls.Image = Image
        cls.tk = tk
        try:  # 先确认本环境能建 Tk root，否则整个类 SKIP
            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:
            raise unittest.SkipTest("无法创建 Tk root: %s" % exc)
        # 打桩：任何弹窗都记录而不显示
        cls.dialogs = []
        for name in ("showinfo", "showwarning", "showerror", "askyesno"):
            def make(n):
                def stub(*a, **kw):
                    cls.dialogs.append((n, a))
                    return True
                return stub
            setattr(ps.messagebox, name, make(name))

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="gui_smoke_")
        self.root = self.tk.Tk()
        self.root.withdraw()          # 隐藏窗口：不显示、也不进 mainloop
        self.app = self.ps.PDFStamper(self.root)
        self.pdf_path = self.ps.make_test_pdf(os.path.join(self.tmp, "gui.pdf"), pages=3)
        self.stamp_path = self.ps.make_test_stamp(os.path.join(self.tmp, "stamp.png"), size=120)

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------------- 用例
    def test_app_builds_and_proxy_properties(self):
        app = self.app
        self.assertEqual(app.total_pages, 0)
        self.assertEqual(app.stamps, [])
        self.assertIs(app.session.pdf_doc, None)
        self.assertEqual(app.file_mode_combo["values"], ("单文件模式", "批量文件模式"))
        self.assertIn("不加印章", app.seal_mode_combo["values"])
        self.assertIn("全部页面加印章", app.seal_mode_combo["values"])
        self.assertEqual(app.progress_var.get(), 0.0)
        app.active_stamp_idx = 3
        self.assertEqual(app.session.active_stamp_idx, 3)
        app.cross_fold_offset = 0.25
        self.assertAlmostEqual(app.session.cross_fold_offset, 0.25)

    def test_open_render_and_drag(self):
        app = self.app
        status = app.open_pdf(self.pdf_path)
        self.assertEqual(status, self.ps.OPEN_OK)
        self.assertEqual(app.total_pages, 3)
        stamp = app.load_stamp(self.stamp_path)
        self.assertIsNotNone(stamp)
        self.assertEqual(len(app.stamps), 1)
        # 找到公章在画布上的实际位置
        items = app.canvas.find_withtag("stamp_0")
        self.assertTrue(items)
        coords = app.canvas.coords(items[0])
        app.on_mouse_down(FakeEvent(int(coords[0]) + 5, int(coords[1]) + 5))
        self.assertEqual(app.selected_stamp, 0)
        app.on_mouse_drag(FakeEvent(int(coords[0]) + 5 + 60, int(coords[1]) + 5 + 40))
        app.on_mouse_drag(FakeEvent(int(coords[0]) + 5 + 90, int(coords[1]) + 5 + 70))
        app.on_mouse_up(FakeEvent(int(coords[0]) + 5 + 90, int(coords[1]) + 5 + 70))
        moved = app.stamps[0]
        self.assertAlmostEqual(moved.x, coords[0] + 90, delta=1.5)
        self.assertAlmostEqual(moved.y, coords[1] + 70, delta=1.5)
        # 拖拽后重绘的图元位置应与模型一致
        after = app.canvas.coords(app.canvas.find_withtag("stamp_0")[0])
        self.assertAlmostEqual(after[0], moved.x, delta=0.5)
        # 缓存：同一页二次渲染不应重新渲染页面位图
        misses_before = app.core.stats["page_miss"]
        app.render_page()
        app.render_page()
        self.assertEqual(app.core.stats["page_miss"], misses_before)
        self.assertGreaterEqual(app.core.stats["page_hit"], 2)

    def test_history_via_gui_and_same_name(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        a = app.add_stamp_image(self.Image.new("RGBA", (60, 60), (255, 0, 0, 255)), "同名")
        b = app.add_stamp_image(self.Image.new("RGBA", (90, 90), (0, 255, 0, 255)), "同名")
        app.delete_stamp_at(0)
        self.assertEqual([s.stamp_id for s in app.stamps], [b.stamp_id])
        self.assertTrue(app.undo())
        self.assertEqual([s.stamp_id for s in app.stamps], [a.stamp_id, b.stamp_id])
        self.assertIs(app.stamps[0].img, a.img)
        self.assertEqual(app.stamps[0].img.size, (60, 60))
        self.assertEqual(app.session.last_restore_missing, [])
        self.assertTrue(app.redo())
        self.assertEqual(len(app.stamps), 1)

    def test_shortcut_guard_with_real_entry(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (60, 60), (255, 0, 0, 255)), "章")
        app.selected_stamp = 0
        self.assertEqual(len(app.stamps), 1)
        # 真实控件类型判定（不依赖 OS 焦点，稳定可断言）
        self.assertTrue(self.is_editable(app.goto_entry), "ttk.Entry 应被判为可编辑控件")
        self.assertTrue(self.is_editable(app.stamp_listbox), "Listbox 应被判为吞键控件")
        self.assertFalse(self.is_editable(app.canvas), "Canvas 不应拦截全局快捷键")

        # 模拟「焦点在输入框」：handler 必须直接返回，不动数据
        before = len(app.stamps)
        page_before = app.current_page
        original = app.focus_blocks_shortcut
        app.focus_blocks_shortcut = lambda: True
        try:
            app.on_delete_shortcut(FakeEvent())
            app.on_next_shortcut(FakeEvent())
            app.on_prev_shortcut(FakeEvent())
            app.on_select_all_shortcut(FakeEvent())
            app.on_undo_shortcut(FakeEvent())
        finally:
            app.focus_blocks_shortcut = original
        self.assertEqual(len(app.stamps), before, "输入框里按 Delete 不该删章")
        self.assertEqual(app.current_page, page_before, "输入框里按左右方向键不该翻页")
        self.assertEqual(app.goto_entry.get(), "")

        # 真实 OS 焦点（窗口被 withdraw 时 Tk 可能不把焦点交给子控件 -> 只记录不失败）
        app.goto_entry.focus_force()
        app.root.update()
        focused = app.root.focus_get()
        if self.is_editable(focused):
            self.assertTrue(app.focus_blocks_shortcut(), "真实聚焦 Entry 应拦截快捷键")
        else:
            print("  [note] 隐藏窗口下 focus 未落到 Entry（实测 %r），跳过 OS 焦点分支" % (focused,))

        # 焦点回到画布：Delete 应删除选中公章、右方向键应翻页
        app.canvas.focus_force()
        app.root.update()
        if not app.focus_blocks_shortcut():
            app.selected_stamp = 0
            app.on_delete_shortcut(FakeEvent())
            self.assertEqual(len(app.stamps), 0, "画布上按 Delete 应删除选中公章")
            app.on_next_shortcut(FakeEvent())
            self.assertEqual(app.current_page, page_before + 1, "画布上按右方向键应当翻页")

    def is_editable(self, widget):
        return self.ps.is_editable_widget(widget)

    def test_cross_fold_preview_and_export(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (120, 120), (255, 0, 0, 200)), "骑缝")
        app.toggle_cross_fold()
        self.assertTrue(app.cross_fold_mode)
        payload = app.render_page()
        item = payload["stamp_items"][0]
        self.assertEqual(item["kind"], "cross_fold")
        self.assertEqual(item["size"][1], 120)
        self.assertLess(item["size"][0], 120)
        out = os.path.join(self.tmp, "gui_crossfold.pdf")
        report = app.export_pdf(out)
        self.assertIsNotNone(report)
        self.assertEqual(report["per_page_image_counts"], [1, 1, 1])
        # 骑缝模式下位置仍由几何函数给出
        geo = self.ps.cross_fold_geometry(app.stamps[0], self.fitz.Rect(0, 0, 595, 842),
                                         3, app.cross_fold_offset, app.scale_factor)
        self.assertAlmostEqual(item["x"], geo["rect_pt"][0] * app.scale_factor, delta=0.6)

    def test_cross_fold_keeps_other_stamp_normal_when_selection_changes(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (120, 120), (255, 0, 0, 200)), "骑缝")
        app.add_stamp_image(self.Image.new("RGBA", (80, 80), (0, 0, 255, 200)), "普通")

        app.active_stamp_idx = 0
        app.toggle_cross_fold()
        app.active_stamp_idx = 1
        payload = app.render_page()

        self.assertEqual([item["kind"] for item in payload["stamp_items"]],
                         ["cross_fold", "stamp"])
        self.assertEqual(payload["stamp_items"][1]["size"], (80, 80))

    def test_rotation_opacity_preview_matches_export(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        stamp = app.add_stamp_image(self.Image.open(self.stamp_path).convert("RGBA"), "旋转章")
        stamp.rotation, stamp.opacity, stamp.scale = 45, 0.5, 1.3
        stamp.x, stamp.y = 200, 260
        payload = app.render_page()
        item = payload["stamp_items"][0]
        self.assertEqual(item["size"], stamp.get_processed_img().size)
        out = os.path.join(self.tmp, "gui_rot.pdf")
        report = app.export_pdf(out)
        emb = report["embedded"][0]
        self.assertAlmostEqual(emb["rect"][2] - emb["rect"][0],
                              item["size"][0] / app.scale_factor, delta=0.5)
        self.assertAlmostEqual(emb["center_pt"][0], (item["x"] + item["size"][0] / 2.0)
                              / app.scale_factor, delta=0.5)
        self.assertEqual(len([d for d in self.dialogs if d[0] == "showerror"]), 0,
                         "导出路径不应报错（旧版 send_to_back 已修复）")

    def test_open_second_pdf_resets_gui_state(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        first_doc = app.pdf_doc
        app.add_stamp_image(self.Image.new("RGBA", (60, 60), (0, 0, 255, 255)), "章")
        app.current_page = 2
        app.selected_stamp = 0
        app.cross_fold_mode = True
        app.open_pdf(self.pdf_path)
        self.assertTrue(first_doc.is_closed)
        self.assertEqual(app.stamps, [])
        self.assertEqual(app.current_page, 0)
        self.assertIsNone(app.selected_stamp)
        self.assertFalse(app.cross_fold_mode)
        self.assertEqual(app.history.undo_stack, [])
        # 画布只剩新文档的页面位图，不应残留任何公章图元
        self.assertEqual(app.canvas.find_withtag("stamp_item"), ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
