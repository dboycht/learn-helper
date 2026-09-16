//! 窗口与自绘界面（Win32 + GDI）。
//!
//! 布局：自绘标题栏（含拖拽/三键）→ 状态卡 → 网页选择 → KPI/进度 → 按钮 → 日志。
//! 全部纯 GDI 绘制；颜色方案与 WinUI 版同一套令牌（深色为主，可切浅色）。

use std::sync::Arc;

use crate::backend::{self, Shared};
use crate::gdi::{self, Font, TextAlign};
use crate::json::Value;
use crate::native::*;

/// 供弹窗（about.rs 等）复用的字体类型与索引。
pub type FontOwned = Font;
pub const DFONT_UI: usize = 0;
pub const DFONT_UI_SM: usize = 1;
pub const DFONT_UI_B: usize = 2;
pub const DFONT_MONO: usize = 3;

/// 弹窗用的一套字体（主窗口之外的第二套）。
pub fn make_dialog_fonts() -> Vec<FontOwned> {
    let specs = [
        (13, FW_NORMAL, "Microsoft YaHei UI"),   // 0 ui
        (12, FW_NORMAL, "Microsoft YaHei UI"),   // 1 ui sm
        (13, FW_SEMIBOLD, "Microsoft YaHei UI"), // 2 ui b
        (12, FW_NORMAL, "Consolas"),             // 3 mono
    ];
    let mut fonts = Vec::new();
    for (pt, weight, face) in specs {
        let mut f = Font::new(pt, weight, face);
        if !f.is_valid() {
            f = Font::new(pt, weight, "Segoe UI");
        }
        fonts.push(f);
    }
    fonts
}

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
    /// 视频倍速切换（点击在 1.0 / 1.5 / 2.0 / 3.0 之间循环）
    Speed,
    /// 「当前网页」下拉选择（点网页框打开选择器）
    Pages,
    /// 「答题设置」对话框
    Settings,
    /// 「关于」弹窗
    About,
    Min,
    Max,
    Close,
}

#[derive(Clone, Copy, PartialEq)]
pub enum Theme {
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

/// 配色令牌（弹窗 about.rs 复用同一套）。
#[derive(Clone, Copy)]
pub struct Colors {
    pub bg: u32,
    pub card: u32,
    pub card_hi: u32,
    pub divider: u32,
    pub text_main: u32,
    pub text_sub: u32,
    pub text_muted: u32,
    pub accent: u32,
    pub accent_soft: u32,
    pub success: u32,
    pub warn: u32,
    pub danger: u32,
    pub danger_bg: u32,
    pub btn_idle: u32,
    pub btn_hover: u32,
    pub caption_hover: u32,
    pub caption_glyph: u32,
}

impl Colors {
    pub fn for_theme(theme: Theme) -> Colors {
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
    /// 上一次绘制时的状态版本号：只有它变了才重绘（避免每 250ms 无条件重画整窗）
    painted_rev: u64,
    /// 累计重绘次数（诊断用：LH_UI_PAINT_REPORT 时周期性落盘）。
    /// 用原子量而不是 App 字段，方便后台线程读取（App 本身不是 Send）。
    paint_count: std::sync::Arc<std::sync::atomic::AtomicU64>,
    theme_glyph: u32,

    // ---- 自实现的窗口拖拽 / 缩放（无系统标题栏）----
    grab: Grab,
    grab_origin: POINT,      // 按下时的**屏幕**坐标
    grab_window: RECT,       // 按下时的窗口矩形
    last_click_ms: u64,      // 双击最大化用
    last_click_pt: POINT,
    /// `LH_UI_ACTION` 的值（仅验证用；settings* 类动作需要 UI 线程建对话框）
    scripted_action: String,
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
            painted_rev: 0,
            paint_count: std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0)),
            theme_glyph: 0xE706,
            grab: Grab::None,
            grab_origin: POINT { x: 0, y: 0 },
            grab_window: RECT::default(),
            last_click_ms: 0,
            last_click_pt: POINT { x: 0, y: 0 },
            scripted_action: std::env::var("LH_UI_ACTION").unwrap_or_default().trim().to_string(),
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

