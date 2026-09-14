//! DWM 设置 —— **只保留最基础的两项：深色标题栏 + 圆角**。
//!
//! ## 为什么不搞玻璃了（2026-09-14 用户明确要求）
//!
//! 用户原话：**"不要这个透明的底，正常UI就行，忘记之前开发半透明玻璃的信息"**。
//! 于是本项目**彻底放弃半透明/亚克力/云母**，只做一个纯色、正常、看得清的窗口。
//!
//! 之前踩过的坑（已作废，仅留作教训）：
//! - 开 `DWMWA_SYSTEMBACKDROP_TYPE`（Mica/Acrylic）会让窗口透出**桌面壁纸**，
//!   壁纸一浅标题栏就变浅蓝，和深色内容拼在一起很怪；
//! - `DwmExtendFrameIntoClientArea(-1)` 会把客户区变成"框架"，
//!   虽然能换回投影，但也可能让背景变透明 —— 与"不要透明底"直接冲突，已删除；
//! - 想钉标题栏颜色还得跟背板打架（`DWMWA_CAPTION_COLOR` 会被无视）。
//!
//! 现在的做法：**客户区全程不透明自绘**，DWM 只负责标题栏配色与圆角。

use crate::native::{self, *};

// DWMWINDOWATTRIBUTE
const DWMWA_USE_IMMERSIVE_DARK_MODE: u32 = 20;
const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;

// DWM_WINDOW_CORNER_PREFERENCE
const DWMWCP_ROUND: i32 = 2;

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

/// 套用最基础的窗口外观：深色标题栏 + Win11 圆角。
///
/// **刻意不做**：系统背板（Mica/Acrylic）、把客户区扩展成框架、自定义边框色。
/// 任何一项都可能让窗口"透出桌面"，与"正常 UI"的要求相反。
pub fn apply_plain(hwnd: HWND, dark: bool) {
    let dark_i = if dark { 1 } else { 0 };
    let r_dark = set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, &dark_i);

    // Win11 圆角；Win10/更早会失败（无害，窗口就是直角）
    let corner = DWMWCP_ROUND;
    let r_corner = set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, &corner);

    crate::trace::trace(&format!(
        "dwm: plain chrome dark={} corner={} build={} (no backdrop / no extended frame)",
        r_dark,
        r_corner,
        native::windows_build()
    ));
}

/// 供自检/日志：本机是否开启了 DWM 合成（纯信息，不做任何透明处理）。
pub fn composition_enabled() -> bool {
    let mut enabled: BOOL = 0;
    unsafe {
        DwmIsCompositionEnabled(&mut enabled) == 0 && enabled != 0
    }
}
