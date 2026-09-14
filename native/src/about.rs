//! 「关于」弹窗（以及后续设置类弹窗共用的最小对话框框架）。
//!
//! 设计取舍：**自绘一个顶层窗口**，而不是用系统 `DialogBox`/`MessageBox`。
//! 原因：本项目是纯色自绘 UI，套一个系统对话框会立刻露出老式控件外观，观感割裂；
//! 自绘还能复用主窗口的字体与配色（`ui::Colors`）。
//!
//! 交互：点「确定」、按 Esc、点右上角 × 都能关闭；窗口不可缩放（内容固定）。

use crate::backend::{self, Shared};
use crate::gdi::{self, TextAlign};
use crate::native::*;
use crate::ui::{self, Colors, Theme};
use std::sync::Arc;

const ABOUT_CLASS: &str = "LearnHelperAboutWnd";
const ID_OK: usize = 1;

/// 弹窗状态：指针经 GWLP_USERDATA 传进窗口过程。
pub struct AboutState {
    pub shared: Arc<Shared>,
    pub theme: Theme,
    pub colors: Colors,
    pub fonts: Vec<ui::FontOwned>,
    pub hwnd: HWND,
    pub hover_ok: bool,
    pub pressed_ok: bool,
}

impl AboutState {
    pub fn new(shared: Arc<Shared>, theme: Theme) -> AboutState {
        AboutState {
            shared,
            theme,
            colors: Colors::for_theme(theme),
            fonts: ui::make_dialog_fonts(),
            hwnd: NULL_HANDLE,
            hover_ok: false,
            pressed_ok: false,
        }
    }

    fn px(&self, logical: i32) -> i32 {
        let dpi = unsafe { GetDpiForWindow(self.hwnd) }.max(96) as i32;
        (logical * dpi * 100 / 96 + 50) / 100
    }

    fn ok_rect(&self, client: RECT) -> RECT {
        let w = self.px(120);
        let h = self.px(40);
        RECT {
            left: client.right - self.px(24) - w,
            top: client.bottom - self.px(20) - h,
            right: client.right - self.px(24),
            bottom: client.bottom - self.px(20),
        }
    }

