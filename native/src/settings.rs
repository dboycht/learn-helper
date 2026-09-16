//! 「答题设置」对话框（纯自绘，复用 `about.rs` 的最小对话框框架）。
//!
//! ## 为什么自绘
//! 塞一批原生 EDIT/COMBOBOX 会立刻露出老式控件外观（同 `about.rs` 的取舍）；
//! 自绘还能让**绘制与命中测试共用同一份 `layout()`**，不会再出现"看着在这里、
//! 点下去没反应"的错位（ERROR.md E40/E41 的教训）。
//!
//! ## 与后端的契约（**不需要改后端**）
//! 回填走 `GET /api/settings`，保存走 **`PUT /api/settings`（合并式写入，只覆盖给出的键）**。
//! ⚠️ 后端**从不回传 `llm.api_key`**（只给 `has_api_key`），所以：
//!   · 「API Key」框默认留空，留空 = **不修改**（绝不能发空串，那会把已存的 key 抹掉）；
//!   · 只有用户真敲了新值才把它放进请求体。
//! ⚠️ 后端只接受白名单键：`server_url` / `answer.{mode,workers,solver_timeout,retry}` /
//!   `llm.{base_url,api_key,model}` / `auto_submit`。发别的键是静默忽略。
//!
//! ## 验证通道（代理点不了界面）
//! `LH_UI_ACTION=settings | settings_save | settings_cancel` 会在打开对话框后
//! 用**真实消息**模拟点击/按键（走的是与鼠标完全相同的窗口过程路径）；
//! `--render-probe-settings` 直接把对话框画进 BMP（与主窗口 `--render-probe` 同一思路）。

use crate::backend::{self, Shared};
use crate::gdi::{self, TextAlign};
use crate::json;
use crate::native::*;
use crate::ui::{self, Colors, Theme};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};

const CLASS: &str = "LearnHelperSettingsWnd";
const TIMER_CARET: usize = 1;
const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8000";

/// 保存/测试完成的通知（后台线程 → UI 线程）；`wp` = 请求号。
const WM_APP_SAVE_DONE: u32 = WM_APP + 2;
const WM_APP_TEST_DONE: u32 = WM_APP + 3;
const WM_APP_HOOK: u32 = WM_APP + 4;

const LEN_TEXT: usize = 512;
const LEN_NUM: usize = 6;

