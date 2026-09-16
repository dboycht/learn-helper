//! 最小 Win32 FFI 声明。
//!
//! **刻意不用 windows-rs**：本项目只需要几十个函数，手写声明可以让最终 exe 保持在
//! 个位数 MB（windows-rs 会把全部元数据与生成胶水链进来）。代价是签名要自己核对，
//! 因此这里只声明**实际用到**的函数，并在有疑问处标注类型。

#![allow(non_snake_case, non_camel_case_types, dead_code)]

use std::ffi::c_void;

pub type HWND = *mut c_void;
pub type HINSTANCE = *mut c_void;
pub type HMENU = *mut c_void;
pub type HICON = *mut c_void;
pub type HCURSOR = *mut c_void;
pub type HBRUSH = *mut c_void;
pub type HFONT = *mut c_void;
pub type HGDIOBJ = *mut c_void;
pub type HDC = *mut c_void;
pub type HBITMAP = *mut c_void;
pub type HPEN = *mut c_void;
pub type WPARAM = usize;
pub type LPARAM = isize;
pub type LRESULT = isize;
pub type ATOM = u16;
pub type BOOL = i32;

pub const NULL_HANDLE: *mut c_void = std::ptr::null_mut();

// ---------------------------------------------------------------- 窗口样式
pub const WS_OVERLAPPED: u32 = 0x0000_0000;
/// ⚠️ 无边框窗口**必须**用 WS_POPUP（0x8000_0000）。
/// `WS_OVERLAPPED` 是 0x00000000 = "不指定样式"，CreateWindowEx 会套默认顶层样式
/// （含 WS_CAPTION）⇒ 标题栏仍被系统画出来（实测踩到，见 ERROR.md E33）。
pub const WS_POPUP: u32 = 0x8000_0000;
pub const WS_CAPTION: u32 = 0x00C0_0000;
pub const WS_SYSMENU: u32 = 0x0008_0000;
pub const WS_THICKFRAME: u32 = 0x0004_0000;
pub const WS_MINIMIZEBOX: u32 = 0x0002_0000;
pub const WS_MAXIMIZEBOX: u32 = 0x0001_0000;
pub const WS_VISIBLE: u32 = 0x1000_0000;
pub const WS_CLIPCHILDREN: u32 = 0x0200_0000;
pub const WS_EX_APPWINDOW: u32 = 0x0004_0000;
/// 不进任务栏、不进 Alt+Tab（下拉/工具窗用；learn-helper 的网页下拉就是它）。
pub const WS_EX_TOOLWINDOW: u32 = 0x0000_0080;
pub const WS_EX_TOPMOST: u32 = 0x0000_0008;
pub const WS_EX_DLGMODALFRAME: u32 = 0x0000_0001;

pub const CW_USEDEFAULT: i32 = 0x8000_0000u32 as i32;

// ---------------------------------------------------------------- 消息
pub const WM_NULL: u32 = 0x0000;
pub const WM_DESTROY: u32 = 0x0002;
pub const WM_SIZE: u32 = 0x0005;
pub const WM_PAINT: u32 = 0x000F;
pub const WM_CLOSE: u32 = 0x0010;
pub const WM_ERASEBKGND: u32 = 0x0014;
pub const WM_SETTINGCHANGE: u32 = 0x001A;
pub const WM_MOUSEMOVE: u32 = 0x0200;
pub const WM_LBUTTONDOWN: u32 = 0x0201;
pub const WM_LBUTTONUP: u32 = 0x0202;
pub const WM_MOUSEWHEEL: u32 = 0x020A;
pub const WM_KEYDOWN: u32 = 0x0100;
pub const WM_KEYUP: u32 = 0x0101;
pub const WM_CHAR: u32 = 0x0102;
pub const WM_SETFOCUS: u32 = 0x0007;
pub const WM_KILLFOCUS: u32 = 0x0008;
pub const WM_SETCURSOR: u32 = 0x0020;
pub const WM_NCDESTROY: u32 = 0x0082;
pub const WM_TIMER: u32 = 0x0113;
pub const WM_NCHITTEST: u32 = 0x0084;
pub const WM_NCCALCSIZE: u32 = 0x0083;
pub const WM_NCCREATE: u32 = 0x0081;
pub const WM_NCACTIVATE: u32 = 0x0086;
pub const WM_NCLBUTTONDOWN: u32 = 0x00A1;
pub const WM_GETMINMAXINFO: u32 = 0x0024;
pub const WM_DPICHANGED: u32 = 0x02E0;
pub const WM_ACTIVATE: u32 = 0x0006;
/// `WM_ACTIVATE` 的 wParam 低 16 位：窗口失活（= 用户点了别处）。
pub const WA_INACTIVE: u32 = 0;
pub const WM_SYSCOMMAND: u32 = 0x0112;
pub const WM_APP: u32 = 0x8000;
/// 后端线程通知 UI 重绘的自定义消息。
pub const WM_APP_BACKEND: u32 = WM_APP + 1;
/// 脚本化动作钩子（`LH_UI_ACTION=settings*`）：请求由 **UI 线程**打开「答题设置」。
pub const WM_APP_ACTION: u32 = WM_APP + 5;