    fn paint(&self, hdc: HDC) {
        let mut client = RECT::default();
        unsafe {
            GetClientRect(self.hwnd, &mut client);
        }
        let colors = self.colors;
        gdi::fill_rect(hdc, client, colors.bg);

        // 标题
        let title_rc = RECT {
            left: self.px(24),
            top: self.px(18),
            right: client.right - self.px(24),
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
        gdi::hline(
            hdc,
            self.px(24),
            client.right - self.px(24),
            title_rc.bottom + self.px(6),
            colors.divider,
        );

        // 信息行（左标签右值）
        let (status, version, device, base, exe) = {
            let st = self.shared.lock();
            (
                st.status_text.clone(),
                st.version.clone(),
                if st.device_id.is_empty() {
                    "(未获取)".to_string()
                } else {
                    st.device_id.clone()
                },
                if st.base_url.is_empty() {
                    "(未连接)".to_string()
                } else {
                    st.base_url.clone()
                },
                if st.backend_exe.is_empty() {
                    "(未启动)".to_string()
                } else {
                    st.backend_exe.clone()
                },
            )
        };

        let rows: Vec<(&str, String)> = vec![
            ("界面版本", format!("v{}", backend::APP_VERSION)),
            ("后端版本", version),
            ("后端地址", base),
            ("运行状态", status),
            ("设备指纹", device),
            ("后端程序", exe),
            ("界面", "原生 Win32（Rust）· 零依赖单 exe".to_string()),
            ("界面日志", "native-diag.log（与本程序同目录）".to_string()),
            ("后端日志", "logs\\learn_helper.log（与后端同目录）".to_string()),
        ];

        let label_w = self.px(84);
        let mut y = title_rc.bottom + self.px(16);
        let line_h = self.px(26);
        for (label, value) in rows {
            let label_rc = RECT {
                left: self.px(24),
                top: y,
                right: self.px(24) + label_w,
                bottom: y + line_h,
            };
            gdi::text_in(
                hdc,
                label,
                label_rc,
                TextAlign::Left,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
            let value_rc = RECT {
                left: label_rc.right + self.px(8),
                top: y,
                right: client.right - self.px(24),
                bottom: y + line_h,
            };
            gdi::text_in(
                hdc,
                &value,
                value_rc,
                TextAlign::Left,
                colors.text_sub,
                &self.fonts[ui::DFONT_MONO],
            );
            y += line_h;
        }

        // 确定按钮
        let ok = self.ok_rect(client);
        let fill = if self.pressed_ok {
            colors.btn_idle
        } else if self.hover_ok {
            colors.btn_hover
        } else {
            colors.accent
        };
        gdi::fill_round_rect(hdc, ok, self.px(8), fill);
        let fg = if self.hover_ok { colors.text_main } else { colors.bg };
        gdi::text_in(
            hdc,
            "确定",
            ok,
            TextAlign::Center,
            fg,
            &self.fonts[ui::DFONT_UI_B],
        );
    }
}

unsafe extern "system" fn about_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_NCCREATE {
        let create = lp as *const CREATESTRUCT;
        let state_ptr = (*create).lpCreateParams as *mut AboutState;
        if !state_ptr.is_null() {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, state_ptr as isize);
            (*state_ptr).hwnd = hwnd;
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
                // 同样双缓冲，避免弹窗文字闪
                let mut client = RECT::default();
                GetClientRect(hwnd, &mut client);
                let w = client.right.max(1);
                let h = client.bottom.max(1);
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
            let mut client = RECT::default();
            GetClientRect(hwnd, &mut client);
            let hover = state.ok_rect(client).contains(x, y);
            if hover != state.hover_ok {
                state.hover_ok = hover;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_LBUTTONDOWN => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let mut client = RECT::default();
            GetClientRect(hwnd, &mut client);
            if state.ok_rect(client).contains(x, y) {
                state.pressed_ok = true;
                SetCapture(hwnd);
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_LBUTTONUP => {
            let was = state.pressed_ok;
            state.pressed_ok = false;
            ReleaseCapture();
            if was {
                DestroyWindow(hwnd);
            }
            0
        }
        WM_KEYDOWN => {
            if wp == 0x1B {
                // VK_ESCAPE
                DestroyWindow(hwnd);
            }
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_NCDESTROY => {
            // 在这里释放状态：DestroyWindow 之后窗口过程仍可能收到消息，
            // 所以清理放在最后一条消息里；同时清掉 GWLP_USERDATA 免得悬垂。
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
        // 尺寸按 owner 所在屏幕的 DPI 缩放
        let dpi = GetDpiForWindow(owner).max(96) as i32;
        let w = (520 * dpi * 100 / 96 + 50) / 100;
        let h = (430 * dpi * 100 / 96 + 50) / 100;

        // 居中于 owner
        let mut owner_rc = RECT::default();
        GetWindowRect(owner, &mut owner_rc);
        let x = owner_rc.left + (owner_rc.width() - w) / 2;
        let y = owner_rc.top + (owner_rc.height() - h) / 2;

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
            // 创建失败：把 Box 收回来避免泄漏，并把原因落盘
            let err = GetLastError();
            drop(Box::from_raw(state));
            crate::trace::trace(&format!(
                "about: CreateWindowExW 失败 err={} (class={})",
                err, ABOUT_CLASS
            ));
            return;
        }
        // 弹窗用系统标题栏 + 深色模式（内容区仍是自绘纯色）
        crate::dwm::apply_plain(hwnd, theme == Theme::Dark);
        // 设成 owner 的弹出窗：跟着主窗口最小化，不会跑到主窗口后面
        SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner as isize);
        crate::trace::trace("about: window shown");
    }
}
