//! GDI 自绘封装。
//!
//! 原则（与 `memory/01` 对齐）：**画在客户端里的东西一律是纯色**。
//! 想要"透气"只能靠 DWM 背板（dwm.rs），自己铺半透明去糊桌面是假的，
//! 所以这里不做 alpha 合成——省事，也避免踩"看起来像玻璃其实糊了"的坑。

use crate::native::*;
use std::ffi::c_void;

pub struct Font {
    pub handle: HFONT,
}

impl Font {
    pub fn new(size_pt: i32, weight: i32, face: &str) -> Font {
        let face_w = wide(face);
        // 负高度 = 字符高度（含内部 leading 的写法），配合 CLEARTYPE 得到清晰小字
        let handle = unsafe {
            CreateFontW(
                -size_pt,
                0,
                0,
                0,
                weight,
                0,
                0,
                0,
                DEFAULT_CHARSET,
                0,
                0,
                CLEARTYPE_QUALITY,
                0,
                face_w.as_ptr(),
            )
        };
        Font { handle }
    }

    pub fn is_valid(&self) -> bool {
        !self.handle.is_null()
    }
}

impl Drop for Font {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                DeleteObject(self.handle);
            }
            self.handle = NULL_HANDLE;
        }
    }
}

/// 一次绘制用的临时画刷/画笔，保证配对释放。
pub struct Brush {
    handle: HBRUSH,
}

impl Brush {
    pub fn solid(color: u32) -> Brush {
        Brush {
            handle: unsafe { CreateSolidBrush(color) },
        }
    }

    pub fn handle(&self) -> HBRUSH {
        self.handle
    }
}

impl Drop for Brush {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                DeleteObject(self.handle);
            }
        }
    }
}

pub struct Pen {
    handle: HPEN,
}

impl Pen {
    pub fn solid(color: u32, width: i32) -> Pen {
        Pen {
            handle: unsafe { CreatePen(PS_SOLID, width, color) },
        }
    }
}

impl Drop for Pen {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                DeleteObject(self.handle);
            }
        }
    }
}

/// 保存/恢复 GDI 选中对象的小守卫。
pub struct Selection {
    hdc: HDC,
    old: HGDIOBJ,
}

impl Selection {
    pub fn select(hdc: HDC, obj: HGDIOBJ) -> Selection {
        let old = unsafe { SelectObject(hdc, obj) };
        Selection { hdc, old }
    }
}

impl Drop for Selection {
    fn drop(&mut self) {
        if !self.old.is_null() {
            unsafe {
                SelectObject(self.hdc, self.old);
            }
        }
    }
}

/// 填充圆角矩形（纯色）。
pub fn fill_round_rect(hdc: HDC, rc: RECT, radius: i32, color: u32) {
    let brush = Brush::solid(color);
    let pen = Pen::solid(color, 1);
    unsafe {
        let _b = Selection::select(hdc, brush.handle() as HGDIOBJ);
        let _p = Selection::select(hdc, pen.handle as HGDIOBJ);
        // RoundRect 的 w/h 是**椭圆直径**，所以要乘 2
        RoundRect(hdc, rc.left, rc.top, rc.right, rc.bottom, radius * 2, radius * 2);
    }
}

/// 圆角描边（不填充）。
pub fn stroke_round_rect(hdc: HDC, rc: RECT, radius: i32, color: u32) {
    let null_brush = unsafe { GetStockObject(NULL_BRUSH_STOCK) };
    let pen = Pen::solid(color, 1);
    unsafe {
        let _b = Selection::select(hdc, null_brush);
        let _p = Selection::select(hdc, pen.handle as HGDIOBJ);
        RoundRect(hdc, rc.left, rc.top, rc.right, rc.bottom, radius * 2, radius * 2);
    }
}

/// 填充直角矩形。
pub fn fill_rect(hdc: HDC, rc: RECT, color: u32) {
    let brush = Brush::solid(color);
    unsafe {
        FillRect(hdc, &rc, brush.handle());
    }
}