pub const HTCLIENT: LRESULT = 1;
pub const HTCAPTION: LRESULT = 2;
pub const HTLEFT: LRESULT = 10;
pub const HTRIGHT: LRESULT = 11;
pub const HTTOP: LRESULT = 12;
pub const HTTOPLEFT: LRESULT = 13;
pub const HTTOPRIGHT: LRESULT = 14;
pub const HTBOTTOM: LRESULT = 15;
pub const HTBOTTOMLEFT: LRESULT = 16;
pub const HTBOTTOMRIGHT: LRESULT = 17;

pub const SC_MINIMIZE: WPARAM = 0xF020;
pub const SC_MAXIMIZE: WPARAM = 0xF030;
pub const SC_RESTORE: WPARAM = 0xF120;
pub const SC_CLOSE: WPARAM = 0xF060;

pub const SW_SHOW: i32 = 5;
pub const SW_MINIMIZE: i32 = 6;
pub const SW_MAXIMIZE: i32 = 3;
pub const SW_RESTORE: i32 = 9;

// ---------------------------------------------------------------- DPI
pub const DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: *mut c_void = -4isize as *mut c_void;

// ---------------------------------------------------------------- GDI
pub const TRANSPARENT: i32 = 1;
pub const PS_SOLID: i32 = 0;
pub const DEFAULT_CHARSET: u32 = 1;
pub const CLEARTYPE_QUALITY: u32 = 5;
pub const FW_NORMAL: i32 = 400;
pub const FW_SEMIBOLD: i32 = 600;
pub const FW_BOLD: i32 = 700;
pub const DT_LEFT: u32 = 0x0000_0000;
pub const DT_CENTER: u32 = 0x0000_0001;
pub const DT_RIGHT: u32 = 0x0000_0002;
pub const DT_VCENTER: u32 = 0x0000_0004;
pub const DT_SINGLELINE: u32 = 0x0000_0020;
pub const DT_NOPREFIX: u32 = 0x0000_0800;
pub const DT_END_ELLIPSIS: u32 = 0x0000_8000;
pub const DT_WORDBREAK: u32 = 0x0000_0010;
pub const DEFAULT_GUI_FONT: i32 = 17;
pub const NULL_BRUSH_STOCK: i32 = 5;
pub const SRCCOPY: u32 = 0x00CC_0020;

