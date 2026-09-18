//! 与 Python 后端通信（**完全复用 1.0.4 已验证的契约**）。
//!
//! 1. 拉起后端 exe（或开发期 `py -3.10 backend/main.py`）
//! 2. 读 stdout 的**握手行**（含随机端口与管道名）
//! 3. 连命名管道收推送事件（日志/进度/状态）
//! 4. HTTP 请求-响应（状态/控制/设置）
//!
//! 所有网络与子进程操作都在**后台线程**里，UI 线程只读共享状态。

use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::json::{self, Value};
use crate::winhttp;

/// 单一来源：Cargo.toml 的 version（改版本号只改那一处）。
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
const READY_TOKEN: &str = "learn-helper-backend-ready";

// ---------------------------------------------------------------- 状态
#[derive(Clone)]
pub struct EngineView {
    pub running: bool,
    pub paused: bool,
    pub page_count: i64,
    pub video_count: i64,
    pub doc_count: i64,
    pub task_text: String,
    pub video_text: String,
    pub quiz_text: String,
    pub last_error: String,
}

impl Default for EngineView {
    fn default() -> Self {
        EngineView {
            running: false,
            paused: false,
            page_count: 0,
            video_count: 0,
            doc_count: 0,
            task_text: "闲置中".into(),
            video_text: "--".into(),
            quiz_text: "--".into(),
            last_error: String::new(),
        }
    }
}

/// UI 与后端线程共享的全部状态（一把锁，粗粒度但足够快）。
#[derive(Default)]
pub struct CoreState {
    pub connected: bool,
    pub status_text: String,
    pub version: String,
    pub device_id: String,
    pub base_url: String,
    pub pages: Vec<String>,
    pub selected_page: String,
    pub engine: EngineView,
    pub logs: Vec<String>,
    pub log_seq: i64,
    pub answer_mode: String,
    pub server_url: String,
    /// 答题设置快照（来自 /api/settings 的 answer 段；对话框回填用）
    pub answer_mode_key: String,
    pub answer_workers: i64,
    pub answer_timeout: i64,
    pub answer_retry: i64,
    /// LLM 设置快照（base_url / model / 是否已存 key —— 后端不回传 key 明文）
    pub llm_base: String,
    pub llm_model: String,
    pub llm_has_key: bool,
    /// 视频倍速（来自后端 settings.run.video_speed）
    pub video_speed: f64,
    /// 提交模式：true=自动提交 / false=仅暂存
    pub auto_submit: bool,
    /// 启动界面时自动打开沙盒浏览器（后端 settings.run.auto_launch_browser）
    pub auto_launch_browser: bool,
    /// 需要 UI 处理的提示（弹窗/追加日志），由 UI 线程消费后清空。
    pub flash: Option<String>,
    pub backend_exe: String,
    /// **重绘版本号**：任何会影响画面的改动都必须 +1。
    /// UI 线程据此判断"要不要重绘"——没有它就只能定时无条件重绘，
    /// 表现就是界面上文字一直"一抽一抽"（实测踩过，见 ERROR.md E38）。
    pub rev: u64,
}

impl CoreState {
    pub fn push_log(&mut self, line: &str) {
        for part in line.split('\n') {
            let text = part.trim_end_matches('\r');
            if text.is_empty() {
                continue;
            }
            self.logs.push(text.to_string());
        }
        // 与后端一致：界面上只保留最近 N 行，避免长跑爆内存
        const MAX: usize = 2000;
        if self.logs.len() > MAX {
            let drop = self.logs.len() - MAX;
            self.logs.drain(0..drop);
        }
    }
}

