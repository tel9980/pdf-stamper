#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI 装配冒烟测试（可选，默认跳过）

默认不运行：它会真的创建一个 Tk root（withdraw 隐藏，不进入 mainloop），
在无显示环境下会自动 SKIP。手动验证 GUI 层与纯逻辑层的接线是否正常：

    cd E:/AI
    PDF_STAMPER_GUI_SMOKE=1 python -m unittest tests.test_gui_smoke -v

检查点：
  * PDFStamper(root) 能在 v3.3 结构下构建全部控件（session 代理属性可读写）
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

    def export_and_wait(self, *args, **kwargs):
        """
        导出现在是后台任务：启动后泵一下事件循环直到它结束，再把 report 取出来。

        走的是与「盖章并导出PDF」按钮完全相同的路径（_begin_export -> _poll_export），
        因此这里能真实覆盖线程调度、事件回传与收尾复位。
        """
        job = self.app.export_pdf(*args, **kwargs)
        self.assertIsNotNone(job, "导出未启动（参数校验失败？）")
        self.app.wait_for_export(job)
        self.assertFalse(self.app._busy_exporting(), "任务结束后应复位为「不在导出」")
        self.assertIsNone(job.error, "导出抛异常: %r" % (job.error,))
        self.assertFalse(job.cancelled_flag, "导出被意外取消")
        return job.result

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
        report = self.export_and_wait(out)
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

    def test_cross_fold_copy_and_page_scope_are_independent(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        source = app.add_stamp_image(self.Image.new("RGBA", (100, 100), (255, 0, 0, 220)), "公章")
        other = app.add_stamp_image(self.Image.new("RGBA", (80, 80), (0, 0, 255, 220)), "另一枚")
        app.active_stamp_idx = 0
        app.seal_mode_var.set("首页加印章")
        app.on_seal_mode_change()
        self.assertEqual(source.page_scope, "first")
        self.assertEqual(other.page_scope, "all")

        source.x, source.y = 150, 180
        source.scale = 1.2
        fold_copy = app.add_cross_fold_copy()
        self.assertIsNot(fold_copy, source)
        self.assertIsNot(fold_copy.img, source.img)
        self.assertTrue(fold_copy.is_cross_fold)
        self.assertEqual(fold_copy.scale, source.scale)
        self.assertEqual(fold_copy.page_scope, "all")

        app.scale_var.set(1.7)
        app.on_scale_change("1.7")
        app.offset_var.set(0.8)
        app.on_offset_change("0.8")
        self.assertEqual(source.scale, 1.2)
        self.assertEqual(source.cross_fold_offset, 0.5)
        self.assertEqual(fold_copy.scale, 1.7)
        self.assertEqual(fold_copy.cross_fold_offset, 0.8)

        app.seal_mode_var.set("尾页加印章")
        app.on_seal_mode_change()
        self.assertEqual(fold_copy.page_scope, "last")
        self.assertEqual(source.page_scope, "first")
        self.assertEqual(other.page_scope, "all")
        self.assertEqual(app.stamp_export_btn["text"], "盖章并导出PDF")
        output = os.path.join(self.tmp, "independent_stamps.pdf")
        report = self.export_and_wait(output)
        self.assertEqual(
            [(item["page"], item["stamp_id"]) for item in report["embedded"]],
            [(0, source.stamp_id), (0, other.stamp_id),
             (1, other.stamp_id), (2, other.stamp_id), (2, fold_copy.stamp_id)])
        kinds = [item["kind"] for item in app.render_page()["stamp_items"]]
        self.assertEqual(kinds, ["stamp", "stamp"])
        app.current_page = 2
        self.assertEqual([item["kind"] for item in app.render_page()["stamp_items"]],
                 ["stamp", "cross_fold"])
        app.seal_mode_var.set("不加印章")
        app.on_seal_mode_change()
        self.assertEqual(fold_copy.page_scope, "none")
        self.assertEqual(source.page_scope, "first")
        self.assertEqual(other.page_scope, "all")
        self.assertEqual(len(app.render_page()["stamp_items"]), 1)

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
        report = self.export_and_wait(out)
        emb = report["embedded"][0]
        self.assertAlmostEqual(emb["rect"][2] - emb["rect"][0],
                              item["size"][0] / app.scale_factor, delta=0.5)
        self.assertAlmostEqual(emb["center_pt"][0], (item["x"] + item["size"][0] / 2.0)
                              / app.scale_factor, delta=0.5)
        self.assertEqual(len([d for d in self.dialogs if d[0] == "showerror"]), 0,
                         "导出路径不应报错（旧版 send_to_back 已修复）")

    def test_zoom_drag_stamp_to_page_bottom_and_export(self):
        app = self.app
        app.open_pdf(self.pdf_path)
        stamp = app.add_stamp_image(self.Image.new("RGBA", (80, 60), (255, 0, 0, 220)), "底部章")
        app.on_view_zoom_change("1.75")
        payload = app.render_page()
        page_height_px = payload["page_bitmap"].height
        target_y = page_height_px - stamp.get_processed_img().height

        app.canvas.yview_moveto(1.0)
        app.root.update()
        self.assertGreater(float(app.canvas.yview()[0]), 0.0,
                           "放大后应能滚动到页面底部")

        scroll_x = app.canvas.canvasx(0)
        scroll_y = app.canvas.canvasy(0)
        start = FakeEvent(int(stamp.x * app.view_zoom - scroll_x + 5),
                          int(stamp.y * app.view_zoom - scroll_y + 5))
        app.on_mouse_down(start)
        target = FakeEvent(int(stamp.x * app.view_zoom - scroll_x + 5),
                           int(target_y * app.view_zoom - scroll_y + 5))
        app.on_mouse_drag(target)
        app.on_mouse_up(target)
        self.assertAlmostEqual(stamp.y, target_y, delta=1.5)

        out = os.path.join(self.tmp, "gui_bottom_zoom.pdf")
        report = self.export_and_wait(out)
        self.assertIsNotNone(report)
        exported = report["embedded"][0]["rect"]
        page_height_pt = app.pdf_doc[0].rect.height
        self.assertAlmostEqual(exported[3], page_height_pt, delta=1.0)
        self.assertAlmostEqual(exported[3] - exported[1],
                               stamp.get_processed_img().height / app.scale_factor,
                               delta=0.5)

    # ------------------------------------------------- 后台导出 / 取消（本轮新增）

    def test_export_returns_job_instead_of_blocking(self):
        """导出改为后台任务：立刻返回 ExportJob，控件同步切到「导出中」。"""
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (80, 80), (255, 0, 0, 220)), "章")
        out = os.path.join(self.tmp, "async_contract.pdf")
        job = app.export_pdf(out)
        self.assertIsInstance(job, self.ps.ExportJob)
        self.assertTrue(app._busy_exporting(), "启动后应处于「导出中」")
        self.assertEqual(str(app.stamp_export_btn["state"]), "disabled",
                         "导出期间应禁用导出按钮，避免重入")
        self.assertEqual(str(app.cancel_export_btn["state"]), "normal",
                         "导出期间应可点取消")
        # 第二次点击必须被拒绝（返回 None 且不弹错误）
        self.assertIsNone(app.export_pdf(out))
        app.wait_for_export(job)
        self.assertIsNone(job.error)
        self.assertTrue(os.path.exists(out))
        # 收尾：控件复位、进度归零
        self.assertFalse(app._busy_exporting())
        self.assertEqual(str(app.stamp_export_btn["state"]), "normal")
        self.assertEqual(str(app.cancel_export_btn["state"]), "disabled")
        self.assertEqual(app.progress_var.get(), 0.0)
        self.assertTrue(any(kind == "showinfo" for kind, _ in self.dialogs))

    def test_export_job_ui_state_machine_with_gated_worker(self):
        """用「卡住的 worker」确定性验证 _begin_export/_poll_export 的控件状态机。"""
        import threading
        app = self.app
        gate = threading.Event()
        seen = []

        def worker(job):
            job.post("status", text="进行中")
            job.post("progress", value=2, maximum=5)
            gate.wait(10)
            return "ok"

        job = app._begin_export(worker, lambda j: seen.append(j))
        self.assertTrue(app._busy_exporting())
        self.assertEqual(str(app.stamp_export_btn["state"]), "disabled")
        self.assertEqual(str(app.cancel_export_btn["state"]), "normal")
        gate.set()
        app.wait_for_export(job)
        self.assertEqual(job.result, "ok")
        self.assertEqual(seen, [job], "完成回调应恰好被调用一次")
        self.assertFalse(app._busy_exporting())
        self.assertEqual(str(app.stamp_export_btn["state"]), "normal")
        self.assertEqual(str(app.cancel_export_btn["state"]), "disabled")
        self.assertEqual(app.progress_var.get(), 0.0)

    def test_cancel_midway_keeps_existing_target_file(self):
        """
        真实路径下的中途取消：worker 已进入导出函数时点「取消导出」，
        因为 os.replace() 尚未执行，已存在的目标文件必须原样保留。
        """
        import threading
        import unittest.mock as mock
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (100, 100), (255, 0, 0, 220)), "章")
        out = os.path.join(self.tmp, "cancel_target.pdf")
        with open(out, "wb") as stream:
            stream.write(b"KEEP ME INTACT")

        entered = threading.Event()
        release = threading.Event()
        real_export = self.ps.export_pdf_with_stamps

        def gated(*args, **kwargs):
            entered.set()          # 通知主线程：worker 已进入导出
            release.wait(10)       # 卡住，让取消一定落在写文件之前
            return real_export(*args, **kwargs)

        with mock.patch.object(self.ps, "export_pdf_with_stamps", gated):
            job = app.export_pdf(out)
            self.assertIsNotNone(job)
            self.assertTrue(entered.wait(10), "worker 未进入导出函数")
            self.assertTrue(app.cancel_export(), "取消请求应被接受")
            self.assertTrue(job.cancelled, "取消标志应已置位")
            release.set()
            app.wait_for_export(job)

        self.assertTrue(job.cancelled_flag, "任务应被标记为已取消")
        self.assertIsNone(job.error, "取消不是错误")
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), b"KEEP ME INTACT",
                             "取消后目标文件不得被改动")
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".pdf_stamper_")]
        self.assertEqual(leftovers, [], "取消后临时文件应被清理")
        self.assertFalse(app._busy_exporting())
        self.assertIn("取消", app.status_bar["text"])

    def test_cancel_export_without_job_is_noop(self):
        self.assertFalse(self.app.cancel_export())

    def test_export_uses_snapshot_so_later_edits_do_not_leak_in(self):
        """点下导出后再改公章参数，不应影响这次导出的结果。"""
        import threading
        import unittest.mock as mock
        app = self.app
        app.open_pdf(self.pdf_path)
        stamp = app.add_stamp_image(self.Image.new("RGBA", (80, 80), (255, 0, 0, 220)), "章")
        stamp.x, stamp.y, stamp.scale = 100, 120, 1.0
        out = os.path.join(self.tmp, "snapshot_export.pdf")

        entered = threading.Event()
        release = threading.Event()
        captured = {}
        real_export = self.ps.export_pdf_with_stamps

        def gated(src_doc, stamps, *args, **kwargs):
            captured["stamps"] = stamps
            entered.set()
            release.wait(10)
            return real_export(src_doc, stamps, *args, **kwargs)

        with mock.patch.object(self.ps, "export_pdf_with_stamps", gated):
            job = app.export_pdf(out)
            self.assertTrue(entered.wait(10), "worker 未进入导出函数")
            # 导出进行中：改界面上的公章（位置与大小都改）
            stamp.x, stamp.scale = 999, 3.0
            release.set()
            app.wait_for_export(job)

        self.assertIsNone(job.error)
        exported = captured["stamps"][0]
        self.assertIsNot(exported, stamp, "导出应使用克隆体，而非界面上的对象")
        self.assertEqual(exported.x, 100.0, "导出应使用点按钮那一刻的坐标")
        self.assertAlmostEqual(exported.scale, 1.0)
        # 结果也确实按旧坐标落位
        rect = job.result["embedded"][0]["rect"]
        self.assertAlmostEqual(rect[0], 100.0 / app.scale_factor, delta=1.0)

    def test_batch_export_runs_in_background(self):
        """批量导出也走后台任务，且逐文件回报进度。"""
        app = self.app
        batch_dir = os.path.join(self.tmp, "batch_src")
        os.makedirs(batch_dir, exist_ok=True)
        paths = [self.ps.make_test_pdf(os.path.join(batch_dir, "a.pdf"), pages=2),
                 self.ps.make_test_pdf(os.path.join(batch_dir, "b.pdf"), pages=2)]
        out_dir = os.path.join(self.tmp, "batch_out")
        os.makedirs(out_dir, exist_ok=True)
        app.batch_paths = paths
        app.file_mode = "批量文件模式"
        app.add_stamp_image(self.Image.new("RGBA", (70, 70), (255, 0, 0, 220)), "章")
        job = app.export_batch_pdf(out_dir)
        self.assertIsInstance(job, self.ps.ExportJob)
        app.wait_for_export(job)
        self.assertIsNone(job.error)
        self.assertEqual(len(job.result["reports"]), 2)
        self.assertEqual(job.result["failures"], [])
        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0] + "_盖章.pdf"
            self.assertTrue(os.path.exists(os.path.join(out_dir, name)), name)
        self.assertFalse(app._busy_exporting())
        self.assertEqual(app.progress_var.get(), 0.0)

    def test_ui_stays_usable_while_export_runs(self):
        """
        本轮改动的真正目的：导出期间主线程不被占住。

        把导出函数卡在 worker 里，然后在主线程照常翻页、拖章、重绘——
        旧版同步实现下这些调用根本轮不到执行（窗口假死）。
        """
        import threading
        import unittest.mock as mock
        app = self.app
        app.open_pdf(self.pdf_path)
        app.add_stamp_image(self.Image.new("RGBA", (80, 80), (255, 0, 0, 220)), "章")
        out = os.path.join(self.tmp, "responsive.pdf")

        gate = threading.Event()
        real_export = self.ps.export_pdf_with_stamps

        def gated(*args, **kwargs):
            gate.wait(10)                 # 模拟一个「很慢」的导出
            return real_export(*args, **kwargs)

        with mock.patch.object(self.ps, "export_pdf_with_stamps", gated):
            job = app.export_pdf(out)
            self.assertIsNotNone(job)
            self.assertTrue(app._busy_exporting(), "导出应在进行中")
            # 导出还卡在 worker 里，主线程照样能干活
            app.next_page()
            self.assertEqual(app.current_page, 1, "导出期间应能翻页")
            stamp = app.stamps[0]
            stamp.x, stamp.y = 150, 160
            payload = app.render_page()
            self.assertTrue(payload["stamp_items"], "导出期间应能重绘")
            app.on_view_zoom_change("1.5")
            self.assertEqual(app.view_zoom, 1.5, "导出期间应能改预览缩放")
            app.prev_page()
            self.assertEqual(app.current_page, 0)
            gate.set()
            app.wait_for_export(job)

        self.assertIsNone(job.error)
        self.assertTrue(os.path.exists(out), "导出应正常完成")

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