// ---------------------------------------------------------------- 其它
pub const SWP_NOSIZE: u32 = 0x0001;
pub const SWP_NOMOVE: u32 = 0x0002;
pub const SWP_NOZORDER: u32 = 0x0004;
pub const SWP_FRAMECHANGED: u32 = 0x0020;
pub const SWP_NOACTIVATE: u32 = 0x0010;
pub const GWL_STYLE: i32 = -16;
pub const GWL_EXSTYLE: i32 = -20;
pub const GWLP_USERDATA: i32 = -21;
pub const GWLP_HWNDPARENT: i32 = -8;
pub const GCLP_HBRBACKGROUND: i32 = -10;
pub const MONITOR_DEFAULTTONEAREST: u32 = 2;

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct MONITORINFO {
    pub cbSize: u32,
    pub rcMonitor: RECT,
    pub rcWork: RECT,
    pub dwFlags: u32,
}
pub const COINIT_APARTMENTTHREADED: u32 = 0x2;
pub const IDC_ARROW: usize = 32512;
pub const IDC_SIZENS: usize = 32645;
pub const IDC_IBEAM: usize = 32513;
/// 手型光标（悬停在链接上）
pub const IDC_HAND: usize = 32649;

// 焦点 / 输入光标 / 按钮行高（自绘对话框用）
pub const SPI_GETKEYBOARDDELAY: u32 = 0x0016;
pub const SM_CYBORDER: i32 = 6;
pub const EM_SETMARGINS: u32 = 0x00D3;
pub const EC_LEFTMARGIN: u32 = 0x0001;
pub const EC_RIGHTMARGIN: u32 = 0x0002;

