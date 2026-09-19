//! 极简 WinHTTP 客户端（系统自带，连 TLS 都由 OS 提供，不引入 reqwest/openssl）。

#![allow(non_snake_case, non_camel_case_types)]

use std::ffi::c_void;
use std::ptr;

type HINTERNET = *mut c_void;

pub const WINHTTP_ACCESS_TYPE_DEFAULT_PROXY: u32 = 0;
pub const WINHTTP_NO_PROXY_NAME: *const u16 = ptr::null();
pub const WINHTTP_NO_PROXY_BYPASS: *const u16 = ptr::null();
pub const WINHTTP_FLAG_SECURE: u32 = 0x0080_0000;
pub const WINHTTP_IGNORE_REQ_TO_SERVER_NAME: u32 = 0x0000_0080;
pub const WINHTTP_IGNORE_CERT_CN_INVALID: u32 = 0x0000_1000;
pub const WINHTTP_IGNORE_CERT_DATE_INVALID: u32 = 0x0000_2000;
pub const WINHTTP_IGNORE_CERT_WRONG_USAGE: u32 = 0x0000_4000;

#[link(name = "winhttp")]
extern "system" {
    fn WinHttpOpen(
        user_agent: *const u16,
        access_type: u32,
        proxy_name: *const u16,
        proxy_bypass: *const u16,
        flags: u32,
    ) -> HINTERNET;
    fn WinHttpConnect(
        session: HINTERNET,
        server: *const u16,
        port: u16,
        reserved: u32,
    ) -> HINTERNET;
    fn WinHttpOpenRequest(
        connect: HINTERNET,
        verb: *const u16,
        object: *const u16,
        version: *const u16,
        referrer: *const u16,
        accept_types: *const *const u16,
        flags: u32,
    ) -> HINTERNET;
    fn WinHttpSendRequest(
        request: HINTERNET,
        headers: *const u16,
        headers_len: i32,
        optional: *mut c_void,
        optional_len: u32,
        total_len: u32,
        context: usize,
    ) -> i32;
    fn WinHttpReceiveResponse(request: HINTERNET, reserved: *mut c_void) -> i32;
    fn WinHttpQueryHeaders(
        request: HINTERNET,
        info_level: u32,
        name: *const u16,
        buffer: *mut c_void,
        length: *mut u32,
        index: *mut u32,
    ) -> i32;
    fn WinHttpQueryDataAvailable(request: HINTERNET, available: *mut u32) -> i32;
    fn WinHttpReadData(
        request: HINTERNET,
        buffer: *mut c_void,
        bytes_to_read: u32,
        bytes_read: *mut u32,
    ) -> i32;
    fn WinHttpCloseHandle(handle: HINTERNET) -> i32;
    fn WinHttpSetTimeouts(
        handle: HINTERNET,
        resolve: i32,
        connect: i32,
        send: i32,
        receive: i32,
    ) -> i32;
    fn WinHttpSetOption(
        handle: HINTERNET,
        option: u32,
        buffer: *mut c_void,
        length: u32,
    ) -> i32;
}

const WINHTTP_QUERY_STATUS_CODE: u32 = 19;
/// ⚠️ 必须带这个标志，否则 WinHttpQueryHeaders 把状态码按**字符串**返回
/// （"200" 的 UTF-16），写进 u32 会得到 0 —— 实测踩过：健康检查永远不通过。
const WINHTTP_QUERY_FLAG_NUMBER: u32 = 0x2000_0000;
const WINHTTP_OPTION_SECURITY_FLAGS: u32 = 31;

pub struct Response {
    pub status: u32,
    pub body: Vec<u8>,
}

