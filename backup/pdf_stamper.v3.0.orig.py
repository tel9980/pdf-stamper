#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF盖章工具 v3.0 - 专业版
支持：多公章、撤销/重做、旋转、批量盖章、骑缝章、透明度调节
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import colorchooser
import fitz  # PyMuPDF
from PIL import Image, ImageTk, ImageDraw
import io
import os
import sys
from datetime import datetime
import json

class StampConfig:
    """公章配置类"""
    def __init__(self, img, name="公章"):
        self.img = img  # PIL Image
        self.name = name
        self.x = 100
        self.y = 100
        self.scale = 1.0
        self.opacity = 1.0
        self.rotation = 0  # 旋转角度（度）
        
    def get_scaled_img(self):
        """获取缩放后的图片"""
        w, h = self.img.size
        new_size = (int(w * self.scale), int(h * self.scale))
        return self.img.resize(new_size, Image.Resampling.LANCZOS)
    
    def get_tk_img(self):
        """获取带透明度和旋转的Tkinter图片"""
        img = self.apply_opacity(self.img, self.opacity)
        if self.rotation != 0:
            img = img.rotate(self.rotation, expand=True, resample=Image.Resampling.BICUBIC)
        scaled = self.get_scaled_img()
        return ImageTk.PhotoImage(scaled)
    
    @staticmethod
    def apply_opacity(img, opacity):
        """应用透明度"""
        if opacity >= 1.0:
            return img
        img_rgba = img.copy().convert('RGBA')
        r, g, b, a = img_rgba.split()
        a = a.point(lambda p: int(p * opacity))
        return Image.merge('RGBA', (r, g, b, a))
    
    def to_dict(self):
        return {
            'name': self.name,
            'x': self.x,
            'y': self.y,
            'scale': self.scale,
            'opacity': self.opacity,
            'rotation': self.rotation
        }
    
    @classmethod
    def from_dict(cls, data, img):
        config = cls(img, data.get('name', '公章'))
        config.x = data.get('x', 100)
        config.y = data.get('y', 100)
        config.scale = data.get('scale', 1.0)
        config.opacity = data.get('opacity', 1.0)
        config.rotation = data.get('rotation', 0)
        return config

class HistoryManager:
    """历史记录管理器（撤销/重做）"""
    def __init__(self):
        self.undo_stack = []
        self.redo_stack = []
        self.max_history = 50
        
    def save_state(self, state):
        """保存状态"""
        self.undo_stack.append(state)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        
    def undo(self):
        """撤销"""
        if not self.undo_stack:
            return None
        state = self.undo_stack.pop()
        self.redo_stack.append(state)
        return self.undo_stack[-1] if self.undo_stack else None
    
    def redo(self):
        """重做"""
        if not self.redo_stack:
            return None
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        return state
    
    def can_undo(self):
        return len(self.undo_stack) > 1  # 至少保留初始状态
    
    def can_redo(self):
        return len(self.redo_stack) > 0

