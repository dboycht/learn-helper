#![windows_subsystem = "windows"]
//! learn-helper 原生界面（Win32 + DWM，零依赖单 exe）。
//!
//! 设计要点（与 `memory/01`、`memory/02` 的结论对齐）：
//! - **玻璃必须由 OS/DWM 提供**：这里用 `DWMWA_SYSTEMBACKDROP_TYPE` 让 DWM 做
//!   Mica/Acrylic 合成；自己画的任何半透明层都糊不到桌面，所以**只调 DWM**。
//! - **保留原生窗口能力**：`WS_THICKFRAME` + 自绘标题栏 = 可缩放/贴边/圆角阴影，
//!   同时用 `DWMWA_WINDOW_CORNER_PREFERENCE` 拿回 Win11 圆角。
//! - **零依赖**：Win32 与 WinHTTP 直接用 FFI 声明，不引入 windows-rs / reqwest。
//!
//! 与后端的契约**完全复用 1.0.4 已验证的接口**（backend/learn_helper/ipc.py）：
//! stdout 握手行 → `GET /api/status` → `POST /api/control` → 命名管道事件流。

mod about;
mod backend;
mod dwm;
mod gdi;
mod json;
mod native;
mod pagepicker;
mod settings;
mod trace;
mod ui;
mod winhttp;

use native::*;

/// 持有单实例互斥体句柄，保证它在进程整个生命周期内不被释放。
struct MutexKeep(*mut std::ffi::c_void);

fn main() {
    trace::reset();
    trace::trace(&format!("native: start v{} build={}", backend::APP_VERSION, native::windows_build()));

    // 单实例：用**命名互斥体**（比 FindWindow 查窗口可靠——窗口可能是残留僵尸）。
    // 互斥体句柄必须活到进程退出：放进 Box::leak 让它永不 Drop。
    unsafe {
        let mutex_name = wide("Local\\LearnHelperNativeSingleton");
        let mutex = CreateMutexW(std::ptr::null_mut(), 0, mutex_name.as_ptr());
        let already = !mutex.is_null() && GetLastError() == 183; // ERROR_ALREADY_EXISTS
        if already {
            trace::trace("native: 已有实例在运行，退出");
            std::process::exit(0);
        }
        if !mutex.is_null() {
            Box::leak(Box::new(MutexKeep(mutex)));
        }
    }

    unsafe {
        // 进程级 DPI 感知。
        // ⚠️ 必须**确认真的生效**：`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` 会失败
        // （例如 manifest/系统策略已固定了别的方式），失败后进程**不是 DPI 感知**的，
        // Windows 就会替我们做**坐标虚拟化** —— 表现为
        // `GetMonitorInfo` 给物理像素(2560x1600)、`GetWindowRect` 给逻辑像素(1707x1067)，
        // 两套坐标混用必然出错（实测拖拽夹取算不对，见 ERROR.md E37）。
        let dpi_ctx_ok = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) != 0;
        let dpi_aware_ok = dpi_ctx_ok || SetProcessDpiAwareness(2) == 0; // 2 = PROCESS_PER_MONITOR_DPI_AWARE
        trace::trace(&format!(
            "native: dpi awareness ctx_ok={} aware_ok={}",
            dpi_ctx_ok, dpi_aware_ok
        ));

        let _ = CoInitializeEx(std::ptr::null_mut(), COINIT_APARTMENTTHREADED);

        let app = ui::App::new();
        let app_ptr = Box::into_raw(Box::new(app));

        // 窗口创建期间（WM_NCCREATE）就会把 app_ptr 存进 GWLP_USERDATA 并回填 hwnd，
        // 因此窗口一存在，后台线程就能安全拿到 hwnd 发消息。
        // DPI 用主显示器 DPI 估算（窗口尺寸/最小尺寸都按它算）。
        let probe_dpi = {
            let dc = unsafe { GetDC(std::ptr::null_mut()) };
            let d = if dc.is_null() { 96 } else { unsafe { GetDeviceCaps(dc, 88) }.max(96) as u32 };
            if !dc.is_null() {
                unsafe { ReleaseDC(std::ptr::null_mut(), dc) };
            }
            d
        };
        let hwnd = ui::create_main_window(app_ptr, probe_dpi);
        if hwnd.is_null() {
            trace::trace("native: CreateWindowExW 失败，退出");
            std::process::exit(2);
        }
        trace::trace("native: 窗口已创建");
        ui::App::bootstrap(hwnd, &mut *app_ptr);

        // 对话框渲染探针：不建窗口、不碰屏幕，直接把「答题设置」画进 BMP。
        // 用法：learn-helper-native.exe --render-probe-settings [宽 高 输出路径]
        if std::env::args().any(|a| a == "--render-probe-settings") {
            let args: Vec<String> = std::env::args().collect();
            let pos = |n: usize, def: i32| {
                args.iter()
                    .position(|a| a == "--render-probe-settings")
                    .and_then(|i| args.get(i + n))
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(def)
            };
            let w = pos(1, 820);
            let h = pos(2, 700);
            let out = args
                .iter()
                .position(|a| a == "--render-probe-settings")
                .and_then(|i| args.get(i + 3))
                .cloned()
                .unwrap_or_else(|| "settings-render.bmp".to_string());
            settings::render_probe(&out, w, h);
            std::process::exit(0);
        }

        // 下拉渲染探针：不建窗口、不碰屏幕，直接把「当前网页」下拉画进 BMP。
        // 用法：learn-helper-native.exe --render-probe-pages [宽 高 输出路径]
        if std::env::args().any(|a| a == "--render-probe-pages") {
            let args: Vec<String> = std::env::args().collect();
            let pos = |n: usize, def: i32| {
                args.iter()
                    .position(|a| a == "--render-probe-pages")
                    .and_then(|i| args.get(i + n))
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(def)
            };
            let w = pos(1, 560);
            let h = pos(2, 190);
            let out = args
                .iter()
                .position(|a| a == "--render-probe-pages")
                .and_then(|i| args.get(i + 3))
                .cloned()
                .unwrap_or_else(|| "pages-render.bmp".to_string());
            pagepicker::render_probe(&out, w, h);
            std::process::exit(0);
        }

        // 渲染探针：把客户区画进内存 DC 并落盘为 BMP（不依赖 PrintWindow / 不碰屏幕）。
        // 用法：learn-helper-native.exe --render-probe [宽 高 输出路径]
        if std::env::args().any(|a| a == "--render-probe") {            let args: Vec<String> = std::env::args().collect();
            let w: i32 = args
                .iter()
                .position(|a| a == "--render-probe")
                .and_then(|i| args.get(i + 1))
                .and_then(|s| s.parse().ok())
                .unwrap_or(1173);
            let h: i32 = args
                .iter()
                .position(|a| a == "--render-probe")
                .and_then(|i| args.get(i + 2))
                .and_then(|s| s.parse().ok())
                .unwrap_or(960);
            let out = args
                .iter()
                .position(|a| a == "--render-probe")
                .and_then(|i| args.get(i + 3))
                .cloned()
                .unwrap_or_else(|| "native-render.bmp".to_string());
            native::render_client_to_bmp(&mut *app_ptr, w, h, &out);
            std::process::exit(0);
        }

        // 事件循环：GetMessage 阻塞在消息上，除非有新事件（定时器/后端消息）
        let mut msg: MSG = std::mem::zeroed();
        loop {
            let r = GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0);
            if r <= 0 {
                break;
            }
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        trace::trace("native: 事件循环结束，退出");

        drop(Box::from_raw(app_ptr));
        CoUninitialize();
    }
    std::process::exit(0);
}
