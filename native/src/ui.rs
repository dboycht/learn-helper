//! 窗口与自绘界面（Win32 + GDI）。
//!
//! 布局：自绘标题栏（含拖拽/三键）→ 状态卡 → 网页选择 → KPI/进度 → 按钮 → 日志。
//! 全部纯 GDI 绘制；颜色方案与 WinUI 版同一套令牌（深色为主，可切浅色）。

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use crate::backend::{self, Shared};
use crate::gdi::{self, Font, TextAlign};
use crate::json::Value;
use crate::native::*;

const CLASS_NAME: &str = "LearnHelperNativeWnd";
const TIMER_UI: usize = 1;
const TIMER_POLL: usize = 2;

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum Ctl {
    Start,
    Pause,
    Refresh,
    Diagnose,
    Stop,
    Theme,
    Min,
    Max,
    Close,
}

#[derive(Clone, Copy, PartialEq)]
enum Theme {
    Dark,
    Light,
}

/// 无边框窗口的拖拽 / 缩放模式（我们自己实现，因为去掉了 WS_CAPTION/WS_THICKFRAME）。
#[derive(Clone, Copy, PartialEq, Debug)]
enum Grab {
    None,
    Move,
    Left,
    Right,
    Top,
    Bottom,
    TopLeft,
    TopRight,
    BottomLeft,
    BottomRight,
}

#[derive(Clone, Copy)]
struct Colors {
    bg: u32,
    card: u32,
    card_hi: u32,
    divider: u32,
    text_main: u32,
    text_sub: u32,
    text_muted: u32,
    accent: u32,
    accent_soft: u32,
    success: u32,
    warn: u32,
    danger: u32,
    danger_bg: u32,
    btn_idle: u32,
    btn_hover: u32,
    caption_hover: u32,
    caption_glyph: u32,
}

impl Colors {
    fn for_theme(theme: Theme) -> Colors {
        match theme {
            Theme::Dark => Colors {
                bg: rgb(0x10, 0x14, 0x18),
                card: rgb(0x17, 0x1C, 0x22),
                card_hi: rgb(0x1E, 0x24, 0x2C),
                divider: rgb(0x2A, 0x32, 0x3C),
                text_main: rgb(0xE8, 0xED, 0xF2),
                text_sub: rgb(0xB8, 0xC2, 0xCE),
                text_muted: rgb(0x8B, 0x96, 0xA5),
                accent: rgb(0x4C, 0xC2, 0xFF),
                accent_soft: rgb(0x14, 0x3A, 0x52),
                success: rgb(0x34, 0xD3, 0x99),
                warn: rgb(0xFF, 0xB4, 0x55),
                danger: rgb(0xFF, 0x6B, 0x6B),
                danger_bg: rgb(0x5A, 0x2A, 0x2A),
                btn_idle: rgb(0x24, 0x2C, 0x35),
                btn_hover: rgb(0x2E, 0x38, 0x43),
                caption_hover: rgb(0x2A, 0x32, 0x3C),
                caption_glyph: rgb(0xB8, 0xC2, 0xCE),
            },
            Theme::Light => Colors {
                bg: rgb(0xF2, 0xF4, 0xF7),
                card: rgb(0xFF, 0xFF, 0xFF),
                card_hi: rgb(0xEC, 0xF0, 0xF5),
                divider: rgb(0xDD, 0xE3, 0xEA),
                text_main: rgb(0x15, 0x20, 0x2B),
                text_sub: rgb(0x47, 0x53, 0x5F),
                text_muted: rgb(0x5A, 0x66, 0x74),
                accent: rgb(0x0D, 0x63, 0x96),
                accent_soft: rgb(0xCF, 0xE4, 0xF4),
                success: rgb(0x0E, 0x7A, 0x50),
                warn: rgb(0x9A, 0x5B, 0x00),
                danger: rgb(0xC5, 0x3B, 0x3B),
                danger_bg: rgb(0xF6, 0xDB, 0xDB),
                btn_idle: rgb(0xE4, 0xE9, 0xEF),
                btn_hover: rgb(0xD3, 0xDB, 0xE4),
                caption_hover: rgb(0xE0, 0xE6, 0xEC),
                caption_glyph: rgb(0x47, 0x53, 0x5F),
            },
        }
    }
}

pub struct App {
    hwnd: HWND,
    shared: Arc<Shared>,
    theme: Theme,
    colors: Colors,

    fonts: Vec<Font>,
    font_ui: usize,   // 12 微软雅黑
    font_ui_sm: usize, // 11
    font_ui_b: usize, // 12 半粗
    font_kpi: usize,  // 24 粗
    font_mono: usize, // 12 Consolas

    dpi: u32,
    w: i32,
    h: i32,
    /// 第一条可见日志行的下标（配合 log_follow 使用）。
    log_scroll: usize,
    /// true = 自动跟随最新日志（用户往上翻后被置为 false）。
    log_follow: bool,
    log_rows: usize,
    max_log_chars: usize,
    hover: Option<Ctl>,
    pressed: Option<Ctl>,
    dragging: bool,
    last_mouse: POINT,
    timer_ui_armed: bool,
    theme_glyph: u32,

    // ---- 自实现的窗口拖拽 / 缩放（无系统标题栏）----
    grab: Grab,
    grab_origin: POINT,      // 按下时的**屏幕**坐标
    grab_window: RECT,       // 按下时的窗口矩形
    last_click_ms: u64,      // 双击最大化用
    last_click_pt: POINT,
}

impl App {
    /// 供 main 用：先建 App（拿 shared），再建窗口，最后 bootstrap。
    pub fn new() -> App {
        let shared = Shared::new();
        let mut app = App {
            hwnd: NULL_HANDLE,
            shared,
            theme: Theme::Dark,
            colors: Colors::for_theme(Theme::Dark),
            fonts: Vec::new(),
            font_ui: 0,
            font_ui_sm: 0,
            font_ui_b: 0,
            font_kpi: 0,
            font_mono: 0,
            dpi: 96,
            w: 880,
            h: 720,
            log_scroll: 0,
            log_follow: true,
            log_rows: 0,
            max_log_chars: 60_000,
            hover: None,
            pressed: None,
            dragging: false,
            last_mouse: POINT { x: 0, y: 0 },
            timer_ui_armed: false,
            theme_glyph: 0xE706,
            grab: Grab::None,
            grab_origin: POINT { x: 0, y: 0 },
            grab_window: RECT::default(),
            last_click_ms: 0,
            last_click_pt: POINT { x: 0, y: 0 },
        };
        app.init_fonts();
        app
    }

