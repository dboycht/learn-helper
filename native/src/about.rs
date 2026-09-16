//! 「关于」弹窗（以及后续设置类弹窗共用的最小对话框框架）。
//!
//! 设计取舍：**自绘一个顶层窗口**，而不是用系统 `DialogBox`/`MessageBox`。
//! 原因：本项目是纯色自绘 UI，套一个系统对话框会立刻露出老式控件外观，观感割裂；
//! 自绘还能复用主窗口的字体与配色（`ui::Colors`）。
//!
//! 内容（2026-09-16 用户要求补齐）：**版本 / 作者 / 许可证 / 仓库**（仓库可点击打开浏览器）
//! + 后端版本/地址/状态/设备指纹/日志路径。
//! ⚠️ 作者、仓库、许可证**不写死在这里**，全部来自 `Cargo.toml` 的编译期常量
//! （`env!("CARGO_PKG_AUTHORS")` / `REPOSITORY` / `LICENSE`），与版本号同一套路 ⇒ 单一来源。
//!
//! 交互：点「确定」、按 Esc、点右上角 × 都能关闭；窗口不可缩放。
//! 窗口高度**由内容行数算出**（`content_height()`），加一行不用手改尺寸（E41 的纪律）。

use crate::backend::{self, Shared};
use crate::gdi::{self, TextAlign};
use crate::native::*;
use crate::ui::{self, Colors, Theme};
use std::sync::Arc;

const ABOUT_CLASS: &str = "LearnHelperAboutWnd";
/// 验证钩子：开窗后自动点一次「仓库」那一行（无人值守验证，见 `LH_UI_ACTION=about_link`）
const WM_APP_ABOUT_LINK: u32 = WM_APP + 7;

/// 元数据（编译期常量，单一来源在 `native/Cargo.toml`）。
pub const APP_AUTHORS: &str = env!("CARGO_PKG_AUTHORS");
pub const APP_REPO: &str = env!("CARGO_PKG_REPOSITORY");
pub const APP_LICENSE: &str = env!("CARGO_PKG_LICENSE");
pub const APP_DESC: &str = env!("CARGO_PKG_DESCRIPTION");

/// 一行信息：标签 + 值；`url` 非空表示**可点**（点击用系统浏览器打开）。
struct Row {
    label: &'static str,
    value: String,
    url: Option<String>,
}

/// 弹窗状态：指针经 GWLP_USERDATA 传进窗口过程。
pub struct AboutState {
    pub shared: Arc<Shared>,
    pub theme: Theme,
    pub colors: Colors,
    pub fonts: Vec<ui::FontOwned>,
    pub hwnd: HWND,
    pub hover_ok: bool,
    pub pressed_ok: bool,
    /// 链接行是否被按下（松开时还在同一行才算点击）
    pub pressed_link: bool,
    /// 鼠标悬停在哪一行（只有可点行会高亮）
    pub hover_row: Option<usize>,
    /// 渲染探针用：显式指定 DPI（不建窗口时按它缩放，见 `--render-probe-*`）
    pub dpi_override: Option<u32>,
}

impl AboutState {
    pub fn new(shared: Arc<Shared>, theme: Theme) -> AboutState {
        AboutState {
            shared,
            theme,
            colors: Colors::for_theme(theme),
            fonts: ui::make_dialog_fonts(96),
            hwnd: NULL_HANDLE,
            hover_ok: false,
            pressed_ok: false,
            pressed_link: false,
            hover_row: None,
            dpi_override: None,
        }
    }

    /// 当前生效的 DPI（探针覆盖 > 窗口真实 DPI > 96 基准）。
    fn dpi(&self) -> u32 {
        if let Some(d) = self.dpi_override {
            return d.max(96);
        }
        if self.hwnd.is_null() {
            96
        } else {
            unsafe { GetDpiForWindow(self.hwnd) }.max(96)
        }
    }