// ---------------------------------------------------------------- 虚拟键
pub const VK_BACK: usize = 0x08;
pub const VK_TAB: usize = 0x09;
pub const VK_RETURN: usize = 0x0D;
pub const VK_ESCAPE: usize = 0x1B;
pub const VK_END: usize = 0x23;
pub const VK_HOME: usize = 0x24;
pub const VK_LEFT: usize = 0x25;
pub const VK_UP: usize = 0x26;
pub const VK_RIGHT: usize = 0x27;
pub const VK_DOWN: usize = 0x28;
pub const VK_DELETE: usize = 0x2E;
pub const VK_SPACE: usize = 0x20;
pub const VK_OEM_PLUS: usize = 0xBB;
pub const VK_OEM_MINUS: usize = 0xBD;
pub const VK_CONTROL: usize = 0x11;
pub const VK_A: usize = 0x41;
pub const VK_C: usize = 0x43;
pub const VK_S: usize = 0x53;
pub const VK_V: usize = 0x56;
pub const VK_X: usize = 0x58;

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct POINT {
    pub x: i32,
    pub y: i32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct RECT {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

impl RECT {
    pub fn width(&self) -> i32 {
        self.right - self.left
    }
    pub fn height(&self) -> i32 {
        self.bottom - self.top
    }
    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.left && x < self.right && y >= self.top && y < self.bottom
    }
    pub fn inset(&self, dx: i32, dy: i32) -> RECT {
        RECT {
            left: self.left + dx,
            top: self.top + dy,
            right: self.right - dx,
            bottom: self.bottom - dy,
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct SIZE {
    pub cx: i32,
    pub cy: i32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct MINMAXINFO {
    pub ptReserved: POINT,
    pub ptMaxSize: POINT,
    pub ptMaxPosition: POINT,
    pub ptMinTrackSize: POINT,
    pub ptMaxTrackSize: POINT,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct PAINTSTRUCT {
    pub hdc: HDC,
    pub fErase: BOOL,
    pub rcPaint: RECT,
    pub fRestore: BOOL,
    pub fIncUpdate: BOOL,
    pub rgbReserved: [u8; 32],
}

impl Default for PAINTSTRUCT {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MSG {
    pub hwnd: HWND,
    pub message: u32,
    pub wParam: WPARAM,
    pub lParam: LPARAM,
    pub time: u32,
    pub pt: POINT,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct WNDCLASSEXW {
    pub cbSize: u32,
    pub style: u32,
    pub lpfnWndProc: Option<unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT>,
    pub cbClsExtra: i32,
    pub cbWndExtra: i32,
    pub hInstance: HINSTANCE,
    pub hIcon: HICON,
    pub hCursor: HCURSOR,
    pub hbrBackground: HBRUSH,
    pub lpszMenuName: *const u16,
    pub lpszClassName: *const u16,
    pub hIconSm: HICON,
}

/// 把 Rust 字符串转成 NUL 结尾的 UTF-16（调用方持有所有权）。
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 供窗口过程使用的极小 utf16 工具。
pub fn utf16_len(p: *const u16) -> usize {
    if p.is_null() {
        return 0;
    }
    let mut n = 0;
    unsafe {
        while *p.add(n) != 0 {
            n += 1;
        }
    }
    n
}

pub fn utf16_to_string(p: *const u16, len: usize) -> String {
    if p.is_null() || len == 0 {
        return String::new();
    }
    let slice = unsafe { std::slice::from_raw_parts(p, len) };
    String::from_utf16_lossy(slice)
}

pub fn rgb(r: u8, g: u8, b: u8) -> u32 {
    (r as u32) | ((g as u32) << 8) | ((b as u32) << 16)
}

// ---------------------------------------------------------------- extern
#[link(name = "user32")]
extern "system" {
    pub fn RegisterClassExW(wc: *const WNDCLASSEXW) -> ATOM;
    pub fn CreateWindowExW(
        ex: u32,
        class: *const u16,
        title: *const u16,
        style: u32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        parent: HWND,
        menu: HMENU,
        inst: HINSTANCE,
        param: *mut c_void,
    ) -> HWND;
    pub fn DefWindowProcW(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT;
    pub fn DestroyWindow(hwnd: HWND) -> BOOL;
    pub fn PostQuitMessage(code: i32);
    pub fn GetMessageW(msg: *mut MSG, hwnd: HWND, min: u32, max: u32) -> BOOL;
    pub fn TranslateMessage(msg: *const MSG) -> BOOL;
    pub fn DispatchMessageW(msg: *const MSG) -> LRESULT;
    pub fn PostMessageW(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> BOOL;
    pub fn SendMessageW(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT;
    pub fn ShowWindow(hwnd: HWND, cmd: i32) -> BOOL;
    pub fn IsZoomed(hwnd: HWND) -> BOOL;
    pub fn IsIconic(hwnd: HWND) -> BOOL;
    pub fn SetForegroundWindow(hwnd: HWND) -> BOOL;
    pub fn GetClientRect(hwnd: HWND, rc: *mut RECT) -> BOOL;
    pub fn GetWindowRect(hwnd: HWND, rc: *mut RECT) -> BOOL;
    pub fn InvalidateRect(hwnd: HWND, rc: *const RECT, erase: BOOL) -> BOOL;
    pub fn BeginPaint(hwnd: HWND, ps: *mut PAINTSTRUCT) -> HDC;
    pub fn EndPaint(hwnd: HWND, ps: *const PAINTSTRUCT) -> BOOL;
    pub fn GetDC(hwnd: HWND) -> HDC;
    pub fn ReleaseDC(hwnd: HWND, hdc: HDC) -> i32;
    pub fn SetTimer(hwnd: HWND, id: usize, ms: u32, cb: *mut c_void) -> usize;
    pub fn KillTimer(hwnd: HWND, id: usize) -> BOOL;
    pub fn GetSystemMetrics(index: i32) -> i32;
    pub fn SetCursor(cur: HCURSOR) -> HCURSOR;
    pub fn LoadCursorW(inst: HINSTANCE, name: *const u16) -> HCURSOR;
    pub fn SetWindowLongPtrW(hwnd: HWND, index: i32, value: isize) -> isize;
    pub fn GetWindowLongPtrW(hwnd: HWND, index: i32) -> isize;
    pub fn SetClassLongPtrW(hwnd: HWND, index: i32, value: isize) -> isize;
    pub fn SetWindowPos(
        hwnd: HWND,
        after: HWND,
        x: i32,
        y: i32,
        cx: i32,
        cy: i32,
        flags: u32,
    ) -> BOOL;
    pub fn ScreenToClient(hwnd: HWND, pt: *mut POINT) -> BOOL;
    pub fn ClientToScreen(hwnd: HWND, pt: *mut POINT) -> BOOL;
    pub fn GetCursorPos(pt: *mut POINT) -> BOOL;
    pub fn SetProcessDpiAwarenessContext(ctx: *mut c_void) -> BOOL;
    pub fn GetDpiForWindow(hwnd: HWND) -> u32;
    pub fn MoveWindow(hwnd: HWND, x: i32, y: i32, w: i32, h: i32, repaint: BOOL) -> BOOL;
    pub fn SetCapture(hwnd: HWND) -> HWND;
    pub fn ReleaseCapture() -> BOOL;
    pub fn IsWindowVisible(hwnd: HWND) -> BOOL;
    pub fn MessageBeep(kind: u32) -> BOOL;
    pub fn SystemParametersInfoW(action: u32, p1: u32, p2: *mut c_void, p3: u32) -> BOOL;
    pub fn GetForegroundWindow() -> HWND;
    pub fn FindWindowW(class: *const u16, title: *const u16) -> HWND;
    pub fn MessageBoxW(hwnd: HWND, text: *const u16, caption: *const u16, flags: u32) -> i32;
    pub fn GetMonitorInfoW(monitor: *mut c_void, info: *mut MONITORINFO) -> BOOL;
    pub fn MonitorFromWindow(hwnd: HWND, flags: u32) -> *mut c_void;
    pub fn SetRect(rc: *mut RECT, l: i32, t: i32, r: i32, b: i32) -> BOOL;
    // ---- 键盘 / 焦点 / 插入符（自绘对话框的输入框需要） ----
    pub fn SetFocus(hwnd: HWND) -> HWND;
    pub fn GetFocus() -> HWND;
    pub fn GetKeyState(vk: i32) -> i16;
    pub fn CreateCaret(hwnd: HWND, bitmap: HBITMAP, w: i32, h: i32) -> BOOL;
    pub fn DestroyCaret() -> BOOL;
    pub fn SetCaretPos(x: i32, y: i32) -> BOOL;
    pub fn ShowCaret(hwnd: HWND) -> BOOL;
    pub fn HideCaret(hwnd: HWND) -> BOOL;
    pub fn IsWindowEnabled(hwnd: HWND) -> BOOL;
    pub fn IsWindow(hwnd: HWND) -> BOOL;
}

pub const MB_OK: u32 = 0x0000_0000;
pub const MB_ICONINFORMATION: u32 = 0x0000_0040;
pub const MB_ICONWARNING: u32 = 0x0000_0030;

#[link(name = "kernel32")]
extern "system" {
    pub fn GetModuleHandleW(name: *const u16) -> HINSTANCE;
    pub fn GetLastError() -> u32;
    pub fn SetLastError(code: u32);
    pub fn CreateMutexW(attrs: *mut c_void, initial_owner: BOOL, name: *const u16) -> *mut c_void;
    pub fn GetTickCount() -> u32;
}

#[link(name = "gdi32")]
extern "system" {
    pub fn CreateSolidBrush(color: u32) -> HBRUSH;
    pub fn CreatePen(style: i32, width: i32, color: u32) -> HPEN;
    pub fn CreateFontW(
        height: i32,
        width: i32,
        escapement: i32,
        orientation: i32,
        weight: i32,
        italic: u32,
        underline: u32,
        strikeout: u32,
        charset: u32,
        out_precision: u32,
        clip_precision: u32,
        quality: u32,
        pitch: u32,
        face: *const u16,
    ) -> HFONT;
    pub fn SelectObject(hdc: HDC, obj: HGDIOBJ) -> HGDIOBJ;
    pub fn DeleteObject(obj: HGDIOBJ) -> BOOL;
    pub fn GetStockObject(index: i32) -> HGDIOBJ;
    pub fn SetBkMode(hdc: HDC, mode: i32) -> i32;
    pub fn SetTextColor(hdc: HDC, color: u32) -> u32;
    pub fn SetBkColor(hdc: HDC, color: u32) -> u32;
    pub fn TextOutW(hdc: HDC, x: i32, y: i32, s: *const u16, len: i32) -> BOOL;
    pub fn DrawTextW(hdc: HDC, s: *const u16, len: i32, rc: *mut RECT, format: u32) -> i32;
    pub fn Rectangle(hdc: HDC, l: i32, t: i32, r: i32, b: i32) -> BOOL;
    pub fn RoundRect(hdc: HDC, l: i32, t: i32, r: i32, b: i32, w: i32, h: i32) -> BOOL;
    pub fn Ellipse(hdc: HDC, l: i32, t: i32, r: i32, b: i32) -> BOOL;
    pub fn FillRect(hdc: HDC, rc: *const RECT, brush: HBRUSH) -> i32;
    pub fn GetTextExtentPoint32W(hdc: HDC, s: *const u16, len: i32, size: *mut SIZE) -> BOOL;
    pub fn CreateCompatibleDC(hdc: HDC) -> HDC;
    pub fn CreateCompatibleBitmap(hdc: HDC, w: i32, h: i32) -> HBITMAP;
    pub fn BitBlt(
        dst: HDC,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        src: HDC,
        sx: i32,
        sy: i32,
        rop: u32,
    ) -> BOOL;
    pub fn DeleteDC(hdc: HDC) -> BOOL;
    pub fn LineTo(hdc: HDC, x: i32, y: i32) -> BOOL;
    pub fn MoveToEx(hdc: HDC, x: i32, y: i32, prev: *mut POINT) -> BOOL;
    pub fn GetDeviceCaps(hdc: HDC, index: i32) -> i32;
    pub fn GetDIBits(
        hdc: HDC,
        hbm: HBITMAP,
        start: u32,
        lines: u32,
        bits: *mut c_void,
        info: *mut BITMAPINFO,
        usage: u32,
    ) -> i32;
}

#[link(name = "dwmapi")]
extern "system" {
    pub fn DwmSetWindowAttribute(hwnd: HWND, attr: u32, value: *const c_void, size: u32) -> i32;
    pub fn DwmExtendFrameIntoClientArea(hwnd: HWND, margins: *const MARGINS) -> i32;
    pub fn DwmIsCompositionEnabled(enabled: *mut BOOL) -> i32;
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct MARGINS {
    pub cxLeftWidth: i32,
    pub cxRightWidth: i32,
    pub cyTopHeight: i32,
    pub cyBottomHeight: i32,
}

#[link(name = "shcore")]
extern "system" {
    pub fn SetProcessDpiAwareness(value: i32) -> i32;
}

#[link(name = "ole32")]
extern "system" {
    pub fn CoInitializeEx(reserved: *mut c_void, coinit: u32) -> i32;
    pub fn CoUninitialize();
}

#[link(name = "uxtheme")]
extern "system" {
    pub fn SetWindowTheme(hwnd: HWND, sub: *const u16, list: *const u16) -> i32;
}

// 打开外部链接（「关于」里的仓库地址）。只用来起系统默认浏览器，不做别的。
#[link(name = "shell32")]
extern "system" {
    pub fn ShellExecuteW(
        hwnd: HWND,
        op: *const u16,
        file: *const u16,
        params: *const u16,
        dir: *const u16,
        show: i32,
    ) -> *mut c_void;
}

pub const SW_SHOWNORMAL: i32 = 1;

// RtlGetVersion：拿真实 build 号（GetVersionEx 会被应用兼容性 shim 骗）
#[repr(C)]
#[derive(Clone, Copy)]
pub struct RTL_OSVERSIONINFOW {
    pub dwOSVersionInfoSize: u32,
    pub dwMajorVersion: u32,
    pub dwMinorVersion: u32,
    pub dwBuildNumber: u32,
    pub dwPlatformId: u32,
    pub szCSDVersion: [u16; 128],
}

impl Default for RTL_OSVERSIONINFOW {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

#[link(name = "ntdll")]
extern "system" {
    pub fn RtlGetVersion(info: *mut RTL_OSVERSIONINFOW) -> i32;
}

pub fn windows_build() -> u32 {
    let mut info = RTL_OSVERSIONINFOW::default();
    info.dwOSVersionInfoSize = std::mem::size_of::<RTL_OSVERSIONINFOW>() as u32;
    unsafe {
        if RtlGetVersion(&mut info) == 0 {
            return info.dwBuildNumber;
        }
    }
    0
}

/// 把客户区按当前状态画进**内存位图**并写 24 位 BMP。
///
/// 这是本项目**唯一可信的界面截图手段**：`PrintWindow` 在无边框窗口上返回的是 DWM 合成的
/// 背板（实测四角采到的仍是壁纸蓝），根本取不到我们自绘的客户区；而 `CopyFromScreen`
/// 会抓到用户的屏幕内容（红线，绝不使用）。直接调用自己的绘制代码则完全绕开这两者。
pub fn render_client_to_bmp(app: &mut crate::ui::App, w: i32, h: i32, out_path: &str) {
    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            crate::trace::trace("probe: GetDC(NULL) 失败");
            return;
        }
        let mem = CreateCompatibleDC(screen);
        let bmp = CreateCompatibleBitmap(screen, w, h);
        if mem.is_null() || bmp.is_null() {
            crate::trace::trace("probe: 创建内存 DC/位图失败");
            ReleaseDC(std::ptr::null_mut(), screen);
            return;
        }
        let old = SelectObject(mem, bmp as HGDIOBJ);

        // 用我们自己的绘制路径渲染（与屏幕上看到的同一份代码）
        app.render_to(mem, w, h);

        // 取回像素
        let mut bi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: h, // 正数 = 自底向上
                biPlanes: 1,
                biBitCount: 32,
                biCompression: 0, // BI_RGB
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
            pixels.as_mut_ptr() as *mut c_void,
            &mut bi,
            0, // DIB_RGB_COLORS
        );

        if lines > 0 {
            write_bmp24(out_path, w, h, &pixels);
            crate::trace::trace(&format!("probe: 已渲染 {}x{} -> {}", w, h, out_path));
        } else {
            crate::trace::trace("probe: GetDIBits 失败");
        }

        SelectObject(mem, old);
        DeleteObject(bmp as HGDIOBJ);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);
    }
}

pub fn write_bmp24(path: &str, w: i32, h: i32, bgra: &[u8]) {    use std::io::Write;
    let row_in = (w as usize) * 4;
    let row_out = ((w as usize) * 3 + 3) / 4 * 4; // 4 字节对齐
    let data_size = row_out * (h as usize);
    let file_size = 54 + data_size;
    let hh = h as usize;

    let mut out: Vec<u8> = Vec::with_capacity(file_size);
    // BITMAPFILEHEADER
    out.extend_from_slice(b"BM");
    out.extend_from_slice(&(file_size as u32).to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&54u32.to_le_bytes());
    // BITMAPINFOHEADER
    out.extend_from_slice(&40u32.to_le_bytes());
    out.extend_from_slice(&w.to_le_bytes());
    out.extend_from_slice(&h.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&24u16.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes()); // BI_RGB
    out.extend_from_slice(&(data_size as u32).to_le_bytes());
    out.extend_from_slice(&2835u32.to_le_bytes());
    out.extend_from_slice(&2835u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());

    let mut row_buf = vec![0u8; row_out];
    for y in 0..hh {
        let src = &bgra[y * row_in..y * row_in + row_in];
        for x in 0..(w as usize) {
            row_buf[x * 3] = src[x * 4];
            row_buf[x * 3 + 1] = src[x * 4 + 1];
            row_buf[x * 3 + 2] = src[x * 4 + 2];
        }
        out.extend_from_slice(&row_buf);
    }

    if let Ok(mut f) = std::fs::File::create(path) {
        let _ = f.write_all(&out);
        let _ = f.flush();
    }
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct BITMAPINFOHEADER {
    pub biSize: u32,
    pub biWidth: i32,
    pub biHeight: i32,
    pub biPlanes: u16,
    pub biBitCount: u16,
    pub biCompression: u32,
    pub biSizeImage: u32,
    pub biXPelsPerMeter: i32,
    pub biYPelsPerMeter: i32,
    pub biClrUsed: u32,
    pub biClrImportant: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct BITMAPINFO {
    pub bmiHeader: BITMAPINFOHEADER,
    pub bmiColors: [u32; 3],
}