    fn init_fonts(&mut self) {
        self.fonts.clear();
        // ⚠️ 字号必须**按 DPI 线性放大**：96 DPI = 1.0x 基准。
        // 曾经的写法是 `pt * k / 4`，而 96 DPI 时 k=4 ⇒ 1.0x 看似对，
        // 但 144 DPI 时 k=3 ⇒ **0.75x —— 字反而变小了**（用户实测反馈"文字太小"，
        // 见 ERROR.md E34）。直径/高度类尺寸（瓦片、按钮）同样必须乘这个数。
        let font_scale = (self.dpi as i32) * 100 / 96; // 96→100, 120→125, 144→150, 192→200
        let scale = |pt: i32| (pt * font_scale + 50) / 100;
        let faces = [
            "Microsoft YaHei UI", // 0 ui
            "Microsoft YaHei UI", // 1 ui sm
            "Microsoft YaHei UI", // 2 ui b
            "Microsoft YaHei UI", // 3 kpi
            "Consolas",           // 4 mono
        ];
        // 基准字号整体上调：Win32 的 pt 与浏览器/Web 的 px 观感不同，12pt 在这个窗口里偏小
        let sizes = [scale(14), scale(13), scale(14), scale(26), scale(13)];
        let weights = [FW_NORMAL, FW_NORMAL, FW_SEMIBOLD, FW_BOLD, FW_NORMAL];
        for i in 0..faces.len() {
            let mut f = Font::new(sizes[i], weights[i], faces[i]);
            if !f.is_valid() {
                f = Font::new(sizes[i], weights[i], "Segoe UI");
            }
            self.fonts.push(f);
        }
        self.font_ui = 0;
        self.font_ui_sm = 1;
        self.font_ui_b = 2;
        self.font_kpi = 3;
        self.font_mono = 4;
    }

    pub fn hwnd_ready_ptr(&self) -> *const isize {
        &self.shared.hwnd as *const _ as *const isize
    }

    /// 在 WM_NCCREATE 里回填窗口句柄（此时窗口已存在，但构造尚未返回）。
    fn set_hwnd(&mut self, hwnd: HWND) {
        self.hwnd = hwnd;
        *self.shared.hwnd.lock().unwrap_or_else(|e| e.into_inner()) = hwnd as isize;
        self.dpi = unsafe { GetDpiForWindow(hwnd) }.max(96);
        // DPI 只有在拿到窗口后才知道，所以字体要在这里按真实 DPI 重建一次
        self.init_fonts();
    }