    /// ⚠️ 拿到窗口后**必须重建字体**：窗口 DPI 只有这时才知道，
    /// 不重建就会"行按 1.5 放大、字还是 96 DPI 的大小"（ERROR.md E47）。
    fn refresh_fonts(&mut self) {
        let dpi = self.dpi();
        let sizes = ui::dialog_font_sizes(dpi);
        self.fonts = ui::make_dialog_fonts(dpi);
        crate::trace::trace(&format!(
            "about: fonts rebuilt dpi={} ui={}px sm={}px b={}px mono={}px",
            dpi, sizes[0], sizes[1], sizes[2], sizes[3]
        ));
    }

    fn px(&self, logical: i32) -> i32 {
        (logical * self.dpi() as i32 * 100 / 96 + 50) / 100
    }

    fn line_h(&self) -> i32 {
        self.px(28)
    }

    fn rows_top(&self) -> i32 {
        self.px(18) + self.px(34) + self.px(6) + self.px(14)
    }

    /// 窗口客户区高度：标题 + 全部行 + 底部按钮区（**由内容算出**，加行不必手改）。
    pub fn content_height(&self) -> i32 {
        let rows = self.rows().len() as i32;
        self.rows_top() + rows * self.line_h() + self.px(16) + self.px(40) + self.px(20)
    }

    pub fn content_width(&self) -> i32 {
        self.px(560)
    }

    /// 信息行（标签 / 值 / 可选链接）。绘制与命中共用这一份。
    fn rows(&self) -> Vec<Row> {
        let (status, version, device, base, exe) = {
            let st = self.shared.lock();
            (
                st.status_text.clone(),
                if st.version.is_empty() { "(未知)".to_string() } else { st.version.clone() },
                if st.device_id.is_empty() { "(未获取)".to_string() } else { st.device_id.clone() },
                if st.base_url.is_empty() { "(未连接)".to_string() } else { st.base_url.clone() },
                if st.backend_exe.is_empty() { "(未启动)".to_string() } else { st.backend_exe.clone() },
            )
        };
        let repo_short = APP_REPO.trim_start_matches("https://").trim_start_matches("http://").to_string();
        vec![
            Row { label: "软件", value: "学习助理 (learn-helper)".to_string(), url: None },
            Row {
                label: "界面版本",
                value: format!("v{}", backend::APP_VERSION),
                url: None,
            },
            Row { label: "后端版本", value: version, url: None },
            Row { label: "作者", value: APP_AUTHORS.replace(':', " / "), url: None },
            Row { label: "许可证", value: APP_LICENSE.to_string(), url: None },
            Row {
                label: "仓库",
                value: repo_short,
                url: Some(APP_REPO.to_string()),
            },
            Row { label: "运行状态", value: status, url: None },
            Row { label: "后端地址", value: base, url: None },
            Row { label: "设备指纹", value: device, url: None },
            Row { label: "后端程序", value: exe, url: None },
            Row { label: "界面", value: "原生 Win32（Rust）· 零依赖单 exe".to_string(), url: None },
            Row { label: "界面日志", value: "native-diag.log（与本程序同目录）".to_string(), url: None },
            Row { label: "后端日志", value: "logs\\learn_helper.log（与后端同目录）".to_string(), url: None },
        ]
    }

    /// 第 i 行的矩形（**绘制与命中共用**）。
    fn row_rect(&self, i: usize) -> RECT {
        let top = self.rows_top() + (i as i32) * self.line_h();
        let width = self.content_width();
        RECT {
            left: self.px(16),
            top,
            right: width - self.px(16),
            bottom: top + self.line_h(),
        }
    }

    fn link_row(&self) -> Option<usize> {
        self.rows().iter().position(|r| r.url.is_some())
    }

    fn ok_rect(&self) -> RECT {
        let w = self.px(120);
        let h = self.px(40);
        RECT {
            left: self.content_width() - self.px(24) - w,
            top: self.content_height() - self.px(20) - h,
            right: self.content_width() - self.px(24),
            bottom: self.content_height() - self.px(20),
        }
    }

