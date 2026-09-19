//! 「当前网页」下拉选择器（自绘 popup 窗口）。
//!
//! ## 为什么是独立 popup 窗口
//! 主窗口是**整窗自绘**（`WS_POPUP` + 全客户区自己画），没有原生子控件体系；
//! 想在下拉时超出窗口边界（网页行下方空间有限）就必须另开一个窗口。
//! 这里用 `WS_POPUP`（无标题栏）+ `WS_EX_TOOLWINDOW`（不进任务栏/Alt+Tab）的小窗，
//! 位置贴着网页框，**点到别处自动关闭**（靠 `WM_ACTIVATE` 的 `WA_INACTIVE`）。
//!
//! ## 交互
//! - 鼠标：移上去高亮，点一项即选中并关闭；
//! - 键盘：↑/↓ 移动、Enter 选中、Esc 关闭；滚轮可滚动（一屏最多 8 行）；
//! - 点标题栏/别的地方 ⇒ 窗口失活 ⇒ 自动销毁（不会留下孤儿弹窗）。
//!
//! ## 验证通道（代理点不了界面）
//! - `LH_UI_ACTION=pages` / `pages_select`：由 UI 线程打开本窗口，后者还会**自动点第 2 项**
//!   （走与鼠标完全相同的窗口过程路径）；
//! - `--render-probe-pages W H out.bmp`：不建窗口，直接把本下拉画进 BMP 供核对。

use crate::backend::{self, Shared};
use crate::gdi::{self, TextAlign};
use crate::json;
use crate::native::*;
use crate::ui::{self, Colors, Theme};
use std::sync::Arc;

const CLASS: &str = "LearnHelperPagePickerWnd";
/// 打开后自动点第 N 项（验证钩子用）；放在 `wp` 里传进窗口过程。
const WM_APP_AUTO_PICK: u32 = WM_APP + 6;

fn row_h(px: &dyn Fn(i32) -> i32) -> i32 {
    px(34).max(24)
}
fn pad(px: &dyn Fn(i32) -> i32) -> i32 {
    px(6).max(4)
}
const MAX_ROWS: usize = 8;

/// 钩子模式下多久收尾（关掉主窗口）。
///
/// 默认 1800ms；`LH_KEEP_OPEN=1` 时给到 120 秒 —— 让探针能观察"下拉还开着时"的
/// 窗口级行为（比如自动开浏览器是否抢焦点）。**只影响验证路径**，正常使用不进这里。
fn hook_delay_ms() -> u64 {
    if std::env::var("LH_KEEP_OPEN").is_ok() {
        crate::trace::trace("pagepicker: LH_KEEP_OPEN=1 -> hook 收尾延后到 120s");
        120_000
    } else {
        1800
    }
}

struct PickerState {
    shared: Arc<Shared>,
    colors: Colors,
    fonts: Vec<ui::FontOwned>,
    hwnd: HWND,
    items: Vec<String>,
    /// 当前选中的标题（用来打勾/高亮）
    selected: String,
    hover: Option<usize>,
    scroll: usize,
    /// 渲染探针用：显式指定 DPI
    dpi_override: Option<u32>,
}