pub struct Shared {
    pub state: Mutex<CoreState>,
    pub hwnd: Arc<Mutex<isize>>,
    pub stop: AtomicBool,
    pub child: Mutex<Option<Child>>,
    /// 上一次"可见状态"的指纹：只有它变了才递增 rev。
    /// ⚠️ 不能"每次 notify 就 +1"：兜底轮询每 1.5s 无条件调一次 notify，
    /// 那样界面仍会以 ~1/1.5s 的频率持续重绘，用户看到的就是文字"一抽一抽"
    /// （实测：修复前空闲期 6 次/5 秒，见 ERROR.md E38）。
    last_fp: Mutex<u64>,
}

impl Shared {
    pub fn new() -> Arc<Shared> {
        Arc::new(Shared {
            state: Mutex::new(CoreState {
                status_text: "正在启动后端…".into(),
                version: APP_VERSION.into(),
                ..Default::default()
            }),
            hwnd: Arc::new(Mutex::new(0)),
            stop: AtomicBool::new(false),
            child: Mutex::new(None),
            last_fp: Mutex::new(0),
        })
    }

    pub fn lock(&self) -> std::sync::MutexGuard<'_, CoreState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// "画面上看得见的东西"的指纹。只包含**会改变画面**的状态，
    /// 不包含 uptime 这类每帧都在变但界面不显示的量。
    fn fingerprint(st: &CoreState) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        st.connected.hash(&mut h);
        st.status_text.hash(&mut h);
        st.device_id.hash(&mut h);
        st.base_url.hash(&mut h);
        st.pages.hash(&mut h);
        st.selected_page.hash(&mut h);
        st.answer_mode.hash(&mut h);
        st.video_speed.to_bits().hash(&mut h);
        st.auto_submit.hash(&mut h);
        // ⚠️ 新加的"会显示在界面上的设置"也要进指纹：漏了它，改了设置界面不会重绘
        st.auto_launch_browser.hash(&mut h);
        st.log_seq.hash(&mut h);
        st.logs.len().hash(&mut h);
        st.engine.running.hash(&mut h);
        st.engine.paused.hash(&mut h);
        st.engine.page_count.hash(&mut h);
        st.engine.video_count.hash(&mut h);
        st.engine.doc_count.hash(&mut h);
        st.engine.task_text.hash(&mut h);
        st.engine.video_text.hash(&mut h);
        st.engine.quiz_text.hash(&mut h);
        st.engine.last_error.hash(&mut h);
        h.finish()
    }

    /// 通知 UI 线程：**只有可见状态真变了**才请求重绘（不阻塞后台线程）。
    pub fn notify_ui(&self) {
        let fp = {
            let st = self.lock();
            Self::fingerprint(&st)
        };
        let changed = {
            let mut last = self.last_fp.lock().unwrap_or_else(|e| e.into_inner());
            if *last == fp {
                false
            } else {
                *last = fp;
                true
            }
        };
        if !changed {
            return;
        }
        {
            let mut st = self.lock();
            st.rev = st.rev.wrapping_add(1);
        }
        let hwnd = *self.hwnd.lock().unwrap_or_else(|e| e.into_inner());
        if hwnd != 0 {
            unsafe {
                crate::native::PostMessageW(hwnd as crate::native::HWND, crate::native::WM_APP_BACKEND, 0, 0);
            }
        }
    }
}

// ---------------------------------------------------------------- 启动
/// 找到后端可执行入口：优先发布包内的 exe，其次开发期源码。
pub fn locate_backend() -> Option<(String, Vec<String>)> {
    let base = std::env::current_exe().ok()?.parent()?.to_path_buf();

    let packaged = base.join("backend").join("learn-helper-core.exe");
    if packaged.exists() {
        return Some((
            packaged.to_string_lossy().into_owned(),
            vec!["--port".into(), "0".into()],
        ));
    }

    // 开发期：沿目录树找 backend/main.py（exe 在 native/target/release 或 winui/bin 下）
    let mut dir: Option<PathBuf> = Some(base);
    for _ in 0..6 {
        let Some(current) = dir else { break };
        let candidate = current.join("backend").join("main.py");
        if candidate.exists() {
            let py = find_python()?;
            let mut args = vec![candidate.to_string_lossy().into_owned()];
            args.push("--port".into());
            args.push("0".into());
            return Some((py, args));
        }
        dir = current.parent().map(|p| p.to_path_buf());
    }
    None
}