    fn hit_link(&self, x: i32, y: i32) -> Option<usize> {
        let idx = self.link_row()?;
        if self.row_rect(idx).contains(x, y) {
            Some(idx)
        } else {
            None
        }
    }

    /// 打开链接。⚠️ 设了 `LH_ABOUT_NO_OPEN` 时**只记日志、不真的开浏览器** ——
    /// 否则无人值守探针一点就会弹出用户的浏览器（验证不能打扰用户，memory/04）。
    fn open_url(&self, url: &str) {
        if std::env::var("LH_ABOUT_NO_OPEN").is_ok() {
            crate::trace::trace(&format!("about: link click -> would open {}", url));
            return;
        }
        let op = wide("open");
        let file = wide(url);
        unsafe {
            ShellExecuteW(
                self.hwnd,
                op.as_ptr(),
                file.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            );
        }
        crate::trace::trace(&format!("about: link click -> open {}", url));
    }

    fn paint(&self, hdc: HDC) {
        let colors = self.colors;
        let w = self.content_width();
        let h = self.content_height();
        let client = RECT { left: 0, top: 0, right: w, bottom: h };
        gdi::fill_rect(hdc, client, colors.bg);

        // 标题 + 副标题（软件名 / 一句话描述）
        let title_rc = RECT {
            left: self.px(24),
            top: self.px(18),
            right: w - self.px(24),
            bottom: self.px(18) + self.px(34),
        };
        gdi::text_in(
            hdc,
            "关于 学习助理",
            title_rc,
            TextAlign::Left,
            colors.text_main,
            &self.fonts[ui::DFONT_UI_B],
        );
        let sub_rc = RECT {
            left: self.px(24),
            top: title_rc.bottom,
            right: w - self.px(24),
            bottom: title_rc.bottom + self.px(18),
        };
        gdi::text_ellipsis(
            hdc,
            APP_DESC,
            sub_rc,
            TextAlign::Left,
            colors.text_muted,
            &self.fonts[ui::DFONT_UI_SM],
        );
        gdi::hline(
            hdc,
            self.px(24),
            w - self.px(24),
            sub_rc.bottom + self.px(4),
            colors.divider,
        );

        // 信息行
        let label_w = self.px(84);
        for (i, row) in self.rows().iter().enumerate() {
            let rc = self.row_rect(i);
            let link = row.url.is_some();
            let hovered = link && self.hover_row == Some(i);
            if hovered {
                gdi::fill_round_rect(hdc, rc, self.px(6), colors.card_hi);
            }
            gdi::text_in(
                hdc,
                row.label,
                RECT {
                    left: rc.left + self.px(8),
                    top: rc.top,
                    right: rc.left + self.px(8) + label_w,
                    bottom: rc.bottom,
                },
                TextAlign::Left,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
            let value_rc = RECT {
                left: rc.left + self.px(8) + label_w + self.px(8),
                top: rc.top,
                right: rc.right - self.px(8),
                bottom: rc.bottom,
            };
            // ⚠️ 右侧是状态串/路径/地址/URL —— 长度不可控 ⇒ 必须用省略号裁剪版，
            // 否则长路径会画到窗口外面去（gdi::text_in 不做裁剪，见 E44）
            let value_font = ui::DFONT_MONO;
            // 链接行统一用强调色（可点），普通行用次级文字色
            let color = if link { colors.accent } else { colors.text_sub };
            gdi::text_ellipsis(hdc, &row.value, value_rc, TextAlign::Left, color, &self.fonts[value_font]);
            if link {
                // 下划线提示"可点"（长度按实际文本宽度算，别画满整行）
                let tw = gdi::measure_text(hdc, &row.value, &self.fonts[value_font]).cx;
                let y = rc.top + rc.height() / 2 + self.px(9);
                gdi::hline(hdc, value_rc.left, (value_rc.left + tw).min(value_rc.right), y, colors.accent);
            }
        }

        // 确定按钮
        let ok = self.ok_rect();
        let fill = if self.pressed_ok {
            colors.btn_idle
        } else if self.hover_ok {
            colors.btn_hover
        } else {
            colors.accent
        };
        gdi::fill_round_rect(hdc, ok, self.px(8), fill);
        let fg = if self.hover_ok { colors.text_main } else { colors.bg };
        gdi::text_in(hdc, "确定", ok, TextAlign::Center, fg, &self.fonts[ui::DFONT_UI_B]);
    }
}

unsafe extern "system" fn about_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_NCCREATE {
        let create = lp as *const CREATESTRUCT;
        let state_ptr = (*create).lpCreateParams as *mut AboutState;
        if !state_ptr.is_null() {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, state_ptr as isize);
            (*state_ptr).hwnd = hwnd;
            // 窗口 DPI 只有这时才知道 ⇒ 立刻按它重建字体（否则高 DPI 下字会偏小，E47）
            (*state_ptr).refresh_fonts();
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    let user = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut AboutState;
    if user.is_null() {
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    let state = &mut *user;

    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(hwnd, &mut ps);
            if !hdc.is_null() {
                // 双缓冲，避免弹窗文字闪
                let w = state.content_width().max(1);
                let h = state.content_height().max(1);
                let mem = CreateCompatibleDC(hdc);
                let bmp = CreateCompatibleBitmap(hdc, w, h);
                if !mem.is_null() && !bmp.is_null() {
                    let old = SelectObject(mem, bmp as HGDIOBJ);
                    state.paint(mem);
                    BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY);
                    SelectObject(mem, old);
                    DeleteObject(bmp as HGDIOBJ);
                    DeleteDC(mem);
                } else {
                    state.paint(hdc);
                }
                EndPaint(hwnd, &ps);
            }
            0
        }
        WM_ERASEBKGND => 1,
        WM_MOUSEMOVE => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let hover = state.ok_rect().contains(x, y);
            let hover_row = state.hit_link(x, y);
            if hover != state.hover_ok || hover_row != state.hover_row {
                state.hover_ok = hover;
                state.hover_row = hover_row;
                // 悬停在链接上给手型光标
                let cursor = if hover_row.is_some() { IDC_HAND } else { IDC_ARROW };
                SetCursor(LoadCursorW(std::ptr::null_mut(), cursor as *const u16));
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_SETCURSOR => {
            let mut pt = POINT::default();
            GetCursorPos(&mut pt);
            ScreenToClient(hwnd, &mut pt);
            let cursor = if state.hit_link(pt.x, pt.y).is_some() { IDC_HAND } else { IDC_ARROW };
            SetCursor(LoadCursorW(std::ptr::null_mut(), cursor as *const u16));
            1
        }
        WM_LBUTTONDOWN => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            if state.ok_rect().contains(x, y) {
                state.pressed_ok = true;
                SetCapture(hwnd);
                InvalidateRect(hwnd, std::ptr::null(), 0);
            } else if state.hit_link(x, y).is_some() {
                // 链接行：按下即记录（松开时确认仍在同一行才算点击）
                state.pressed_link = true;
                SetCapture(hwnd);
            }
            0
        }
        WM_LBUTTONUP => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let was_ok = state.pressed_ok;
            let was_link = state.pressed_link;
            state.pressed_ok = false;
            state.pressed_link = false;
            ReleaseCapture();
            if was_link {
                if state.hit_link(x, y).is_some() {
                    if let Some(idx) = state.link_row() {
                        let url = state.rows()[idx].url.clone().unwrap_or_default();
                        state.open_url(&url);
                    }
                }
            } else if was_ok {
                DestroyWindow(hwnd);
            }
            0
        }
        WM_KEYDOWN => {
            if wp == VK_ESCAPE {
                DestroyWindow(hwnd);
            }
            0
        }
        WM_APP_ABOUT_LINK => {
            // 验证钩子：**走与鼠标完全相同的路径**（发一对真实鼠标消息到链接行中心）
            crate::trace::trace("about-hook: clicking the repository link row");
            if let Some(idx) = state.link_row() {
                let rc = state.row_rect(idx);
                let x = rc.left + rc.width() / 2;
                let y = rc.top + rc.height() / 2;
                let lp = ((y as isize) << 16) | ((x as isize) & 0xFFFF);
                SendMessageW(hwnd, WM_LBUTTONDOWN, 1, lp);
                SendMessageW(hwnd, WM_LBUTTONUP, 0, lp);
            } else {
                crate::trace::trace("about-hook: no clickable row found");
            }
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_NCDESTROY => {
            // DestroyWindow 之后窗口过程仍可能收到消息，所以清理放在最后一条消息里
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            drop(Box::from_raw(user));
            crate::trace::trace("about: window destroyed, state freed");
            0
        }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

#[repr(C)]
struct CREATESTRUCT {
    lpCreateParams: *mut std::ffi::c_void,
}

fn ensure_class() {
    unsafe {
        let class_w = wide(ABOUT_CLASS);
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: 0,
            lpfnWndProc: Some(about_proc),
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
        // 已注册会返回 0 + ERROR_CLASS_ALREADY_EXISTS，无害
        RegisterClassExW(&wc);
    }
}

/// 打开「关于」弹窗（非阻塞；同一时刻只允许一个）。
pub fn show(owner: HWND, shared: Arc<Shared>, theme: Theme) {
    create(owner, shared, theme, false);
}

/// 打开并注入脚本化动作（`LH_UI_ACTION=about_link`：开窗后自动点一次仓库链接）。
pub fn show_with_hook(owner: HWND, shared: Arc<Shared>, theme: Theme) {
    create(owner, shared, theme, true);
}

fn create(owner: HWND, shared: Arc<Shared>, theme: Theme, hook: bool) {
    unsafe {
        if !FindWindowW(wide(ABOUT_CLASS).as_ptr(), std::ptr::null()).is_null() {
            // 已经开着一个就把它提到前面
            let existing = FindWindowW(wide(ABOUT_CLASS).as_ptr(), std::ptr::null());
            SetForegroundWindow(existing);
            return;
        }
    }

    ensure_class();

    let state = Box::into_raw(Box::new(AboutState::new(shared, theme)));

    unsafe {
        // 尺寸按 owner 所在屏幕的 DPI 缩放；**高度由内容行数算出**（加载更多行不会溢出）
        let dpi = GetDpiForWindow(owner).max(96) as i32;
        let scale = |v: i32| (v * dpi * 100 / 96 + 50) / 100;
        let w = scale((*state).content_width());
        let h = scale((*state).content_height());

        // 居中于 owner
        let mut owner_rc = RECT::default();
        GetWindowRect(owner, &mut owner_rc);
        let mut x = owner_rc.left + (owner_rc.width() - w) / 2;
        let mut y = owner_rc.top + (owner_rc.height() - h) / 2;
        // 夹到工作区（多显示器 / 主窗口贴边时不要跑到屏幕外）
        let mon = MonitorFromWindow(owner, MONITOR_DEFAULTTONEAREST);
        let mut mi = MONITORINFO {
            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };
        if !mon.is_null() && GetMonitorInfoW(mon, &mut mi) != 0 {
            x = x.clamp(mi.rcWork.left, (mi.rcWork.right - w).max(mi.rcWork.left));
            y = y.clamp(mi.rcWork.top, (mi.rcWork.bottom - h).max(mi.rcWork.top));
        }

        let class_w = wide(ABOUT_CLASS);
        let title_w = wide("关于 学习助理");
        // 带标题栏的系统窗口（内容区仍是我们自绘）：这样用户能拖动/关闭它，
        // 也不用再自己实现一套拖拽。WS_EX_APPWINDOW 保证它出现在任务栏/Alt+Tab，
        // 万一被主窗口盖住也能找回来。
        let style = WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_VISIBLE;
        let ex_style = WS_EX_APPWINDOW;

        let hwnd = CreateWindowExW(
            ex_style,
            class_w.as_ptr(),
            title_w.as_ptr(),
            style,
            x,
            y,
            w,
            h,
            owner,
            std::ptr::null_mut(),
            GetModuleHandleW(std::ptr::null()),
            state as *mut std::ffi::c_void,
        );
        if hwnd.is_null() {
            let err = GetLastError();
            drop(Box::from_raw(state));
            crate::trace::trace(&format!(
                "about: CreateWindowExW 失败 err={} (class={})",
                err, ABOUT_CLASS
            ));
            return;
        }
        crate::dwm::apply_plain(hwnd, theme == Theme::Dark);
        SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner as isize);
        crate::trace::trace(&format!(
            "about: window shown {}x{} rows={} author='{}' repo={} license={}",
            w,
            h,
            (*state).rows().len(),
            APP_AUTHORS,
            APP_REPO,
            APP_LICENSE
        ));

        if hook {
            // 验证钩子：延后模拟点击仓库链接；再延时关掉主窗口，脚本不必强杀进程
            let raw = hwnd as isize;
            let owner_raw = owner as isize;
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(1200));
                unsafe { PostMessageW(raw as HWND, WM_APP_ABOUT_LINK, 0, 0) };
                std::thread::sleep(std::time::Duration::from_millis(1200));
                crate::trace::trace("about-hook: closing main window");
                unsafe { PostMessageW(owner_raw as HWND, WM_CLOSE, 0, 0) };
            });
        }
    }
}