    /// 窗口建好之后：套用最基础的窗口外观 + 定时器 + 拉起后端。
    pub fn bootstrap(hwnd: HWND, app: &mut App) {
        // 纯色正常窗口：不做玻璃/背板（用户要求，见 dwm.rs 顶部说明）
        crate::dwm::apply_plain(hwnd, app.theme == Theme::Dark);
        crate::trace::trace(&format!(
            "ui: bootstrap plain solid ui, dpi={}",
            app.dpi
        ));
        {
            let mut st = app.shared.lock();
            st.push_log(&format!(
                "[native] 界面已启动（v{}）· 纯色界面",
                backend::APP_VERSION
            ));
        }
        app.shared.notify_ui();

        // 验证钩子：注入一串**样例网页标题**（`|` 分隔），让"网页下拉"能在没有真实浏览器的
        // 情况下被端到端验证（见 native\pages_probe.ps1）。正常运行时不设置这个变量。
        if let Ok(list) = std::env::var("LH_PROBE_PAGES") {
            let items: Vec<String> = list
                .split('|')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            if !items.is_empty() {
                let mut st = app.shared.lock();
                st.pages = items.clone();
                st.push_log(&format!("[native] (验证) 注入样例网页 {} 个", items.len()));
                drop(st);
                crate::trace::trace(&format!("ui: LH_PROBE_PAGES injected {} item(s)", items.len()));
            }
        }

        unsafe {
            SetTimer(hwnd, TIMER_UI, 250, std::ptr::null_mut());
            SetTimer(hwnd, TIMER_POLL, 1500, std::ptr::null_mut());
        }
        crate::trace::trace("ui: timers armed, starting backend");

        // 诊断：每 5 秒把累计重绘次数落盘一次（验证"空闲时不再周期性重画"）。
        // 修复前应当 ≈ 4 次/秒（250ms 无条件重绘）；修复后空闲期应接近 0。
        if std::env::var("LH_UI_PAINT_REPORT").is_ok() {
            let counter = app.paint_count.clone();
            std::thread::spawn(move || {
                let mut last = 0u64;
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    let now = counter.load(std::sync::atomic::Ordering::Relaxed);
                    crate::trace::trace(&format!(
                        "paint-report: paints in last 5s = {} (total {})",
                        now.saturating_sub(last),
                        now
                    ));
                    last = now;
                }
            });
        }