class PDFStamper:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF盖章工具 v3.0")
        self.root.geometry("1400x900")
        
        # 状态变量
        self.pdf_path = None
        self.pdf_doc = None
        self.current_page = 0
        self.total_pages = 0
        
        # 多公章管理
        self.stamps = []  # 列表 of StampConfig
        self.active_stamp_idx = 0  # 当前选中的公章索引
        
        # 页面配置（批量盖章）
        self.page_configs = {}  # {page_index: {stamp_idx: stamp_config_dict}}
        
        # 模式
        self.cross_fold_mode = False
        self.cross_fold_offset = 0.5
        
        # 历史记录
        self.history = HistoryManager()
        
        # 公章层级（底层/上层）
        self.stamp_underlay = True  # True=公章在底层，False=公章在上层（默认）
        
        # 渲染参数
        self.render_dpi = 150
        self.scale_factor = self.render_dpi / 72
        
        # 选中的公章（用于拖拽）
        self.selected_stamp = None
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_y = 0
        
        # 缓存
        self.page_img = None
        
        self.setup_ui()
        
    def setup_ui(self):
        # 创建主布局
        self.create_toolbar()
        self.create_side_panel()
        self.create_canvas()
        self.create_status_bar()
        
        # 绑定快捷键
        self.bind_shortcuts()
        
    def create_toolbar(self):
        """创建顶部工具栏"""
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        
        # 文件操作
        ttk.Button(toolbar, text="📂 打开PDF", command=self.open_pdf).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="🖼️ 加载公章", command=self.load_stamp).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="💾 导出PDF", command=self.export_pdf).pack(side=tk.LEFT, padx=2)
        
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 撤销/重做
        self.undo_btn = ttk.Button(toolbar, text="↶ 撤销", command=self.undo, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=2)
        self.redo_btn = ttk.Button(toolbar, text="↷ 重做", command=self.redo, state=tk.DISABLED)
        self.redo_btn.pack(side=tk.LEFT, padx=2)
        
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        # 当前公章控制
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
        
        # 模式按钮
        self.cross_fold_btn = ttk.Button(toolbar, text="骑缝章: 关", command=self.toggle_cross_fold)
        self.cross_fold_btn.pack(side=tk.LEFT, padx=2)
        
        # 公章层级控制
        self.layer_btn = ttk.Button(toolbar, text="公章: 底层", command=self.toggle_layer)
        self.layer_btn.pack(side=tk.LEFT, padx=2)
        
        ttk.Button(toolbar, text="🗑️ 删除选中", command=self.delete_selected_stamp).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="🔄 重置所有", command=self.reset_all).pack(side=tk.LEFT, padx=2)
        
    def create_side_panel(self):
        """创建右侧面板"""
        side_panel = ttk.Frame(self.root, width=250)
        side_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)
        
        # 公章列表
        ttk.Label(side_panel, text="📋 公章列表", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(0, 5))
        
        self.stamp_listbox = tk.Listbox(side_panel, height=8, selectmode=tk.SINGLE)
        self.stamp_listbox.pack(fill=tk.X, pady=(0, 5))
        self.stamp_listbox.bind('<<ListboxSelect>>', self.on_stamp_select)
        
        ttk.Button(side_panel, text="➕ 添加公章", command=self.load_stamp).pack(fill=tk.X, pady=(0, 5))
        ttk.Button(side_panel, text="❌ 删除公章", command=self.delete_stamp).pack(fill=tk.X, pady=(0, 10))
        
        # 页面导航
        ttk.Label(side_panel, text="📄 页面导航", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(0, 5))
        
        nav_frame = ttk.Frame(side_panel)
        nav_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Button(nav_frame, text="◀", command=self.prev_page, width=5).pack(side=tk.LEFT, padx=2)
        self.page_label = ttk.Label(nav_frame, text="1 / 1", width=10, anchor=tk.CENTER)
        self.page_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(nav_frame, text="▶", command=self.next_page, width=5).pack(side=tk.LEFT, padx=2)
        
        # 跳转到指定页
        goto_frame = ttk.Frame(side_panel)
        goto_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(goto_frame, text="跳转到:").pack(side=tk.LEFT)
        self.goto_entry = ttk.Entry(goto_frame, width=8)
        self.goto_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(goto_frame, text="Go", command=self.goto_page, width=5).pack(side=tk.LEFT)
        
        # 骑缝章配置
        ttk.Label(side_panel, text="🔗 骑缝章配置", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(10, 5))
        
        cross_frame = ttk.Frame(side_panel)
        cross_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(cross_frame, text="偏移:").pack(side=tk.LEFT)
        self.offset_var = tk.DoubleVar(value=0.5)
        self.offset_slider = ttk.Scale(cross_frame, from_=0.0, to=1.0, variable=self.offset_var, 
                                       command=self.on_offset_change, length=120)
        self.offset_slider.pack(side=tk.LEFT, padx=5)
        self.offset_label = ttk.Label(cross_frame, text="50%")
        self.offset_label.pack(side=tk.LEFT)
        
        # 操作提示
        ttk.Label(side_panel, text="💡 操作提示", font=('', 12, 'bold')).pack(anchor=tk.W, pady=(10, 5))
        
        tips_text = """• 鼠标拖拽：移动公章
• 滚轮/←→：翻页
• Ctrl+Z：撤销
• Ctrl+Y：重做
• Delete：删除选中公章
• Ctrl+A：全选公章"""
        
        tips_label = ttk.Label(side_panel, text=tips_text, justify=tk.LEFT, foreground='gray')
        tips_label.pack(anchor=tk.W, pady=(0, 10))
        
    def create_canvas(self):
        """创建主画布"""
        self.canvas_frame = ttk.Frame(self.root)
        self.canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.canvas = tk.Canvas(self.canvas_frame, bg='#1e1e1e', cursor="hand2")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # 滚动条
        h_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        v_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        
        # 绑定事件
        self.canvas.bind("<Button-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel)
        
    def create_status_bar(self):
        """创建状态栏"""
        self.status_bar = ttk.Label(self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        
    def bind_shortcuts(self):
        """绑定快捷键"""
        self.root.bind('<Control-z>', lambda e: self.undo())
        self.root.bind('<Control-y>', lambda e: self.redo())
        self.root.bind('<Delete>', lambda e: self.delete_selected_stamp())
        self.root.bind('<Left>', lambda e: self.prev_page())
        self.root.bind('<Right>', lambda e: self.next_page())
        self.root.bind('<Control-a>', lambda e: self.select_all_stamps())
        
    # ==================== PDF操作 ====================
    
    def open_pdf(self):
        filepath = filedialog.askopenfilename(
            title="选择PDF文件",
            filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
        )
        if filepath:
            try:
                self.pdf_doc = fitz.open(filepath)
                self.pdf_path = filepath
                self.total_pages = len(self.pdf_doc)
                self.current_page = 0
                self.page_configs = {}
                self.render_page()
                self.page_label.config(text=f"1 / {self.total_pages}")
                self.status_bar.config(text=f"已打开: {os.path.basename(filepath)} ({self.total_pages}页)")
            except Exception as e:
                messagebox.showerror("错误", f"打开PDF失败: {str(e)}")
    
    # ==================== 公章操作 ====================
    
    def load_stamp(self):
        filepath = filedialog.askopenfilename(
            title="选择公章图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"), ("所有文件", "*.*")]
        )
        if filepath:
            try:
                img = Image.open(filepath).convert("RGBA")
                stamp = StampConfig(img, os.path.basename(filepath))
                self.stamps.append(stamp)
                self.active_stamp_idx = len(self.stamps) - 1
                self.update_stamp_list()
                self.render_page()
                self.status_bar.config(text=f"已加载: {stamp.name}")
            except Exception as e:
                messagebox.showerror("错误", f"加载失败: {str(e)}")
    
    def delete_stamp(self):
        selection = self.stamp_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要删除的公章")
            return
        idx = selection[0]
        if messagebox.askyesno("确认", f"确定删除公章 '{self.stamps[idx].name}'？"):
            del self.stamps[idx]
            if self.active_stamp_idx >= len(self.stamps):
                self.active_stamp_idx = max(0, len(self.stamps) - 1)
            self.update_stamp_list()
            self.render_page()
    
    def delete_selected_stamp(self):
        """删除当前页选中的公章"""
        if self.selected_stamp is not None:
            idx = self.selected_stamp
            if 0 <= idx < len(self.stamps):
                del self.stamps[idx]
                self.selected_stamp = None
                if self.active_stamp_idx >= len(self.stamps):
                    self.active_stamp_idx = max(0, len(self.stamps) - 1)
                self.update_stamp_list()
                self.render_page()
                self.save_history()
    
    def on_stamp_select(self, event):
        selection = self.stamp_listbox.curselection()
        if selection:
            self.active_stamp_idx = selection[0]
            stamp = self.stamps[self.active_stamp_idx]
            # 更新UI控件
            self.scale_var.set(stamp.scale)
            self.scale_label.config(text=f"{int(stamp.scale * 100)}%")
            self.opacity_var.set(stamp.opacity)
            self.opacity_label.config(text=f"{int(stamp.opacity * 100)}%")
            self.rotation_var.set(stamp.rotation)
            self.rotation_label.config(text=f"{stamp.rotation}°")
            self.render_page()
    
    def update_stamp_list(self):
        """更新公章列表"""
        self.stamp_listbox.delete(0, tk.END)
        for i, stamp in enumerate(self.stamps):
            marker = "► " if i == self.active_stamp_idx else "  "
            self.stamp_listbox.insert(tk.END, f"{marker}{stamp.name}")
        if self.stamps:
            self.stamp_listbox.selection_set(self.active_stamp_idx)
    
    # ==================== 渲染 ====================
    
    def render_page(self):
        if not self.pdf_doc:
            return
        
        self.canvas.delete("all")
        
        # 渲染PDF页面
        page = self.pdf_doc[self.current_page]
        pix = page.get_pixmap(dpi=self.render_dpi)
        img_data = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_data))
        self.page_img = ImageTk.PhotoImage(img)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.page_img)
        
        # 渲染所有公章
        for idx, stamp in enumerate(self.stamps):
            tk_img = stamp.get_tk_img()
            tag = f"stamp_{idx}"
            tags = [tag]
            if idx == self.active_stamp_idx:
                tags.append("active")
            if idx == self.selected_stamp:
                tags.append("selected")
            
            # 骑缝章处理
            if self.cross_fold_mode and idx == self.active_stamp_idx:
                self.draw_cross_fold_stamp(stamp, tk_img, tag)
            else:
                self.canvas.create_image(
                    stamp.x, stamp.y,
                    anchor=tk.NW, image=tk_img, tags=tags
                )
        
        # 更新状态
        self.page_label.config(text=f"{self.current_page + 1} / {self.total_pages}")
        self.update_undo_redo_buttons()
    
    def draw_cross_fold_stamp(self, stamp, tk_img, tag):
        """绘制骑缝章"""
        if not self.pdf_doc:
            return
        
        page = self.pdf_doc[self.current_page]
        page_rect = page.rect
        canvas_page_width = page_rect.width * self.scale_factor
        
        half_stamp_width = tk_img.width() / 2
        cross_fold_x = canvas_page_width - half_stamp_width - (half_stamp_width * 2 * self.cross_fold_offset)
        
        # 主印章
        self.canvas.create_image(
            cross_fold_x, stamp.y,
            anchor=tk.NW, image=tk_img, tags=[tag, "cross_fold"]
        )
        
        # 下一页预览（如果有）
        if self.current_page < self.total_pages - 1:
            next_x = -half_stamp_width * 2 * self.cross_fold_offset + tk_img.width()
            self.canvas.create_image(
                next_x, stamp.y,
                anchor=tk.NW, image=tk_img, tags=[f"{tag}_preview", "cross_fold_preview"]
            )
    
    # ==================== 鼠标交互 ====================
    
    def on_mouse_down(self, event):
        if not self.stamps:
            return
        
        # 查找点击的公章（从上到下）
        items = self.canvas.find_closest(event.x, event.y)
        if not items:
            return
        
        clicked_item = items[0]
        tags = self.canvas.gettags(clicked_item)
        
        # 查找公章索引
        stamp_idx = None
        for tag in tags:
            if tag.startswith("stamp_") and not tag.endswith("_preview"):
                try:
                    stamp_idx = int(tag.split("_")[1])
                    break
                except:
                    pass
        
        if stamp_idx is not None and stamp_idx < len(self.stamps):
            self.selected_stamp = stamp_idx
            self.active_stamp_idx = stamp_idx
            self.is_dragging = True
            stamp = self.stamps[stamp_idx]
            self.drag_start_x = event.x - stamp.x
            self.drag_start_y = event.y - stamp.y
            self.update_stamp_list()
            self.render_page()
    
    def on_mouse_drag(self, event):
        if self.is_dragging and self.selected_stamp is not None:
            stamp = self.stamps[self.selected_stamp]
            stamp.x = event.x - self.drag_start_x
            stamp.y = event.y - self.drag_start_y
            self.render_page()
    
    def on_mouse_up(self, event):
        if self.is_dragging:
            self.is_dragging = False
            self.save_history()
    
    def on_mouse_wheel(self, event):
        if not self.pdf_doc:
            return
        if hasattr(event, 'delta'):
            if event.delta > 0:
                self.prev_page()
            elif event.delta < 0:
                self.next_page()
        else:
            if event.num == 4:
                self.prev_page()
            elif event.num == 5:
                self.next_page()
    
    # ==================== 页面导航 ====================
    
    def prev_page(self):
        if self.pdf_doc and self.current_page > 0:
            self.current_page -= 1
            self.render_page()
            self.page_label.config(text=f"{self.current_page + 1} / {self.total_pages}")
    
    def next_page(self):
        if self.pdf_doc and self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.render_page()
            self.page_label.config(text=f"{self.current_page + 1} / {self.total_pages}")
    
    def goto_page(self):
        if not self.pdf_doc:
            return
        try:
            page_num = int(self.goto_entry.get())
            if 1 <= page_num <= self.total_pages:
                self.current_page = page_num - 1
                self.render_page()
            else:
                messagebox.showwarning("提示", f"页码应在 1-{self.total_pages} 之间")
        except ValueError:
            messagebox.showwarning("提示", "请输入有效的页码")
    
    # ==================== 历史记录 ====================
    
    def save_history(self):
        """保存当前状态到历史记录"""
        state = {
            'stamps': [s.to_dict() for s in self.stamps],
            'current_page': self.current_page
        }
        self.history.save_state(state)
        self.update_undo_redo_buttons()
    
    def undo(self):
        if not self.history.can_undo():
            return
        state = self.history.undo()
        if state:
            self.restore_state(state)
            self.update_undo_redo_buttons()
    
    def redo(self):
        if not self.history.can_redo():
            return
        state = self.history.redo()
        if state:
            self.restore_state(state)
            self.update_undo_redo_buttons()
    
    def restore_state(self, state):
        """恢复状态"""
        # 恢复公章（需要保留图片引用）
        stamps_data = state.get('stamps', [])
        new_stamps = []
        for data in stamps_data:
            # 查找对应的图片
            for old_stamp in self.stamps:
                if old_stamp.name == data['name']:
                    stamp = StampConfig.from_dict(data, old_stamp.img)
                    new_stamps.append(stamp)
                    break
        
        self.stamps = new_stamps if new_stamps else self.stamps
        self.current_page = state.get('current_page', self.current_page)
        self.render_page()
    
    def update_undo_redo_buttons(self):
        """更新撤销/重做按钮状态"""
        self.undo_btn.config(state=tk.NORMAL if self.history.can_undo() else tk.DISABLED)
        self.redo_btn.config(state=tk.NORMAL if self.history.can_redo() else tk.DISABLED)
    
    # ==================== 参数调整 ====================
    
    def on_scale_change(self, val):
        if self.stamps and self.active_stamp_idx < len(self.stamps):
            stamp = self.stamps[self.active_stamp_idx]
            stamp.scale = float(val)
            self.scale_label.config(text=f"{int(stamp.scale * 100)}%")
            self.render_page()
    
    def on_opacity_change(self, val):
        if self.stamps and self.active_stamp_idx < len(self.stamps):
            stamp = self.stamps[self.active_stamp_idx]
            stamp.opacity = float(val)
            self.opacity_label.config(text=f"{int(stamp.opacity * 100)}%")
            self.render_page()
    
    def on_rotation_change(self, val):
        if self.stamps and self.active_stamp_idx < len(self.stamps):
            stamp = self.stamps[self.active_stamp_idx]
            stamp.rotation = int(float(val))
            self.rotation_label.config(text=f"{stamp.rotation}°")
            self.render_page()
    
    def on_offset_change(self, val):
        self.cross_fold_offset = float(val)
        self.offset_label.config(text=f"{int(self.cross_fold_offset * 100)}%")
        self.render_page()
    
    # ==================== 模式切换 ====================
    
    def toggle_cross_fold(self):
        self.cross_fold_mode = not self.cross_fold_mode
        text = "骑缝章: 开" if self.cross_fold_mode else "骑缝章: 关"
        self.cross_fold_btn.config(text=text)
        self.render_page()
        self.status_bar.config(text=f"骑缝章模式: {'开启' if self.cross_fold_mode else '关闭'}")
    
    def toggle_layer(self):
        """切换公章层级：底层/上层"""
        self.stamp_underlay = not self.stamp_underlay
        text = "公章: 底层" if self.stamp_underlay else "公章: 上层"
        self.layer_btn.config(text=text)
        layer_name = "底层（文字下面）" if self.stamp_underlay else "上层（文字上面）"
        self.status_bar.config(text=f"公章层级：{layer_name}")
        messagebox.showinfo("公章层级", 
            f"已切换为：{layer_name}\n\n"
            "底层：公章在文字下面，适合水印效果\n"
            "上层：公章在文字上面，适合正式盖章")
    
    def reset_all(self):
        if not self.stamps:
            return
        if messagebox.askyesno("确认", "确定要重置所有公章到默认位置吗？"):
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
        """全选公章（用于批量操作）"""
        self.selected_stamp = None
        self.render_page()
        self.status_bar.config(text="已全选（可进行批量操作）")
    
    # ==================== 导出 ====================
    
    def export_pdf(self):
        if not self.pdf_doc or not self.stamps:
            messagebox.showwarning("提示", "请先打开PDF并加载至少一个公章")
            return
        
        save_path = filedialog.asksaveasfilename(
            title="保存盖章PDF",
            defaultextension=".pdf",
            filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
        )
        if not save_path:
            return
        
        try:
            new_doc = fitz.open()
            new_doc.insert_pdf(self.pdf_doc)
            
            for i in range(self.total_pages):
                page = new_doc[i]
                rect = page.rect
                
                for stamp in self.stamps:
                    # 骑缝章处理
                    if self.cross_fold_mode and stamp == self.stamps[self.active_stamp_idx]:
                        half_width = (stamp.img.width * stamp.scale) / 2
                        offset_pdf = half_width * 2 * self.cross_fold_offset / self.scale_factor
                        pdf_x = rect.width - half_width / self.scale_factor - offset_pdf
                        pdf_y = (rect.height - stamp.img.height * stamp.scale) / 2
                    else:
                        pdf_x = stamp.x / self.scale_factor
                        pdf_y = stamp.y / self.scale_factor
                    
                    stamp_rect = fitz.Rect(
                        pdf_x, pdf_y,
                        pdf_x + stamp.img.width * stamp.scale,
                        pdf_y + stamp.img.height * stamp.scale
                    )
                    
                    # 处理透明度和旋转
                    img_to_save = StampConfig.apply_opacity(stamp.img, stamp.opacity)
                    if stamp.rotation != 0:
                        img_to_save = img_to_save.rotate(stamp.rotation, expand=True, resample=Image.Resampling.BICUBIC)
                    
                    img_bytes = io.BytesIO()
                    img_to_save.save(img_bytes, format='PNG')
                    img_bytes.seek(0)
                    
                    # 插入公章图片
                    img_ref = page.insert_image(stamp_rect, stream=img_bytes.read())
                    
                    # 将公章移到文字底层（印章在文字下面）
                    if self.stamp_underlay:
                        page.send_to_back(img_ref)
            
            new_doc.save(save_path)
            new_doc.close()
            messagebox.showinfo("成功", f"PDF已保存:\n{save_path}")
            self.status_bar.config(text=f"已导出: {os.path.basename(save_path)}")
            
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")

def main():
    root = tk.Tk()
    app = PDFStamper(root)
    root.mainloop()

if __name__ == "__main__":
    main()
