//! 诊断落盘（对应 WinUI 版的 `ui-diag.log`）。
//!
//! 代理既不能截图也不能点界面，所以"程序启动了到底走到哪一步"必须能从文件里读出来。
//! 启动阶段（窗口/材质/后端握手）全部写这里；`native-diag.log` 与 exe 同目录。

use std::fs::OpenOptions;
use std::io::Write;
use std::sync::Mutex;

static TRACE_LOCK: Mutex<()> = Mutex::new(());

fn trace_path() -> std::path::PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    base.join("native-diag.log")
}

/// 追加一行（时间戳 + 文本）。永不 panic：诊断不能成为失败原因。
pub fn trace(message: &str) {
    let _guard = TRACE_LOCK.lock();
    let path = trace_path();

    // 有意不引入时间库：用系统时间戳由 to_local 的简易格式替代
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(file, "[{}] {}", now, message);
        // ⚠️ 立刻刷盘：进程被强杀时最后几行才不会丢（排障时正是最需要它们）
        let _ = file.flush();
        let _ = file.sync_data();
    }
}

/// 启动时清空（保持文件短小，便于每次运行只读本轮）。
pub fn reset() {
    let _guard = TRACE_LOCK.lock();
    let path = trace_path();
    let _ = std::fs::write(&path, b"");
}