        // 可脚本驱动的动作钩子（代理点不了界面，验证按钮链路只能靠它）：
        //   LH_UI_ACTION=refresh_pages|diagnose|pause|resume|start|stop|speed|settings|
        //                settings_save|settings_cancel|pages|pages_select
        // 启动 2.5s 后（等后端握手完）自动触发一次，并把结果写进 trace。
        if let Ok(action) = std::env::var("LH_UI_ACTION") {
            let action = action.trim().to_string();
            if !action.is_empty() {
                let shared = app.shared.clone();
                let hwnd_raw = hwnd as isize;
                crate::trace::trace(&format!("ui: scripted action queued: {}", action));
                // 「答题设置」/「网页下拉」类动作必须由 UI 线程建窗口：PostMessage 回主窗口
                if action.starts_with("settings") || action.starts_with("pages") {
                    let action_ui = action.clone();
                    std::thread::spawn(move || {
                        // 保存/取消要先把后端跑起来（否则 connected=false 直接拒绝保存）
                        std::thread::sleep(std::time::Duration::from_millis(3500));
                        crate::trace::trace(&format!("ui: scripted action '{}' -> UI thread", action_ui));
                        if hwnd_raw != 0 {
                            unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_ACTION, 0, 0) };
                        }
                    });
                } else {
                    let action_bg = action.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_millis(2500));
                        // speed 不走 /api/control（它是本地设置），单独分派
                        if action_bg == "speed" {
                            cycle_video_speed(shared.clone());
                            std::thread::sleep(std::time::Duration::from_millis(1500));
                            let speed = shared.lock().video_speed;
                            crate::trace::trace(&format!(
                                "ui: scripted 'speed' -> video_speed={}",
                                speed
                            ));
                        } else {
                            let shared_for_action = shared.clone();
                            let result = backend::control(&shared_for_action, &action_bg, None);
                            {
                                let mut st = shared.lock();
                                match &result {
                                    Ok(msg) => st.push_log(&format!(
                                        "[verify] {} → {}",
                                        action_bg, msg
                                    )),
                                    Err(err) => st.push_log(&format!(
                                        "[verify] {} 失败：{}",
                                        action_bg, err
                                    )),
                                }
                            }
                            crate::trace::trace(&format!(
                                "ui: scripted action '{}' -> {:?}",
                                action_bg, result
                            ));
                        }
                        // 让 UI 重绘，便于随后抓图核对
                        if hwnd_raw != 0 {
                            unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_BACKEND, 0, 0) };
                        }
                        std::thread::sleep(std::time::Duration::from_millis(1200));
                        crate::trace::trace(&format!(
                            "ui: state after '{}': {:?}",
                            action_bg,
                            crate::backend::describe_state(&shared.lock())
                        ));
                    });
                }
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
        // 探针可选注入：给个**超长页面标题**，用来验证"文字不会溢到按钮上"（省略号裁剪）。
        // 默认不注入，正常渲染/正常运行都不受影响。
        if let Ok(long) = std::env::var("LH_PROBE_LONG_TITLE") {
            if !long.is_empty() {
                let mut st = self.shared.lock();
                st.pages = vec![long.clone()];
                st.status_text = long;
            }
        }
        // 🔎 布局体检（只在渲染探针里跑）：把"网页选择行"各控件的**实测矩形**和
        // 按钮文字的**实测宽度**落盘。这样"文字有没有溢出按钮"是**用数字判断**，
        // 不用靠肉眼看图（本项目的规矩：先取事实，再读代码，见 ERROR.md E26/E39）。
        let l = self.layout();
        let btn = self.refresh_rect(l.page);
        let speed = self.speed_rect(l.page);
        let text_w = self.measure_label("检测/刷新网页", self.font_ui_sm);
        let page_box_w = l.page.right - self.px(16) - (self.px(16) + self.px(80));
        crate::trace::trace(&format!(
            "probe-layout: client={}x{} page_box_w={} (placeholder_text={}) refresh=[{},{}) w={} text={} pad_lr={} speed=[{},{}) answer_w={} min_w={}",
            w,
            h,
            page_box_w,
            self.measure_label("[待检测] 点击右侧刷新获取浏览器标签页", self.font_ui),
            btn.left,
            btn.right,
            btn.width(),
            text_w,
            ((btn.width() - text_w) / 2).max(0),
            speed.left,
            speed.right,
            self.answer_mode_width(),
            self.min_content_width(),
        ));
        self.paint(hdc);
    }

    // ================================================================ 绘制
    pub fn paint(&mut self, hdc: HDC) {
        let w = self.w;
        let h = self.h;
        let colors = self.colors;
        let layout = self.layout();
        let state = self.shared.lock();
        // 网页框里显示什么（2026-09-16 起它**可点**，所以文案要引导用户去点）：
        // - 已选：显示选中的那一页；
        // - 未选但已检测到 N 个：提示"点此处选择"，而不是把 N 个标题全糊在一起
        //   （那样一长就被省略号截掉，等于什么都没说）；
        // - 还没检测：提示先点「检测/刷新网页」。
        let pages_text = if !state.selected_page.is_empty() {
            state.selected_page.clone()
        } else if state.pages.is_empty() {
            "[待检测] 点「检测/刷新网页」获取浏览器标签页".to_string()
        } else if state.pages.len() == 1 {
            format!("未选择 —— 点此处选中：{}", state.pages[0])
        } else {
            format!("未选择（已检测到 {} 个网页）—— 点此处选择", state.pages.len())
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
            video_speed: if state.video_speed > 0.0 { state.video_speed } else { 2.0 },
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

        // 标题栏右侧：【关于】【答题设置】【主题切换】+ 窗口三键
        // （系统键位习惯：最小化 - □ ×；⚠️ 控件一律靠左数，右边被系统三键占着，E-33 记过）
        let btn_w = self.px(48).max(40);
        let btn_h = th;
        let x_start = w - btn_w * 3;
        let glyph_theme = if self.theme == Theme::Dark { 0xE706 } else { 0xE708 };

        let controls = [
            (Ctl::About, x_start - btn_w * 3, 0, btn_w, btn_h, 0xE946), // E946 = Info
            (Ctl::Settings, x_start - btn_w * 2, 0, btn_w, btn_h, 0xE713), // E713 = Settings
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
    fn draw_segmdl(&self, hdc: HDC, glyph: u32, rc: RECT, color: u32) {
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
        // ⚠️ 状态串里带地址与版本号、长度不可控 ⇒ 用省略号裁剪版，别盖到右边"设备指纹"上
        let status_text = st.status_text.clone();
        gdi::text_ellipsis(
            hdc,
            &status_text,
            RECT { left: x, top: rc.top, right: rc.right - self.px(300), bottom: rc.bottom },
            TextAlign::Left,
            colors.accent,
            &self.fonts[self.font_mono],
        );

        // 右侧：设备指纹
        if !st.device.is_empty() {
            gdi::text_ellipsis(
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
        let m = self.px(16);

        // 从左到右：标签 → 网页框 → 「检测/刷新网页」按钮 → 倍速 → 答题方式
        // ⚠️ 这里所有横坐标都**按同一套宽度常量推导**，并且绘制与命中共用
        // `page_box_rect()` / `refresh_rect()` / `speed_rect()` 三个函数。
        // 之前是各处手写偏移，结果倍速胶囊压在网页框上、而"检测/刷新网页"根本没有按钮
        // （用户实测反馈被遮挡，见 ERROR.md E40）。
        let mut x = rc.left + m;
        gdi::text_in(
            hdc,
            "当前网页",
            RECT { left: x, top: rc.top, right: x + self.px(70), bottom: rc.bottom },
            TextAlign::Left,
            colors.text_main,
            &self.fonts[self.font_ui_b],
        );
        x += self.px(80);

        // 网页框（宽度 = 到"刷新按钮"为止）：**可点** ⇒ 打开「当前网页」下拉选择
        let box_rc = self.page_box_rect(rc);
        let box_hover = self.hover == Some(Ctl::Pages);
        gdi::fill_round_rect(
            hdc,
            box_rc,
            self.btn_radius(),
            if box_hover { colors.btn_hover } else { colors.card_hi },
        );
        let text_color = if st.pages_text.is_empty() || st.pages_text.starts_with('[') {
            colors.text_muted
        } else {
            colors.text_sub
        };
        // 右侧留出 ▾ 指示器（点击热区/下拉的视觉线索）
        let caret_w = self.px(22);
        // ⚠️ 页面标题长度不可控：必须用**带省略号裁剪**的版本，否则长标题会直接
        // 盖到右边「检测/刷新网页」按钮上（用户 2026-09-16 反馈"文字溢出"，见 gdi::text_ellipsis）
        gdi::text_ellipsis(
            hdc,
            &st.pages_text,
            RECT {
                left: box_rc.left + self.px(10),
                top: box_rc.top,
                right: box_rc.right - caret_w,
                bottom: box_rc.bottom,
            },
            TextAlign::Left,
            text_color,
            &self.fonts[self.font_ui],
        );
        let caret_rc = RECT {
            left: box_rc.right - caret_w,
            top: box_rc.top,
            right: box_rc.right - self.px(4),
            bottom: box_rc.bottom,
        };
        self.draw_segmdl(hdc, 0xE70Du32, caret_rc, colors.text_muted); // E70D = ChevronDown

        // 「检测/刷新网页」按钮（独立控件，不再是"整行可点"）
        let btn = self.refresh_rect(rc);
        let btn_hover = self.hover == Some(Ctl::Refresh);
        let btn_fill = if btn_hover { colors.accent } else { colors.card_hi };
        gdi::fill_round_rect(hdc, btn, self.btn_radius(), btn_fill);
        gdi::text_in(
            hdc,
            "检测/刷新网页",
            btn,
            TextAlign::Center,
            if btn_hover { colors.bg } else { colors.accent },
            &self.fonts[self.font_ui_sm],
        );

        // 倍速（可点：循环 1.0 / 1.5 / 2.0 / 3.0）；运行中不响应
        let speed_rc = self.speed_rect(rc);
        let speed_hover = self.hover == Some(Ctl::Speed);
        let speed_fill = if speed_hover && !st.running {
            colors.btn_hover
        } else {
            colors.card_hi
        };
        gdi::fill_round_rect(hdc, speed_rc, self.btn_radius(), speed_fill);
        let speed_fg = if st.running { colors.text_muted } else { colors.accent };
        gdi::text_in(
            hdc,
            &format!("倍速 {:.1}x", st.video_speed),
            speed_rc,
            TextAlign::Center,
            speed_fg,
            &self.fonts[self.font_ui_sm],
        );

        // 答题方式摘要（最右）
        let mode = if st.answer_mode.is_empty() { "内部答题 API" } else { &st.answer_mode };
        gdi::text_in(
            hdc,
            &format!("答题方式：{}", mode),
            RECT {
                left: speed_rc.right + self.px(10),
                top: rc.top,
                right: rc.right - m,
                bottom: rc.bottom,
            },
            TextAlign::Right,
            colors.text_muted,
            &self.fonts[self.font_ui_sm],
        );
    }

    /// **客户区最小高度**：各固定行之和 + 日志区最小高度。
    /// 同样用于 `ptMinTrackSize` 与拖拽夹取，保证"缩到最小"时布局仍然成立。
    pub fn min_content_height(&self) -> i32 {
        let m = self.px(18).max(12); // 与 m() 一致
        let gap = self.px(10);
        let title = self.title_h() + self.px(2);
        let status = self.px(60).max(48);
        let page = self.px(60).max(48);
        let kpi = self.px(170).max(120);
        let buttons = self.px(46).max(36);
        let log_min = self.px(140); // 日志区至少能看到几行
        title + status + page + kpi + buttons + log_min + gap * 4 + m
    }

    /// 量一段文本的像素宽度（临时取一次窗口 DC，量完立刻释放）。
    ///
    /// ⚠️ 这是"按钮宽度按文本实测"的**唯一入口**：测量与绘制**必须用同一个字体槽**，
    /// 否则会出现"量出来够宽、画出来溢出"这种最难查的不一致。
    fn measure_label(&self, text: &str, font: usize) -> i32 {
        let hdc = unsafe { GetDC(self.hwnd) };
        if hdc.is_null() {
            return 0;
        }
        let w = gdi::measure_text(hdc, text, &self.fonts[font]).cx;
        unsafe { ReleaseDC(self.hwnd, hdc) };
        w
    }

    /// 网页框右边界（= 刷新按钮左边留 10px 间距）。绘制与命中共用。
    fn page_box_right(&self, card: RECT) -> i32 {
        self.refresh_rect(card).left - self.px(10)
    }

    /// 「当前网页」输入框矩形（**绘制与命中共用**）：点它就是打开网页下拉选择。
    fn page_box_rect(&self, card: RECT) -> RECT {
        RECT {
            left: card.left + self.px(16) + self.px(80),
            top: card.top + self.px(11),
            right: self.page_box_right(card),
            bottom: card.bottom - self.px(11),
        }
    }

    /// 倍速胶囊矩形（**绘制与命中共用**）。它占最右侧靠内的位置。
    fn speed_rect(&self, card: RECT) -> RECT {
        let right = card.right - self.px(16) - self.answer_mode_width();
        RECT {
            left: right - self.speed_width(),
            top: card.top + self.px(11),
            right,
            bottom: card.bottom - self.px(11),
        }
    }

    /// 「检测/刷新网页」按钮矩形（**绘制与命中共用**）：排在**倍速左侧**、留 px(10) 间距。
    /// 之前它与倍速用同一条右边界 ⇒ 两个控件完全叠在一起（实测重叠 144px，E41）。
    fn refresh_rect(&self, card: RECT) -> RECT {
        let right = self.speed_rect(card).left - self.px(10);
        let w = self.refresh_button_width();
        RECT {
            left: right - w,
            top: card.top + self.px(11),
            right,
            bottom: card.bottom - self.px(11),
        }
    }

    /// 刷新按钮宽度（按文本实测 + 内边距，带下限）。
    ///
    /// 用 `ui_sm`（13pt）而不是 `ui_b`：中文标签在 14pt 半粗下要 ~217px，
    /// 按钮会宽到挤掉网页框；13pt 足够清晰又省空间。
    ///
    /// ⚠️ 历史（别再改回去）：
    /// - 2026-09-14：宽度是手写死值 `px(92)` ⇒ 文字被截断（E41），改成按文本实测；
    /// - 2026-09-15：用户反馈"不够显示" ⇒ 再 +`px(10)`；
    /// - 2026-09-16：用户反馈**仍不够宽、文字溢出** ⇒ 内边距提到左右各 `px(24)`、
    ///   下限提到 `px(220)`，并加一条硬保证（宽度 ≥ 文字 + `px(40)`）。
    ///   判据：`--render-probe` 会把按钮宽/文字宽/左右内边距落盘（`probe-layout:` 行），
    ///   **内边距必须 ≥ px(24)，看数字判断，别靠肉眼**（E26）。
    fn refresh_button_width(&self) -> i32 {
        let measured = self.measure_label("检测/刷新网页", self.font_ui_sm);
        let text = if measured > 0 { measured } else { self.px(150) };
        // 文字 + 左右各 px(24) 内边距；下限 px(220)。
        // 并且**显式保证**：宽度至少 = 文字 + px(40)（防止下限/测量异常时反过来夹住文字）。
        (text + self.px(48)).max(self.px(220)).max(text + self.px(40))
    }

    /// 「答题方式」占位宽度（按文本实测 + 间距）。
    fn answer_mode_width(&self) -> i32 {
        let measured = self.measure_label("答题方式：内部答题 API", self.font_ui_sm);
        let base = if measured > 0 { measured } else { self.px(150) };
        base + self.px(24)
    }

    /// 倍速胶囊宽度（按 "倍速 2.0x" 实测）。
    fn speed_width(&self) -> i32 {
        let measured = self.measure_label("倍速 2.0x", self.font_ui_sm);
        let base = if measured > 0 { measured } else { self.px(80) };
        (base + self.px(28)).max(self.px(96))
    }

    /// **网页选择行所需的客户区最小宽度**：各元素宽度 + 间距 + 两侧外边距。
    ///
    /// 用它同时决定 (a) 窗口的 `ptMinTrackSize` 与 (b) 拖拽缩放的夹取下限，
    /// 这样"窗口缩到最小"和"布局需要的宽度"永远一致 —— 否则缩到最小就会错位
    /// （用户反馈，见 ERROR.md E41）。
    pub fn min_content_width(&self) -> i32 {
        // ⚠️ "与文本无关的固定部分"必须与建窗前的 `min_window_width()` 共用同一份，
        // 否则窗口可能一开出来就**小于自己的最小宽度**（E41 同类坑）。
        let total = page_row_fixed_width(self.dpi)
            + self.refresh_button_width()
            + self.speed_width()
            + self.answer_mode_width();
        // 再留一点余量，避免"刚好相等"时四舍五入后仍重叠
        total.max(self.px(1100)) + self.px(24)
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
    ///
    /// ⚠️ 这里**不再放**「检测/刷新网页」：网页选择行里已经有一个同名按钮（`Ctl::Refresh`
    /// 由 `refresh_rect()` 提供），两个入口重复、用户明确指出"怎么有两个"（2026-09-16）。
    /// ⇒ 刷新只保留网页行那一个；`Ctl::Refresh` 的 `invoke` 分支**仍然保留**（网页行按钮要用它）。
    fn button_specs(&self, st: &PaintState) -> Vec<(Ctl, &'static str, i32)> {
        let labels: Vec<(Ctl, &'static str)> = vec![
            (Ctl::Start, if st.running { "正在运行…" } else { "启动刷课" }),
            (Ctl::Pause, if st.paused { "继续执行" } else { "暂停进程" }),
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
        // 标题栏：关于 / 主题 / 三键（与绘制共用同一套位置）
        if y < th {
            let controls = [
                (Ctl::About, x_start - btn_w * 3),
                (Ctl::Settings, x_start - btn_w * 2),
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
                video_speed: s.video_speed,
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
        // 网页选择行：网页框（打开下拉选择）、「检测/刷新网页」按钮、倍速胶囊
        // 各自的可点区域响应；标签本身不做事。
        // ⚠️ 之前是"整行都返回 Refresh"（点标签也触发刷新，很费解，见 ERROR.md E40）；
        // 现在网页框有明确动作（下拉选页）并画了 ▾ 指示器。
        if y >= layout.page.top && y < layout.page.bottom {
            if self.speed_rect(layout.page).contains(x, y) {
                return Some(Ctl::Speed);
            }
            if self.refresh_rect(layout.page).contains(x, y) {
                return Some(Ctl::Refresh);
            }
            if self.page_box_rect(layout.page).contains(x, y) {
                return Some(Ctl::Pages);
            }
            return None;
        }
        None
    }

    /// 取窗口所在显示器的工作区（排除任务栏）。返回 (工作区矩形, 显示器左上角)。
    /// 拖拽夹取用它，保证窗口不会被推入任务栏或完全移出屏幕。
    fn monitor_work_area_for(&self, rc: RECT) -> (RECT, (i32, i32)) {
        unsafe {
            let mon = MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST);
            let mut mi = MONITORINFO {
                cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                ..Default::default()
            };
            if !mon.is_null() && GetMonitorInfoW(mon, &mut mi) != 0 {
                return (mi.rcWork, (mi.rcMonitor.left, mi.rcMonitor.top));
            }
            // 兜底：拿不到显示器信息时，用"整屏逻辑尺寸"当工作区，宁可夹得保守
            let _ = rc;
            let w = GetSystemMetrics(0);
            let h = GetSystemMetrics(1);
            (
                RECT {
                    left: 0,
                    top: 0,
                    right: w,
                    bottom: h,
                },
                (0, 0),
            )
        }
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
            // 最小尺寸由布局算出（见 min_content_width/height），与 WM_GETMINMAXINFO 一致
            let min_w = self.min_content_width();
            let min_h = self.min_content_height();
            match self.grab {
                Grab::Move => {
                    rc.left += dx;
                    rc.top += dy;
                    rc.right += dx;
                    rc.bottom += dy;
                    // ⚠️ **必须夹取到显示器工作区**：实测往右下连拖 8 次会把窗口整块推出
                    // 屏幕（用户看到的就是"窗口消失了"，而且没有兜底找不回来）。
                    // 坐标已统一为**物理像素**（进程真正 DPI 感知后 GetWindowRect 与
                    // GetMonitorInfo 才在同一坐标系，见 ERROR.md E37）。
                    //
                    // 规则（两条同时满足，拖动过程与松手后都不会出界）：
                    //  1. 标题栏必须留在工作区内（否则抓不回来）；
                    //  2. 窗口整体不能被拖出工作区（下方也要留得住）。
                    let keep_x = self.px(140);          // 水平方向至少露出这么宽
                    let title_h = self.title_h();
                    let (work, mon_org) = self.monitor_work_area_for(rc);
                    let w = self.grab_window.width();
                    let h = self.grab_window.height();

                    // 水平：左右各留 keep_x
                    let min_left = work.left - (w - keep_x);
                    let max_left = work.right - keep_x;
                    // 垂直：标题栏整体留在工作区内（上边界），且窗口底边不越过工作区底边
                    // （下边界）。两条一起夹 ⇒ 拖动/松手后都不会有"抓不着"的状态。
                    let min_top = mon_org.1;
                    let max_top_by_title = work.bottom - title_h;
                    let max_top_by_bottom = work.bottom - h;
                    // 窗口比工作区高时，优先保证标题栏可见
                    let max_top = if max_top_by_bottom < min_top {
                        max_top_by_title.max(min_top)
                    } else {
                        max_top_by_title.min(max_top_by_bottom).max(min_top)
                    };

                    rc.left = rc.left.clamp(min_left.min(max_left), max_left);
                    rc.top = rc.top.clamp(min_top, max_top);
                    rc.right = rc.left + w;
                    rc.bottom = rc.top + h;
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
                // ⚠️ 这份白名单必须把**所有标题栏按钮**列全：漏一个（这里是新加的
                // Ctl::Settings）就会落进 `_` 分支被当成"标题栏空白 ⇒ 拖窗口"——
                // 表现正是"按钮画得出来、点下去毫无反应"（实测踩到，见 ERROR.md E42）。
                Some(Ctl::Min)
                | Some(Ctl::Max)
                | Some(Ctl::Close)
                | Some(Ctl::Theme)
                | Some(Ctl::Settings)
                | Some(Ctl::About) => {
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
                    // 最大化状态下拖标题栏：先还原成普通窗口再拖（Windows 的标准行为），
                    // 否则会把"铺满屏幕的窗口"整体搬走，看起来同样像界面消失。
                    if unsafe { IsZoomed(self.hwnd) } != 0 {
                        unsafe {
                            ShowWindow(self.hwnd, SW_RESTORE);
                        }
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

    /// 兜底：松手后确认窗口**还能被抓到**。若因任何原因（多显示器变化、夹取算错、
    /// 系统还原窗口位置）跑到工作区外，就把它拉回来。
    /// 这是"窗口拖走后再也找不回来"的最后一道保险（用户实测反馈过，见 ERROR.md E37）。
    fn ensure_on_screen(&mut self) {
        unsafe {
            let mut rc = RECT::default();
            if GetWindowRect(self.hwnd, &mut rc) == 0 {
                return;
            }
            let (work, mon_org) = self.monitor_work_area_for(rc);
            let title_h = self.title_h();

            // 垂直：标题栏必须完整落在工作区内
            let mut top = rc.top.max(mon_org.1);
            let max_top = (work.bottom - title_h).max(mon_org.1);
            if top > max_top {
                top = max_top;
            }
            // 水平：至少留 px(140) 宽在工作区内
            let keep = self.px(140);
            let mut left = rc.left;
            if left + rc.width() < work.left + keep {
                left = work.left + keep - rc.width();
            }
            if left > work.right - keep {
                left = work.right - keep;
            }

            if left != rc.left || top != rc.top {
                SetWindowPos(
                    self.hwnd,
                    std::ptr::null_mut(),
                    left,
                    top,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                );
                crate::trace::trace(&format!(
                    "ui: ensure_on_screen ({},{}) -> ({},{})",
                    rc.left, rc.top, left, top
                ));
            }
        }
    }

    fn on_lbutton_up(&mut self, x: i32, y: i32) {
        if self.grab != Grab::None {
            self.grab = Grab::None;
            unsafe { ReleaseCapture() };
            // 拖拽结束做一次"还能抓到吗"的兜底检查
            self.ensure_on_screen();
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
                // 纯色界面：只同步系统标题栏的深浅，不涉及任何背板
                crate::dwm::apply_plain(hwnd, self.theme == Theme::Dark);
                {
                    let mut st = self.shared.lock();
                    st.push_log(&format!(
                        "[外观] 已切换{}主题",
                        if self.theme == Theme::Dark { "深色" } else { "浅色" }
                    ));
                }
                self.shared.notify_ui();
            }
            Ctl::About => {
                let mut st = self.shared.lock();
                st.push_log("[native] 打开「关于」");
                drop(st);
                self.shared.notify_ui();
                crate::about::show(hwnd, self.shared.clone(), self.theme);
            }
            Ctl::Settings => {
                let mut st = self.shared.lock();
                st.push_log("[native] 打开「答题设置」");
                drop(st);
                self.shared.notify_ui();
                crate::settings::show(hwnd, self.shared.clone(), self.theme);
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
            Ctl::Speed => {
                cycle_video_speed(self.shared.clone());
            }
            Ctl::Pages => {
                // 打开「当前网页」下拉（再点一次网页框 = 收起，见 pagepicker::show）
                // ⚠️ 变量别叫 `box`：它是 Rust 保留字（本轮在 settings.rs 已经踩过一次）
                let page_rc = self.layout().page;
                let box_rc = self.page_box_rect(page_rc);
                crate::pagepicker::show(hwnd, self.shared.clone(), self.theme, box_rc, None, false);
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
                    // **双缓冲**：先画进内存 DC，再一次性 BitBlt 到屏幕。
                    // 直接往屏幕 DC 逐块画会看到中间过程（文字/卡片"闪一下"），
                    // 这是用户报的"一抽一抽"的第二个成因（见 ERROR.md E38）。
                    let w = self.w.max(1);
                    let h = self.h.max(1);
                    let mem = unsafe { CreateCompatibleDC(hdc) };
                    let bmp = unsafe { CreateCompatibleBitmap(hdc, w, h) };
                    if !mem.is_null() && !bmp.is_null() {
                        let old = unsafe { SelectObject(mem, bmp as HGDIOBJ) };
                        self.paint(mem);
                        unsafe {
                            BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY);
                            SelectObject(mem, old);
                            DeleteObject(bmp as HGDIOBJ);
                            DeleteDC(mem);
                        }
                    } else {
                        // 内存 DC 建立失败也不能白屏：退回直接绘制
                        self.paint(hdc);
                    }
                    unsafe { EndPaint(self.hwnd, &ps) };
                }
                self.painted_rev = self.shared.lock().rev;
                // 重绘计数（验证"空闲时不再每 250ms 重画"用；见 ERROR.md E38）
                self.paint_count
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
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
                    // ⚠️ **只在状态真的变了才重绘**。
                    // 之前这里是无条件 `InvalidateRect`，等于每 250ms 把整个窗口重画一遍，
                    // 加上没有双缓冲 ⇒ 界面上的文字一直"一抽一抽"（用户实测反馈，见 ERROR.md E38）。
                    let rev = self.shared.lock().rev;
                    if rev != self.painted_rev {
                        unsafe { InvalidateRect(self.hwnd, std::ptr::null(), 0) };
                    }
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
            WM_APP_ACTION => {
                // 脚本化动作（验证通道；**必须由 UI 线程建窗口**）
                let action = self.scripted_action.clone();
                let hook = if action.is_empty() { "settings".to_string() } else { action };
                let theme = self.theme;
                if hook.starts_with("pages") {
                    // 「当前网页」下拉：pages = 只打开；pages_select = 打开并自动选第 2 项
                    let page_rc = self.layout().page;
                    let box_rc = self.page_box_rect(page_rc);
                    let auto = if hook == "pages_select" { Some(1) } else { None };
                    crate::trace::trace(&format!(
                        "ui: opening page picker via hook '{}' (auto_pick={:?})",
                        hook, auto
                    ));
                    crate::pagepicker::show(self.hwnd, self.shared.clone(), theme, box_rc, auto, true);
                } else {
                    crate::trace::trace(&format!("ui: opening settings dialog via hook '{}'", hook));
                    crate::settings::show_with_hook(self.hwnd, self.shared.clone(), theme, &hook);
                }
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
                    // ⚠️ 最小宽度**由布局算出**（网页行各元素宽度之和），不能拍脑袋写死。
                    // 之前写死 640，而网页行实际需要 ~1200px ⇒ 缩到最小就互相压住/错位
                    // （用户反馈"弄到最小的时候有错位"，见 ERROR.md E41）。
                    let min_w = self.min_content_width();
                    let min_h = self.min_content_height();
                    unsafe {
                        (*info).ptMinTrackSize = POINT { x: min_w, y: min_h };
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
    /// 视频倍速（来自后端设置）
    video_speed: f64,
}

/// 倍速循环：1.0 → 1.5 → 2.0 → 3.0 → 1.0。
///
/// 拆成独立函数（而不是写在 `invoke` 里）是为了让 `LH_UI_ACTION=speed` 这条
/// **脚本化验证路径**能复用同一份逻辑 —— 代理点不了界面，验证只能靠它。
fn cycle_video_speed(shared: Arc<Shared>) {
    if shared.lock().engine.running {
        let mut st = shared.lock();
        st.push_log("[native] 刷课运行中，暂不改倍速（停止后再调）");
        drop(st);
        shared.notify_ui();
        return;
    }
    let current = shared.lock().video_speed;
    const OPTIONS: [f64; 4] = [1.0, 1.5, 2.0, 3.0];
    let next = OPTIONS
        .iter()
        .find(|v| **v > current + 1e-6)
        .copied()
        .unwrap_or(OPTIONS[0]);
    std::thread::spawn(move || {
        // 后端是**合并式写入**：只覆盖 video_speed，不会动其它设置
        let body = format!("{{\"video_speed\":{}}}", next);
        match backend::update_settings(&shared, &body) {
            Ok(msg) => {
                let mut st = shared.lock();
                st.video_speed = next;
                st.push_log(&format!("[native] 倍速已设为 {:.1}x {}", next, msg));
            }
            Err(err) => {
                let mut st = shared.lock();
                st.push_log(&format!("[native] 倍速设置失败：{}", err));
            }
        }
        shared.notify_ui();
    });
}

fn spawn_action(shared: Arc<Shared>, action: &'static str, params: Option<Value>) {    std::thread::spawn(move || {
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

/// 96 DPI 基准的"整数量"（与 `App::px` 同一套规则，供窗口创建前估算尺寸用）。
fn px_at(dpi: u32, logical: i32) -> i32 {
    (logical * (dpi.max(96) as i32) * 100 / 96 + 50) / 100
}

/// 网页选择行里"与文本无关"的固定宽度：两侧外边距 + 标签 + 标签间距 + 网页框下限 + 两个间距。
///
/// ⚠️ **运行时 `App::min_content_width()` 与建窗前 `min_window_width()` 必须共用这一份**，
/// 否则两边漂移，窗口开出来就可能小于自己的最小宽度（E41 同类坑，2026-09-16 对齐）。
fn page_row_fixed_width(dpi: u32) -> i32 {
    let p = |v: i32| px_at(dpi, v);
    p(16) + p(70) + p(80) + p(220) + p(10) + p(10) + p(16)
}

/// 创建窗口前估算"客户区最小宽度"（此时还没有窗口，量不了文本，用保守常量）。
/// 与 `App::min_content_width()` 共用 `page_row_fixed_width()`，元素常量取各实测函数的上限。
fn min_window_width(dpi: u32) -> i32 {
    let p = |v: i32| px_at(dpi, v);
    let total = page_row_fixed_width(dpi) + p(220) + p(108) + p(240);
    (p(1100)).max(total) + p(24)
}

/// 创建窗口前估算"客户区最小高度"。
fn min_window_height(dpi: u32) -> i32 {
    let m = px_at(dpi, 18);
    let gap = px_at(dpi, 10);
    let title = px_at(dpi, 52) + px_at(dpi, 2);
    let status = px_at(dpi, 60);
    let page = px_at(dpi, 60);
    let kpi = px_at(dpi, 170);
    let buttons = px_at(dpi, 46);
    let log_min = px_at(dpi, 140);
    (px_at(dpi, 640)).max(title + status + page + kpi + buttons + log_min + gap * 4 + m)
}

/// 创建主窗口；``app_ptr`` 是 `*mut App`（Box::into_raw 的产物），``dpi`` 是目标屏 DPI。
pub fn create_main_window(app_ptr: *mut App, dpi: u32) -> HWND {
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

        // 按屏幕 DPI 缩放并居中。宽度**不能小于布局算出的最小宽度**，
        // 否则窗口一打开就已经低于自己的下限（改小尺寸下限后这里必须同步，见 E41）。
        let screen_w = GetSystemMetrics(0);
        let screen_h = GetSystemMetrics(1);
        let scale = ((screen_w as f32 / 1920.0).clamp(1.0, 2.0)).min(1.5);
        let min_w = min_window_width(dpi);
        let win_w = ((1100.0 * scale) as i32).max(min_w);
        let win_h = ((720.0 * scale) as i32).max(min_window_height(dpi));
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