// ================================================================ 类型
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Ctl {
    Mode(usize),
    Field(Field),
    /// (数字字段, +1 / -1)
    Step(Field, i32),
    AutoSubmit,
    TestServer,
    Save,
    Cancel,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Field {
    ServerUrl,
    Workers,
    Timeout,
    Retry,
    LlmBase,
    LlmKey,
    LlmModel,
}

impl Field {
    fn numeric(self) -> bool {
        matches!(self, Field::Workers | Field::Timeout | Field::Retry)
    }
    fn max_len(self) -> usize {
        if self.numeric() {
            LEN_NUM
        } else {
            LEN_TEXT
        }
    }
    /// (最小, 最大, 默认)
    fn range(self) -> (i64, i64, i64) {
        match self {
            Field::Workers => (1, 16, 4),
            Field::Timeout => (10, 600, 240),
            Field::Retry => (0, 5, 2),
            _ => (0, 0, 0),
        }
    }
}

/// 一行设置项（标签 + 输入框；数字行还有 -/+ 与单位）。
struct RowSpec {
    ctl: Ctl,
    label: &'static str,
    field: Field,
    suffix: &'static str,
    hint: &'static str,
}

/// 全部控件矩形（**绘制与命中共用**）。
struct Layout {
    title: RECT,
    subtitle: RECT,
    modes: [(RECT, &'static str, &'static str); 3],
    rows: Vec<Row>,
    auto: RECT,
    test: RECT,
    save: RECT,
    cancel: RECT,
    status: RECT,
}

struct Row {
    label: RECT,
    field_rc: RECT,
    minus: RECT,
    plus: RECT,
    spec: RowSpec,
}

/// 表单数据 + 编辑状态。经 `GWLP_USERDATA` 传给窗口过程。
struct Form {
    server_url: String,
    llm_base: String,
    llm_key: String,
    llm_model: String,
    has_api_key: bool,
    mode: String,
    workers: i64,
    timeout: i64,
    retry: i64,
    auto_submit: bool,
    /// 初始快照（用于"只提交改动项"与"有改动*"）
    init: Snapshot,
    /// 从后端读到过设置没有
    loaded: bool,

    // 编辑状态
    focus: Option<Field>,
    caret: usize,
    /// true = 整字段选中（首次键入替换全部）
    sel_all: bool,
    hover: Option<Ctl>,
    pressed: Option<Ctl>,
    error: Option<Field>,
    status: String,
    saving: bool,
    testing: bool,
    caret_on: bool,
    /// 用户真的改过的字段（只提交这些；避免把"还没读到的项"用默认值覆盖掉后端）
    touched: Vec<Field>,
}

#[derive(Clone, PartialEq)]
struct Snapshot {
    server_url: String,
    llm_base: String,
    llm_key: String,
    llm_model: String,
    mode: String,
    workers: i64,
    timeout: i64,
    retry: i64,
    auto_submit: bool,
}

impl Default for Form {
    fn default() -> Form {
        Form {
            server_url: String::new(),
            llm_base: String::new(),
            llm_key: String::new(),
            llm_model: String::new(),
            has_api_key: false,
            mode: "server".into(),
            workers: 0,
            timeout: 0,
            retry: 0,
            auto_submit: true,
            init: Snapshot {
                server_url: String::new(),
                llm_base: String::new(),
                llm_key: String::new(),
                llm_model: String::new(),
                mode: String::new(),
                workers: -1,
                timeout: -1,
                retry: -1,
                auto_submit: true,
            },
            loaded: false,
            focus: None,
            caret: 0,
            sel_all: false,
            hover: None,
            pressed: None,
            error: None,
            status: String::new(),
            saving: false,
            testing: false,
            caret_on: true,
            touched: Vec::new(),
        }
    }
}

impl Form {
    /// 用 `GET /api/settings` 的 data 段回填。
    fn load_from(&mut self, data: &json::Value) {
        let answer = data.get("answer");
        let llm = data.get("llm");
        let run = data.get("run");

        let url = data.str_at("server_url");
        self.server_url = if url.is_empty() { DEFAULT_SERVER_URL.to_string() } else { url };
        if let Some(a) = answer {
            let mode = a.str_at("mode");
            if !mode.is_empty() {
                self.mode = mode;
            }
            self.workers = a.int_at("workers").max(0);
            self.timeout = a.int_at("solver_timeout").max(0);
            self.retry = a.int_at("retry").max(0);
        }
        if let Some(l) = llm {
            self.llm_base = l.str_at("base_url");
            self.llm_model = l.str_at("model");
            self.has_api_key = l.bool_at("has_api_key");
        }
        if let Some(r) = run {
            if r.get("auto_submit").is_some() {
                self.auto_submit = r.bool_at("auto_submit");
            }
        }
        // API Key 永远从空白开始编辑（后端不回传）
        self.llm_key.clear();
        self.loaded = true;
        self.touched.clear();
        self.init = self.snapshot();
        let f = self.focus.unwrap_or(Field::ServerUrl);
        self.caret = self.field_text(f).chars().count();
    }

    fn snapshot(&self) -> Snapshot {
        Snapshot {
            server_url: self.server_url.clone(),
            llm_base: self.llm_base.clone(),
            llm_key: self.llm_key.clone(),
            llm_model: self.llm_model.clone(),
            mode: self.mode.clone(),
            workers: self.workers,
            timeout: self.timeout,
            retry: self.retry,
            auto_submit: self.auto_submit,
        }
    }

    fn dirty(&self) -> bool {
        self.snapshot() != self.init || !self.touched.is_empty()
    }

    fn touch(&mut self, f: Field) {
        if !self.touched.contains(&f) {
            self.touched.push(f);
        }
    }

    fn field_text(&self, f: Field) -> String {
        match f {
            Field::ServerUrl => self.server_url.clone(),
            Field::LlmBase => self.llm_base.clone(),
            Field::LlmKey => self.llm_key.clone(),
            Field::LlmModel => self.llm_model.clone(),
            Field::Workers => self.workers.to_string(),
            Field::Timeout => self.timeout.to_string(),
            Field::Retry => self.retry.to_string(),
        }
    }

    fn set_field_text(&mut self, f: Field, text: String) {
        match f {
            Field::ServerUrl => self.server_url = text,
            Field::LlmBase => self.llm_base = text,
            Field::LlmKey => self.llm_key = text,
            Field::LlmModel => self.llm_model = text,
            Field::Workers | Field::Timeout | Field::Retry => {
                let digits: String =
                    text.chars().filter(|c| c.is_ascii_digit()).take(LEN_NUM).collect();
                let v: i64 = digits.parse().unwrap_or(0);
                match f {
                    Field::Workers => self.workers = v,
                    Field::Timeout => self.timeout = v,
                    _ => self.retry = v,
                }
            }
        }
    }

    fn focus_on(&mut self, f: Field) {
        self.finalize();
        self.focus = Some(f);
        self.caret = self.field_text(f).chars().count();
        self.sel_all = true;
        self.caret_on = true;
        self.error = None;
    }

    /// 数字字段钳位（保存/失焦时调用）。超范围就夹到区间内。
    fn finalize(&mut self) {
        let Some(f) = self.focus else { return };
        if !f.numeric() {
            return;
        }
        let (min, max, default) = f.range();
        let raw = self.field_text(f);
        let value = match raw.trim().parse::<i64>() {
            Ok(v) => v.clamp(min, max),
            Err(_) => default,
        };
        if value.to_string() != raw {
            self.set_field_text(f, value.to_string());
        }
        self.caret = self.field_text(f).chars().count();
        self.error = None;
    }

    fn nudge(&mut self, f: Field, delta: i32) {
        let (min, max, _) = f.range();
        let step = if f == Field::Timeout { 10 } else { 1 };
        let cur = match f {
            Field::Workers => self.workers,
            Field::Timeout => self.timeout,
            _ => self.retry,
        };
        let next = (cur + delta as i64 * step).clamp(min, max);
        match f {
            Field::Workers => self.workers = next,
            Field::Timeout => self.timeout = next,
            _ => self.retry = next,
        }
        self.touch(f);
        if self.focus == Some(f) {
            self.caret = self.field_text(f).chars().count();
            self.sel_all = false;
        }
    }

    fn input_char(&mut self, ch: char) {
        let Some(f) = self.focus else { return };
        if f.numeric() && !ch.is_ascii_digit() {
            return;
        }
        let mut text = self.field_text(f);
        if self.sel_all {
            text.clear();
            self.caret = 0;
            self.sel_all = false;
        }
        if text.chars().count() >= f.max_len() {
            return;
        }
        let idx = self.caret.min(text.chars().count());
        let mut out = String::with_capacity(text.len() + 4);
        for (i, c) in text.chars().enumerate() {
            if i == idx {
                out.push(ch);
            }
            out.push(c);
        }
        if idx >= text.chars().count() {
            out.push(ch);
        }
        self.set_field_text(f, out);
        self.caret = idx + 1;
        self.touch(f);
        self.error = None;
    }

    fn backspace(&mut self) {
        let Some(f) = self.focus else { return };
        if self.sel_all {
            self.set_field_text(f, String::new());
            self.caret = 0;
            self.sel_all = false;
            self.touch(f);
            return;
        }
        if self.caret == 0 {
            return;
        }
        let text = self.field_text(f);
        let idx = self.caret.min(text.chars().count());
        let out: String =
            text.chars().enumerate().filter(|(i, _)| *i != idx - 1).map(|(_, c)| c).collect();
        self.set_field_text(f, out);
        self.caret = idx - 1;
        self.touch(f);
        self.error = None;
    }

    fn delete(&mut self) {
        let Some(f) = self.focus else { return };
        if self.sel_all {
            self.set_field_text(f, String::new());
            self.caret = 0;
            self.sel_all = false;
            self.touch(f);
            return;
        }
        let text = self.field_text(f);
        if self.caret >= text.chars().count() {
            return;
        }
        let out: String =
            text.chars().enumerate().filter(|(i, _)| *i != self.caret).map(|(_, c)| c).collect();
        self.set_field_text(f, out);
        self.touch(f);
        self.error = None;
    }

    /// 组装 `PUT /api/settings` 的请求体：**只放用户真改过、且与原值不同**的字段。
    fn build_patch(&self) -> String {
        let mut parts: Vec<String> = Vec::new();
        let changed = |f: Field, touched: &Vec<Field>| touched.contains(&f);

        if changed(Field::ServerUrl, &self.touched) && self.server_url != self.init.server_url {
            parts.push(format!("\"server_url\":{}", json_str(&self.server_url)));
        }
        let mut answer: Vec<String> = Vec::new();
        if changed(Field::Workers, &self.touched) && self.workers != self.init.workers {
            answer.push(format!("\"workers\":{}", self.workers));
        }
        if changed(Field::Timeout, &self.touched) && self.timeout != self.init.timeout {
            answer.push(format!("\"solver_timeout\":{}", self.timeout));
        }
        if changed(Field::Retry, &self.touched) && self.retry != self.init.retry {
            answer.push(format!("\"retry\":{}", self.retry));
        }
        if self.mode != self.init.mode {
            answer.push(format!("\"mode\":{}", json_str(&self.mode)));
        }
        if !answer.is_empty() {
            parts.push(format!("\"answer\":{{{}}}", answer.join(",")));
        }
        let mut llm: Vec<String> = Vec::new();
        if changed(Field::LlmBase, &self.touched) && self.llm_base != self.init.llm_base {
            llm.push(format!("\"base_url\":{}", json_str(&self.llm_base)));
        }
        if changed(Field::LlmModel, &self.touched) && self.llm_model != self.init.llm_model {
            llm.push(format!("\"model\":{}", json_str(&self.llm_model)));
        }
        // ⚠️ 空 = 不修改（后端不回传原 key，发空串会把已存的 key 清掉）
        if changed(Field::LlmKey, &self.touched) && !self.llm_key.trim().is_empty() {
            llm.push(format!("\"api_key\":{}", json_str(self.llm_key.trim())));
        }
        if !llm.is_empty() {
            parts.push(format!("\"llm\":{{{}}}", llm.join(",")));
        }
        if self.auto_submit != self.init.auto_submit {
            parts.push(format!("\"auto_submit\":{}", self.auto_submit));
        }
        format!("{{{}}}", parts.join(","))
    }
}

/// 最小 JSON 字符串转义（后端要求 ASCII 安全的报文）。
fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// ================================================================ 状态
struct SettingsState {
    shared: Arc<Shared>,
    theme: Theme,
    colors: Colors,
    fonts: Vec<ui::FontOwned>,
    hwnd: HWND,
    form: Form,
    /// 保存/测试结果槽（后台线程写，UI 线程 take）
    save_slot: Arc<Mutex<Option<(i64, Result<String, String>)>>>,
    test_slot: Arc<Mutex<Option<(i64, Result<String, String>)>>>,
    save_seq: Arc<AtomicI64>,
    test_seq: Arc<AtomicI64>,
    /// 探针模式：客户区尺寸由参数给出，不读真实窗口
    probe_client: Option<RECT>,
    /// 渲染探针用：显式指定 DPI
    dpi_override: Option<u32>,
}

impl SettingsState {
    fn new(shared: Arc<Shared>, theme: Theme) -> SettingsState {
        SettingsState {
            shared,
            theme,
            colors: Colors::for_theme(theme),
            fonts: ui::make_dialog_fonts(96),
            hwnd: NULL_HANDLE,
            form: Form::default(),
            save_slot: Arc::new(Mutex::new(None)),
            test_slot: Arc::new(Mutex::new(None)),
            save_seq: Arc::new(AtomicI64::new(0)),
            test_seq: Arc::new(AtomicI64::new(0)),
            probe_client: None,
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
            "settings: fonts rebuilt dpi={} ui={}px sm={}px b={}px mono={}px",
            dpi, sizes[0], sizes[1], sizes[2], sizes[3]
        ));
    }

    fn px(&self, logical: i32) -> i32 {
        (logical * self.dpi() as i32 * 100 / 96 + 50) / 100
    }

    fn client(&self) -> RECT {
        if let Some(rc) = self.probe_client {
            return rc;
        }
        let mut rc = RECT::default();
        unsafe { GetClientRect(self.hwnd, &mut rc) };
        rc
    }

    // ------------------------------------------------------------ 布局
    fn layout(&self) -> Layout {
        let client = self.client();
        let m = self.px(22);
        let right = (client.right - m).max(m + 100);
        let px = |v: i32| self.px(v);

        let title = RECT { left: m, top: px(16), right, bottom: px(16) + px(30) };
        let subtitle = RECT { left: m, top: title.bottom, right, bottom: title.bottom + px(18) };

        // 答题方式：三张等宽卡片（绘制/命中共用）
        let card_h = px(72);
        let gap = px(10);
        let card_top = subtitle.bottom + px(12);
        let card_bottom = card_top + card_h;
        let avail = ((right - m) - gap * 2) / 3;
        let modes = [
            (
                RECT { left: m, top: card_top, right: m + avail, bottom: card_bottom },
                "内部答题 API",
                "走自建后端 /solve（推荐）",
            ),
            (
                RECT {
                    left: m + avail + gap,
                    top: card_top,
                    right: m + avail * 2 + gap,
                    bottom: card_bottom,
                },
                "自配大模型",
                "直连 OpenAI 兼容接口",
            ),
            (
                RECT { left: m + avail * 2 + gap * 2, top: card_top, right, bottom: card_bottom },
                "仅识别不答题",
                "只识别题型，不填涂",
            ),
        ];

        let specs: Vec<RowSpec> = vec![
            RowSpec {
                ctl: Ctl::Field(Field::ServerUrl),
                label: "答题后端地址",
                field: Field::ServerUrl,
                suffix: "",
                hint: DEFAULT_SERVER_URL,
            },
            RowSpec {
                ctl: Ctl::Field(Field::Workers),
                label: "并发求解线程",
                field: Field::Workers,
                suffix: "个",
                hint: "1 ~ 16",
            },
            RowSpec {
                ctl: Ctl::Field(Field::Timeout),
                label: "单题超时",
                field: Field::Timeout,
                suffix: "秒",
                hint: "10 ~ 600",
            },
            RowSpec {
                ctl: Ctl::Field(Field::Retry),
                label: "失败重试",
                field: Field::Retry,
                suffix: "次",
                hint: "0 ~ 5",
            },
            RowSpec {
                ctl: Ctl::Field(Field::LlmBase),
                label: "大模型 Base URL",
                field: Field::LlmBase,
                suffix: "",
                hint: "https://api.openai.com/v1",
            },
            RowSpec {
                ctl: Ctl::Field(Field::LlmKey),
                label: "大模型 API Key",
                field: Field::LlmKey,
                suffix: "",
                hint: "",
            },
            RowSpec {
                ctl: Ctl::Field(Field::LlmModel),
                label: "大模型名称",
                field: Field::LlmModel,
                suffix: "",
                hint: "gpt-4o",
            },
        ];

        let row_h = px(42);
        let row_gap = px(8);
        let label_w = px(140);
        let bot_w = px(30);
        let step_w = px(96);
        let mut y = card_bottom + px(14);
        let mut rows: Vec<Row> = Vec::new();
        for spec in specs {
            let top = y;
            let bottom = top + row_h;
            let label = RECT { left: m, top, right: m + label_w, bottom };
            let full = RECT { left: m + label_w, top, right, bottom };
            let (field_rc, minus, plus) = if spec.field.numeric() {
                // 布局（**绘制与命中共用**）：范围提示 ----[−] [单位 值] [+]
                // ⚠️ 「+」必须整体落在右边界以内 —— 之前把它放在 right 之外，
                // 于是被窗口裁掉一半（实测，见 ERROR.md E42）。
                let plus = RECT { left: right - bot_w, top, right, bottom };
                let field_rc = RECT {
                    left: plus.left - px(8) - step_w,
                    top,
                    right: plus.left - px(8),
                    bottom,
                };
                let minus = RECT {
                    left: field_rc.left - px(8) - bot_w,
                    top,
                    right: field_rc.left - px(8),
                    bottom,
                };
                (field_rc, minus, plus)
            } else {
                (full, RECT::default(), RECT::default())
            };
            rows.push(Row { label, field_rc, minus, plus, spec });
            y = bottom + row_gap;
        }

        // 底部：自动提交 + 按钮行 + 状态行
        let btn_h = px(40);
        let btn_w = px(124);
        let btn_top = y + px(12);
        let btn_bottom = btn_top + btn_h;
        let cancel = RECT { left: right - btn_w, top: btn_top, right, bottom: btn_bottom };
        let save = RECT {
            left: cancel.left - px(10) - btn_w,
            top: btn_top,
            right: cancel.left - px(10),
            bottom: btn_bottom,
        };
        let test = RECT {
            left: save.left - px(10) - btn_w,
            top: btn_top,
            right: save.left - px(10),
            bottom: btn_bottom,
        };
        let auto =
            RECT { left: m, top: btn_top, right: test.left - px(12), bottom: btn_bottom };
        let status = RECT {
            left: m,
            top: btn_bottom + px(8),
            right,
            bottom: btn_bottom + px(8) + px(22),
        };

        Layout { title, subtitle, modes, rows, auto, test, save, cancel, status }
    }

    fn hit(&self, x: i32, y: i32) -> Option<Ctl> {
        let l = self.layout();
        for (i, (rc, _, _)) in l.modes.iter().enumerate() {
            if rc.contains(x, y) {
                return Some(Ctl::Mode(i));
            }
        }
        for row in &l.rows {
            if row.spec.field.numeric() {
                if row.minus.contains(x, y) {
                    return Some(Ctl::Step(row.spec.field, -1));
                }
                if row.plus.contains(x, y) {
                    return Some(Ctl::Step(row.spec.field, 1));
                }
            }
            if row.field_rc.contains(x, y) || row.label.contains(x, y) {
                return Some(row.spec.ctl);
            }
        }
        if l.auto.contains(x, y) {
            return Some(Ctl::AutoSubmit);
        }
        if l.test.contains(x, y) {
            return Some(Ctl::TestServer);
        }
        if l.save.contains(x, y) {
            return Some(Ctl::Save);
        }
        if l.cancel.contains(x, y) {
            return Some(Ctl::Cancel);
        }
        None
    }

    fn mode_index(&self) -> usize {
        match self.form.mode.as_str() {
            "llm" => 1,
            "off" => 2,
            _ => 0,
        }
    }

    fn set_mode(&mut self, idx: usize) {
        let m = match idx {
            1 => "llm",
            2 => "off",
            _ => "server",
        };
        self.form.mode = m.to_string();
    }
}

