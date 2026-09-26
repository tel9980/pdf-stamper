#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
渲染性能基准（E 项）：30 页 PDF + 3 个公章。

只测纯逻辑（RenderCore），不启动 GUI / Tk 主循环。
输出两组数字：
  1) 单次 render_page 耗时  —— 冷缓存（页面位图 + 公章位图全部重算），等价于旧版每次
     <B1-Motion> 都 render_page() 的开销
  2) 页面位图缓存命中耗时    —— 热缓存（同一页、同一批参数），等价于拖拽结束后重绘的开销

运行：
    cd E:/AI && python tests/bench_render.py          # 完整基准
    cd E:/AI && python tests/bench_render.py --quick   # 少迭代（供 unittest 调用）
"""

import os
import statistics
import sys
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BENCH_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:  # PyMuPDF >= 1.24 提供 pymupdf 别名；用别名可避免 fitz 的弃用警告
    import pymupdf as fitz
except ImportError:  # 旧版本（requirements 下限 1.23）只有 fitz
    import fitz
from PIL import Image

import pdf_stamper as ps

PAGES = 30
STAMPS = 3
STAMP_SIZE = 260
SCALE_FACTOR = ps.canvas_scale(ps.DEFAULT_RENDER_DPI)


def ensure_artifacts(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, "bench_30p.pdf")
    if not os.path.exists(pdf_path):
        doc = fitz.open()
        for i in range(PAGES):
            page = doc.new_page(width=595, height=842)
            for line in range(40):
                page.insert_text((60, 60 + line * 18),
                                 "Bench page %d line %d  Lorem ipsum dolor sit amet" % (i + 1, line),
                                 fontsize=10, fontname="helv")
        doc.save(pdf_path)
        doc.close()
    return pdf_path


def make_stamps():
    stamps = []
    for i in range(STAMPS):
        img = Image.new("RGBA", (STAMP_SIZE, STAMP_SIZE), (200, 20, 20, 255))
        stamp = ps.StampConfig(img, "章%d" % (i + 1))
        stamp.x = 120 + i * 90
        stamp.y = 200 + i * 70
        stamp.scale = 0.6 + i * 0.25
        stamp.opacity = 0.55 + i * 0.2
        stamp.rotation = 15 * i
        stamps.append(stamp)
    return stamps


def timeit(fn, iterations):
    """返回 (中位数 ms, 最好 ms, 最差 ms, 结果对象)。"""
    samples = []
    result = None
    for _ in range(iterations):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples), min(samples), max(samples), result


def cold_render(core, doc, page_index, stamps):
    """模拟旧版 render_page：缓存全部作废，位图整页重渲染 + 公章重算。"""
    ps.stamp_image_cache_clear()
    core.invalidate()
    return core.build_payload(doc, page_index, stamps)


def warm_render(core, doc, page_index, stamps):
    return core.build_payload(doc, page_index, stamps)


def legacy_render(doc, page_index, stamps, dpi=ps.DEFAULT_RENDER_DPI):
    """
    旧版路径的等价复现（对照组）：重新以 dpi 渲染整页 + PNG 编码/解码 +
    每个公章重新 resize/rotate，不走任何缓存。用于量化 v3.1 的收益。
    """
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    import io as _io
    img = Image.open(_io.BytesIO(pix.tobytes("png")))
    out = [img]
    for stamp in stamps:
        base = ps.apply_opacity(stamp.img, stamp.opacity)
        if stamp.rotation:
            base = base.rotate(stamp.rotation, expand=True, resample=Image.Resampling.BICUBIC)
        w, h = stamp.img.size
        out.append(base.resize((int(w * stamp.scale), int(h * stamp.scale)),
                               Image.Resampling.LANCZOS))
    return out


def main(argv):
    quick = "--quick" in argv
    cold_iters, warm_iters = (3, 5) if quick else (9, 30)

    out_dir = os.path.join(PROJECT_DIR, "test_output")
    pdf_path = ensure_artifacts(out_dir)
    doc = fitz.open(pdf_path)
    stamps = make_stamps()
    page_index = PAGES // 2

    print("=" * 68)
    print("渲染基准： %d 页 PDF + %d 个公章，render_dpi=%d，scale_factor=%.4f"
          % (doc.page_count, len(stamps), ps.DEFAULT_RENDER_DPI, SCALE_FACTOR))
    print("页面: %s (index=%d)" % (os.path.basename(pdf_path), page_index))
    print("=" * 68)

    core = ps.RenderCore(dpi=ps.DEFAULT_RENDER_DPI)

    cold_med, cold_min, cold_max, _ = timeit(
        lambda: cold_render(core, doc, page_index, stamps), cold_iters)
    info = core.cache_info()
    warm_med, warm_min, warm_max, _ = timeit(
        lambda: warm_render(core, doc, page_index, stamps), warm_iters)
    legacy_med, legacy_min, legacy_max, _ = timeit(
        lambda: legacy_render(doc, page_index, stamps), cold_iters)

    after = core.cache_info()
    print("单次 render_page 耗时（冷缓存，%d 次迭代）      : 中位 %.2f ms  (min %.2f / max %.2f)"
          % (cold_iters, cold_med, cold_min, cold_max))
    print("页面位图缓存命中耗时（热缓存，%d 次迭代）      : 中位 %.2f ms  (min %.2f / max %.2f)"
          % (warm_iters, warm_med, warm_min, warm_max))
    print("旧版路径复现（整页 PNG 编解码 + 公章重算）      : 中位 %.2f ms" % legacy_med)
    print("-" * 68)
    print("缓存加速比（冷/热）                          : %.1fx" % (cold_med / max(warm_med, 1e-9)))
    print("相对旧版路径加速比                            : %.1fx" % (legacy_med / max(warm_med, 1e-9)))
    print("页面位图缓存: hit=%d miss=%d，命中缓存页数=%d"
          % (after["stats"]["page_hit"], after["stats"]["page_miss"], after["cached_pages"]))
    print("公章位图缓存: hit=%d miss=%d，全局条目数=%d/%d"
          % (after["stats"]["stamp_hit"], after["stats"]["stamp_miss"],
             after["stamp_cache"]["entries"], after["stamp_cache"]["max"]))
    print("缓存内容不变时 build_payload 返回同一位图对象: %s"
          % (core.get_page_bitmap(doc, page_index) is core.get_page_bitmap(doc, page_index)))
    print("=" * 68)

    # 拖拽一次「只 move 图元」的成本（纯逻辑侧：只更新坐标，不重绘）
    drag_med, _, _, _ = timeit(lambda: [setattr(s, "x", s.x + 1) for s in stamps], warm_iters)
    print("拖拽逐帧（仅更新坐标，不渲染）: 中位 %.4f ms" % drag_med)

    doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
