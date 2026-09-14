//! DWM 外观：玻璃材质 / 圆角 / 深色标题栏。
//!
//! 这是本项目**唯一**能拿到"整窗透出桌面并模糊"的正路（`memory/01` §1/§2：
//! 整窗玻璃只能由 OS/DWM 提供，自绘半透明糊不到桌面）。所以这里只调 DWM，
//! 绝不自己铺一层半透明去假装玻璃。
//!
//! 三级回退（用户可见结论写进日志）：
//!   Mica → Acrylic(Thin) → 纯色

use crate::native::{self, *};

// DWMWINDOWATTRIBUTE
const DWMWA_USE_IMMERSIVE_DARK_MODE: u32 = 20;
const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;
const DWMWA_SYSTEMBACKDROP_TYPE: u32 = 38;
const DWMWA_BORDER_COLOR: u32 = 34;
const DWMWA_CAPTION_COLOR: u32 = 35;

// DWM_WINDOW_CORNER_PREFERENCE
const DWMWCP_DEFAULT: i32 = 0;
const DWMWCP_DONOTROUND: i32 = 1;
const DWMWCP_ROUND: i32 = 2;
const DWMWCP_ROUNDSMALL: i32 = 3;

// DWM_SYSTEMBACKDROP_TYPE
const DWMSBT_AUTO: i32 = 0;
const DWMSBT_NONE: i32 = 1;
const DWMSBT_MAINWINDOW: i32 = 2; // Mica
const DWMSBT_TRANSIENTWINDOW: i32 = 3; // Acrylic
const DWMSBT_TABBEDWINDOW: i32 = 4; // Mica Alt

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Backdrop {
    Mica,
    MicaAlt,
    Acrylic,
    Solid,
}

impl Backdrop {
    pub fn label(self) -> &'static str {
        match self {
            Backdrop::Mica => "Mica",
            Backdrop::MicaAlt => "Mica Alt",
            Backdrop::Acrylic => "Acrylic",
            Backdrop::Solid => "纯色兜底",
        }
    }
}

fn set_attr(hwnd: HWND, attr: u32, value: &i32) -> bool {
    unsafe {
        DwmSetWindowAttribute(
            hwnd,
            attr,
            value as *const i32 as *const _,
            std::mem::size_of::<i32>() as u32,
        ) == 0
    }
}

/// 套用：深色标题栏 + 圆角（这两条与材质无关，始终尝试）。
pub fn apply_chrome(hwnd: HWND, dark: bool) {
    let dark_i = if dark { 1 } else { 0 };
    let _ = set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, &dark_i);

    // Win11 圆角；Win10 会返回失败（无害）
    let corner = DWMWCP_ROUND;
    let _ = set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &corner);

    // 让 DWM 画出与主题一致的 1px 边框（否则玻璃窗口边缘会有一条亮线）
    let border = if dark { 0x0022_1C17u32 } else { 0x00EC_E6E1u32 };
    unsafe {
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_BORDER_COLOR,
            &border as *const u32 as *const _,
            4,
        );
    }
}

/// 尝试设置系统背板；返回是否被 DWM 接受。
fn try_backdrop(hwnd: HWND, kind: i32) -> bool {
    set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, &kind)
}

/// 选择并套用玻璃材质，返回**实际生效**的那一级（供界面如实显示）。
pub fn apply_glass(hwnd: HWND, dark: bool) -> Backdrop {
    apply_chrome(hwnd, dark);

    let build = native::windows_build();
    // 系统背板类型需要 Win11 22H2（build 22621）以上；更早的系统只能纯色。
    if build < 22621 {
        let _ = try_backdrop(hwnd, DWMSBT_NONE);
        return Backdrop::Solid;
    }

    // 顺序即优先级：Mica 最"沉"，Acrylic 更透。
    for (kind, backdrop) in [
        (DWMSBT_MAINWINDOW, Backdrop::Mica),
        (DWMSBT_TRANSIENTWINDOW, Backdrop::Acrylic),
    ] {
        if try_backdrop(hwnd, kind) {
            return backdrop;
        }
    }

    let _ = try_backdrop(hwnd, DWMSBT_NONE);
    let _ = DWMSBT_AUTO;
    let _ = DWMWCP_DEFAULT;
    let _ = DWMWCP_DONOTROUND;
    let _ = DWMWCP_ROUNDSMALL;
    let _ = DWMWA_CAPTION_COLOR;
    Backdrop::Solid
}

/// 供日志/自检：报告 DWM 合成是否开启。
pub fn composition_enabled() -> bool {
    let mut enabled: BOOL = 0;
    unsafe {
        DwmIsCompositionEnabled(&mut enabled) == 0
    }
    .then_some(())
    .map(|_| enabled != 0)
    .unwrap_or(false)
}
