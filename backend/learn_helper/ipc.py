# -*- coding: utf-8 -*-
"""前后端通信层（1.0.4 用户约定的混合方案）。

- **HTTP**（``127.0.0.1:<随机端口>``）：离散请求-响应 —— 状态、网页列表、启动/暂停/停止、
  诊断、设置、日志补拉、退出。
- **命名管道**（``\\\\.\\pipe\\learn-helper-<pid>``，**C# 侧发起连接、Python 侧监听**）：
  日志 / 进度 / 状态**推送**（单向，JSON Lines）。

设计要点：

1. Hub 是唯一的状态持有者与推送总线；引擎只调用 ``emit_*``，不认识 HTTP 与管道。
2. 管道写失败/无人连接**绝不影响主流程**：推送是"尽力而为"，丢了有 HTTP 补拉（``/api/logs``）。
3. 所有 JSON 一律 ``ensure_ascii=True`` 编码为 **纯 ASCII 字节**，两边都不会踩编码坑
   （历史教训：PS/GBK 与 BOM，见 rules/01 §8）。
"""

import json
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import APP_VERSION, DEVICE_ID, LOGGER

PROTOCOL_VERSION = 1
PIPE_PREFIX = 'learn-helper-'

# Windows 命名管道常量的字面量（避免依赖 pywin32）
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_INSTANCE_CONNECTED = 536
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_INVALID_HANDLE_VALUE = -1


def dumps_ascii(obj):
    """纯 ASCII 的 JSON 文本（中文走 \\uXXXX 转义，杜绝跨进程编码问题）。"""
    return json.dumps(obj, ensure_ascii=True, separators=(',', ':'), default=str)