impl PickerState {
    fn new(shared: Arc<Shared>, theme: Theme, items: Vec<String>, selected: String) -> PickerState {
        PickerState {
            shared,
            colors: Colors::for_theme(theme),
            fonts: ui::make_dialog_fonts(96),
            hwnd: NULL_HANDLE,
            items,
            selected,
            hover: None,
            scroll: 0,
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

    /// ⚠️ 拿到窗口后**必须重建字体**（窗口 DPI 只有这时才知道，见 ERROR.md E47）。
    fn refresh_fonts(&mut self) {
        let dpi = self.dpi();
        let sizes = ui::dialog_font_sizes(dpi);
        self.fonts = ui::make_dialog_fonts(dpi);
        crate::trace::trace(&format!(
            "pagepicker: fonts rebuilt dpi={} ui={}px sm={}px b={}px mono={}px",
            dpi, sizes[0], sizes[1], sizes[2], sizes[3]
        ));
    }

    fn px(&self, logical: i32) -> i32 {
        (logical * self.dpi() as i32 * 100 / 96 + 50) / 100
    }

    fn visible_rows(&self) -> usize {
        self.items.len().clamp(1, MAX_ROWS)
    }

    /// 第 i 行的矩形（**绘制与命中共用**）。
    fn row_rect(&self, client: RECT, i: usize) -> RECT {
        let p = |v: i32| self.px(v);
        let top = client.top + pad(&p) + (i as i32) * row_h(&p);
        RECT {
            left: client.left + p(4),
            top,
            right: client.right - p(4),
            bottom: top + row_h(&p),
        }
    }

    fn hit(&self, client: RECT, x: i32, y: i32) -> Option<usize> {
        if self.items.is_empty() {
            return None;
        }
        for i in 0..self.visible_rows() {
            if self.row_rect(client, i).contains(x, y) {
                return Some(self.scroll + i);
            }
        }
        None
    }

    fn paint(&self, hdc: HDC, client: RECT) {
        let p = |v: i32| self.px(v);
        let colors = self.colors;
        // 圆角卡片 + 1px 描边（窗口本身用 DWM 圆角，见 show()）
        gdi::fill_round_rect(hdc, client, p(8), colors.card);
        gdi::stroke_round_rect(hdc, client.inset(1, 1), p(8), colors.divider);

        if self.items.is_empty() {
            gdi::text_in(
                hdc,
                "尚未检测到网页 —— 请先点「检测/刷新网页」",
                client.inset(p(12), 0),
                TextAlign::Left,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
            return;
        }

        for i in 0..self.visible_rows() {
            let idx = self.scroll + i;
            let Some(title) = self.items.get(idx) else { break };
            let rc = self.row_rect(client, i);
            let is_selected = !self.selected.is_empty() && *title == self.selected;
            let hovered = self.hover == Some(idx);
            if hovered {
                gdi::fill_round_rect(hdc, rc, p(6), colors.card_hi);
            }
            // 勾选标记（当前选中的那一页）
            let text_left = if is_selected {
                let tick = RECT {
                    left: rc.left + p(6),
                    top: rc.top,
                    right: rc.left + p(6) + p(20),
                    bottom: rc.bottom,
                };
                draw_glyph(hdc, 0xE73Eu32, tick, colors.accent, &p);
                tick.right + p(4)
            } else {
                rc.left + p(10)
            };
            let color = if is_selected {
                colors.accent
            } else if hovered {
                colors.text_main
            } else {
                colors.text_sub
            };
            // 标题长度不可控 ⇒ 省略号裁剪（E44），否则长标题会盖到滚动条/边框上
            gdi::text_ellipsis(
                hdc,
                title,
                RECT { left: text_left, top: rc.top, right: rc.right - p(10), bottom: rc.bottom },
                TextAlign::Left,
                color,
                &self.fonts[ui::DFONT_UI],
            );
        }

        // 超出一屏时提示"还有多少"
        if self.items.len() > MAX_ROWS {
            let more = RECT {
                left: client.left + p(4),
                top: client.bottom - p(18),
                right: client.right - p(4),
                bottom: client.bottom - p(2),
            };
            gdi::text_in(
                hdc,
                &format!("第 {}-{} / 共 {}（滚轮翻页）", self.scroll + 1,
                         (self.scroll + self.visible_rows()).min(self.items.len()),
                         self.items.len()),
                more,
                TextAlign::Right,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
        }
    }
}

fn draw_glyph(hdc: HDC, glyph: u32, rc: RECT, color: u32, px: &dyn Fn(i32) -> i32) {
    let size = px(10).max(8);
    let mut font = ui::FontOwned::new(size, FW_NORMAL, "Segoe MDL2 Assets");
    if !font.is_valid() {
        font = ui::FontOwned::new(size, FW_NORMAL, "Segoe UI Symbol");
    }
    if let Some(ch) = char::from_u32(glyph) {
        gdi::text_in(hdc, &ch.to_string(), rc, TextAlign::Center, color, &font);
    }
}

/// 选中一项：**乐观更新界面**（立刻显示新标题），同时让后端记住这个选择。
fn pick(state: &mut PickerState, index: usize) {
    let Some(title) = state.items.get(index).cloned() else { return };
    {
        let mut st = state.shared.lock();
        st.selected_page = title.clone();
        st.push_log(&format!("[native] 已选择网页：{}", title));
    }
    state.shared.notify_ui();
    let shared = state.shared.clone();
    let hwnd_raw = state.hwnd as isize;
    std::thread::spawn(move || {
        let params = json::obj(&[("page", json::s(&title))]);
        let result = backend::control(&shared, "select_page", Some(params));
        let mut st = shared.lock();
        match result {
            Ok(msg) => {
                if !msg.is_empty() {
                    st.push_log(&format!("[native] {}", msg));
                }
            }
            Err(err) => st.push_log(&format!("[native] 选择网页失败：{}", err)),
        }
        drop(st);
        shared.notify_ui();
        if hwnd_raw != 0 {
            unsafe { PostMessageW(hwnd_raw as HWND, WM_CLOSE, 0, 0) };
        }
    });
}

unsafe extern "system" fn picker_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_NCCREATE {
        let create = lp as *const CREATESTRUCT;
        let state_ptr = (*create).lpCreateParams as *mut PickerState;
        if !state_ptr.is_null() {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, state_ptr as isize);
            (*state_ptr).hwnd = hwnd;
            // 窗口 DPI 只有这时才知道 ⇒ 立刻重建字体（否则高 DPI 下字偏小，E47）
            (*state_ptr).refresh_fonts();
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    let user = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut PickerState;
    if user.is_null() {
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    let state = &mut *user;

    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(hwnd, &mut ps);
            if !hdc.is_null() {
                let mut client = RECT::default();
                GetClientRect(hwnd, &mut client);
                let w = client.right.max(1);
                let h = client.bottom.max(1);
                // 双缓冲（与主窗口/设置弹窗同一套做法）
                let mem = CreateCompatibleDC(hdc);
                let bmp = CreateCompatibleBitmap(hdc, w, h);
                if !mem.is_null() && !bmp.is_null() {
                    let old = SelectObject(mem, bmp as HGDIOBJ);
                    state.paint(mem, client);
                    BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY);
                    SelectObject(mem, old);
                    DeleteObject(bmp as HGDIOBJ);
                    DeleteDC(mem);
                } else {
                    state.paint(hdc, client);
                }
                EndPaint(hwnd, &ps);
            }
            0
        }
        WM_ERASEBKGND => 1,
        WM_MOUSEMOVE => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let mut client = RECT::default();
            GetClientRect(hwnd, &mut client);
            let hover = state.hit(client, x, y);
            if hover != state.hover {
                state.hover = hover;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_LBUTTONUP => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let mut client = RECT::default();
            GetClientRect(hwnd, &mut client);
            if let Some(idx) = state.hit(client, x, y) {
                crate::trace::trace(&format!("pagepicker: picked #{}", idx));
                pick(state, idx);
            } else {
                // 点空白处 = 关闭（与常见下拉一致）
                DestroyWindow(hwnd);
            }
            0
        }
        WM_MOUSEWHEEL => {
            let delta = ((wp >> 16) as i16) as i32;
            let max_scroll = state.items.len().saturating_sub(state.visible_rows());
            if delta > 0 {
                state.scroll = state.scroll.saturating_sub(1);
            } else {
                state.scroll = (state.scroll + 1).min(max_scroll);
            }
            InvalidateRect(hwnd, std::ptr::null(), 0);
            0
        }
        WM_KEYDOWN => {
            match wp {
                VK_ESCAPE => {
                    DestroyWindow(hwnd);
                }
                VK_DOWN | VK_UP => {
                    if !state.items.is_empty() {
                        let cur = state.hover.unwrap_or(if wp == VK_DOWN {
                            state.scroll.saturating_sub(1)
                        } else {
                            state.scroll
                        });
                        let next = if wp == VK_DOWN {
                            (cur + 1).min(state.items.len() - 1)
                        } else {
                            cur.saturating_sub(1)
                        };
                        state.hover = Some(next);
                        // 让高亮项始终可见
                        if next < state.scroll {
                            state.scroll = next;
                        } else if next >= state.scroll + state.visible_rows() {
                            state.scroll = next + 1 - state.visible_rows();
                        }
                        InvalidateRect(hwnd, std::ptr::null(), 0);
                    }
                }
                VK_RETURN => {
                    if let Some(idx) = state.hover {
                        pick(state, idx);
                    }
                }
                _ => return DefWindowProcW(hwnd, msg, wp, lp),
            }
            0
        }
        WM_ACTIVATE => {
            // 失活 = 用户点了别处 ⇒ 关掉（下拉的标准行为，也避免留下孤儿弹窗）
            if (wp & 0xFFFF) as u32 == WA_INACTIVE {
                DestroyWindow(hwnd);
            }
            0
        }
        WM_APP_AUTO_PICK => {
            let idx = wp as usize;
            crate::trace::trace(&format!("pagepicker: auto-pick #{} (hook)", idx));
            if idx < state.items.len() {
                // ⚠️ **走与鼠标完全相同的路径**（rules/01 §8.4：探针必须与真实客户端等价）：
                // 不是直接调 `pick()`，而是给自己发一对 WM_LBUTTONDOWN/UP。
                let mut client = RECT::default();
                GetClientRect(hwnd, &mut client);
                let row = idx.saturating_sub(state.scroll);
                let rc = state.row_rect(client, row);
                let x = rc.left + rc.width() / 2;
                let y = rc.top + rc.height() / 2;
                let lp = ((y as isize) << 16) | ((x as isize) & 0xFFFF);
                state.hover = Some(idx);
                SendMessageW(hwnd, WM_LBUTTONDOWN, 1, lp);
                SendMessageW(hwnd, WM_LBUTTONUP, 0, lp);
            } else {
                crate::trace::trace(&format!(
                    "pagepicker: auto-pick #{} out of range (items={})",
                    idx,
                    state.items.len()
                ));
                DestroyWindow(hwnd);
            }
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_NCDESTROY => {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            crate::trace::trace("pagepicker: window destroyed, state freed");
            drop(Box::from_raw(user));
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
        let class_w = wide(CLASS);
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: 0,
            lpfnWndProc: Some(picker_proc),
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
    }
}

/// 打开下拉。`anchor_client` 是**主窗口客户区坐标**里的网页框矩形；
/// `auto_pick` 为 `Some(n)` 时自动选第 n 项（验证钩子用）；`hook_mode` 为真时
/// 跑完关掉主窗口，让无人值守脚本不必强杀进程（两者只在 `LH_UI_ACTION` 下为真）。
pub fn show(
    owner: HWND,
    shared: Arc<Shared>,
    theme: Theme,
    anchor_client: RECT,
    auto_pick: Option<usize>,
    hook_mode: bool,
) {
    unsafe {
        // 已经开着就先关掉（切换式：再点一次网页框 = 收起）
        let existing = FindWindowW(wide(CLASS).as_ptr(), std::ptr::null());
        if !existing.is_null() {
            DestroyWindow(existing);
            return;
        }
    }
    ensure_class();

    let (items, selected) = {
        let st = shared.lock();
        (st.pages.clone(), st.selected_page.clone())
    };

    let state = Box::into_raw(Box::new(PickerState::new(shared, theme, items, selected)));
    unsafe {
        let dpi = GetDpiForWindow(owner).max(96) as i32;
        let scale = |v: i32| (v * dpi * 100 / 96 + 50) / 100;
        let pad_px = scale(6).max(4);
        let row = scale(34).max(24);

        // 位置：贴着网页框下方，宽度比网页框略宽（长标题更好读），并夹进工作区
        let mut pt = POINT { x: anchor_client.left, y: anchor_client.bottom + 2 };
        ClientToScreen(owner, &mut pt);
        let mut w = (anchor_client.width() + scale(60)).max(scale(320));
        let rows = (*state).visible_rows() as i32;
        let mut h = rows * row + pad_px * 2;
        if (*state).items.len() > MAX_ROWS {
            h += scale(16);
        }

        let mon = MonitorFromWindow(owner, MONITOR_DEFAULTTONEAREST);
        let mut mi = MONITORINFO {
            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };
        let mut x = pt.x;
        let mut y = pt.y;
        if !mon.is_null() && GetMonitorInfoW(mon, &mut mi) != 0 {
            // 右边越界就往左收；下边放不下就翻到网页框上方
            if x + w > mi.rcWork.right {
                x = (mi.rcWork.right - w).max(mi.rcWork.left);
            }
            if y + h > mi.rcWork.bottom {
                let mut above = POINT { x: anchor_client.left, y: anchor_client.top - h - 2 };
                ClientToScreen(owner, &mut above);
                y = above.y.max(mi.rcWork.top);
            }
            w = w.min(mi.rcWork.right - mi.rcWork.left);
        }

        let class_w = wide(CLASS);
        let hwnd = CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            class_w.as_ptr(),
            std::ptr::null(),
            WS_POPUP | WS_VISIBLE,
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
            crate::trace::trace(&format!("pagepicker: CreateWindowExW 失败 err={}", err));
            return;
        }
        (*state).hwnd = hwnd;
        // 无标题栏 popup：只借 DWM 的 Win11 圆角（apply_plain 不做任何透明/背板）
        crate::dwm::apply_plain(hwnd, theme == Theme::Dark);
        crate::trace::trace(&format!(
            "pagepicker: shown items={} selected='{}' rect={}x{} at {},{}",
            (*state).items.len(),
            (*state).selected,
            w,
            h,
            x,
            y
        ));

        // 让它拿到焦点：这样"点别处"会触发 WM_ACTIVATE(WA_INACTIVE) 自动关闭，
        // 键盘 ↑/↓/Enter/Esc 也才有意义。
        SetForegroundWindow(hwnd);
        SetFocus(hwnd);

        if let Some(n) = auto_pick {
            let raw = hwnd as isize;
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(700));
                unsafe { PostMessageW(raw as HWND, WM_APP_AUTO_PICK, n as WPARAM, 0) };
            });
        }

        if hook_mode {
            // 验证钩子：给足时间把 select_page 发出去（含后端落盘），再优雅关掉主窗口
            //
            // ⚠️ `LH_KEEP_OPEN=1` 时**不主动收尾**（等很久很久）：有些探针要观察"下拉还开着的时候
            // 窗口级行为"（例如自动开浏览器会不会抢焦点把它关掉）。默认 1800ms 的正常收尾会
            // 把这类观测打断（autolaunch_pause_probe 实测踩到）。
            let owner_raw = owner as isize;
            let delay = if auto_pick.is_some() { 3500 } else { hook_delay_ms() };
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(delay));
                crate::trace::trace("pagepicker: hook mode -> closing main window");
                unsafe { PostMessageW(owner_raw as HWND, WM_CLOSE, 0, 0) };
            });
        }
    }
}

