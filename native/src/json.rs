//! 极小 JSON 解析/生成（够用就行，避免为一个解析器引依赖）。
//!
//! 后端约定所有 JSON 都是纯 ASCII（中文走 `\uXXXX`，见 backend/learn_helper/ipc.py），
//! 所以解析器必须支持转义序列，尤其是 `\uXXXX` —— 否则界面上的中文会变成乱码。

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(BTreeMap<String, Value>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(map) => map.get(key),
            _ => None,
        }
    }

    pub fn str_at(&self, key: &str) -> String {
        match self.get(key) {
            Some(Value::Str(s)) => s.clone(),
            Some(Value::Num(n)) => fmt_num(*n),
            Some(Value::Bool(b)) => b.to_string(),
            _ => String::new(),
        }
    }

    pub fn num_at(&self, key: &str) -> f64 {
        match self.get(key) {
            Some(Value::Num(n)) => *n,
            Some(Value::Str(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn int_at(&self, key: &str) -> i64 {
        self.num_at(key) as i64
    }

    pub fn bool_at(&self, key: &str) -> bool {
        match self.get(key) {
            Some(Value::Bool(b)) => *b,
            Some(Value::Num(n)) => *n != 0.0,
            Some(Value::Str(s)) => s == "true",
            _ => false,
        }
    }

    pub fn str_list_at(&self, key: &str) -> Vec<String> {
        match self.get(key) {
            Some(Value::Arr(items)) => items
                .iter()
                .filter_map(|v| match v {
                    Value::Str(s) => Some(s.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        }
    }
}

/// 数字格式化：整数不带小数点，避免 "3" 显示成 "3.0"。
pub fn fmt_num(n: f64) -> String {
    if (n.fract()).abs() < f64::EPSILON && n.abs() < 1e15 {
        format!("{}", n as i64)
    } else {
        let s = format!("{:.2}", n);
        s.trim_end_matches('0').trim_end_matches('.').to_string()
    }
}

/// 按**字符**（不是字节）安全截断，尾部加省略号。
///
/// ⚠️ 别用 `&s[..s.len().min(n)]`：那是**字节**下标，而本项目后端日志全是中文
/// （一个汉字 3 字节）⇒ 一旦 n 落在一个字符中间就直接 panic，而 release profile
/// 是 `panic = "abort"`，等于**整个界面进程被杀**（实测踩到，见 ERROR.md E62）。
pub fn truncate_chars(text: &str, max_chars: usize) -> String {
    if text.chars().count() <= max_chars {
        return text.to_string();
    }
    let mut out: String = text.chars().take(max_chars).collect();
    out.push('…');
    out
}

/// 解析一个 JSON 文档（顶层值）。失败返回 `None`，**任何输入都不 panic**。
pub fn parse(input: &str) -> Option<Value> {
    let bytes: Vec<char> = input.chars().collect();
    let mut pos = 0usize;
    // 后端可能在 JSON 前打印过别的行；这里只接受"从第一个 { 开始"的内容。
    while pos < bytes.len() && bytes[pos] != '{' && bytes[pos] != '[' {
        pos += 1;
    }
    if pos >= bytes.len() {
        return None;
    }
    let (value, _) = parse_value(&bytes, pos)?;
    Some(value)
}

fn skip_ws(s: &[char], mut i: usize) -> usize {
    while i < s.len() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\r' || s[i] == '\t') {
        i += 1;
    }
    i
}

fn parse_value(s: &[char], i: usize) -> Option<(Value, usize)> {
    let i = skip_ws(s, i);
    if i >= s.len() {
        return None;
    }
    match s[i] {
        '{' => parse_object(s, i),
        '[' => parse_array(s, i),
        '"' => {
            let (text, next) = parse_string(s, i)?;
            Some((Value::Str(text), next))
        }
        't' => {
            if s.len() >= i + 4 && s[i..i + 4].iter().collect::<String>() == "true" {
                Some((Value::Bool(true), i + 4))
            } else {
                None
            }
        }
        'f' => {
            if s.len() >= i + 5 && s[i..i + 5].iter().collect::<String>() == "false" {
                Some((Value::Bool(false), i + 5))
            } else {
                None
            }
        }
        'n' => {
            if s.len() >= i + 4 && s[i..i + 4].iter().collect::<String>() == "null" {
                Some((Value::Null, i + 4))
            } else {
                None
            }
        }
        _ => parse_number(s, i),
    }
}

fn parse_object(s: &[char], i: usize) -> Option<(Value, usize)> {
    let mut map = BTreeMap::new();
    let mut i = skip_ws(s, i + 1);
    if i < s.len() && s[i] == '}' {
        return Some((Value::Obj(map), i + 1));
    }
    loop {
        i = skip_ws(s, i);
        if i >= s.len() || s[i] != '"' {
            return None;
        }
        let (key, next) = parse_string(s, i)?;
        i = skip_ws(s, next);
        if i >= s.len() || s[i] != ':' {
            return None;
        }
        let (value, next) = parse_value(s, i + 1)?;
        map.insert(key, value);
        i = skip_ws(s, next);
        if i >= s.len() {
            return None;
        }
        match s[i] {
            ',' => i += 1,
            '}' => return Some((Value::Obj(map), i + 1)),
            _ => return None,
        }
    }
}

fn parse_array(s: &[char], i: usize) -> Option<(Value, usize)> {
    let mut items = Vec::new();
    let mut i = skip_ws(s, i + 1);
    if i < s.len() && s[i] == ']' {
        return Some((Value::Arr(items), i + 1));
    }
    loop {
        let (value, next) = parse_value(s, i)?;
        items.push(value);
        i = skip_ws(s, next);
        if i >= s.len() {
            return None;
        }
        match s[i] {
            ',' => i += 1,
            ']' => return Some((Value::Arr(items), i + 1)),
            _ => return None,
        }
    }
}

fn parse_string(s: &[char], i: usize) -> Option<(String, usize)> {
    // s[i] == '"'
    let mut out = String::new();
    let mut idx = i + 1;
    // ⚠️ **下标记账铁律**：本循环里"下一个待读字符"只由**每个分支自己**赋值一次，
    // 不再有"分支内 += N 之后外面又 += 1"的两段式记账。
    // 原来正是那种写法出的错：代理对分支 `idx += 11` 之后，外层那句共用的
    // `idx += 1` 又加了一次 ⇒ idx 冲过闭合引号，循环以 `idx >= len` 结束、
    // 连 `return Some(...)` 都到不了，`parse()` 返回 None，**整条消息被丢掉**。
    // 症状就是"带 emoji 的后端日志在界面上凭空消失"（见 ERROR.md E63）：
    // 十六进制取值本身没错，中文等 BMP 字符一直正常，只有需要拼代理对的
    // emoji / 生僻字才暴露，所以此前一直没人发现。
    while idx < s.len() {
        let ch = s[idx];
        if ch == '"' {
            return Some((out, idx + 1));
        }
        if ch != '\\' {
            out.push(ch);
            idx += 1;
            continue;
        }
        // 转义序列：`idx` 指向反斜杠。
        let esc = idx + 1;
        if esc >= s.len() {
            return None;                       // 结尾一个孤立的反斜杠
        }
        match s[esc] {
            '"' => { out.push('"');  idx = esc + 1; }
            '\\' => { out.push('\\'); idx = esc + 1; }
            '/' => { out.push('/');  idx = esc + 1; }
            'b' => { out.push('\u{8}'); idx = esc + 1; }
            'f' => { out.push('\u{c}'); idx = esc + 1; }
            'n' => { out.push('\n'); idx = esc + 1; }
            'r' => { out.push('\r'); idx = esc + 1; }
            't' => { out.push('\t'); idx = esc + 1; }
            'u' => {
                // `\uXXXX` 的十六进制在 `s[esc+1 .. esc+5]`（半开区间）。
                if esc + 5 > s.len() {
                    return None;               // 不足 4 位十六进制
                }
                let hex: String = s[esc + 1..esc + 5].iter().collect();
                let code = match u32::from_str_radix(&hex, 16) {
                    Ok(v) => v,
                    Err(_) => {
                        // 不是合法十六进制：整个文档判为非法（与原来一致）。
                        return None;
                    }
                };
                // 代理对：`\uD83D\uDE00` 需要再读一个 `\uXXXX` 拼成一个字符。
                // 低位十六进制在 `s[esc+6 .. esc+10]`，需要 `esc + 10 <= s.len()`。
                let is_high = (0xD800..0xDC00).contains(&code);
                if is_high
                    && esc + 10 <= s.len()
                    && s[esc + 5] == '\\'
                    && s[esc + 6] == 'u'
                {
                    let hex2: String = s[esc + 7..esc + 11].iter().collect();
                    let low = u32::from_str_radix(&hex2, 16).unwrap_or(0);
                    if (0xDC00..0xE000).contains(&low) {
                        let combined = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                        if let Some(chr) = char::from_u32(combined) {
                            out.push(chr);
                            idx = esc + 11;    // 两个转义合计 12 个字符
                            continue;
                        }
                    }
                }
                // 不是合法代理对（含单独的高/低位）：能当字符用就用，否则用替换符。
                out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                idx = esc + 5;
            }
            // 未知转义：原样保留被转义的字符（宽松处理，不判整份文档非法）。
            other => {
                out.push(other);
                idx = esc + 1;
            }
        }
    }
    None
}
fn parse_number(s: &[char], i: usize) -> Option<(Value, usize)> {
    let start = i;
    let mut idx = i;
    if idx < s.len() && (s[idx] == '-' || s[idx] == '+') {
        idx += 1;
    }
    while idx < s.len() && (s[idx].is_ascii_digit() || s[idx] == '.' || s[idx] == 'e' || s[idx] == 'E' || s[idx] == '-' || s[idx] == '+') {
        idx += 1;
    }
    if idx == start {
        return None;
    }
    let text: String = s[start..idx].iter().collect();
    text.parse::<f64>().ok().map(|n| (Value::Num(n), idx))
}

// ------------------------------------------------------------------ 生成
fn escape_into(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            // 非 ASCII 一律转义：与后端"纯 ASCII JSON"的约定保持一致
            c if (c as u32) > 0x7F => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn stringify(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Num(n) => out.push_str(&fmt_num(*n)),
        Value::Str(s) => escape_into(out, s),
        Value::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Obj(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape_into(out, k);
                out.push(':');
                write_value(out, v);
            }
            out.push('}');
        }
    }
}

pub fn obj(pairs: &[(&str, Value)]) -> Value {
    let mut map = BTreeMap::new();
    for (k, v) in pairs {
        map.insert((*k).to_string(), v.clone());
    }
    Value::Obj(map)
}

pub fn s(text: &str) -> Value {
    Value::Str(text.to_string())
}

pub fn json_num(n: f64) -> Value {
    Value::Num(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 手写解析器面对**畸形输入**绝不能 panic。
    ///
    /// ⚠️ 这条测试的价值在于 release profile 是 `panic = "abort"`：解析器跑在
    /// "每条命名管道消息 + 每次 HTTP 响应"的路径上，任何一处下标越界都会
    /// **直接杀掉整个界面进程**（不是抛个异常那么温和）。
    #[test]
    fn malformed_input_never_panics() {
        let cases = [
            "",
            "{",
            "}",
            "[",
            "\"",
            "{\"a\":}",
            "{\"a\"}",
            "{,}",
            "[,]",
            "{\"a\":1,}",
            "\\",
            "\"\\",
            "\"\\u",
            "\"\\uD83D\\",
            "\"\\uD83D",
            "\"\\uD83D\\uDE00",       // 合法的 emoji 代理对，但缺闭合引号
            "\"\\uD83D\\uDE00\"",     // 完整合法
            "\"\\uD800\\uD800\"",     // 高位 + 高位（非法组合）
            "\"\\uDC00\\uD800\"",     // 低位在前（非法）
            "\"\\uD83D\\n\"",
            "\"\\uD83D\\u\"",
            "\"\\uD83D\\uZZZZ\"",
            "\"\\u\"",
            "\"\\u0\"",
            "\"\\uD83\"",
            "{\"a\":1",
            "{\"a\":}",
            "{\"\\uD83D\":1}",
            "{\"a\":\"\\uD83D\\\"}",
            "[-",
            "[1,",
            "[1,",
            "nul",
            "tru",
            "fals",
            "{}{}",
            "[][]",
            "\u{4e2d}\u{6587}",       // 顶层裸中文
            "{\"\u{4e2d}\u{6587}\":\"\u{503c}\"}",
        ];
        for case in cases {
            // 只要不 panic 就算过；返回 None 或某个值都可以接受。
            let _ = parse(case);
        }
    }

    #[test]
    fn string_level_surrogate_pair() {
        // 保留一个直接把 parse_string 钉住的用例：`parse()` 是"给握手行用的导航器"
        // （只认以 { / [ 开头的文档），所以字符串级的行为要在这里测。
        let chars: Vec<char> = "\"\\uD83D\\uDE00\"".chars().collect();
        assert_eq!(parse_string(&chars, 0), Some(("😀".to_string(), 14)));
    }

    #[test]
    fn surrogate_pair_decodes() {
        // 后端日志里的 emoji 会被转义成代理对，必须能还原回来
        // （否则带 emoji 的那条日志会被整条丢掉）。
        assert_eq!(
            parse("{\"text\":\"\\uD83D\\uDE00\"}"),
            Some(obj(&[("text", Value::Str("😀".to_string()))]))
        );
        // 前后还有别的字符
        assert_eq!(
            parse("{\"text\":\"a\\uD83D\\uDE00b\"}"),
            Some(obj(&[("text", Value::Str("a😀b".to_string()))]))
        );
        // BMP 字符（中文，不需要代理对）一直是好的，别被改坏
        assert_eq!(
            parse("{\"text\":\"\\u4e2d\\u6587\"}"),
            Some(obj(&[("text", Value::Str("中文".to_string()))]))
        );
        // 高位后面不是合法低位：不应 panic，退化为单个码位
        let lone = parse_string(&"\"\\uD83Dx\"".chars().collect::<Vec<char>>(), 0);
        assert!(lone.is_some(), "lone high surrogate should still parse");
    }

    /// 按字符截断：中文不能被切在字符中间（切了就是 panic）。
    #[test]
    fn truncate_chars_is_utf8_safe() {
        let zh = "这是一个很长的中文标题用于测试截断行为";
        for n in 0..=zh.chars().count() + 2 {
            let out = truncate_chars(zh, n);
            assert!(out.chars().count() <= n + 1, "n={n} out={out:?}");
        }
        assert_eq!(truncate_chars("abc", 10), "abc");
        assert_eq!(truncate_chars(zh, zh.chars().count()), zh);
        // 恰好卡在 3 字节字符边界上的长度
        assert!(truncate_chars(zh, 1).starts_with('这'));
    }

    #[test]
    fn parses_typical_backend_payload() {
        let doc = "{\"type\":\"status\",\"running\":false,\"uptime\":1.5,\"pages\":[\"a\",\"b\"]}";
        let v = parse(doc).expect("should parse");
        match v {
            Value::Obj(_) => {}
            other => panic!("expected object, got {other:?}"),
        }
    }
}