// ================================================================ 绘制
struct Ctx<'a> {
    hdc: HDC,
    state: &'a SettingsState,
    colors: Colors,
    px: &'a dyn Fn(i32) -> i32,
}

fn measure(hdc: HDC, text: &str, font: &ui::FontOwned) -> (i32, i32) {
    let s = gdi::measure_text(hdc, text, font);
    (s.cx, s.cy)
}

/// 画字段文本（长文本向左滚动，保证插入符可见）；返回插入符 x 坐标。
fn draw_field_text(ctx: &Ctx, rc: RECT, text: &str, caret: usize, placeholder: bool) -> i32 {
    let pad = (ctx.px)(10);
    let mono = &ctx.state.fonts[ui::DFONT_MONO];
    let ui_font = &ctx.state.fonts[ui::DFONT_UI];
    let use_mono = !text.is_empty() && text.chars().all(|c| c.is_ascii());
    let font = if use_mono { mono } else { ui_font };
    let (w_full, h) = measure(ctx.hdc, text, font);
    let w_before =
        measure(ctx.hdc, &text.chars().take(caret.min(text.chars().count())).collect::<String>(), font).0;
    let avail = (rc.width() - pad * 2).max(10);
    let left = if !placeholder && w_before > avail {
        rc.left + pad - (w_before - avail)
    } else {
        rc.left + pad
    };

    let clip = RECT { left: rc.left + 2, top: rc.top + 1, right: rc.right - 2, bottom: rc.bottom - 1 };
    gdi::clip_round_rect(ctx.hdc, clip, 4);
    if !text.is_empty() {
        let color = if placeholder { ctx.colors.text_muted } else { ctx.colors.text_main };
        gdi::text(ctx.hdc, text, left, rc.top + (rc.height() - h) / 2, color, font);
    }
    gdi::reset_clip(ctx.hdc);
    let _ = w_full;
    if placeholder {
        rc.left + pad
    } else {
        left + w_before
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

fn fill_and_border(ctx: &Ctx, rc: RECT, fill: u32, border: u32, radius: i32) {
    gdi::fill_round_rect(ctx.hdc, rc, radius, fill);
    gdi::stroke_round_rect(ctx.hdc, rc, radius, border);
}

impl SettingsState {
    /// 完整重绘。窗口 WM_PAINT 与渲染探针**共用这一份**。
    fn paint(&self, hdc: HDC, client: RECT) {
        let colors = self.colors;
        let f = &self.form;
        let px = |v: i32| self.px(v);
        let ctx = Ctx { hdc, state: self, colors, px: &px };
        let l = self.layout();

        gdi::fill_rect(hdc, client, colors.bg);

        // 标题 / 副标题（有改动时标题带 *）
        let title = if f.dirty() { "答题设置 · 有未保存的改动" } else { "答题设置" };
        gdi::text_in(hdc, title, l.title, TextAlign::Left, colors.text_main, &self.fonts[ui::DFONT_UI_B]);
        let sub = if f.loaded {
            "改动只在点「保存」后写入 config.json（「测试连接」不会保存）"
        } else {
            "正在读取后端设置…（读到之前无法保存）"
        };
        let sub_color = if f.loaded { colors.text_muted } else { colors.warn };
        gdi::text_in(hdc, sub, l.subtitle, TextAlign::Left, sub_color, &self.fonts[ui::DFONT_UI_SM]);
        gdi::hline(hdc, l.subtitle.left, l.subtitle.right, l.subtitle.bottom + px(5), colors.divider);

        // 答题方式三选一
        let sel = self.mode_index();
        for (i, (rc, label, tip)) in l.modes.iter().enumerate() {
            let on = i == sel;
            let hover = matches!(f.hover, Some(Ctl::Mode(h)) if h == i);
            let fill = if on {
                colors.accent_soft
            } else if hover {
                colors.card_hi
            } else {
                colors.card
            };
            fill_and_border(&ctx, *rc, fill, if on { colors.accent } else { colors.divider }, px(10));
            let dot = px(9).max(7);
            let cx = rc.left + px(16);
            let cy = rc.top + px(19);
            let dot_rc = RECT { left: cx - dot, top: cy - dot, right: cx + dot, bottom: cy + dot };
            gdi::fill_round_rect(hdc, dot_rc, dot, if on { colors.accent } else { colors.card_hi });
            gdi::stroke_round_rect(hdc, dot_rc, dot, if on { colors.accent } else { colors.divider });
            if on {
                gdi::fill_round_rect(hdc, dot_rc.inset(dot / 2, dot / 2), dot, colors.bg);
            }
            let text_left = dot_rc.right + px(10);
            gdi::text_in(
                hdc,
                label,
                RECT {
                    left: text_left,
                    top: rc.top + px(10),
                    right: rc.right - px(8),
                    bottom: rc.top + px(32),
                },
                TextAlign::Left,
                if on { colors.accent } else { colors.text_main },
                &self.fonts[ui::DFONT_UI_B],
            );
            gdi::text_in(
                hdc,
                tip,
                RECT {
                    left: text_left,
                    top: rc.top + px(32),
                    right: rc.right - px(8),
                    bottom: rc.bottom - px(8),
                },
                TextAlign::Left,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
        }

        // 输入行
        for row in &l.rows {
            let field = row.spec.field;
            let focused = f.focus == Some(field);
            let error = f.error == Some(field);
            let hovered = matches!(f.hover, Some(Ctl::Field(x)) if x == field);
            gdi::text_in(
                hdc,
                row.spec.label,
                row.label,
                TextAlign::Left,
                colors.text_sub,
                &self.fonts[ui::DFONT_UI],
            );
            let fill = if focused || hovered { colors.card_hi } else { colors.card };
            let border = if error {
                colors.danger
            } else if focused {
                colors.accent
            } else {
                colors.divider
            };
            fill_and_border(&ctx, row.field_rc, fill, border, px(8));

            if field.numeric() {
                let font = &self.fonts[ui::DFONT_UI];
                let text = f.field_text(field);
                let (tw, th) = measure(hdc, &text, font);
                let tx = row.field_rc.right - px(10) - tw;
                let ty = row.field_rc.top + (row.field_rc.height() - th) / 2;
                gdi::text(hdc, &text, tx, ty, colors.text_main, font);
                if focused && f.caret_on {
                    let before =
                        measure(hdc, &text.chars().take(f.caret).collect::<String>(), font).0;
                    caret_bar(hdc, tx + before, row.field_rc, &px, colors.accent);
                }
                // 单位（值框左端）+ 范围提示（在「-」左边，右对齐到「-」）
                gdi::text_in(
                    hdc,
                    row.spec.suffix,
                    RECT {
                        left: row.field_rc.left + px(10),
                        top: row.field_rc.top,
                        right: row.field_rc.left + px(10) + px(28),
                        bottom: row.field_rc.bottom,
                    },
                    TextAlign::Left,
                    colors.text_muted,
                    &self.fonts[ui::DFONT_UI_SM],
                );
                if !row.spec.hint.is_empty() {
                    gdi::text_in(
                        hdc,
                        row.spec.hint,
                        RECT {
                            left: row.label.right + px(12),
                            top: row.field_rc.top,
                            right: row.minus.left - px(10),
                            bottom: row.field_rc.bottom,
                        },
                        TextAlign::Right,
                        colors.text_muted,
                        &self.fonts[ui::DFONT_UI_SM],
                    );
                }
                for (rc, delta, glyph) in
                    [(row.minus, -1, 0xE738u32), (row.plus, 1, 0xE710u32)]
                {
                    let hover = matches!(f.hover, Some(Ctl::Step(x, d)) if x == field && d == delta);
                    gdi::fill_round_rect(hdc, rc, px(6), if hover { colors.btn_hover } else { colors.btn_idle });
                    draw_glyph(hdc, glyph, rc, colors.text_main, &px);
                }
            } else {
                let mut text = f.field_text(field);
                let mut placeholder = false;
                if text.is_empty() {
                    if field == Field::LlmKey {
                        text = if f.has_api_key {
                            "已保存（留空 = 不修改）".to_string()
                        } else {
                            "未设置（填了才会写入）".to_string()
                        };
                    } else if !row.spec.hint.is_empty() {
                        text = row.spec.hint.to_string();
                    }
                    placeholder = true;
                }
                let caret = if focused { f.caret } else { text.chars().count() };
                let caret_x = draw_field_text(&ctx, row.field_rc, &text, caret, placeholder);
                if focused && f.caret_on {
                    caret_bar(hdc, caret_x, row.field_rc, &px, colors.accent);
                }
            }
        }

        // 自动提交
        let sz = px(18);
        let cb = RECT {
            left: l.auto.left,
            top: l.auto.top + (l.auto.height() - sz) / 2,
            right: l.auto.left + sz,
            bottom: l.auto.top + (l.auto.height() + sz) / 2,
        };
        let hover_auto = f.hover == Some(Ctl::AutoSubmit);
        fill_and_border(
            &ctx,
            cb,
            if f.auto_submit { colors.accent } else { colors.card },
            if f.auto_submit {
                colors.accent
            } else if hover_auto {
                colors.accent
            } else {
                colors.divider
            },
            px(5),
        );
        if f.auto_submit {
            draw_glyph(hdc, 0xE73Eu32, cb, colors.bg, &px);
        }
        gdi::text_in(
            hdc,
            "自动提交答案（取消勾选 = 仅暂存）",
            RECT { left: cb.right + px(8), top: l.auto.top, right: l.auto.right, bottom: l.auto.bottom },
            TextAlign::Left,
            colors.text_sub,
            &self.fonts[ui::DFONT_UI],
        );

        // 按钮
        for (ctl, rc, label) in [
            (Ctl::TestServer, l.test, "测试连接"),
            (Ctl::Save, l.save, "保存"),
            (Ctl::Cancel, l.cancel, "取消"),
        ] {
            let hover = f.hover == Some(ctl);
            let primary = ctl == Ctl::Save;
            let busy = (primary && f.saving) || (ctl == Ctl::TestServer && f.testing);
            let (fill, fg) = if primary {
                (
                    if busy {
                        colors.card_hi
                    } else if hover {
                        colors.btn_hover
                    } else {
                        colors.accent
                    },
                    if busy {
                        colors.text_muted
                    } else if hover {
                        colors.text_main
                    } else {
                        colors.bg
                    },
                )
            } else {
                (
                    if hover { colors.btn_hover } else { colors.btn_idle },
                    if primary { colors.bg } else { colors.text_main },
                )
            };
            gdi::fill_round_rect(hdc, rc, px(8), fill);
            if !primary {
                gdi::stroke_round_rect(hdc, rc, px(8), colors.divider);
            }
            let text = if busy {
                if primary {
                    "保存中…"
                } else {
                    "测试中…"
                }
            } else {
                label
            };
            gdi::text_in(hdc, text, rc, TextAlign::Center, fg, &self.fonts[ui::DFONT_UI_B]);
        }

        // 状态行
        if !f.status.is_empty() {
            gdi::text_in(
                hdc,
                &f.status,
                l.status,
                TextAlign::Left,
                colors.text_muted,
                &self.fonts[ui::DFONT_UI_SM],
            );
        }
    }

    // ------------------------------------------------------------ 动作
    fn set_status(&mut self, text: &str) {
        self.form.status = text.to_string();
    }

    fn begin_save(&mut self) {
        if self.form.saving {
            return;
        }
        self.form.finalize();
        if !self.form.loaded {
            self.set_status("尚未读到后端设置，无法保存（等后端就绪后重开本窗口）");
            return;
        }
        let patch = self.form.build_patch();
        if patch == "{}" {
            self.set_status("没有改动需要保存");
            return;
        }
        if !self.shared.lock().connected {
            self.set_status("后端未连接，无法保存");
            return;
        }
        let id = self.save_seq.fetch_add(1, Ordering::SeqCst) + 1;
        self.form.saving = true;
        self.form.status = format!("正在保存… {}", patch);
        crate::trace::trace(&format!("settings: save #{} body={}", id, patch));

        let shared = self.shared.clone();
        let slot = self.save_slot.clone();
        let hwnd_raw = self.hwnd as isize;
        std::thread::spawn(move || {
            let outcome = backend::update_settings(&shared, &patch);
            if let Ok(mut g) = slot.lock() {
                *g = Some((id, outcome));
            }
            if hwnd_raw != 0 {
                unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_SAVE_DONE, id as WPARAM, 0) };
            }
        });
    }

    fn begin_test(&mut self) {
        if self.form.testing || self.form.saving {
            return;
        }
        if !self.shared.lock().connected {
            self.set_status("后端未连接，无法测试");
            return;
        }
        let url = self.form.server_url.clone();
        let id = self.test_seq.fetch_add(1, Ordering::SeqCst) + 1;
        self.form.testing = true;
        self.form.status = format!("正在测试 {} …", url);
        crate::trace::trace(&format!("settings: test #{} url={}", id, url));

        let shared = self.shared.clone();
        let slot = self.test_slot.clone();
        let hwnd_raw = self.hwnd as isize;
        std::thread::spawn(move || {
            let params = json::obj(&[("server_url", json::s(&url))]);
            let outcome = backend::control(&shared, "test_backend", Some(params));
            if let Ok(mut g) = slot.lock() {
                *g = Some((id, outcome));
            }
            if hwnd_raw != 0 {
                unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_TEST_DONE, id as WPARAM, 0) };
            }
        });
    }

    fn take_save(&mut self) -> Option<Result<String, String>> {
        let mut g = self.save_slot.lock().ok()?;
        let (id, outcome) = g.take()?;
        if id != self.save_seq.load(Ordering::SeqCst) {
            return None;
        }
        Some(outcome)
    }

    fn take_test(&mut self) -> Option<Result<String, String>> {
        let mut g = self.test_slot.lock().ok()?;
        let (id, outcome) = g.take()?;
        if id != self.test_seq.load(Ordering::SeqCst) {
            return None;
        }
        Some(outcome)
    }
}