    /// 窗口建好之后：DWM 外观 + 定时器 + 拉起后端。
    pub fn bootstrap(hwnd: HWND, app: &mut App) {
        let backdrop = crate::dwm::apply_glass(hwnd, app.theme == Theme::Dark);
        crate::trace::trace(&format!(
            "ui: bootstrap backdrop={} composition={} dpi={}",
            backdrop.label(),
            crate::dwm::composition_enabled(),
            app.dpi
        ));
        {
            let mut st = app.shared.lock();
            st.push_log(&format!(
                "[native] 界面已启动（v{}）· 玻璃材质：{} · DWM 合成：{}",
                backend::APP_VERSION,
                backdrop.label(),
                if crate::dwm::composition_enabled() { "开" } else { "关" }
            ));
        }
        app.shared.notify_ui();

        unsafe {
            SetTimer(hwnd, TIMER_UI, 250, std::ptr::null_mut());
            SetTimer(hwnd, TIMER_POLL, 1500, std::ptr::null_mut());
        }
        crate::trace::trace("ui: timers armed, starting backend");

        // 可脚本驱动的动作钩子（代理点不了界面，验证按钮链路只能靠它）：
        //   LH_UI_ACTION=refresh_pages|diagnose|pause|resume|start|stop
        // 启动 2.5s 后（等后端握手完）自动触发一次，并把结果写进 trace。
        if let Ok(action) = std::env::var("LH_UI_ACTION") {
            let action = action.trim().to_string();
            if !action.is_empty() {
                let shared = app.shared.clone();
                let hwnd_raw = hwnd as isize;
                crate::trace::trace(&format!("ui: scripted action queued: {}", action));
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(2500));
                    let shared_for_action = shared.clone();
                    let result = backend::control(&shared_for_action, &action, None);
                    {
                        let mut st = shared.lock();
                        match &result {
                            Ok(msg) => st.push_log(&format!("[verify] {} → {}", action, msg)),
                            Err(err) => st.push_log(&format!("[verify] {} 失败：{}", action, err)),
                        }
                    }
                    crate::trace::trace(&format!(
                        "ui: scripted action '{}' -> {:?}",
                        action, result
                    ));
                    // 让 UI 重绘，便于随后抓图核对
                    if hwnd_raw != 0 {
                        unsafe {
                            PostMessageW(hwnd_raw as HWND, WM_APP_BACKEND, 0, 0);
                        }
                    }
                    std::thread::sleep(std::time::Duration::from_millis(1200));
                    crate::trace::trace(&format!(
                        "ui: state after '{}': {:?}",
                        action,
                        crate::backend::describe_state(&shared.lock())
                    ));
                });
            }
        }

        backend::start_async(app.shared.clone());
    }

    // ================================================================ 布局
    /// 统一的 DPI 缩放：96 DPI = 1.0x。**所有长度（字号、圆角、行高、瓦片）都走它**，
    /// 避免再出现"字号与高度用了不同缩放系数"的错位（ERROR.md E34）。
    fn px(&self, logical: i32) -> i32 {
        (logical * (self.dpi as i32) * 100 / 96 + 50) / 100
    }
    fn m(&self) -> i32 {
        self.px(18).max(12)
    }
    fn title_h(&self) -> i32 {
        self.px(52).max(44)
    }
    fn card_radius(&self) -> i32 {
        self.px(14).max(10)
    }
    fn btn_radius(&self) -> i32 {
        self.px(8).max(6)
    }
    fn font_scale(&self) -> i32 {
        ((self.dpi as i32) * 100 / 96).max(100)
    }

    fn layout(&self) -> Layout {
        let m = self.m();
        let inner = RECT {
            left: m,
            top: self.title_h() + self.px(2),
            right: self.w - m,
            bottom: self.h - m,
        };
        let gap = self.px(10);

        // 状态卡
        let status_h = self.px(60).max(48);
        let status = RECT {
            left: inner.left,
            top: inner.top,
            right: inner.right,
            bottom: inner.top + status_h,
        };

        // 网页选择卡
        let page_h = self.px(60).max(48);
        let page = RECT {
            left: inner.left,
            top: status.bottom + gap,
            right: inner.right,
            bottom: status.bottom + gap + page_h,
        };

        // KPI + 进度：高度必须容得下【大数字 + 标签】两行，且随 DPI 一起放大
        let kpi_h = self.px(170).max(120);
        let kpi = RECT {
            left: inner.left,
            top: page.bottom + gap,
            right: inner.left + (inner.width() - gap) / 2,
            bottom: page.bottom + gap + kpi_h,
        };
        let progress = RECT {
            left: kpi.right + gap,
            top: kpi.top,
            right: inner.right,
            bottom: kpi.bottom,
        };

        // 按钮行
        let btn_h = self.px(46).max(36);
        let buttons = RECT {
            left: inner.left,
            top: kpi.bottom + gap,
            right: inner.right,
            bottom: kpi.bottom + gap + btn_h,
        };

        // 日志
        let log = RECT {
            left: inner.left,
            top: buttons.bottom + gap,
            right: inner.right,
            bottom: inner.bottom,
        };

        Layout {
            status,
            page,
            kpi,
            progress,
            buttons,
            log,
        }
    }

    /// 渲染探针：把**客户区**按当前状态重绘到给定 hdc（不经过 DWM、不碰屏幕）。
    ///
    /// 为什么需要它：`PrintWindow` 在无边框窗口上取不到客户区（返回的是 DWM 合成的背板），
    /// 于是"界面到底画成什么样"就没法自动化验证。这里直接调用我们自己的绘制代码，
    /// 把同一份布局画进内存 DC，落盘成 BMP 供核对 —— 见 `--render-probe`。
    pub fn render_to(&mut self, hdc: HDC, w: i32, h: i32) {
        self.w = w;
        self.h = h;
        self.paint(hdc);
    }

    // ================================================================ 绘制
    pub fn paint(&mut self, hdc: HDC) {
        let w = self.w;
        let h = self.h;
        let colors = self.colors;
        let layout = self.layout();
        let state = self.shared.lock();
        let pages_text = if state.selected_page.is_empty() {
            if state.pages.is_empty() {
                "[待检测] 点击右侧刷新获取浏览器标签页".to_string()
            } else {
                state.pages.join(" / ")
            }
        } else {
            state.selected_page.clone()
        };
        let state_for_paint = PaintState {
            status_text: state.status_text.clone(),
            device: state.device_id.clone(),
            pages_text,
            running: state.engine.running,
            paused: state.engine.paused,
            page_count: state.engine.page_count,
            video_count: state.engine.video_count,
            doc_count: state.engine.doc_count,
            task_text: state.engine.task_text.clone(),
            video_text: state.engine.video_text.clone(),
            quiz_text: state.engine.quiz_text.clone(),
            answer_mode: state.answer_mode.clone(),
            server_url: state.server_url.clone(),
            logs: state.logs.clone(),
            last_error: state.engine.last_error.clone(),
            maximized: unsafe { IsZoomed(self.hwnd) } != 0,
        };        drop(state);

        // 背景
        gdi::fill_rect(hdc, RECT { left: 0, top: 0, right: w, bottom: h }, colors.bg);

        self.paint_title_bar(hdc, w, state_for_paint.maximized);
        self.paint_card(hdc, layout.status, colors, &state_for_paint);
        self.paint_page_card(hdc, layout.page, colors, &state_for_paint);
        self.paint_kpi_progress(hdc, layout.kpi, layout.progress, colors, &state_for_paint);
        self.paint_buttons(hdc, layout.buttons, colors, &state_for_paint);
        self.paint_log(hdc, layout.log, colors, &state_for_paint);
    }

    fn paint_title_bar(&mut self, hdc: HDC, w: i32, maximized: bool) {
        let colors = self.colors;
        let th = self.title_h();
        let title_rc = RECT {
            left: 18,
            top: 0,
            right: w / 2,
            bottom: th,
        };
        gdi::text_in(hdc, &format!("学习助理 v{}", backend::APP_VERSION), title_rc, TextAlign::Left, colors.text_main, &self.fonts[self.font_ui_b]);

        // 主题切换 + 窗口三键（右侧，系统键位习惯：最小化 - □ ×）
        let btn_w = self.px(48).max(40);
        let btn_h = th;
        let x_start = w - btn_w * 3;
        let glyph_theme = if self.theme == Theme::Dark { 0xE706 } else { 0xE708 };
        let _ = glyph_theme;

        let controls = [
            (Ctl::Theme, x_start - btn_w, 0, btn_w, btn_h, glyph_theme),
            (Ctl::Min, x_start, 0, btn_w, btn_h, 0xE921),
            (Ctl::Max, x_start + btn_w, 0, btn_w, btn_h, if maximized { 0xE923 } else { 0xE922 }),
            (Ctl::Close, x_start + btn_w * 2, 0, btn_w, btn_h, 0xE8BB),
        ];
        for (ctl, x, y, bw, bh, glyph) in controls {
            let rc = RECT { left: x, top: y, right: x + bw, bottom: y + bh };
            let is_close = ctl == Ctl::Close;
            let hover = self.hover == Some(ctl);
            let pressed = self.pressed == Some(ctl);
            let bg = if is_close {
                if hover { rgb(0xC4, 0x2B, 0x1C) } else { 0 }
            } else if hover {
                colors.caption_hover
            } else {
                0
            };
            if bg != 0 {
                gdi::fill_rect(hdc, rc, bg);
            }
            let glyph_color = if is_close && hover {
                rgb(0xFF, 0xFF, 0xFF)
            } else {
                colors.caption_glyph
            };
            self.draw_segmdl(hdc, glyph, rc, glyph_color);
        }
    }

    /// 画 Segoe MDL2 字形（标题栏三键/主题用）。
    fn draw_segmdl(&mut self, hdc: HDC, glyph: u32, rc: RECT, color: u32) {
        // 标题栏按钮高度约 46，glyph 用 10pt 左右
        let size = self.px(11).max(9);
        let face = "Segoe MDL2 Assets";
        let mut font = Font::new(size, FW_NORMAL, face);
        if !font.is_valid() {
            font = Font::new(size, FW_NORMAL, "Segoe UI Symbol");
        }
        if let Some(ch) = char::from_u32(glyph) {
            let s = ch.to_string();
            gdi::text_in(hdc, &s, rc, TextAlign::Center, color, &font);
        }
    }

    fn paint_card(&self, hdc: HDC, rc: RECT, colors: Colors, st: &PaintState) {
        gdi::fill_round_rect(hdc, rc, self.card_radius(), colors.card);
        let m = 14;
        let mut x = rc.left + m;

        // 胶囊：「后端」
        let pill_rc = RECT { left: x, top: rc.top + self.px(13), right: x + self.px(56), bottom: rc.bottom - self.px(13) };
        gdi::pill(hdc, pill_rc, colors.accent_soft, "后端", colors.accent, &self.fonts[self.font_ui_b]);
        x = pill_rc.right + 12;

        // 连接状态（等宽字体，便于和地址对齐）
        let status_text = st.status_text.clone();
        gdi::text_in(
            hdc,
            &status_text,
            RECT { left: x, top: rc.top, right: rc.right - self.px(300), bottom: rc.bottom },
            TextAlign::Left,
            colors.accent,
            &self.fonts[self.font_mono],
        );

        // 右侧：设备指纹
        if !st.device.is_empty() {
            gdi::text_in(
                hdc,
                &format!("设备 {}", st.device),
                RECT { left: rc.right - self.px(300), top: rc.top, right: rc.right - m, bottom: rc.bottom },
                TextAlign::Right,
                colors.text_muted,
                &self.fonts[self.font_mono],
            );
        }
    }

    fn paint_page_card(&self, hdc: HDC, rc: RECT, colors: Colors, st: &PaintState) {
        gdi::fill_round_rect(hdc, rc, self.card_radius(), colors.card);
        let m = 14;
        let mut x = rc.left + m;

        gdi::text_in(
            hdc,
            "当前网页",
            RECT { left: x, top: rc.top, right: x + 70, bottom: rc.bottom },
            TextAlign::Left,
            colors.text_main,
            &self.fonts[self.font_ui_b],
        );
        x += 80;

        // 页面列表（简单画成一条可点区域：点击 = 刷新后弹出选择？本轮先显示当前页）
        let box_rc = RECT { left: x, top: rc.top + self.px(11), right: rc.right - self.px(190), bottom: rc.bottom - self.px(11) };
        gdi::fill_round_rect(hdc, box_rc, self.btn_radius(), colors.card_hi);
        let text_color = if st.pages_text.is_empty() || st.pages_text.starts_with('[') {
            colors.text_muted
        } else {
            colors.text_sub
        };
        gdi::text_in(hdc, &st.pages_text, box_rc.inset(10, 0), TextAlign::Left, text_color, &self.fonts[self.font_ui]);

        // 答题方式摘要（右侧）
        let mode = if st.answer_mode.is_empty() { "内部答题 API" } else { &st.answer_mode };
        gdi::text_in(
            hdc,
            &format!("答题方式：{}", mode),
            RECT { left: rc.right - self.px(190), top: rc.top, right: rc.right - m, bottom: rc.bottom },
            TextAlign::Right,
            colors.text_muted,
            &self.fonts[self.font_ui_sm],
        );
    }

    fn paint_kpi_progress(&self, hdc: HDC, kpi: RECT, progress: RECT, colors: Colors, st: &PaintState) {
        // KPI 卡
        gdi::fill_round_rect(hdc, kpi, self.card_radius(), colors.card);
        gdi::text_in(
            hdc,
            "当前页面任务感知",
            RECT { left: kpi.left + self.px(16), top: kpi.top + self.px(10), right: kpi.right - self.px(16), bottom: kpi.top + self.px(42) },
            TextAlign::Left,
            colors.text_main,
            &self.fonts[self.font_ui_b],
        );
        let tiles = [
            (st.video_count, "视频任务", colors.accent),
            (st.doc_count, "文档阅读", colors.success),
            (st.page_count, "处理页数", colors.warn),
        ];
        // 瓦片区 = 标题下方到底部内边距之间（字号放大后必须显式算，不能只给"大概的"高度）
        let pad = self.px(16);
        let tile_top = kpi.top + self.px(48);
        let tile_bottom = kpi.bottom - pad;
        let tile_gap = self.px(12);
        let tile_w = (kpi.width() - pad * 2 - tile_gap * 2) / 3;
        for (i, (count, label, color)) in tiles.iter().enumerate() {
            let tx = kpi.left + pad + i as i32 * (tile_w + tile_gap);
            let tile = RECT { left: tx, top: tile_top, right: tx + tile_w, bottom: tile_bottom };
            gdi::fill_round_rect(hdc, tile, self.px(10), colors.card_hi);
            // 数字在上、标签在下，各自占固定带，绝不重叠（E30 的修法）
            let label_h = self.px(28).max(20);
            let num_rc = RECT {
                left: tile.left,
                top: tile.top + self.px(4),
                right: tile.right,
                bottom: tile.bottom - label_h,
            };
            gdi::text_in(hdc, &count.to_string(), num_rc, TextAlign::Center, *color, &self.fonts[self.font_kpi]);
            let label_rc = RECT {
                left: tile.left,
                top: tile.bottom - label_h - self.px(4),
                right: tile.right,
                bottom: tile.bottom - self.px(4),
            };
            gdi::text_in(hdc, label, label_rc, TextAlign::Center, colors.text_muted, &self.fonts[self.font_ui_sm]);
        }

        // 进度卡
        gdi::fill_round_rect(hdc, progress, self.card_radius(), colors.card);
        let task_text = if st.last_error.is_empty() {
            format!("当前状态　{}", st.task_text)
        } else {
            format!("⚠ {}", st.last_error)
        };
        let rows = [
            (task_text, colors.accent, true),
            (format!("音视频进度　{}", st.video_text), colors.text_sub, false),
            (format!("答题进度　　{}", st.quiz_text), colors.text_sub, false),
        ];
        let row_h = progress.height() / 3;
        for (i, (text, color, bold)) in rows.iter().enumerate() {
            let rc = RECT {
                left: progress.left + self.px(18),
                top: progress.top + i as i32 * row_h,
                right: progress.right - self.px(18),
                bottom: progress.top + (i + 1) as i32 * row_h,
            };
            let font_idx = if *bold { self.font_ui_b } else { self.font_ui };
            gdi::text_in(hdc, text, rc, TextAlign::Left, *color, &self.fonts[font_idx]);
        }
    }

    /// 按钮定义（**绘制与命中测试共用同一份**，避免两者算出的宽度不一致）。
    /// 宽度按**实测文本宽度**算，字号放大后不会溢出按钮行（ERROR.md E34）。
    fn button_specs(&self, st: &PaintState) -> Vec<(Ctl, &'static str, i32)> {
        let labels: Vec<(Ctl, &'static str)> = vec![
            (Ctl::Start, if st.running { "正在运行…" } else { "启动刷课" }),
            (Ctl::Pause, if st.paused { "继续执行" } else { "暂停进程" }),
            (Ctl::Refresh, "检测/刷新网页"),
            (Ctl::Diagnose, "诊断页面"),
            (Ctl::Stop, "终止并退出"),
        ];
        let hdc = unsafe { GetDC(self.hwnd) };
        let mut specs = Vec::new();
        for (ctl, label) in labels {
            let measured = if hdc.is_null() {
                0
            } else {
                gdi::measure_text(hdc, label, &self.fonts[self.font_ui_b]).cx
            };
            if !hdc.is_null() {
                unsafe { ReleaseDC(self.hwnd, hdc) };
            }
            let pad = self.px(34);
            let min_w = match ctl {
                Ctl::Start => self.px(120),
                Ctl::Stop => self.px(108),
                _ => self.px(92),
            };
            let extra = if ctl == Ctl::Start { self.px(14) } else { 0 };
            specs.push((ctl, label, (measured + pad + extra).max(min_w)));
        }
        specs
    }

    /// 按钮行总宽（用于居中）。
    fn buttons_total_width(&self, specs: &[(Ctl, &'static str, i32)]) -> i32 {
        let gap = self.px(10);
        specs.iter().map(|s| s.2).sum::<i32>() + gap * (specs.len() as i32 - 1)
    }

    fn paint_buttons(&mut self, hdc: HDC, rc: RECT, colors: Colors, st: &PaintState) {
        let gap = self.px(10);
        let specs = self.button_specs(st);
        let total = self.buttons_total_width(&specs);
        let mut x = rc.left + (rc.width() - total) / 2;
        for (ctl, label, width) in specs {
            let btn = RECT { left: x, top: rc.top, right: x + width, bottom: rc.bottom };
            let hover = self.hover == Some(ctl);
            let mut fill = colors.btn_idle;
            let mut fg = colors.text_main;
            if ctl == Ctl::Start {
                fill = if st.running {
                    colors.card_hi
                } else if hover {
                    rgb(0x2E, 0x8F, 0xC5)
                } else {
                    colors.accent
                };
                fg = if st.running { colors.text_muted } else { rgb(0x0A, 0x16, 0x1F) };
            } else if ctl == Ctl::Stop {
                fill = if hover { rgb(0x7A, 0x3A, 0x3A) } else { colors.danger_bg };
                fg = colors.danger;
            } else if hover {
                fill = colors.btn_hover;
            }
            gdi::fill_round_rect(hdc, btn, self.btn_radius(), fill);
            gdi::text_in(hdc, label, btn, TextAlign::Center, fg, &self.fonts[self.font_ui_b]);
            x += width + gap;
        }
    }

    fn paint_log(&mut self, hdc: HDC, rc: RECT, colors: Colors, st: &PaintState) {
        gdi::fill_round_rect(hdc, rc, self.card_radius(), colors.card);
        gdi::text_in(
            hdc,
            "运行日志",
            RECT { left: rc.left + self.px(16), top: rc.top + self.px(10), right: rc.right - self.px(16), bottom: rc.top + self.px(38) },
            TextAlign::Left,
            colors.text_main,
            &self.fonts[self.font_ui_b],
        );

        // 日志区（裁剪圆角）
        let log_rc = RECT { left: rc.left + self.px(14), top: rc.top + self.px(46), right: rc.right - self.px(14), bottom: rc.bottom - self.px(12) };
        gdi::fill_round_rect(hdc, log_rc, 6, colors.card_hi);
        gdi::clip_round_rect(hdc, log_rc, 6);

        let line_h = self.px(22).max(16);
        let visible = (log_rc.height() / line_h).max(1) as usize;
        let total = st.logs.len();
        self.log_rows = total;

        // 滚动模型：log_scroll = **第一条可见行**的下标；None 表示"跟随底部"。
        // 新日志进来时若处于跟随状态就自动滚到底，用户往上翻过则保持不动。
        let max_first = total.saturating_sub(visible);
        let first = match self.log_follow {
            true => max_first,
            false => self.log_scroll.min(max_first),
        };
        self.log_scroll = first;

        let mut row = 0usize;
        for idx in first..total {
            if row >= visible {
                break;
            }
            let y = log_rc.top + (row as i32) * line_h + 4;
            gdi::text(
                hdc,
                &st.logs[idx],
                log_rc.left + 8,
                y,
                colors.text_sub,
                &self.fonts[self.font_mono],
            );
            row += 1;
        }
        gdi::reset_clip(hdc);
    }

    // ================================================================ 鼠标
    /// 无边框窗口的边框命中：返回属于哪条边（用于自实现缩放）。
    /// 判据：距客户区边缘 < EDGE 像素；四角优先（同时命中两条边）。
    fn border_hit(&self, x: i32, y: i32) -> Grab {
        const EDGE: i32 = 6;
        let left = x < EDGE;
        let right = x >= self.w - EDGE;
        let top = y < EDGE;
        let bottom = y >= self.h - EDGE;
        match (left, right, top, bottom) {
            (true, _, true, _) => Grab::TopLeft,
            (_, true, true, _) => Grab::TopRight,
            (true, _, _, true) => Grab::BottomLeft,
            (_, true, _, true) => Grab::BottomRight,
            (true, _, _, _) => Grab::Left,
            (_, true, _, _) => Grab::Right,
            (_, _, true, _) => Grab::Top,
            (_, _, _, true) => Grab::Bottom,
            _ => Grab::None,
        }
    }

    fn hit_test(&self, x: i32, y: i32) -> Option<Ctl> {
        let layout = self.layout();
        let th = self.title_h();
        let btn_w = self.px(48).max(40);
        let x_start = self.w - btn_w * 3;
        // 标题栏三键（含主题）
        if y < th {
            let controls = [
                (Ctl::Theme, x_start - btn_w),
                (Ctl::Min, x_start),
                (Ctl::Max, x_start + btn_w),
                (Ctl::Close, x_start + btn_w * 2),
            ];
            for (ctl, bx) in controls {
                if x >= bx && x < bx + btn_w {
                    return Some(ctl);
                }
            }
            return None; // 标题栏其余区域 = 拖拽区
        }
        // 按钮行（与绘制共用 button_specs，宽度必然一致）
        let st = {
            let s = self.shared.lock();
            PaintState {
                status_text: String::new(),
                device: String::new(),
                pages_text: String::new(),
                running: s.engine.running,
                paused: s.engine.paused,
                page_count: 0,
                video_count: 0,
                doc_count: 0,
                task_text: String::new(),
                video_text: String::new(),
                quiz_text: String::new(),
                answer_mode: String::new(),
                server_url: String::new(),
                logs: Vec::new(),
                last_error: String::new(),
                maximized: false,
            }
        };
        let gap = self.px(10);
        let specs = self.button_specs(&st);
        let total = self.buttons_total_width(&specs);
        let mut bx = layout.buttons.left + (layout.buttons.width() - total) / 2;
        for (ctl, _label, width) in specs {
            if x >= bx && x < bx + width && y >= layout.buttons.top && y < layout.buttons.bottom {
                return Some(ctl);
            }
            bx += width + gap;
        }
        // 网页选择行 = 点一下刷新（占位：本轮直接触发刷新）
        if y >= layout.page.top && y < layout.page.bottom {
            return Some(Ctl::Refresh);
        }
        None
    }

    fn on_mouse_move(&mut self, x: i32, y: i32) {
        // 正在拖拽/缩放：直接按位移算新矩形
        if self.grab != Grab::None {
            let mut pt = POINT { x, y };
            unsafe {
                crate::native::ClientToScreen(self.hwnd, &mut pt);
            }
            let dx = pt.x - self.grab_origin.x;
            let dy = pt.y - self.grab_origin.y;
            let mut rc = self.grab_window;
            let min_w = (640 * self.dpi as i32 / 96).max(520);
            let min_h = (520 * self.dpi as i32 / 96).max(420);
            match self.grab {
                Grab::Move => {
                    rc.left += dx;
                    rc.top += dy;
                    rc.right += dx;
                    rc.bottom += dy;
                }
                Grab::Left => rc.left += dx,
                Grab::Right => rc.right += dx,
                Grab::Top => rc.top += dy,
                Grab::Bottom => rc.bottom += dy,
                Grab::TopLeft => {
                    rc.left += dx;
                    rc.top += dy;
                }
                Grab::TopRight => {
                    rc.right += dx;
                    rc.top += dy;
                }
                Grab::BottomLeft => {
                    rc.left += dx;
                    rc.bottom += dy;
                }
                Grab::BottomRight => {
                    rc.right += dx;
                    rc.bottom += dy;
                }
                Grab::None => {}
            }
            if rc.width() < min_w {
                if matches!(self.grab, Grab::Left | Grab::TopLeft | Grab::BottomLeft) {
                    rc.left = rc.right - min_w;
                } else {
                    rc.right = rc.left + min_w;
                }
            }
            if rc.height() < min_h {
                if matches!(self.grab, Grab::Top | Grab::TopLeft | Grab::TopRight) {
                    rc.top = rc.bottom - min_h;
                } else {
                    rc.bottom = rc.top + min_h;
                }
            }
            unsafe {
                SetWindowPos(
                    self.hwnd,
                    std::ptr::null_mut(),
                    rc.left,
                    rc.top,
                    rc.width(),
                    rc.height(),
                    SWP_NOZORDER | SWP_NOACTIVATE,
                );
            }
            return;
        }

        let hit = self.hit_test(x, y);
        if hit != self.hover {
            self.hover = hit;
            unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
        }

        // 边框光标（无系统边框，自己按命中区域换光标）
        let border = self.border_hit(x, y);
        unsafe {
            let id = match border {
                Grab::Left | Grab::Right => 32644usize,   // IDC_SIZEWE
                Grab::Top | Grab::Bottom => 32645usize,   // IDC_SIZENS
                Grab::TopLeft | Grab::BottomRight => 32642usize, // IDC_SIZENWSE
                Grab::TopRight | Grab::BottomLeft => 32643usize, // IDC_SIZENESW
                _ => return,
            };
            let cur = LoadCursorW(std::ptr::null_mut(), id as *const u16);
            if !cur.is_null() {
                SetCursor(cur);
            }
        }
    }

    fn on_lbutton_down(&mut self, x: i32, y: i32) {
        // 先看边框（缩放优先）
        let border = self.border_hit(x, y);
        if border != Grab::None {
            self.grab = border;
            let mut pt = POINT { x, y };
            let mut rc = RECT::default();
            unsafe {
                crate::native::ClientToScreen(self.hwnd, &mut pt);
                GetWindowRect(self.hwnd, &mut rc);
                SetCapture(self.hwnd);
            }
            self.grab_origin = pt;
            self.grab_window = rc;
            return;
        }

        if y < self.title_h() {
            let hit = self.hit_test(x, y);
            match hit {
                Some(Ctl::Min) | Some(Ctl::Max) | Some(Ctl::Close) | Some(Ctl::Theme) => {
                    self.pressed = hit;
                    unsafe { SetCapture(self.hwnd) };
                }
                _ => {
                    // 标题栏空白 = 拖动窗口；双击 = 最大化/还原（自己实现）
                    let now = unsafe { GetTickCount() } as u64;
                    let is_double = now.saturating_sub(self.last_click_ms) < 400
                        && (x - self.last_click_pt.x).abs() < 6
                        && (y - self.last_click_pt.y).abs() < 6;
                    self.last_click_ms = now;
                    self.last_click_pt = POINT { x, y };
                    if is_double {
                        self.toggle_maximize();
                        return;
                    }
                    self.grab = Grab::Move;
                    let mut pt = POINT { x, y };
                    let mut rc = RECT::default();
                    unsafe {
                        crate::native::ClientToScreen(self.hwnd, &mut pt);
                        GetWindowRect(self.hwnd, &mut rc);
                        SetCapture(self.hwnd);
                    }
                    self.grab_origin = pt;
                    self.grab_window = rc;
                }
            }
            return;
        }

        let hit = self.hit_test(x, y);
        if hit.is_some() {
            self.pressed = hit;
            unsafe { SetCapture(self.hwnd) };
        }
        self.last_mouse = POINT { x, y };
    }

    fn toggle_maximize(&mut self) {
        unsafe {
            if IsZoomed(self.hwnd) != 0 {
                ShowWindow(self.hwnd, SW_RESTORE);
            } else {
                ShowWindow(self.hwnd, SW_MAXIMIZE);
            }
            InvalidateRect(self.hwnd, std::ptr::null(), 0);
        }
    }

    fn on_lbutton_up(&mut self, x: i32, y: i32) {
        if self.grab != Grab::None {
            self.grab = Grab::None;
            unsafe { ReleaseCapture() };
            return;
        }
        let was = self.pressed;
        self.pressed = None;
        self.dragging = false;
        unsafe { ReleaseCapture() };
        let Some(ctl) = was else { return };
        if !self.hit_test(x, y).is_some_and(|h| h == ctl) {
            return;
        }
        self.invoke(ctl);
    }

    fn invoke(&mut self, ctl: Ctl) {
        let hwnd = self.hwnd;
        let shared = self.shared.clone();
        crate::trace::trace(&format!("ui: invoke {:?}", ctl));
        match ctl {
            Ctl::Min => unsafe {
                ShowWindow(hwnd, SW_MINIMIZE);
            },
            Ctl::Max => unsafe {
                if IsZoomed(hwnd) != 0 {
                    ShowWindow(hwnd, SW_RESTORE);
                } else {
                    ShowWindow(hwnd, SW_MAXIMIZE);
                }
                InvalidateRect(hwnd, std::ptr::null(), 0);
            },
            Ctl::Close => unsafe {
                SendMessageW(hwnd, WM_CLOSE, 0, 0);
            },
            Ctl::Theme => {
                self.theme = if self.theme == Theme::Dark { Theme::Light } else { Theme::Dark };
                self.colors = Colors::for_theme(self.theme);
                let backdrop = crate::dwm::apply_glass(hwnd, self.theme == Theme::Dark);
                {
                    let mut st = self.shared.lock();
                    st.push_log(&format!(
                        "[外观] 已切换{}主题 · 玻璃材质 {}",
                        if self.theme == Theme::Dark { "深色" } else { "浅色" },
                        backdrop.label()
                    ));
                }
                self.shared.notify_ui();
            }
            Ctl::Start => {
                spawn_action(shared, "start", None);
            }
            Ctl::Pause => {
                let paused = self.shared.lock().engine.paused;
                spawn_action(shared, if paused { "resume" } else { "pause" }, None);
            }
            Ctl::Stop => {
                spawn_action(shared, "stop", None);
            }
            Ctl::Refresh => {
                spawn_action(shared, "refresh_pages", None);
            }
            Ctl::Diagnose => {
                spawn_action(shared, "diagnose", None);
            }
        }
    }

    pub fn wnd_proc(&mut self, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
        match msg {
            WM_PAINT => {
                let mut ps = PAINTSTRUCT::default();
                let hdc = unsafe { BeginPaint(self.hwnd, &mut ps) };
                if !hdc.is_null() {
                    self.paint(hdc);
                    unsafe { EndPaint(self.hwnd, &ps) };
                }
                0
            }
            WM_SIZE => {
                self.w = (lp & 0xFFFF) as i32;
                self.h = ((lp >> 16) & 0xFFFF) as i32;
                unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
                0
            }
            WM_ERASEBKGND => 1, // 全自绘，不需要擦背景
            WM_MOUSEMOVE => {
                let x = (lp & 0xFFFF) as i32;
                let y = ((lp >> 16) & 0xFFFF) as i32;
                self.on_mouse_move(x, y);
                0
            }
            WM_LBUTTONDOWN => {
                let x = (lp & 0xFFFF) as i32;
                let y = ((lp >> 16) & 0xFFFF) as i32;
                self.on_lbutton_down(x, y);
                0
            }
            WM_LBUTTONUP => {
                let x = (lp & 0xFFFF) as i32;
                let y = ((lp >> 16) & 0xFFFF) as i32;
                self.on_lbutton_up(x, y);
                0
            }
            WM_MOUSEWHEEL => {
                let delta = ((wp >> 16) as i16) as i32;
                // 向上滚（delta>0）= 看更早的日志 ⇒ 关掉跟随并回退一行
                if delta > 0 {
                    self.log_follow = false;
                    self.log_scroll = self.log_scroll.saturating_sub(3);
                } else {
                    self.log_scroll = self.log_scroll.saturating_add(3);
                    // 已经到最底就恢复跟随
                    if self.log_scroll + 1 >= self.log_rows.saturating_sub(1) {
                        self.log_follow = true;
                    }
                }
                unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
                0
            }
            WM_TIMER => {
                if wp == TIMER_UI {
                    // 检查是否有日志要补充（简单起见：有更新就重绘）
                    let has_new = {
                        let st = self.shared.lock();
                        st.logs.len() > 0
                    };
                    if has_new && !self.timer_ui_armed {
                        self.timer_ui_armed = true;
                    }
                    // 每 250ms 抽一次：如果 backend 事件改过 state，重绘
                    let _ = has_new;
                    unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
                } else if wp == TIMER_POLL {
                    let st = self.shared.lock();
                    let connected = st.connected;
                    drop(st);
                    if connected {
                        let shared = self.shared.clone();
                        std::thread::spawn(move || backend::refresh_status(shared));
                    }
                }
                0
            }
            WM_APP_BACKEND => {
                unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
                0
            }
            WM_CLOSE => {
                backend::shutdown(self.shared.clone());
                unsafe {
                    DestroyWindow(self.hwnd);
                }
                0
            }
            WM_DESTROY => {
                unsafe {
                    KillTimer(self.hwnd, TIMER_UI);
                    KillTimer(self.hwnd, TIMER_POLL);
                    PostQuitMessage(0);
                }
                0
            }
            WM_GETMINMAXINFO => {
                let info = lp as *mut MINMAXINFO;
                if !info.is_null() {
                    let scale = self.dpi as i32 / 96;
                    unsafe {
                        (*info).ptMinTrackSize = POINT {
                            x: (640 * scale).max(520),
                            y: (520 * scale).max(420),
                        };
                        // 无边框窗口默认"最大化"会盖住任务栏，必须自己夹到工作区
                        let mon = MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST);
                        let mut mi = MONITORINFO {
                            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                            ..Default::default()
                        };
                        if !mon.is_null() && GetMonitorInfoW(mon, &mut mi) != 0 {
                            let work_w = mi.rcWork.width();
                            let work_h = mi.rcWork.height();
                            // 以"相对显示器工作区原点"的形式给出最大跟踪尺寸
                            // （POINT 与 RECT 前 8 字节同构，直接转型）
                            SetRect(
                                &mut (*info).ptMaxPosition as *mut POINT as *mut RECT,
                                mi.rcWork.left - mi.rcMonitor.left,
                                mi.rcWork.top - mi.rcMonitor.top,
                                0,
                                0,
                            );
                            (*info).ptMaxSize = POINT { x: work_w, y: work_h };
                            (*info).ptMaxTrackSize = POINT { x: work_w, y: work_h };
                        }
                    }
                }
                0
            }
            _ => unsafe { DefWindowProcW(self.hwnd, msg, wp, lp) },
        }
    }
}