impl Response {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

/// 对 ``http://host:port/path`` 或 ``https://host[:port]/path`` 发一次请求。
/// 超时统一 30s（WinHTTP 毫秒）。失败返回 Err(描述)。
pub fn request(
    method: &str,
    url: &str,
    json_body: Option<&str>,
    headers: &[(&str, &str)],
    timeout_ms: u32,
) -> Result<Response, String> {
    unsafe {
        let (scheme, rest) = url.split_once("://").ok_or("URL 缺协议")?;
        let secure = scheme.eq_ignore_ascii_case("https");
        let (host_port, path_q) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let (host, port) = match host_port.rfind(':') {
            Some(i) if host_port[i + 1..].chars().all(|c| c.is_ascii_digit()) => {
                let p: u16 = host_port[i + 1..].parse().unwrap_or(if secure { 443 } else { 80 });
                (&host_port[..i], p)
            }
            _ => (host_port, if secure { 443 } else { 80 }),
        };

        let agent_w = wide_str("learn-helper/1.0.4");
        let host_w = wide_str(host);
        let session = WinHttpOpen(
            agent_w.as_ptr(),
            WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
            WINHTTP_NO_PROXY_NAME,
            WINHTTP_NO_PROXY_BYPASS,
            0,
        );
        if session.is_null() {
            return Err("WinHttpOpen 失败".into());
        }
        let _s = Cleanup(session);

        // 连接超时 5s；收发 30s（答题可能很久，调用方自己设）
        WinHttpSetTimeouts(session, 5000, 5000, timeout_ms as i32, timeout_ms as i32);

        let connect = WinHttpConnect(session, host_w.as_ptr(), port, 0);
        if connect.is_null() {
            return Err("WinHttpConnect 失败".into());
        }
        let _c = Cleanup(connect);

        let verb = wide_str(method);
        let object = wide_str(path_q);
        let request_handle = WinHttpOpenRequest(
            connect,
            verb.as_ptr(),
            object.as_ptr(),
            ptr::null(),
            ptr::null(),
            ptr::null(),
            if secure { WINHTTP_FLAG_SECURE } else { 0 },
        );
        if request_handle.is_null() {
            return Err("WinHttpOpenRequest 失败".into());
        }
        let _r = Cleanup(request_handle);

        if secure {
            // 本地/自配端点可能证书不全，忽略证书校验（本工具是用户自己连自己的服务）
            let flags: u32 = WINHTTP_IGNORE_REQ_TO_SERVER_NAME
                | WINHTTP_IGNORE_CERT_CN_INVALID
                | WINHTTP_IGNORE_CERT_DATE_INVALID
                | WINHTTP_IGNORE_CERT_WRONG_USAGE;
            WinHttpSetOption(
                request_handle,
                WINHTTP_OPTION_SECURITY_FLAGS,
                &flags as *const u32 as *mut c_void,
                4,
            );
        }

        // 组装请求头（含 Content-Type / Content-Length）
        let mut header_text = String::from("Content-Type: application/json\r\n");
        for (k, v) in headers {
            header_text.push_str(k);
            header_text.push_str(": ");
            header_text.push_str(v);
            header_text.push_str("\r\n");
        }
        // ⚠️ `Content-Length` 必须是**字节数**（`body.len()` 是字符数）。
        // 当前调用方都用 `json::stringify`（非 ASCII 已转义成 \uXXXX）所以数值恰好相等，
        // 但一旦哪天有人传了带中文的 body，这里就会告诉服务器一个偏小的长度 ⇒
        // 请求体被截断、后端报 JSON 解析失败（而且很难看出是发的人算错了）。
        if let Some(body) = json_body {
            header_text.push_str(&format!("Content-Length: {}\r\n", body.as_bytes().len()));
        }

        let body_bytes: Vec<u8> = match json_body {
            Some(b) => b.as_bytes().to_vec(),
            None => Vec::new(),
        };
        let header_w = wide_str(&header_text);
        let sent = WinHttpSendRequest(
            request_handle,
            header_w.as_ptr(),
            if header_w.is_empty() { 0 } else { -1 },
            if body_bytes.is_empty() {
                ptr::null_mut()
            } else {
                body_bytes.as_ptr() as *mut c_void
            },
            body_bytes.len() as u32,
            body_bytes.len() as u32,
            0,
        );
        if sent == 0 {
            return Err(format!("WinHttpSendRequest 失败 ({})", last_err()));
        }
        if WinHttpReceiveResponse(request_handle, ptr::null_mut()) == 0 {
            return Err(format!("WinHttpReceiveResponse 失败 ({})", last_err()));
        }

        // 状态码（必须带 FLAG_NUMBER，见常量处注释）
        let mut status: u32 = 0;
        let mut status_len: u32 = 4;
        WinHttpQueryHeaders(
            request_handle,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            ptr::null(),
            &mut status as *mut u32 as *mut c_void,
            &mut status_len,
            ptr::null_mut(),
        );

        // 读 body
        let mut body: Vec<u8> = Vec::new();
        loop {
            let mut avail: u32 = 0;
            if WinHttpQueryDataAvailable(request_handle, &mut avail) == 0 || avail == 0 {
                break;
            }
            let mut buf = vec![0u8; avail as usize];
            let mut read: u32 = 0;
            if WinHttpReadData(request_handle, buf.as_mut_ptr() as *mut c_void, avail, &mut read) == 0
                || read == 0
            {
                break;
            }
            body.extend_from_slice(&buf[..read as usize]);
        }

        Ok(Response { status, body })
    }
}

struct Cleanup(HINTERNET);
impl Drop for Cleanup {
    fn drop(&mut self) {
        unsafe {
            WinHttpCloseHandle(self.0);
        }
    }
}

fn wide_str(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

#[link(name = "kernel32")]
extern "system" {
    fn GetLastError() -> u32;
}

fn last_err() -> u32 {
    unsafe { GetLastError() }
}