/// 画一条水平分隔线。
pub fn hline(hdc: HDC, x1: i32, x2: i32, y: i32, color: u32) {
    let pen = Pen::solid(color, 1);
    unsafe {
        let _p = Selection::select(hdc, pen.handle as HGDIOBJ);
        MoveToEx(hdc, x1, y, std::ptr::null_mut());
        LineTo(hdc, x2, y);
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum TextAlign {
    Left,
    Center,
    Right,
}

/// 单行文本绘制（按 x 直接落笔；调用方负责裁剪）。
pub fn text(hdc: HDC, s: &str, x: i32, y: i32, color: u32, font: &Font) {
    if s.is_empty() || !font.is_valid() {
        return;
    }
    let w = wide(s);
    unsafe {
        let _f = Selection::select(hdc, font.handle as HGDIOBJ);
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, color);
        TextOutW(hdc, x, y, w.as_ptr(), (w.len() - 1) as i32);
    }
}

/// 在给定矩形内按对齐方式画单行文本（垂直居中）。
pub fn text_in(hdc: HDC, s: &str, rc: RECT, align: TextAlign, color: u32, font: &Font) {
    if s.is_empty() || !font.is_valid() {
        return;
    }
    let w = wide(s);
    let mut size = SIZE::default();
    let measured = unsafe {
        let _f = Selection::select(hdc, font.handle as HGDIOBJ);
        GetTextExtentPoint32W(hdc, w.as_ptr(), (w.len() - 1) as i32, &mut size) != 0
    };
    if !measured {
        return;
    }
    let x = match align {
        TextAlign::Left => rc.left,
        TextAlign::Center => rc.left + (rc.width() - size.cx) / 2,
        TextAlign::Right => rc.right - size.cx,
    };
    let y = rc.top + (rc.height() - size.cy) / 2;
    unsafe {
        let _f = Selection::select(hdc, font.handle as HGDIOBJ);
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, color);
        TextOutW(hdc, x, y, w.as_ptr(), (w.len() - 1) as i32);
    }
}

pub fn measure_text(hdc: HDC, s: &str, font: &Font) -> SIZE {
    let mut size = SIZE::default();
    if s.is_empty() || !font.is_valid() {
        return size;
    }
    let w = wide(s);
    unsafe {
        let _f = Selection::select(hdc, font.handle as HGDIOBJ);
        GetTextExtentPoint32W(hdc, w.as_ptr(), (w.len() - 1) as i32, &mut size);
    }
    size
}

/// 在给定矩形内画单行文本，**超宽就用省略号截断**（绝不溢出到相邻控件上）。
///
/// 与 `text_in` 的分工（重要）：
/// - `text_in`：按对齐方式落笔，**不做任何裁剪** —— 只适合**长度可控**的文案
///   （按钮标签、固定标题）；
/// - 本函数：用 `DrawTextW` + `DT_END_ELLIPSIS`，**长度不可控**的内容一律用它
///   （页面标题、状态串、文件路径）。
///
/// 实测教训（2026-09-16）：网页框里的**页面标题**是用 `text_in` 画的，标题一长
/// 就直接盖到右边「检测/刷新网页」按钮上 —— 用户看到的是"按钮不够宽、文字溢出了"。
/// 把按钮加宽只是缓解；**根治是让文字不会溢出去**。
pub fn text_ellipsis(hdc: HDC, s: &str, rc: RECT, align: TextAlign, color: u32, font: &Font) {
    if s.is_empty() || !font.is_valid() || rc.right <= rc.left {
        return;
    }
    let w = wide(s);
    let mut r = rc;
    // ⚠️ DT_NOPREFIX 不能省：状态/网址里带 `&` 时会被当成助记符前缀吞掉一个字符
    let format = DT_SINGLELINE
        | DT_VCENTER
        | DT_NOPREFIX
        | DT_END_ELLIPSIS
        | match align {
            TextAlign::Left => DT_LEFT,
            TextAlign::Center => DT_CENTER,
            TextAlign::Right => DT_RIGHT,
        };
    unsafe {
        let _f = Selection::select(hdc, font.handle as HGDIOBJ);
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, color);
        DrawTextW(hdc, w.as_ptr(), (w.len() - 1) as i32, &mut r, format);
    }
}

/// 用圆角矩形近似画一个"胶囊"标签（用于公告/状态胶囊）。
pub fn pill(hdc: HDC, rc: RECT, fill: u32, label: &str, fg: u32, font: &Font) {
    fill_round_rect(hdc, rc, rc.height() / 2, fill);
    text_in(hdc, label, rc, TextAlign::Center, fg, font);
}

/// 在 hdc 上设置裁剪区（画圆角内容时避免溢出）。
pub fn clip_round_rect(hdc: HDC, rc: RECT, radius: i32) {
    unsafe {
        let rgn = CreateRoundRectRgn(
            rc.left,
            rc.top,
            rc.right + 1,
            rc.bottom + 1,
            radius * 2,
            radius * 2,
        );
        if !rgn.is_null() {
            SelectClipRgn(hdc, rgn);
            DeleteObject(rgn as HGDIOBJ);
        }
    }
}

pub fn reset_clip(hdc: HDC) {
    unsafe {
        SelectClipRgn(hdc, std::ptr::null_mut());
    }
}

#[link(name = "gdi32")]
extern "system" {
    fn CreateRoundRectRgn(l: i32, t: i32, r: i32, b: i32, w: i32, h: i32) -> *mut c_void;
    fn SelectClipRgn(hdc: HDC, rgn: *mut c_void) -> i32;
}