fn caret_bar(hdc: HDC, caret_x: i32, rc: RECT, px: &dyn Fn(i32) -> i32, color: u32) {
    let ch = (rc.height() - px(16)).max(6);
    gdi::fill_rect(
        hdc,
        RECT {
            left: caret_x + 1,
            top: rc.top + (rc.height() - ch) / 2,
            right: caret_x + 2,
            bottom: rc.top + (rc.height() + ch) / 2,
        },
        color,
    );
}

// ================================================================ 窗口过程
#[repr(C)]
struct CREATESTRUCT {
    lpCreateParams: *mut std::ffi::c_void,
}

unsafe extern "system" fn settings_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_NCCREATE {
        let create = lp as *const CREATESTRUCT;
        let state_ptr = (*create).lpCreateParams as *mut SettingsState;
        if !state_ptr.is_null() {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, state_ptr as isize);
            (*state_ptr).hwnd = hwnd;
            // 窗口 DPI 只有这时才知道 ⇒ 立刻重建字体（否则高 DPI 下字偏小，E47）
            (*state_ptr).refresh_fonts();
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    let user = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut SettingsState;
    if user.is_null() {
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    let state = &mut *user;

    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(hwnd, &mut ps);
            if !hdc.is_null() {
                let client = state.client();
                let w = client.right.max(1);
                let h = client.bottom.max(1);
                // 双缓冲（同主窗口：直接画屏幕会看到文字闪）
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
        WM_SETFOCUS => {
            CreateCaret(hwnd, std::ptr::null_mut(), 1, state.px(16));
            state.form.caret_on = true;
            InvalidateRect(hwnd, std::ptr::null(), 0);
            0
        }
        WM_KILLFOCUS => {
            DestroyCaret();
            0
        }
        WM_TIMER => {
            if wp == TIMER_CARET && state.form.focus.is_some() {
                state.form.caret_on = !state.form.caret_on;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_MOUSEMOVE => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let hover = state.hit(x, y);
            if hover != state.form.hover {
                state.form.hover = hover;
                InvalidateRect(hwnd, std::ptr::null(), 0);
            }
            0
        }
        WM_SETCURSOR => {
            let mut pt = POINT::default();
            GetCursorPos(&mut pt);
            ScreenToClient(hwnd, &mut pt);
            let cursor = match state.hit(pt.x, pt.y) {
                Some(Ctl::Field(f)) if !f.numeric() => IDC_IBEAM,
                _ => IDC_ARROW,
            };
            SetCursor(LoadCursorW(std::ptr::null_mut(), cursor as *const u16));
            1
        }
        WM_LBUTTONDOWN => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            let hit = state.hit(x, y);
            state.form.pressed = hit;
            match hit {
                Some(Ctl::Field(f)) => {
                    // ⚠️ 点进文本字段 = **全选**（再打字即替换整段内容）。
                    // 绝不要在这里"按点击位置放插入符"：那会把 sel_all 清掉，
                    // 于是"点进去打字"变成**追加**在原文后面（实测踩到，见 ERROR.md E42）。
                    // 想精细改字符请用 ←/→/Home/End/Backspace。
                    state.form.focus_on(f);
                    unsafe { SetFocus(hwnd) };
                }
                Some(Ctl::Mode(i)) => {
                    state.form.finalize();
                    state.form.focus = None;
                    state.set_mode(i);
                    state.form.caret_on = false;
                }
                Some(Ctl::Step(f, d)) => {
                    state.form.finalize();
                    state.form.focus = None;
                    state.form.nudge(f, d);
                }
                Some(Ctl::AutoSubmit) => {
                    state.form.finalize();
                    state.form.focus = None;
                    state.form.auto_submit = !state.form.auto_submit;
                }
                _ => {
                    state.form.finalize();
                    state.form.focus = None;
                }
            }
            SetCapture(hwnd);
            InvalidateRect(hwnd, std::ptr::null(), 0);
            0
        }
        WM_LBUTTONUP => {
            let x = (lp & 0xFFFF) as i32;
            let y = ((lp >> 16) & 0xFFFF) as i32;
            ReleaseCapture();
            let pressed = state.form.pressed.take();
            if pressed.is_some() && pressed == state.hit(x, y) {
                match pressed.unwrap() {
                    Ctl::Save => state.begin_save(),
                    Ctl::TestServer => state.begin_test(),
                    Ctl::Cancel => {
                        crate::trace::trace("settings: cancel clicked (nothing written)");
                        DestroyWindow(hwnd);
                        return 0;
                    }
                    _ => {}
                }
            }
            InvalidateRect(hwnd, std::ptr::null(), 0);
            0
        }
        WM_CHAR => {
            if let Some(c) = char::from_u32(wp as u32) {
                if c == '\r' {
                    if state.form.focus.is_some() {
                        state.form.finalize();
                        state.form.focus = None;
                        state.form.caret_on = false;
                        InvalidateRect(hwnd, std::ptr::null(), 0);
                    } else {
                        state.begin_save();
                    }
                } else if c == '\u{1b}' {
                    DestroyWindow(hwnd);
                    return 0;
                } else if (c as u32) >= 0x20 && c != '\u{7f}' {
                    state.form.input_char(c);
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                }
            }
            0
        }
        WM_KEYDOWN => {
            let ctrl = (GetKeyState(VK_CONTROL as i32) as u16 & 0x8000) != 0;
            match wp {
                VK_ESCAPE => {
                    crate::trace::trace("settings: Escape → 关闭（不写盘）");
                    DestroyWindow(hwnd);
                    return 0;
                }
                VK_RETURN => {
                    if state.form.focus.is_some() {
                        state.form.finalize();
                        state.form.focus = None;
                        state.form.caret_on = false;
                        InvalidateRect(hwnd, std::ptr::null(), 0);
                    } else {
                        state.begin_save();
                    }
                    0
                }
                VK_S if ctrl => {
                    state.begin_save();
                    0
                }
                VK_BACK => {
                    state.form.backspace();
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_DELETE => {
                    state.form.delete();
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_LEFT => {
                    state.form.sel_all = false;
                    state.form.caret = state.form.caret.saturating_sub(1);
                    state.form.caret_on = true;
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_RIGHT => {
                    state.form.sel_all = false;
                    let len = state.form.focus.map(|f| state.form.field_text(f).chars().count()).unwrap_or(0);
                    state.form.caret = (state.form.caret + 1).min(len);
                    state.form.caret_on = true;
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_HOME => {
                    state.form.sel_all = false;
                    state.form.caret = 0;
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_END => {
                    state.form.sel_all = false;
                    let len = state.form.focus.map(|f| state.form.field_text(f).chars().count()).unwrap_or(0);
                    state.form.caret = len;
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                    0
                }
                VK_A if ctrl => {
                    if let Some(f) = state.form.focus {
                        state.form.sel_all = true;
                        state.form.caret = state.form.field_text(f).chars().count();
                        InvalidateRect(hwnd, std::ptr::null(), 0);
                    }
                    0
                }
                _ => DefWindowProcW(hwnd, msg, wp, lp),
            }
        }
        WM_APP_SAVE_DONE => {
            let id = wp as i64;
            if id == state.save_seq.load(Ordering::SeqCst) {
                match state.take_save() {
                    Some(Ok(msg)) => {
                        state.form.saving = false;
                        state.form.init = state.form.snapshot();
                        state.form.touched.clear();
                        crate::trace::trace(&format!("settings: save ok: {}", msg));
                        {
                            let mut st = state.shared.lock();
                            st.push_log(&format!(
                                "[设置] {}",
                                if msg.is_empty() { "已保存" } else { &msg }
                            ));
                        }
                        state.shared.notify_ui();
                        DestroyWindow(hwnd);
                        return 0;
                    }
                    Some(Err(err)) => {
                        state.form.saving = false;
                        crate::trace::trace(&format!("settings: save failed: {}", err));
                        state.set_status(&format!("保存失败：{}", err));
                        InvalidateRect(hwnd, std::ptr::null(), 0);
                    }
                    None => {}
                }
            }
            0
        }
        WM_APP_TEST_DONE => {
            let id = wp as i64;
            if id == state.test_seq.load(Ordering::SeqCst) {
                if let Some(outcome) = state.take_test() {
                    state.form.testing = false;
                    match outcome {
                        Ok(msg) => state.set_status(&format!(
                            "测试连接：{}",
                            if msg.is_empty() { "成功" } else { &msg }
                        )),
                        Err(err) => state.set_status(&format!("测试连接失败：{}", err)),
                    }
                    InvalidateRect(hwnd, std::ptr::null(), 0);
                }
            }
            0
        }
        WM_APP_HOOK => {
            if wp == usize::MAX {
                // 后台拉到的设置回来了：回填表单（用户已经动手改过就不覆盖）
                let data = settings_from_state(&state.shared);
                if let Some(data) = data {
                    if state.form.touched.is_empty() && !state.form.loaded {
                        state.form.load_from(&data);
                        state.form.focus_on(Field::ServerUrl);
                        crate::trace::trace("settings: filled from /api/settings");
                    }
                }
                InvalidateRect(hwnd, std::ptr::null(), 0);
                return 0;
            }
            let state2 = state as *mut SettingsState;
            let action = wp as usize;
            hook_action(hwnd, state2, action);
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_DESTROY => {
            KillTimer(hwnd, TIMER_CARET);
            0
        }
        WM_NCDESTROY => {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            crate::trace::trace("settings: window destroyed, state freed");
            drop(Box::from_raw(user));
            0
        }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

/// 按点击的 x 坐标近似定位插入符。
///
/// ⚠️ **当前 UI 不调用它**：点进字段一律"全选"（见 WM_LBUTTONDOWN 里的 E42 注释）。
/// 保留它是为了以后要做"点哪算哪"时不用重写，同时也留一份"按文本宽度定位"的实现参考。
#[allow(dead_code)]
fn caret_from_x(state: &SettingsState, f: Field, x: i32) -> Option<usize> {
    let l = state.layout();
    let row = l.rows.iter().find(|r| r.spec.field == f)?;
    let text = state.form.field_text(f);
    let hdc = unsafe { GetDC(state.hwnd) };
    if hdc.is_null() {
        return None;
    }
    let font = if !text.is_empty() && text.chars().all(|c| c.is_ascii()) {
        &state.fonts[ui::DFONT_MONO]
    } else {
        &state.fonts[ui::DFONT_UI]
    };
    let rel = x - row.field_rc.left - state.px(10);
    let mut best = 0usize;
    for i in 0..text.chars().count() {
        let w = gdi::measure_text(
            hdc,
            &text.chars().take(i + 1).collect::<String>(),
            font,
        )
        .cx;
        if rel >= w - state.px(4) {
            best = i + 1;
        }
    }
    unsafe { ReleaseDC(state.hwnd, hdc) };
    Some(best.min(text.chars().count()))
}

// ================================================================ 脚本化验证钩子
const HOOK_SAVE: usize = 1;
const HOOK_CANCEL: usize = 2;
const HOOK_OPEN: usize = 3;

/// 用**真实消息**驱动对话框（走与鼠标完全相同的窗口过程路径）。
fn hook_action(hwnd: HWND, state: *mut SettingsState, action: usize) {
    if state.is_null() {
        return;
    }
    unsafe {
        let s = &mut *state;
        // 所有 hook 都先打一行"开窗事实"：探针据此断言对话框真的建起来并回填了设置
        crate::trace::trace(&format!(
            "settings-hook: opened loaded={} url={} mode={} workers={} timeout={} retry={} auto={}",
            s.form.loaded,
            s.form.server_url,
            s.form.mode,
            s.form.workers,
            s.form.timeout,
            s.form.retry,
            s.form.auto_submit
        ));
        match action {
            HOOK_SAVE => {
                // 1) 点进「答题后端地址」= 首次点击 ⇒ 全选；随后键入即整段替换
                click_rect(hwnd, s, rect_of_field(s, Field::ServerUrl));
                send_chars(hwnd, "http://127.0.0.1:18080");
                std::thread::sleep(std::time::Duration::from_millis(150));
                // 2) 点「并发求解线程 +」两次（4 → 6）
                let plus = rect_of_step(s, Field::Workers, 1);
                click_rect(hwnd, s, plus);
                click_rect(hwnd, s, plus);
                // 3) 点「单题超时 +」一次（240 → 250）
                click_rect(hwnd, s, rect_of_step(s, Field::Timeout, 1));
                std::thread::sleep(std::time::Duration::from_millis(150));
                crate::trace::trace(&format!(
                    "settings-hook: before save url={} workers={} timeout={} retry={} patch={}",
                    s.form.server_url,
                    s.form.workers,
                    s.form.timeout,
                    s.form.retry,
                    s.form.build_patch()
                ));
                // 4) 点保存
                click_rect(hwnd, s, s.layout().save);
                let mut closed = false;
                for _ in 0..150 {
                    std::thread::sleep(std::time::Duration::from_millis(100));
                    if IsWindow(hwnd) == 0 {
                        closed = true;
                        break;
                    }
                }
                if closed {
                    crate::trace::trace("settings-hook: dialog closed after save");
                } else {
                    crate::trace::trace(&format!(
                        "settings-hook: save still open status={}",
                        s.form.status
                    ));
                }
                exit_after_hook();
            }
            HOOK_CANCEL => {
                click_rect(hwnd, s, rect_of_field(s, Field::ServerUrl));
                send_chars(hwnd, "http://127.0.0.1:19999");
                std::thread::sleep(std::time::Duration::from_millis(150));
                crate::trace::trace(&format!(
                    "settings-hook: cancel path url={} patch={}",
                    s.form.server_url,
                    s.form.build_patch()
                ));
                SendMessageW(hwnd, WM_KEYDOWN, VK_ESCAPE, 0);
                std::thread::sleep(std::time::Duration::from_millis(300));
                crate::trace::trace(&format!(
                    "settings-hook: after escape window_alive={}",
                    IsWindow(hwnd) != 0
                ));
                exit_after_hook();
            }
            _ => {
                crate::trace::trace("settings-hook: open-only, nothing more to do");
                exit_after_hook();
            }
        }
    }
}

/// 钩子跑完把主窗口关掉，让探针脚本不用超时强杀（`LH_UI_ACTION` 专用于无人值守验证，
/// 正常运行时这个变量不存在，因此不会影响用户）。
unsafe fn exit_after_hook() {
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_millis(1200));
        let main = FindWindowW(wide("LearnHelperNativeWnd").as_ptr(), std::ptr::null());
        if !main.is_null() {
            crate::trace::trace("settings-hook: closing main window");
            PostMessageW(main, WM_CLOSE, 0, 0);
        }
    });
}

unsafe fn click_rect(hwnd: HWND, _state: &SettingsState, rc: RECT) {
    if rc.right <= rc.left || rc.bottom <= rc.top {
        crate::trace::trace("settings-hook: 目标矩形为空，跳过点击");
        return;
    }
    let x = rc.left + (rc.width() / 2);
    let y = rc.top + (rc.height() / 2);
    let lp = ((y as isize) << 16) | ((x as isize) & 0xFFFF);
    SendMessageW(hwnd, WM_LBUTTONDOWN, 1, lp);
    SendMessageW(hwnd, WM_LBUTTONUP, 0, lp);
}

unsafe fn send_chars(hwnd: HWND, text: &str) {
    for c in text.chars() {
        SendMessageW(hwnd, WM_CHAR, c as WPARAM, 0);
    }
}

fn rect_of_field(s: &SettingsState, f: Field) -> RECT {
    s.layout()
        .rows
        .iter()
        .find(|r| r.spec.field == f)
        .map(|r| r.field_rc)
        .unwrap_or_default()
}

fn rect_of_step(s: &SettingsState, f: Field, delta: i32) -> RECT {
    s.layout()
        .rows
        .iter()
        .find(|r| r.spec.field == f)
        .map(|r| if delta > 0 { r.plus } else { r.minus })
        .unwrap_or_default()
}

// ================================================================ 创建
fn ensure_class() {
    unsafe {
        let class_w = wide(CLASS);
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: 0x0002, // CS_HREDRAW
            lpfnWndProc: Some(settings_proc),
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

/// 打开「答题设置」（非阻塞；同一时刻只允许一个）。
pub fn show(owner: HWND, shared: Arc<Shared>, theme: Theme) {
    unsafe {
        let existing = FindWindowW(wide(CLASS).as_ptr(), std::ptr::null());
        if !existing.is_null() {
            SetForegroundWindow(existing);
            return;
        }
    }
    ensure_class();
    create(owner, shared, theme, HOOK_OPEN);
}

/// 打开并注入脚本化动作（无人值守验证用；会先关掉已在开的那个）。
pub fn show_with_hook(owner: HWND, shared: Arc<Shared>, theme: Theme, action: &str) {
    unsafe {
        let existing = FindWindowW(wide(CLASS).as_ptr(), std::ptr::null());
        if !existing.is_null() {
            DestroyWindow(existing);
            std::thread::sleep(std::time::Duration::from_millis(120));
        }
    }
    ensure_class();
    let hook = match action {
        "settings_save" => HOOK_SAVE,
        "settings_cancel" => HOOK_CANCEL,
        _ => HOOK_OPEN,
    };
    create(owner, shared, theme, hook);
}

fn create(owner: HWND, shared: Arc<Shared>, theme: Theme, hook: usize) -> HWND {
    // 先用一份快照建对象：后端已连过时界面里就有全部设置（省一次网络往返）
    let mut state = Box::new(SettingsState::new(shared.clone(), theme));
    let snapshot = settings_from_state(&shared);
    if let Some(data) = &snapshot {
        state.form.load_from(data);
        state.form.focus_on(Field::ServerUrl);
    }

    let state_ptr = Box::into_raw(state);

    unsafe {
        let dpi = GetDpiForWindow(owner).max(96) as i32;
        let scale = |v: i32| (v * dpi * 100 / 96 + 50) / 100;
        // ⚠️ 尺寸按"逻辑像素"给，但**高 DPI 下会被乘上去**：144 DPI 时 820x700 逻辑
        // 变成 1230x1050 物理像素，在 1600 高的屏幕上会被工作区夹住、底部按钮贴边
        // （实测）。所以这里留足余量：内容实需约 620 逻辑高。
        let w = scale(770);
        let h = scale(630);

        let mut owner_rc = RECT::default();
        GetWindowRect(owner, &mut owner_rc);
        let mut x = owner_rc.left + (owner_rc.width() - w) / 2;
        let mut y = owner_rc.top + (owner_rc.height() - h) / 2;
        // 夹到工作区：多显示器 / 主窗口贴边时不要跑到屏幕外（E37 的同类坑）
        let mon = MonitorFromWindow(owner, MONITOR_DEFAULTTONEAREST);
        let mut mi =
            MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        if !mon.is_null() && GetMonitorInfoW(mon, &mut mi) != 0 {
            x = x.clamp(mi.rcWork.left, (mi.rcWork.right - w).max(mi.rcWork.left));
            y = y.clamp(mi.rcWork.top, (mi.rcWork.bottom - h).max(mi.rcWork.top));
        }

        let class_w = wide(CLASS);
        let title_w = wide("答题设置");
        let style = WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_VISIBLE;
        let hwnd = CreateWindowExW(
            WS_EX_APPWINDOW,
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
            state_ptr as *mut std::ffi::c_void,
        );
        if hwnd.is_null() {
            let err = GetLastError();
            drop(Box::from_raw(state_ptr));
            crate::trace::trace(&format!("settings: CreateWindowExW 失败 err={}", err));
            return std::ptr::null_mut();
        }
        (*state_ptr).hwnd = hwnd;
        // 内容区自绘；系统标题栏按主题设深色 + 圆角
        crate::dwm::apply_plain(hwnd, theme == Theme::Dark);
        SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner as isize);
        SetTimer(hwnd, TIMER_CARET, 600, std::ptr::null_mut());

        crate::trace::trace(&format!(
            "settings: window shown loaded={} mode={} url={} rect={}x{}",
            (*state_ptr).form.loaded,
            (*state_ptr).form.mode,
            (*state_ptr).form.server_url,
            w,
            h
        ));

        SetForegroundWindow(hwnd);
        SetFocus(hwnd);

        // 没拿到快照：后台拉一次设置，回来了就回填（UI 线程取）
        if snapshot.is_none() {
            fetch_and_fill(shared.clone(), hwnd as isize);
        }

        // 脚本化钩子：延迟执行（等窗口画完 + 后端就绪）
        let hwnd_raw = hwnd as isize;
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(if hook == HOOK_OPEN { 1200 } else { 2600 }));
            if hwnd_raw != 0 {
                unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_HOOK, hook as WPARAM, 0) };
            }
        });

        hwnd
    }
}

/// 后台拉 `GET /api/settings` 并把表单填进去（UI 线程不阻塞）。
fn fetch_and_fill(shared: Arc<Shared>, hwnd_raw: isize) {
    std::thread::spawn(move || {
        for _ in 0..20 {
            if shared.lock().connected {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        backend::refresh_settings(shared.clone());
        if hwnd_raw != 0 {
            unsafe { PostMessageW(hwnd_raw as HWND, WM_APP_HOOK, usize::MAX, 0) };
        }
    });
}

/// 从共享状态组一份 settings JSON（等价于后端 `GET /api/settings` 的 data 段）。
fn settings_from_state(shared: &Shared) -> Option<json::Value> {
    let st = shared.lock();
    if st.answer_workers <= 0 && st.answer_mode_key.is_empty() && st.server_url.is_empty() {
        return None;
    }
    Some(json::obj(&[
        ("server_url", json::s(&st.server_url)),
        (
            "answer",
            json::obj(&[
                ("mode", json::s(&st.answer_mode_key)),
                ("workers", json::json_num(st.answer_workers as f64)),
                ("solver_timeout", json::json_num(st.answer_timeout as f64)),
                ("retry", json::json_num(st.answer_retry as f64)),
            ]),
        ),
        (
            "llm",
            json::obj(&[
                ("base_url", json::s(&st.llm_base)),
                ("model", json::s(&st.llm_model)),
                ("has_api_key", json::Value::Bool(st.llm_has_key)),
            ]),
        ),
        ("run", json::obj(&[("auto_submit", json::Value::Bool(st.auto_submit))])),
    ]))
}

/// 渲染探针：把对话框客户区画进 BMP（不显示窗口、不碰屏幕）。
/// 与主窗口 `--render-probe` 同一思路（`PrintWindow` 取不到自绘内容，见 ERROR.md E33/E10）。
///
/// `dpi` 用来在探针里复现高 DPI 的排版（144 DPI 时传 1155×945 这类**物理**尺寸），
/// 这样"字号有没有跟着 DPI 缩放"肉眼一看就知道（ERROR.md E47）。
pub fn render_probe(out_path: &str, w: i32, h: i32, dpi: u32) {
    let shared = Shared::new();
    {
        let mut st = shared.lock();
        st.server_url = DEFAULT_SERVER_URL.to_string();
    }
    let mut state = Box::new(SettingsState::new(shared, Theme::Dark));
    state.probe_client = Some(RECT { left: 0, top: 0, right: w, bottom: h });
    state.dpi_override = Some(dpi.max(96));
    state.refresh_fonts();
    let data = json::obj(&[
        ("server_url", json::s(DEFAULT_SERVER_URL)),
        (
            "answer",
            json::obj(&[
                ("mode", json::s("server")),
                ("workers", json::json_num(4.0)),
                ("solver_timeout", json::json_num(240.0)),
                ("retry", json::json_num(2.0)),
            ]),
        ),
        (
            "llm",
            json::obj(&[
                ("base_url", json::s("https://api.openai.com/v1")),
                ("model", json::s("gpt-4o")),
                ("has_api_key", json::Value::Bool(true)),
            ]),
        ),
        ("run", json::obj(&[("auto_submit", json::Value::Bool(true))])),
    ]);
    state.form.load_from(&data);
    state.form.focus_on(Field::ServerUrl);
    state.form.status = "示例状态行：测试连接：后端 v2.1.1 · 正常".to_string();

    unsafe {
        let screen = GetDC(std::ptr::null_mut());
        if screen.is_null() {
            crate::trace::trace("settings-probe: GetDC 失败");
            return;
        }
        let mem = CreateCompatibleDC(screen);
        let bmp = CreateCompatibleBitmap(screen, w, h);
        if mem.is_null() || bmp.is_null() {
            crate::trace::trace("settings-probe: 创建内存 DC/位图失败");
            ReleaseDC(std::ptr::null_mut(), screen);
            return;
        }
        let old = SelectObject(mem, bmp as HGDIOBJ);
        state.paint(mem, state.client());
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
            crate::trace::trace(&format!("settings-probe: 已渲染 {}x{} -> {}", w, h, out_path));
        } else {
            crate::trace::trace("settings-probe: GetDIBits 失败");
        }
        SelectObject(mem, old);
        DeleteObject(bmp as HGDIOBJ);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);
    }
}