/// 渲染探针：把下拉按样例数据画进 BMP（不建窗口、不碰屏幕）。
/// `dpi` 用来复现高 DPI 排版（ERROR.md E47）。
pub fn render_probe(out_path: &str, w: i32, h: i32, dpi: u32) {
    let shared = Shared::new();
    let items = vec![
        "超星学习通 - 数据结构与算法 - 第3章 树与二叉树 - 3.2 二叉树的遍历".to_string(),
        "超星学习通".to_string(),
        "mooc1.chaoxing.com/mooc-ans/knowledge/cards".to_string(),
        "新标签页".to_string(),
        "百度一下，你就知道".to_string(),
    ];
    let selected = items[0].clone();
    let mut state = Box::new(PickerState::new(shared, Theme::Dark, items, selected));
    state.dpi_override = Some(dpi.max(96));
    state.refresh_fonts();
    state.hover = Some(1);

    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            crate::trace::trace("pagepicker-probe: GetDC 失败");
            return;
        }
        let mem = CreateCompatibleDC(screen);
        let bmp = CreateCompatibleBitmap(screen, w, h);
        if mem.is_null() || bmp.is_null() {
            crate::trace::trace("pagepicker-probe: 创建内存 DC/位图失败");
            ReleaseDC(std::ptr::null_mut(), screen);
            return;
        }
        let old = SelectObject(mem, bmp as HGDIOBJ);
        let client = RECT { left: 0, top: 0, right: w, bottom: h };
        state.paint(mem, client);
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
            crate::trace::trace(&format!("pagepicker-probe: 已渲染 {}x{} -> {}", w, h, out_path));
        } else {
            crate::trace::trace("pagepicker-probe: GetDIBits 失败");
        }
        SelectObject(mem, old);
        DeleteObject(bmp as HGDIOBJ);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);
    }
}