/// 渲染探针：把「关于」弹窗按样例数据画进 BMP（不建窗口、不碰屏幕）。
/// `dpi` 用来复现高 DPI 排版（ERROR.md E47）。
pub fn render_probe(out_path: &str, _w: i32, _h: i32, dpi: u32) {
    let shared = Shared::new();
    {
        let mut st = shared.lock();
        st.status_text = "已连接 v2.1.2 · http://127.0.0.1:6225".to_string();
        st.version = "2.1.2".to_string();
        st.device_id = "DEV-9700E71B8EDA".to_string();
        st.base_url = "http://127.0.0.1:6225".to_string();
        st.backend_exe = "D:\\code\\DeepSeekHarness\\learn-helper\\backend\\main.py".to_string();
    }
    let mut state = Box::new(AboutState::new(shared, Theme::Dark));
    state.dpi_override = Some(dpi.max(96));
    state.refresh_fonts();
    state.hover_row = state.link_row();

    let w = state.content_width();
    let h = state.content_height();
    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            crate::trace::trace("about-probe: GetDC 失败");
            return;
        }
        let mem = CreateCompatibleDC(screen);
        let bmp = CreateCompatibleBitmap(screen, w, h);
        if mem.is_null() || bmp.is_null() {
            crate::trace::trace("about-probe: 创建内存 DC/位图失败");
            ReleaseDC(std::ptr::null_mut(), screen);
            return;
        }
        let old = SelectObject(mem, bmp as HGDIOBJ);
        state.paint(mem);
        let mut bi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: h,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: 0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut pixels = vec![0u8; (w * h * 4) as usize];
        let lines = GetDIBits(
            mem,
            bmp,
            0,
            h as u32,
            pixels.as_mut_ptr() as *mut std::ffi::c_void,
            &mut bi,
            0,
        );
        if lines > 0 {
            crate::native::write_bmp24(out_path, w, h, &pixels);
            crate::trace::trace(&format!("about-probe: 已渲染 {}x{} -> {}", w, h, out_path));
        } else {
            crate::trace::trace("about-probe: GetDIBits 失败");
        }
        SelectObject(mem, old);
        DeleteObject(bmp as HGDIOBJ);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);
    }
}