struct Layout {
    status: RECT,
    page: RECT,
    kpi: RECT,
    progress: RECT,
    buttons: RECT,
    log: RECT,
}

struct PaintState {
    status_text: String,
    device: String,
    pages_text: String,
    running: bool,
    paused: bool,
    page_count: i64,
    video_count: i64,
    doc_count: i64,
    task_text: String,
    video_text: String,
    quiz_text: String,
    answer_mode: String,
    server_url: String,
    logs: Vec<String>,
    last_error: String,
    maximized: bool,
}

fn spawn_action(shared: Arc<Shared>, action: &'static str, params: Option<Value>) {
    std::thread::spawn(move || {
        let result = backend::control(&shared, action, params);
        let mut st = shared.lock();
        match result {
            Ok(msg) => {
                st.push_log(&format!("[native] {} → {}", action, msg));
                if let Some(flash) = st.flash.take() {
                    let _ = flash;
                }
            }
            Err(err) => {
                st.push_log(&format!("[native] {} 失败：{}", action, err));
                st.status_text = format!("⚠ {}", err);
            }
        }
        drop(st);
        shared.notify_ui();
    });
}

// ================================================================ 窗口
unsafe extern "system" fn wnd_proc_static(
    hwnd: HWND,
    msg: u32,
    wp: WPARAM,
    lp: LPARAM,
) -> LRESULT {
    if msg == WM_NCCREATE {
        // 创建时把 App 指针存进 GWLP_USERDATA，并把 hwnd 回填给 App。
        // ⚠️ 这里必须回填 hwnd：App::bootstrap 之后后台线程要靠它 PostMessage。
        let create = lp as *const CREATESTRUCTW;
        let app_ptr = (*create).lpCreateParams as *mut App;
        if !app_ptr.is_null() {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, app_ptr as isize);
            (*app_ptr).set_hwnd(hwnd);
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    let user = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut App;
    if user.is_null() {
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    let app = &mut *user;
    app.wnd_proc(msg, wp, lp)
}

#[repr(C)]
struct CREATESTRUCTW {
    lpCreateParams: *mut std::ffi::c_void,
}

/// 创建主窗口；``app_ptr`` 是 `*mut App`（Box::into_raw 的产物）。
pub fn create_main_window(app_ptr: *mut App) -> HWND {
    unsafe {
        let class_w = wide(CLASS_NAME);
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: 0,
            lpfnWndProc: Some(wnd_proc_static),
            cbClsExtra: 0,
            cbWndExtra: 0,
            hInstance: GetModuleHandleW(std::ptr::null()),
            hIcon: std::ptr::null_mut(),
            hCursor: LoadCursorW(std::ptr::null_mut(), IDC_ARROW as *const u16),
            hbrBackground: std::ptr::null_mut(),
            lpszMenuName: std::ptr::null(),
            lpszClassName: class_w.as_ptr(),
            hIconSm: std::ptr::null_mut(),
        };
        RegisterClassExW(&wc);

        let title_w = wide("学习助理");
        // ⚠️ **必须用 WS_POPUP，不能用 WS_OVERLAPPED**（ERROR.md E33 实测）：
        // `WS_OVERLAPPED` 的值就是 0x00000000，等于"不指定任何样式"，
        // 于是 CreateWindowEx 套用**默认顶层窗口样式**（含 WS_CAPTION | WS_SYSMENU |
        // WS_MINIMIZEBOX | WS_MAXIMIZEBOX）—— 实测窗口样式读回来是 0x16C00000，
        // `CAPTION=True`，标题栏仍由 DWM 画。WS_POPUP 才是真正的"无边框"。
        // WS_EX_APPWINDOW 保证它仍然出现在任务栏与 Alt+Tab 里。
        let style = WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN;

        // 按屏幕 DPI 缩放并居中：固定 880x720 在 150% 缩放下会超出屏幕被裁掉（实测 E30）
        let screen_w = GetSystemMetrics(0);
        let screen_h = GetSystemMetrics(1);
        let scale = ((screen_w as f32 / 1920.0).clamp(1.0, 2.0)).min(1.5);
        let win_w = (880.0 * scale) as i32;
        let win_h = (720.0 * scale) as i32;
        let x = ((screen_w - win_w) / 2).max(0);
        let y = ((screen_h - win_h) / 2).max(0);
        crate::trace::trace(&format!(
            "ui: screen={}x{} window={}x{} at {},{} scale={:.2}",
            screen_w, screen_h, win_w, win_h, x, y, scale
        ));

        let hwnd = CreateWindowExW(
            WS_EX_APPWINDOW,
            class_w.as_ptr(),
            title_w.as_ptr(),
            style,
            x,
            y,
            win_w,
            win_h,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            GetModuleHandleW(std::ptr::null()),
            app_ptr as *mut std::ffi::c_void,
        );
        if hwnd.is_null() {
            return hwnd;
        }
        // 客户区底色用窗口类画刷兜底：我们的自绘是"每帧全画"，
        // 但窗口首次显示/尺寸变化时系统可能先擦一遍背景，
        // 若 hbrBackground 为空会露出背板（实测：露出一条浅蓝色带，见 ERROR.md E33）。
        let bg = CreateSolidBrush(rgb(0x10, 0x14, 0x18));
        if !bg.is_null() {
            SetClassLongPtrW(hwnd, GCLP_HBRBACKGROUND, bg as isize);
        }
        ShowWindow(hwnd, SW_SHOW);
        hwnd
    }
}