def find_free_port():
    """让 OS 挑一个空闲端口（绑定 0 后立即取回）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def _remembered_page_title():
    """读 config.json 里记住的网页标题（缺失/异常一律返回空串）。"""
    try:
        from .config import load_config
        return str(load_config().get('last_page_title') or '').strip()
    except Exception:
        return ''


# ============================================================================
# Hub：状态 + 事件推送总线
# ============================================================================
class Hub:
    """状态与事件的唯一持有者。

    事件类型（前端按 ``type`` 分派）：
    - ``hello``    连接即发：版本 / 设备指纹 / 协议版本
    - ``log``      日志行（含 seq，可用 /api/logs?since= 补拉）
    - ``pages``    标签页列表
    - ``progress`` 任务/视频/答题三段进度
    - ``status``   引擎状态变化（运行/暂停/结束）
    """

    MAX_LOG_BUFFER = 500

    def __init__(self, config_path=None):
        self.lock = threading.RLock()
        self.pages = []
        self.pages_at = 0.0
        # 启动时恢复"上次选中的网页"（config.json: last_page_title）——
        # `select_page` 会写它，界面重启后不必重选（2026-09-16）。
        self.selected_title = _remembered_page_title()
        self._last_saved_url = None
        self.engine = None
        self.pipe = None
        self.started_at = time.time()
        self._log_buf = []
        self._log_seq = 0

    def attach(self, engine, pipe=None):
        self.engine = engine
        if pipe is not None:
            self.pipe = pipe

    # ---------------- 推送原语 ----------------
    def broadcast(self, event):
        with self.lock:
            pipe = self.pipe
        if pipe is not None:
            pipe.broadcast(event)

    def emit_log(self, text):
        text = '' if text is None else str(text)
        for line in text.splitlines() or ['']:
            with self.lock:
                self._log_seq += 1
                seq = self._log_seq
                self._log_buf.append({'seq': seq, 'text': line})
                if len(self._log_buf) > self.MAX_LOG_BUFFER:
                    del self._log_buf[:-self.MAX_LOG_BUFFER]
            try:
                LOGGER.info(line)
            except Exception:
                pass
            n = self.pipe.client_count() if self.pipe else 0
            self.broadcast({'type': 'log', 'seq': seq, 'text': line})
            LOGGER.debug(f'[IPC] log seq={seq} -> {n} 个管道客户端')

    def emit_progress(self, task=None, video=None, quiz=None):
        if task is not None:
            self.engine.task_text = task
        if video is not None:
            self.engine.video_text = video
        if quiz is not None:
            self.engine.quiz_text = quiz
        self.broadcast({'type': 'progress', 'task': self.engine.task_text,
                        'video': self.engine.video_text, 'quiz': self.engine.quiz_text})

    def emit_status(self):
        snapshot = self.snapshot()
        self.broadcast(snapshot)
        return snapshot

    def emit_pages(self, pages):
        with self.lock:
            self.pages = list(pages or [])
            self.pages_at = time.time()
        self.broadcast({'type': 'pages', 'pages': list(self.pages), 'at': self.pages_at})

    # ---------------- 快照 ----------------
    def snapshot(self):
        eng = self.engine
        with self.lock:
            pages = list(self.pages)
            pages_at = self.pages_at
            selected = self.selected_title
        data = {
            'type': 'status',
            'version': APP_VERSION,
            'protocol': PROTOCOL_VERSION,
            'device_id': DEVICE_ID,
            'pipe': self.pipe.name if self.pipe else '',
            'selected_page': selected,
            'pages': pages,
            'pages_at': pages_at,
            'uptime': round(time.time() - self.started_at, 1),
        }
        if eng is not None:
            data['engine'] = eng.status()
            if not data['selected_page']:
                data['selected_page'] = eng.status().get('selected_page', '')
        return data

    def logs_since(self, since=0):
        with self.lock:
            return [e for e in self._log_buf if e['seq'] > int(since or 0)]

    def recent_logs(self, limit=100):
        """最近 N 条日志（新客户端接入时补发，避免"连上之前的日志凭空消失"）。"""
        with self.lock:
            buf = list(self._log_buf)
        return buf[-int(limit):] if limit else []


# ============================================================================
# 命名管道服务端（C# 侧发起连接；Python 侧监听）
# ============================================================================
class PipeServer:
    """Windows 命名管道的服务端。

    每条连接一个读线程 + 一个写队列；读线程只用来发现断开（前端只收不发）。
    写失败即视为该连接已断开并剔除；**推送失败永不抛回主流程**。
    """

    def __init__(self, hub, name=None):
        self.hub = hub
        self.name = name or f'{PIPE_PREFIX}{os.getpid()}'
        self.stop_event = threading.Event()
        self._accept_thread = None
        self._clients = {}          # conn_id -> (handle, write_lock, queue)
        self._next_id = 0
        self._lock = threading.RLock()
        self.available = False
        self.last_error = ''

    # ---------------- 生命周期 ----------------
    def start(self):
        try:
            import ctypes  # noqa: F401  仅 Windows 可用
            ctypes.windll.kernel32
        except Exception as e:  # pragma: no cover - 非 Windows
            self.last_error = f'命名管道仅支持 Windows: {e}'
            return False
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                               name='pipe-accept')
        self._accept_thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        with self._lock:
            clients = list(self._clients.items())
            self._clients.clear()
        for _cid, (handle, _wlock, _q) in clients:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle):
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass

    # ---------------- 连接接受 ----------------
    def _create_pipe(self):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
        kernel32.CreateNamedPipeW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        handle = kernel32.CreateNamedPipeW(
            rf'\\.\pipe\{self.name}',
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            65536,   # out buffer
            65536,   # in buffer
            0, None)
        if handle in (None, _INVALID_HANDLE_VALUE, 0xFFFFFFFFFFFFFFFF):
            self.last_error = f'CreateNamedPipe 失败: err={ctypes.get_last_error()}'
            return None
        return handle

    def _accept_loop(self):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.ConnectNamedPipe.restype = ctypes.c_int
        kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.GetLastError.restype = ctypes.c_uint32
        while not self.stop_event.is_set():
            handle = self._create_pipe()
            if handle is None:
                LOGGER.warning(f'[管道] {self.last_error}')
                if self.stop_event.wait(1.0):
                    break
                continue
            connected = kernel32.ConnectNamedPipe(ctypes.c_void_p(handle), None)
            err = ctypes.get_last_error()
            if not connected and err not in (_ERROR_PIPE_CONNECTED, _ERROR_PIPE_INSTANCE_CONNECTED):
                self._close_handle(handle)
                if self.stop_event.wait(0.3):
                    break
                continue
            if self.stop_event.is_set():
                self._close_handle(handle)
                break
            self.available = True
            self._spawn_client(handle)

    def _spawn_client(self, handle):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._next_id += 1
            cid = self._next_id
            self._clients[cid] = (handle, threading.Lock(), q)
        hub = self.hub
        try:
            self._write_raw(handle, dumps_ascii({
                'type': 'hello', 'version': APP_VERSION,
                'protocol': PROTOCOL_VERSION, 'device_id': DEVICE_ID,
                'pipe': self.name,
            }) + '\n')
        except Exception as e:
            LOGGER.info(f'[管道] hello 发送失败: {e}')
            self._drop_client(cid)
            return
        # 补发历史日志：管道是**流式**的，客户端只收到"连上之后"的事件，
        # 否则重连一次就会丢掉之前的所有日志（实测踩到）。
        try:
            for entry in hub.recent_logs():
                self._write_raw(handle, dumps_ascii({
                    'type': 'log', 'seq': entry['seq'], 'text': entry['text'],
                    'replay': True}) + '\n')
        except Exception as e:
            LOGGER.info(f'[管道] 历史日志补发失败: {e}')
        try:
            latest = hub.snapshot()
            self._write_raw(handle, dumps_ascii(latest) + '\n')
        except Exception:
            pass
        threading.Thread(target=self._reader, args=(cid, handle), daemon=True,
                         name=f'pipe-read-{cid}').start()
        threading.Thread(target=self._writer, args=(cid, q), daemon=True,
                         name=f'pipe-write-{cid}').start()

    def _reader(self, cid, handle):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.ReadFile.restype = ctypes.c_int
        kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        buf = ctypes.create_string_buffer(4096)
        read = ctypes.c_uint32(0)
        while not self.stop_event.is_set():
            ok = kernel32.ReadFile(ctypes.c_void_p(handle), buf, 4096,
                                   ctypes.byref(read), None)
            if not ok:
                break
        self._drop_client(cid)

    def _writer(self, cid, q):
        while not self.stop_event.is_set():
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                break
            with self._lock:
                entry = self._clients.get(cid)
            if entry is None:
                break
            handle, wlock, _q = entry
            try:
                with wlock:
                    self._write_raw(handle, chunk)
            except Exception:
                self._drop_client(cid)
                break

    @staticmethod
    def _write_raw(handle, text):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.WriteFile.restype = ctypes.c_int
        kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        data = text.encode('ascii', 'replace')
        written = ctypes.c_uint32(0)
        ok = kernel32.WriteFile(ctypes.c_void_p(handle), data, len(data),
                                ctypes.byref(written), None)
        if not ok:
            raise OSError('WriteFile 失败')

    def _drop_client(self, cid):
        with self._lock:
            entry = self._clients.pop(cid, None)
        if entry is None:
            return
        handle, _wlock, q = entry
        try:
            q.put_nowait(None)
        except Exception:
            pass
        self._close_handle(handle)

    def client_count(self):
        with self._lock:
            return len(self._clients)

    # ---------------- 广播 ----------------
    def broadcast(self, event):
        chunk = dumps_ascii(event) + '\n'
        with self._lock:
            items = list(self._clients.items())
        for cid, (_handle, _wlock, q) in items:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                # 推送积压说明前端没在消费：丢弃最旧的一条，保新
                try:
                    q.get_nowait()
                    q.put_nowait(chunk)
                except Exception:
                    pass
        if event.get('type') == 'log':
            LOGGER.debug(f'[IPC] broadcast log -> {len(items)} client(s)')


# ============================================================================
# HTTP 服务（请求-响应）
# ============================================================================
class ApiServer:
    """``/api/*`` 的 HTTP 服务；线程模型 = ThreadingHTTPServer（每连接一条线程）。"""

    def __init__(self, hub, port, on_shutdown=None):
        self.hub = hub
        self.on_shutdown = on_shutdown
        self.port = int(port)
        self._httpd = None
        self._thread = None
        self._stop_event = threading.Event()

    # ---------------- 生命周期 ----------------
    def bind(self):
        """只创建并绑定 socket（先拿端口，再启动服务，避免握手竞态）。"""
        handler = _make_handler(self.hub, self.on_shutdown)
        self._httpd = ThreadingHTTPServer(('127.0.0.1', self.port), handler)
        self._httpd.daemon_threads = True
        # handle_request() 的等待上限：没有它就会无限阻塞，退出轮询就失效了。
        self._httpd.timeout = 0.3
        self.port = self._httpd.server_address[1]
        return self.port

    def start(self):
        """后台线程跑服务循环（自测脚本用；正式运行由主线程直接调用 serve_forever）。"""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True,
                                        name='http-api')
        self._thread.start()
        return self._thread

    def serve_forever(self):
        """服务循环 = ``handle_request()`` + 0.3s 超时轮询。

        ⚠️ **刻意不用 ``httpd.serve_forever()``**：它的退出依赖 selectors 被唤醒，
        本机实测出现过 ``httpd.shutdown()`` 正常返回、而主线程仍卡在 ``select``
        永不返回（见 ERROR.md E28）。``handle_request()`` 有超时且不依赖唤醒，
        退出条件只看自己的 ``_stop_event``，行为可预测。
        """
        ticks = 0
        LOGGER.info('[HTTP] 服务循环开始（主线程）')
        while not self._stop_event.is_set():
            try:
                self._httpd.handle_request()
            except Exception as e:
                LOGGER.debug(f'[HTTP] handle_request 异常: {e}')
            ticks += 1
            if ticks % 600 == 0:
                LOGGER.debug(f'[HTTP] 主循环存活 ticks={ticks}')
        LOGGER.info(f'[HTTP] 服务循环退出（ticks={ticks}）')

    def request_stop(self):
        """请求主循环退出（可从任意线程调用，含 HTTP 处理器线程）。

        不需要 ``httpd.shutdown()``：主循环每 0.3s 自己检查 ``_stop_event``。
        """
        self._stop_event.set()

    def server_close(self):
        """释放监听 socket（应在主循环返回后、由收尾方调用）。"""
        try:
            if self._httpd is not None:
                self._httpd.server_close()
        except Exception:
            pass

    def stop(self):
        self.request_stop()
        self.server_close()


def _make_handler(hub, on_shutdown):
    """生成绑定了 hub 的请求处理器（BaseHTTPRequestHandler 是每次请求新建的）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = f'learn-helper/{APP_VERSION}'
        protocol_version = 'HTTP/1.1'

        # ---- 基础设施 ----
        def log_message(self, fmt, *args):      # 静音默认 stderr 日志
            return

        def _send(self, code, obj):
            body = dumps_ascii(obj).encode('ascii')
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except Exception:
                pass

        def _ok(self, message='', data=None, **extra):
            payload = {'ok': True, 'message': message, 'version': APP_VERSION}
            if data is not None:
                payload['data'] = data
            payload.update(extra)
            self._send(200, payload)

        def _err(self, message, code=400):
            self._send(code, {'ok': False, 'message': str(message), 'version': APP_VERSION})

        def _read_json(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except Exception:
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                obj = json.loads(raw.decode('utf-8'))
            except Exception as e:
                raise ValueError(f'请求体不是合法 JSON: {e}')
            return obj if isinstance(obj, dict) else {}

        def _query(self):
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            return parsed.path.rstrip('/') or '/', {k: v[0] for k, v in qs.items()}

        # ---- 路由 ----
        def do_GET(self):
            path, query = self._query()
            try:
                if path in ('/api/health', '/health'):
                    return self._ok('healthy', pipe=hub.pipe.name if hub.pipe else '',
                                    protocol=PROTOCOL_VERSION)
                if path in ('/api/version', '/version'):
                    return self._ok('', version=APP_VERSION, protocol=PROTOCOL_VERSION,
                                    device_id=DEVICE_ID)
                if path == '/api/status':
                    payload = hub.snapshot()
                    return self._ok('', **payload)
                if path == '/api/pages':
                    if hub.engine is None:
                        return self._err('引擎未就绪')
                    res = hub.engine.list_pages()
                    if res.get('ok'):
                        hub.emit_pages(res.get('pages') or [])
                    return self._ok(res.get('message', ''), res)
                if path == '/api/logs':
                    since = int(query.get('since') or 0)
                    entries = hub.logs_since(since)
                    return self._ok('', {'entries': entries, 'last': hub._log_seq})
                if path == '/api/settings':
                    return self._ok('', settings_payload(hub))
                if path == '/api/diag':
                    if hub.engine is None:
                        return self._err('引擎未就绪')
                    res = hub.engine.diagnose()
                    return (self._ok if res.get('ok') else self._err)(
                        res.get('message', ''), res.get('message', ''))
                return self._err(f'未知接口: {path}', 404)
            except Exception as e:
                LOGGER.exception('[HTTP] GET 异常')
                return self._err(f'{type(e).__name__}: {e}', 500)

        def do_POST(self):
            path, _query_args = self._query()
            try:
                if path == '/api/control':
                    body = self._read_json()
                    return self._control(body)
                if path == '/api/settings':
                    return self._settings_update()
                if path == '/api/shutdown':
                    self._ok('正在退出')
                    if on_shutdown is not None:
                        threading.Thread(target=on_shutdown, daemon=True).start()
                    return
                return self._err(f'未知接口: {path}', 404)
            except ValueError as e:
                return self._err(str(e), 400)
            except Exception as e:
                LOGGER.exception('[HTTP] POST 异常')
                return self._err(f'{type(e).__name__}: {e}', 500)

        # PUT 走与 POST 相同的 settings 语义（前端按 REST 惯例用 PUT）
        def do_PUT(self):
            path, _query_args = self._query()
            try:
                if path == '/api/settings':
                    return self._settings_update()
                return self._err(f'未知接口: {path}', 404)
            except ValueError as e:
                return self._err(str(e), 400)
            except Exception as e:
                LOGGER.exception('[HTTP] PUT 异常')
                return self._err(f'{type(e).__name__}: {e}', 500)

        # ---- 业务 ----
        def _control(self, body):
            engine = hub.engine
            if engine is None:
                return self._err('引擎未就绪', 503)
            action = str(body.get('action') or '').strip()
            params = body.get('params') or {}
            if not isinstance(params, dict):
                params = {}
            if action == 'start':
                ok, msg = engine.start(selected_title=params.get('page')
                                       or body.get('page'))
            elif action == 'stop':
                engine.stop(close_browser=bool(params.get('close_browser')))
                ok, msg = True, '已请求停止'
            elif action == 'pause':
                ok, msg = engine.pause()
            elif action == 'resume':
                ok, msg = engine.resume()
            elif action == 'select_page':
                page = str(params.get('page') or '').strip()
                with hub.lock:
                    hub.selected_title = page
                # **选了就记住**：写进 config.json 的 last_page_title，
                # 这样后端重启（界面关掉再开）后仍然选中同一页 —— 否则每次都要重选。
                if page:
                    try:
                        from .config import update_config
                        update_config({'last_page_title': page})
                    except Exception as e:
                        LOGGER.warning(f'[选择网页] 记忆失败: {e}')
                ok, msg = True, f'已选择网页：{page}'
            elif action == 'refresh_pages':
                res = engine.list_pages()
                ok, msg = bool(res.get('ok')), res.get('message', '')
                hub.emit_pages(res.get('pages') or [])
                return self._ok(msg, res, **hub.snapshot())
            elif action == 'launch_browser':
                # 界面启动后自动拉起沙盒浏览器（老 Tk 版 auto_launch_browser_on_start 的行为）。
                # 失败只记日志（比如本机没装 Edge/Chrome），**不该打扰用户**。
                ok, msg = engine.auto_launch_browser()
                # ⚠️ 把结论**写进后端日志**：`_control` 的公共尾巴只调 emit_status()，
                # 界面内存里的日志外部读不到 ⇒ 探针无法证明"后端答了跳过还是真去拉了"
                # （autolaunch_probe 的"关掉开关"用例实测踩到）。emit_log 同时会落 LOGGER。
                hub.emit_log(f'[浏览器] {msg}')
                if not ok:
                    hub.emit_log(f'[浏览器] 自动打开失败：{msg}（可点「检测/刷新网页」重试）')
            elif action == 'diagnose':
                res = engine.diagnose()
                ok, msg = bool(res.get('ok')), res.get('message', '')
            elif action == 'test_backend':
                res = engine.test_backend(base=params.get('server_url'))
                ok, msg = bool(res.get('ok')), res.get('message', '')
            elif action == 'self_test':
                ok, msg = engine.run_self_test(mode=params.get('mode') or 'server',
                                               base=params.get('server_url'))
            elif action == 'shutdown':
                ok, msg = True, '正在退出'
                engine.stop(close_browser=True)
                if on_shutdown is not None:
                    threading.Thread(target=on_shutdown, daemon=True).start()
            else:
                return self._err(f'未知指令: {action or "(空)"}', 400)
            hub.emit_status()
            payload = hub.snapshot()
            payload['action_ok'] = bool(ok)
            payload['action_message'] = str(msg)
            return self._ok(str(msg), **payload)

        def _settings_update(self):
            body = self._read_json()
            engine = hub.engine
            from .config import update_config
            patch = {}
            messages = []
            if 'server_url' in body and body.get('server_url') is not None:
                patch['server_url'] = str(body['server_url']).rstrip('/')
                messages.append(f'后端地址已保存: {patch["server_url"]}')
            if isinstance(body.get('answer'), dict):
                patch['answer'] = {k: v for k, v in body['answer'].items()
                                   if k in ('mode', 'workers', 'solver_timeout', 'retry')}
                messages.append('答题设置已保存')
            if isinstance(body.get('llm'), dict):
                patch['llm'] = {k: v for k, v in body['llm'].items()
                                if k in ('base_url', 'api_key', 'model')}
                messages.append('大模型设置已保存')
            if 'video_speed' in body:
                if engine is None:
                    return self._err('引擎未就绪', 503)
                ok, msg = engine.set_video_speed(body['video_speed'])
                if not ok:
                    return self._err(msg)
                messages.append(msg)
            if 'auto_submit' in body:
                if engine is None:
                    return self._err('引擎未就绪', 503)
                ok, msg = engine.set_auto_submit(body['auto_submit'])
                if not ok:
                    return self._err(msg)
                messages.append(msg)
            if 'auto_launch_browser' in body:
                if engine is None:
                    return self._err('引擎未就绪', 503)
                ok, msg = engine.set_auto_launch_browser(body['auto_launch_browser'])
                if not ok:
                    return self._err(msg)
                messages.append(msg)
            if patch:
                update_config(patch)
            if engine is not None:
                engine.reload_settings()
            hub.emit_log('；'.join(messages) if messages else '设置已更新')
            hub.emit_status()
            return self._ok('；'.join(messages), settings_payload(hub))

    return Handler


def settings_payload(hub):
    """设置快照（供 GET/PUT /api/settings 与前端表单回填）。"""
    from .config import (ANSWER_MODE_LABELS, DEVICE_ID, effective_server_url,
                         get_answer_cfg, get_llm_cfg)
    engine = hub.engine
    acfg = get_answer_cfg()
    llm = get_llm_cfg()
    return {
        'server_url': effective_server_url(),
        'device_id': DEVICE_ID,
        'answer': {
            'mode': acfg['mode'],
            'mode_label': ANSWER_MODE_LABELS.get(acfg['mode'], acfg['mode']),
            'workers': acfg['workers'],
            'solver_timeout': acfg['solver_timeout'],
            'retry': acfg['retry'],
        },
        'llm': {
            'base_url': llm['base_url'],
            'model': llm['model'],
            'has_api_key': bool(llm['api_key']),
        },
        'run': {
            'video_speed': engine.settings.get('video_speed', 2.0) if engine else 2.0,
            'auto_submit': engine.settings.get('auto_submit', True) if engine else True,
            'auto_launch_browser': (engine.settings.get('auto_launch_browser', True)
                                    if engine else True),
        },
    }