fn find_python() -> Option<String> {
    // ⚠️ 为什么要"解析出真正的解释器路径"而不直接用 `py`（2026-09-16 实测）：
    //   用 `py` 时进程链是 **UI → py.exe → python.exe**。UI 一旦被强杀/异常退出，
    //   中间那层 `py.exe` **仍然活着**，于是后端的"stdin EOF ⇒ 自杀"逻辑收不到 EOF
    //   ⇒ **界面已经没了、后端还挂在那里**（实测残留：一个 10 分钟前的后端仍在监听端口，
    //   用户截图里发现的；见 ERROR.md E45）。
    //   直接 spawn `python.exe` 后进程链只剩一层：UI 一死 stdin 立刻 EOF，后端干净退出；
    //   而且 `shutdown()` 里的 `child.kill()` 也终于杀的是真后端而不是外层 shim。
    // 注意：这里**保持与 `py <script>` 相同的解释器选择**（不改解释器版本），只去掉 shim；
    //   "实际跑的是默认 py（本机 = 3.12）而不是文档写的 3.10" 是另一个问题，见 DEVELOPMENT.md。
    if let Some(path) = python_exe_path("py") {
        return Some(path);
    }
    // 兜底：解析不出来就直接用 launcher（有 shim，但至少能跑起来）
    for exe in ["py", "python"] {
        let mut cmd = Command::new(exe);
        cmd.args(["-c", "print(1)"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if let Ok(mut child) = cmd.spawn() {
            if let Ok(status) = child.wait() {
                if status.success() {
                    return Some(exe.to_string());
                }
            }
        }
    }
    None
}

/// 解析 `py` 背后真实解释器的可执行文件路径（拿不到就返回 `None`，由调用方兜底）。
fn python_exe_path(launcher: &str) -> Option<String> {
    let out = Command::new(launcher)
        .args(["-c", "import sys;print(sys.executable)"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW：不要闪一个黑框
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if path.is_empty() || !PathBuf::from(&path).exists() {
        return None;
    }
    Some(path)
}

/// 在后台线程完成：拉起后端 → 握手 → 连管道 → 首次拉状态。
pub fn start_async(shared: Arc<Shared>) {
    // 验证钩子：`LH_NO_BACKEND=1` 时**完全不拉后端**（界面自己跑，没有 HTTP/管道）。
    //
    // 为什么需要它：探针要验证"界面自己的行为"时，后端会持续往日志面板写行
    // （握手/状态轮询），于是"日志条数不变"这类断言根本立不住 —— 实测踩到
    // （`logscroll_probe.ps1` 的"日志不满一屏就没有滚动条"用例）。
    // 早先那套"把 exe 复制到没有 backend/ 的目录"的绕法**不可靠**：
    // `locate_backend()` 会沿目录树向上找，从 %TEMP% 子目录往上 4 层仍可能找到真后端。
    if std::env::var("LH_NO_BACKEND").is_ok() {
        crate::trace::trace("backend: start skipped (LH_NO_BACKEND set)");
        let mut st = shared.lock();
        st.status_text = "验证模式：未启动后端".into();
        st.push_log("[native] (验证) LH_NO_BACKEND=1：未启动后端");
        drop(st);
        shared.notify_ui();
        return;
    }
    std::thread::spawn(move || {
        match spawn_and_handshake(&shared) {
            Ok((port, pipe_name)) => {
                {
                    let mut st = shared.lock();
                    st.connected = true;
                    st.base_url = format!("http://127.0.0.1:{}", port);
                    let base = st.base_url.clone();
                    st.status_text = format!("已连接 v{} · {}", APP_VERSION, base);
                    st.push_log(&format!(
                        "[native] 后端已连接：HTTP {}　管道 {}",
                        base, pipe_name
                    ));
                }
                shared.notify_ui();
                crate::trace::trace("backend: connected; pipe reader + poller starting");

                refresh_settings(shared.clone());
                refresh_status(shared.clone());

                // 管道读线程（阻塞读；stop 时自然退出）
                let reader_shared = shared.clone();
                let pipe_for_reader = pipe_name.clone();
                std::thread::spawn(move || pipe_loop(reader_shared, pipe_for_reader));

                // 兜底轮询：管道丢了也不至于界面停摆
                poll_loop(shared);
            }
            Err(err) => {
                crate::trace::trace(&format!("backend: start failed: {}", err));
                let mut st = shared.lock();
                st.connected = false;
                st.status_text = format!("后端未连接：{}", err);
                st.push_log(&format!("[native] 启动后端失败：{}", err));
                st.push_log("[native] 界面仍可使用；修复后点「刷新网页」或重启程序重试。");
                drop(st);
                shared.notify_ui();
            }
        }
    });
}

fn spawn_and_handshake(shared: &Arc<Shared>) -> Result<(u16, String), String> {
    let (exe, args) = locate_backend().ok_or_else(|| {
        "未找到后端（backend\\learn-helper-core.exe 或 backend\\main.py + Python 3.10）".to_string()
    })?;

    {
        let mut st = shared.lock();
        st.backend_exe = exe.clone();
        st.push_log(&format!("[native] 启动后端：{} {}", exe, args.join(" ")));
    }
    crate::trace::trace(&format!("backend: spawn {} {}", exe, args.join(" ")));

    let mut child = Command::new(&exe)
        .args(&args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .spawn()
        .map_err(|e| format!("无法启动 {}：{}", exe, e))?;

    // stderr 单独抽干，避免管道写满卡死后端
    if let Some(err) = child.stderr.take() {
        std::thread::spawn(move || {
            let reader = BufReader::new(err);
            let mut count = 0;
            for line in reader.lines().map_while(Result::ok) {
                count += 1;
                if count <= 12 {
                    crate::trace::trace(&format!("backend[stderr]: {}", line));
                }
            }
            crate::trace::trace(&format!("backend[stderr]: closed after {} line(s)", count));
        });
    }

    let stdout = child.stdout.take().ok_or("后端 stdout 不可用")?;
    {
        let mut slot = shared.child.lock().unwrap_or_else(|e| e.into_inner());
        *slot = Some(child);
    }

    // 读握手行：最多等 30s（后端冷启动要起 HTTP + 管道 + 日志）
    let (tx, rx) = std::sync::mpsc::channel::<Option<(u16, String)>>();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        let mut result = None;
        let mut seen = 0;
        for line in reader.lines().map_while(Result::ok) {
            seen += 1;
            crate::trace::trace(&format!("backend[stdout#{}]: {}", seen, &line[..line.len().min(120)]));
            if line.contains(READY_TOKEN) {
                if let Some(value) = json::parse(&line) {
                    let port = value.int_at("port") as u16;
                    let pipe = value.str_at("pipe");
                    result = Some((port, pipe));
                    break;
                }
            }
        }
        let _ = tx.send(result);
    });

    match rx.recv_timeout(Duration::from_secs(30)) {
        Ok(Some((port, pipe))) if port > 0 => {
            crate::trace::trace(&format!("backend: handshake port={} pipe={}", port, pipe));
            // 等 /api/health 真的通（握手行只说明进程起来了）
            let base = format!("http://127.0.0.1:{}", port);
            let deadline = Instant::now() + Duration::from_secs(20);
            let mut attempts = 0;
            while Instant::now() < deadline {
                attempts += 1;
                match winhttp::request("GET", &format!("{}/api/health", base), None, &[], 3000) {
                    Ok(resp) => {
                        if attempts == 1 || resp.status == 200 {
                            crate::trace::trace(&format!(
                                "backend: health attempt={} status={} body={}",
                                attempts,
                                resp.status,
                                resp.text().chars().take(80).collect::<String>()
                            ));
                        }
                        if resp.status == 200 {
                            return Ok((port, pipe));
                        }
                    }
                    Err(err) => {
                        if attempts <= 2 {
                            crate::trace::trace(&format!("backend: health error: {}", err));
                        }
                    }
                }
                std::thread::sleep(Duration::from_millis(200));
            }
            Err("握手成功但 /api/health 无响应".into())
        }
        Ok(_) => Err("后端未返回握手行（详见后端日志）".into()),
        Err(_) => Err("等待后端握手超时（30s）".into()),
    }
}

/// 后台线程也能安全写的 trace（避免与主线 trace 争用被拒）。
fn trace_bg(message: &str) {
    crate::trace::trace(message);
}

// ---------------------------------------------------------------- 管道事件
fn pipe_loop(shared: Arc<Shared>, pipe_name: String) {
    if pipe_name.is_empty() {
        crate::trace::trace("backend: pipe name empty, skip reader");
        return;
    }
    let path = format!(r"\\.\pipe\{}", pipe_name);
    crate::trace::trace(&format!("backend: pipe reader start {}", path));
    let mut backoff = 250u64;
    let mut attempts = 0u32;
    while !shared.stop.load(Ordering::Relaxed) {
        attempts += 1;
        match std::fs::OpenOptions::new().read(true).write(true).open(&path) {
            Ok(file) => {
                crate::trace::trace(&format!("backend: pipe connected (attempt {})", attempts));
                backoff = 250;
                let mut reader = BufReader::new(file);
                let mut line = String::new();
                loop {
                    line.clear();
                    match reader.read_line(&mut line) {
                        Ok(0) => break,
                        Ok(_) => handle_event(&shared, &line),
                        Err(e) => {
                            crate::trace::trace(&format!("backend: pipe read error {}", e));
                            break;
                        }
                    }
                    if shared.stop.load(Ordering::Relaxed) {
                        return;
                    }
                }
            }
            Err(e) => {
                if attempts <= 3 || attempts % 5 == 0 {
                    crate::trace::trace(&format!(
                        "backend: pipe open failed (attempt {}) {}",
                        attempts, e
                    ));
                }
                std::thread::sleep(Duration::from_millis(backoff));
                backoff = (backoff * 2).min(3000);
            }
        }
        std::thread::sleep(Duration::from_millis(backoff.min(1000)));
    }
}

fn handle_event(shared: &Arc<Shared>, line: &str) {
    let Some(value) = json::parse(line) else {
        return;
    };
    let kind = value.str_at("type");
    match kind.as_str() {
        "hello" => {
            let mut st = shared.lock();
            if st.version.is_empty() {
                st.version = value.str_at("version");
            }
            st.device_id = value.str_at("device_id");
        }
        "log" => {
            let text = value.str_at("text");
            let mut st = shared.lock();
            st.log_seq = value.int_at("seq");
            st.push_log(&text);
        }
        "progress" => {
            let mut st = shared.lock();
            st.engine.task_text = value.str_at("task");
            st.engine.video_text = value.str_at("video");
            st.engine.quiz_text = value.str_at("quiz");
        }
        "pages" => {
            let pages = value.str_list_at("pages");
            let mut st = shared.lock();
            if !pages.is_empty() {
                st.pages = pages;
            }
        }
        "status" => {
            let mut st = shared.lock();
            apply_engine(&mut st, &value);
        }
        _ => return,
    }
    shared.notify_ui();
}

fn apply_engine(st: &mut CoreState, value: &Value) {
    let Some(engine) = value.get("engine") else {
        return;
    };
    st.engine.running = engine.bool_at("running");
    st.engine.paused = engine.bool_at("paused");
    st.engine.page_count = engine.int_at("page_count");
    st.engine.video_count = engine.int_at("video_count");
    st.engine.doc_count = engine.int_at("doc_count");
    let task = engine.str_at("task_text");
    if !task.is_empty() {
        st.engine.task_text = task;
    }
    let video = engine.str_at("video_text");
    if !video.is_empty() {
        st.engine.video_text = video;
    }
    let quiz = engine.str_at("quiz_text");
    if !quiz.is_empty() {
        st.engine.quiz_text = quiz;
    }
    st.engine.last_error = engine.str_at("last_error");
    let selected = value.str_at("selected_page");
    if !selected.is_empty() {
        st.selected_page = selected;
    }
}

/// 兜底轮询：每 2s 拉一次状态，保证界面与真实状态一致。
fn poll_loop(shared: Arc<Shared>) {
    while !shared.stop.load(Ordering::Relaxed) {
        std::thread::sleep(Duration::from_millis(2000));
        if shared.stop.load(Ordering::Relaxed) {
            break;
        }
        refresh_status(shared.clone());
    }
}

pub fn refresh_status(shared: Arc<Shared>) {
    let base = shared.lock().base_url.clone();
    if base.is_empty() {
        return;
    }
    if let Ok(resp) = winhttp::request("GET", &format!("{}/api/status", base), None, &[], 5000) {
        if resp.status == 200 {
            if let Some(value) = json::parse(&resp.text()) {
                let mut st = shared.lock();
                st.device_id = value.str_at("device_id");
                let pages = value.str_list_at("pages");
                if !pages.is_empty() {
                    st.pages = pages;
                }
                apply_engine(&mut st, &value);
                drop(st);
                shared.notify_ui();
            }
        }
    }
}

pub fn refresh_settings(shared: Arc<Shared>) {
    let base = shared.lock().base_url.clone();
    if base.is_empty() {
        return;
    }
    if let Ok(resp) = winhttp::request("GET", &format!("{}/api/settings", base), None, &[], 5000) {
        if resp.status == 200 {
            if let Some(value) = json::parse(&resp.text()) {
                let data = value.get("data").cloned().unwrap_or(value);
                let mode_label = data
                    .get("answer")
                    .map(|a| a.str_at("mode_label"))
                    .unwrap_or_default();
                let mode_key = data.get("answer").map(|a| a.str_at("mode")).unwrap_or_default();
                let server_url = data.str_at("server_url");
                let device = data.str_at("device_id");
                let run = data.get("run");
                let speed = run.map(|r| r.num_at("video_speed")).unwrap_or(0.0);
                let auto_submit = run.map(|r| r.bool_at("auto_submit")).unwrap_or(true);
                let auto_launch_browser =
                    run.map(|r| r.bool_at("auto_launch_browser")).unwrap_or(true);
                let llm = data.get("llm");
                let llm_base = llm.map(|l| l.str_at("base_url")).unwrap_or_default();
                let llm_model = llm.map(|l| l.str_at("model")).unwrap_or_default();
                let llm_has_key = llm.map(|l| l.bool_at("has_api_key")).unwrap_or(false);
                let answer = data.get("answer");
                let workers = answer.map(|a| a.int_at("workers")).unwrap_or(0);
                let timeout = answer.map(|a| a.int_at("solver_timeout")).unwrap_or(0);
                let retry = answer.map(|a| a.int_at("retry")).unwrap_or(0);
                let mut st = shared.lock();
                if !mode_label.is_empty() {
                    st.answer_mode = mode_label;
                }
                if !mode_key.is_empty() {
                    st.answer_mode_key = mode_key;
                }
                if workers > 0 {
                    st.answer_workers = workers;
                }
                if timeout > 0 {
                    st.answer_timeout = timeout;
                }
                st.answer_retry = retry;
                if !llm_base.is_empty() {
                    st.llm_base = llm_base;
                }
                if !llm_model.is_empty() {
                    st.llm_model = llm_model;
                }
                st.llm_has_key = llm_has_key;
                if !server_url.is_empty() {
                    st.server_url = server_url;
                }
                if !device.is_empty() {
                    st.device_id = device;
                }
                if speed > 0.0 {
                    st.video_speed = speed;
                }
                st.auto_submit = auto_submit;
                st.auto_launch_browser = auto_launch_browser;
                drop(st);
                shared.notify_ui();
            }
        }
    }
}

// ---------------------------------------------------------------- HTTP 动作
/// 发送控制指令 / 刷新网页；结果写回共享状态并返回提示文本。
pub fn control(shared: &Arc<Shared>, action: &str, params: Option<Value>) -> Result<String, String> {
    let base = {
        let st = shared.lock();
        if !st.connected {
            return Err("后端尚未连接".into());
        }
        st.base_url.clone()
    };
    let body = json::stringify(&json::obj(&[
        ("action", json::s(action)),
        (
            "params",
            params.unwrap_or_else(|| json::obj(&[])),
        ),
    ]));
    let resp = winhttp::request(
        "POST",
        &format!("{}/api/control", base),
        Some(&body),
        &[],
        60_000,
    )?;
    let text = resp.text();
    let value = json::parse(&text).ok_or_else(|| "响应不是合法 JSON".to_string())?;

    {
        let mut st = shared.lock();
        let pages = value.str_list_at("pages");
        if !pages.is_empty() {
            st.pages = pages;
        }
        let selected = value.str_at("selected_page");
        if !selected.is_empty() {
            st.selected_page = selected;
        }
        apply_engine(&mut st, &value);
    }
    shared.notify_ui();

    if value.get("action_ok").map(|v| matches!(v, Value::Bool(false))).unwrap_or(false) {
        return Err(value.str_at("action_message"));
    }
    Ok(value.str_at("action_message"))
}

/// 单独取网页列表（refresh_pages 也走 control，这里给「刷新网页」按钮用）。
pub fn refresh_pages(shared: &Arc<Shared>) -> Result<String, String> {
    control(shared, "refresh_pages", None)
}

/// 更新设置（PUT /api/settings）。`json_body` 是**部分字段**的 JSON 对象，
/// 后端只覆盖给出的键（合并式写入，不会抹掉其它设置）。
pub fn update_settings(shared: &Arc<Shared>, json_body: &str) -> Result<String, String> {
    let base = {
        let st = shared.lock();
        if !st.connected {
            return Err("后端尚未连接".into());
        }
        st.base_url.clone()
    };
    let resp = winhttp::request(
        "PUT",
        &format!("{}/api/settings", base),
        Some(json_body),
        &[],
        10_000,
    )?;
    let text = resp.text();
    let value = json::parse(&text).ok_or_else(|| "响应不是合法 JSON".to_string())?;
    // 成功时同步刷新本地设置快照
    if value.get("ok").map(|v| matches!(v, Value::Bool(true))).unwrap_or(false) {
        refresh_settings(shared.clone());
        Ok(value.str_at("message"))
    } else {
        Err(value.str_at("message"))
    }
}

pub fn shutdown(shared: Arc<Shared>) {
    shared.stop.store(true, Ordering::SeqCst);
    let base = shared.lock().base_url.clone();
    if !base.is_empty() {
        let _ = winhttp::request("POST", &format!("{}/api/shutdown", base), Some("{}"), &[], 2000);
    }
    // 兜底：给 3 秒优雅退出，否则强杀（避免残留后端进程）
    let owned = shared.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(3));
        let mut slot = owned.child.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(child) = slot.as_mut() {
            let _ = child.kill();
        }
    });
}

/// 供日志面板显示的一行摘要。
pub fn describe_state(st: &CoreState) -> String {
    format!(
        "connected={} base={} pages={} running={}",
        st.connected,
        st.base_url,
        st.pages.len(),
        st.engine.running
    )
}
