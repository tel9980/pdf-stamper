#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台导出基准：量化「导出期间主线程是否仍然响应」。

对比口径：
  * 旧路径（同步）：export_pdf_with_stamps 直接在调用线程跑，期间主线程完全冻结。
  * 新路径（后台）：ExportJob 起线程跑，主线程靠 root.after() 收进度。

测什么：
  1. `export_pdf()` 的返回耗时（应接近 0，而不是整个导出耗时）；
  2. 导出期间主线程能跑多少次 `root.update()` 循环（越多越"不卡"）；
  3. 同一份文档同步导出的总耗时（作为对照）。

注意：必须把 `messagebox` 打桩。真实弹窗是**模态**的，会一直阻塞主线程等你点确定——
那样测出来的"阻塞时间"是等人点按钮，而不是导出本身（这个坑踩过一次，记在这里）。

运行：
    cd E:/AI && python tests/bench_export_thread.py --quick
"""

import argparse
import os
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image

import pdf_stamper as ps


def stub_dialogs():
    """把弹窗打桩成 no-op，否则模态框会一直阻塞主线程。"""
    for name in ("showinfo", "showwarning", "showerror", "askyesno"):
        setattr(ps.messagebox, name, lambda *a, **kw: True)


def make_doc(path, pages):
    ps.make_test_pdf(path, pages=pages)


def make_stamp(path, size=260):
    ps.make_test_stamp(path, size=size)


def bench_sync(src_path, out_path, stamps, repeats):
    """旧口径：同步导出，主线程全程冻结。"""
    samples = []
    for _ in range(repeats):
        doc, status = ps.open_pdf_document(src_path)
        assert status == ps.OPEN_OK, status
        try:
            start = time.perf_counter()
            ps.export_pdf_with_stamps(doc, stamps, out_path,
                                      scale_factor=ps.canvas_scale())
            samples.append((time.perf_counter() - start) * 1000.0)
        finally:
            doc.close()
    samples.sort()
    return samples[len(samples) // 2]


def bench_async(src_path, out_path, stamp_path, repeats, stamp_count=3):
    """
    新口径：走真实 GUI 路径（PDFStamper.export_pdf）。

    返回 (导出总耗时中位, export_pdf() 返回耗时中位, 主线程循环次数中位)
    """
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    app = ps.PDFStamper(root)
    app.open_pdf(src_path)
    # 与同步路径用同样数量/位置/大小的公章，保证口径一致
    base = Image.open(stamp_path).convert("RGBA")
    for i in range(stamp_count):
        stamp = app.add_stamp_image(base.copy(), "章%d" % i)
        stamp.x, stamp.y = 80 + i * 60, 120 + i * 40

    totals, returns, ticks = [], [], []
    try:
        for _ in range(repeats):
            start = time.perf_counter()
            job = app.export_pdf(out_path)
            returns.append((time.perf_counter() - start) * 1000.0)
            # 主线程模拟「一直在跑事件循环」：能转多少圈 = 有多不卡
            loops = 0
            deadline = time.monotonic() + 120
            while app._busy_exporting() and time.monotonic() < deadline:
                root.update()
                loops += 1
                time.sleep(0.002)      # 让 after 定时器有机会到期
            totals.append((time.perf_counter() - start) * 1000.0)
            ticks.append(loops)
            assert job is not None and job.error is None, getattr(job, "error", None)
    finally:
        try:
            app.destroy()
        except Exception:
            pass
        root.destroy()

    totals.sort()
    returns.sort()
    ticks.sort()
    mid = len(totals) // 2
    return totals[mid], returns[mid], ticks[mid]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="小规模快速跑")
    parser.add_argument("--pages", type=int, default=0, help="文档页数（覆盖 --quick 预设）")
    parser.add_argument("--repeats", type=int, default=0, help="重复次数")
    args = parser.parse_args()

    pages = args.pages or (40 if args.quick else 200)
    repeats = args.repeats or (3 if args.quick else 5)

    stub_dialogs()

    print("=" * 68)
    print("后台导出基准：%d 页 PDF + 3 个公章，render_dpi=%d" % (pages, ps.DEFAULT_RENDER_DPI))
    print("=" * 68)

    with tempfile.TemporaryDirectory(prefix="bench_export_") as work:
        src_path = os.path.join(work, "src.pdf")
        stamp_path = os.path.join(work, "stamp.png")
        make_doc(src_path, pages)
        make_stamp(stamp_path)

        stamp_img = Image.open(stamp_path).convert("RGBA")
        stamps = [ps.StampConfig(stamp_img.copy(), "章%d" % i) for i in range(3)]
        for i, stamp in enumerate(stamps):
            stamp.x, stamp.y = 80 + i * 60, 120 + i * 40

        out_sync = os.path.join(work, "out_sync.pdf")
        sync_ms = bench_sync(src_path, out_sync, stamps, repeats)

        out_async = os.path.join(work, "out_async.pdf")
        total_ms, return_ms, ticks = bench_async(
            src_path, out_async, stamp_path, repeats)

        print()
        print("-" * 68)
        print("旧路径（同步导出，主线程冻结）")
        print("  导出总耗时                        : 中位 %.1f ms" % sync_ms)
        print("  期间主线程循环次数                : 0（完全冻结，界面无响应）")
        print()
        print("新路径（后台任务 + root.after 轮询）")
        print("  导出总耗时                        : 中位 %.1f ms" % total_ms)
        print("  export_pdf() 返回耗时             : 中位 %.2f ms  <-- 立即返回" % return_ms)
        print("  期间主线程循环次数                : 中位 %d 次" % ticks)
        print("-" * 68)
        if total_ms > 0:
            print("调用方阻塞时间占比                : %.4f%%" %
                  (100.0 * return_ms / total_ms))
        print("结论：导出耗时不变，但主线程在导出期间可持续处理事件（不再假死）。")
        print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
