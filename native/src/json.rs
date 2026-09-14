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
    while idx < s.len() {
        let ch = s[idx];
        match ch {
            '"' => return Some((out, idx + 1)),
            '\\' => {
                idx += 1;
                if idx >= s.len() {
                    return None;
                }
                match s[idx] {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'b' => out.push('\u{8}'),
                    'f' => out.push('\u{c}'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'u' => {
                        if idx + 4 >= s.len() {
                            return None;
                        }
                        let hex: String = s[idx + 1..idx + 5].iter().collect();
                        let code = u32::from_str_radix(&hex, 16).ok()?;
                        idx += 4;
                        // 处理代理对（emoji / 生僻字的 \uD83D\uDE00 形式）
                        if (0xD800..0xDC00).contains(&code)
                            && idx + 6 < s.len()
                            && s[idx + 1] == '\\'
                            && s[idx + 2] == 'u'
                        {
                            let hex2: String = s[idx + 3..idx + 7].iter().collect();
                            if let Ok(low) = u32::from_str_radix(&hex2, 16) {
                                if (0xDC00..0xE000).contains(&low) {
                                    let combined =
                                        0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                                    if let Some(c) = char::from_u32(combined) {
                                        out.push(c);
                                    }
                                    idx += 6;
                                }
                            }
                        } else if let Some(c) = char::from_u32(code) {
                            out.push(c);
                        } else {
                            out.push('\u{FFFD}');
                        }
                    }
                    other => out.push(other),
                }
                idx += 1;
            }
            _ => {
                out.push(ch);
                idx += 1;
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
