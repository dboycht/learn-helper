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

/// 套用：深色标题栏 + 圆角 + 标题栏底色（这两条与材质无关，始终尝试）。
pub fn apply_chrome(hwnd: HWND, dark: bool) {
    let dark_i = if dark { 1 } else { 0 };
    let r_dark = set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, &dark_i);

    // Win11 圆角；Win10 会返回失败（无害）
    let corner = DWMWCP_ROUND;
    let r_corner = set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &corner);

    // ⚠️ 把**标题栏钉成与应用内容同色**（实测教训，见 ERROR.md E33）：
    // 只调 DWMWA_USE_IMMERSIVE_DARK_MODE 时，标题栏仍是 DWM 背板（Mica/Acrylic）在采样
    // **桌面壁纸** —— 用户浅色壁纸下它变成浅蓝 #98B4D0..#B7CFE8，而客户区是我们自绘的
    // 深色 #101418，两者拼在一起非常突兀，且我自绘的标题文字（深色）落在浅底上几乎看不清。
    // 钉死 caption 颜色后，标题栏不再依赖壁纸，观感一致且永远可读。
    let caption = if dark { 0x0018_1410u32 } else { 0x00F7_F4F2u32 }; // 注意 DWM 颜色是 0x00BBGGRR
    let r_caption = unsafe {
        DwmSetWindowAttribute(
            hwnd,
            DWMWA_CAPTION_COLOR,
            &caption as *const u32 as *const _,
            4,
        ) == 0
    };

    // ⚠️ 不再设 DWMWA_BORDER_COLOR：我们设的那条 1px 边框是**用户看得见的"多出来一层边框"**
    // （用户实测反馈"为什么窗口上还有一个窗口边框"）。去掉后交给 DWM 默认处理。
    let border_removed = true;

    crate::trace::trace(&format!(
        "dwm: dark={} corner={} caption_color={} border_removed={} build={}",
        r_dark,
        r_corner,
        r_caption,
        border_removed,
        crate::native::windows_build()
    ));

    // 设完属性后强制刷新一次窗口框架：部分 Windows 版本不会立即重画标题栏，
    // 需要 SWP_FRAMECHANGED 触发（实测：只设 attribute 画面不变）。
    unsafe {
        SetWindowPos(
            hwnd,
            std::ptr::null_mut(),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        );
    }
}

/// 尝试设置系统背板；返回是否被 DWM 接受。
fn try_backdrop(hwnd: HWND, kind: i32) -> bool {
    set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, &kind)
}

/// 选择并套用玻璃材质，返回**实际生效**的那一级（供界面如实显示）。
///
/// ⚠️ **2026-09-14 实测结论（ERROR.md E33）**：`DWMWA_SYSTEMBACKDROP_TYPE` 一旦开启，
/// 标题栏就由 DWM 用背板绘制，**`DWMWA_CAPTION_COLOR` 会被无视**
/// （实测四个 DWM 调用全部返回成功，标题栏像素依旧是壁纸色的浅蓝渐变
/// `#98B4D0 → #B7CFE8`，与自绘的深色客户区 `#101418` 拼在一起非常突兀）。
///
/// 因此默认**不开系统背板**，整窗自绘纯色：观感一致、永远可读、不依赖壁纸。
/// 想要玻璃观感时可以显式打开（`apply_glass(hwnd, dark, true)`），
/// 但那会连带把标题栏交回 DWM，观感取决于壁纸。
pub fn apply_glass(hwnd: HWND, dark: bool) -> Backdrop {
    apply_glass_with_backdrop(hwnd, dark, false)
}

pub fn apply_glass_with_backdrop(hwnd: HWND, dark: bool, allow_backdrop: bool) -> Backdrop {
    apply_chrome(hwnd, dark);

    // ⚠️ 无边框窗口默认**没有投影**（少了那一圈系统阴影，边缘看着很"硬"，
    // 用户会觉得"窗口外面还套着一个框"）。把整个客户区声明为"框架"即可让 DWM
    // 重新为它合成圆角 + 投影；我们自己在客户区画满不透明内容，不会被看穿。
    unsafe {
        let margins = MARGINS {
            cxLeftWidth: -1,
            cxRightWidth: -1,
            cyTopHeight: -1,
            cyBottomHeight: -1,
        };
        let hr = DwmExtendFrameIntoClientArea(hwnd, &margins);
        crate::trace::trace(&format!("dwm: extend frame into client (shadow) hr={}", hr));
    }

    let build = native::windows_build();
    if !allow_backdrop || build < 22621 {
        // 纯色：让 DWM 不要在我们的客户区上画任何东西
        let _ = try_backdrop(hwnd, DWMSBT_NONE);
        crate::trace::trace("dwm: system backdrop disabled (solid, consistent with painted client)");
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
