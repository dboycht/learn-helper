"""后端客户端 —— Qt 前端与 Python 后端之间的唯一通道。

**协议一个字都没改**（`ipc.py` 的 HTTP + 命名管道），只是把原来 Rust 侧
（`native/src/backend.rs`）的那份实现换成 Python：

```
界面启动 → subprocess 拉起 backend/main.py --port 0（stdin=PIPE，父死子亡）
        → 读 stdout 的**握手行**（JSON，含 port / pipe）
        → GET /api/health 健康检查
        → 打开命名管道逐行读事件（log / progress / status / pages）
        → 控制类走 POST /api/control，设置类走 GET/PUT /api/settings
```

⚠️ 三个必须照抄的约定（都是踩过坑才有的，别自作聪明改掉）：
1. **stdin 必须保持打开**：后端按设计"stdin EOF 即自杀"。我们自己拉起就必须攥着这条管道，
   否则后端启动即退出（历史上被误判成"新构建崩了"，见 ERROR.md E68）。
2. **握手行要按字符截断**再写日志（不能按字节切，中文会切出半个字，E62/E63）。
3. **HTTP 一律绕开系统代理**（`proxies={'http': None, 'https': None}`）：本机回环地址
   走代理会莫名其妙超时（内网环境实测）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

import requests
from PySide6.QtCore import QObject, Signal

READY_TOKEN = 'learn-helper-backend-ready'


def _app_dir() -> str:
    """界面应该把**自己的运行时数据**（config.json / logs / native-diag.log）放在哪。

    ⚠️ **冻结（PyInstaller onefile）时绝不能用 `__file__` 拼路径**（2026-09-21 实测踩到）：
    冻结后 `__file__` 指向 `_MEIPASS` 那个**临时解包目录**（`%TEMP%\\_MEIxxxx`），
    进程一退就被删 ⇒ 写在那里的诊断日志"当场消失"，用户和探针都找不到。
    （同一个坑在 `native/rthook_node.py` 里也踩过，见 ERROR.md E72。）

    顺序：`LH_BASE_DIR` 显式覆盖 → 冻结时 exe 所在目录 → 源码时仓库根。
    """
    override = os.environ.get('LH_BASE_DIR')
    if override:
        return override
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 本文件在 <repo>/frontend/backend_client.py ⇒ 源码运行时上溯两级就是仓库根；
# 冻结时用 exe 所在目录（见 _app_dir 的说明）。
PROJECT_DIR = _app_dir()
BACKEND_ENTRY = os.path.join(PROJECT_DIR, 'backend', 'main.py')

_NO_PROXY = {'http': None, 'https': None}


def _truncate(text: str, limit: int) -> str:
    """按**字符**截断（中文安全）。"""
    text = text or ''
    return text if len(text) <= limit else text[:limit] + '…'


class BackendClient(QObject):
    """后端进程 + HTTP + 管道。所有回调都在 Qt 主线程发出（用 Signal）。"""

    # (级别, 文本)；级别给界面决定要不要写诊断文件
    logged = Signal(str, str)
    connected = Signal(str, str)      # base_url, pipe_name
    failed = Signal(str)
    status_changed = Signal()         # 拉到新 status 时通知界面刷新
    pages_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.pipe_name: str = ''
        self.base_url: str = ''
        self.version: str = ''
        self.connected_ok = False
        self._stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._pipe_thread: Optional[threading.Thread] = None
        self._last_status: dict = {}
        self._trace_hook: Optional[Callable[[str], None]] = None

    # ---------------------------------------------------------------- 启动
    def set_trace_hook(self, hook: Callable[[str], None]) -> None:
        """外部（诊断日志）挂钩：每条内部事件都喂给它一份，便于排障。"""
        self._trace_hook = hook

    def _trace(self, line: str) -> None:
        if self._trace_hook:
            try:
                self._trace_hook(line)
            except Exception:
                pass

    def start(self) -> None:
        """后台线程拉起后端并握手（不阻塞界面）。"""
        threading.Thread(target=self._spawn_and_handshake, daemon=True,
                         name='backend-start').start()

    def _spawn_and_handshake(self) -> None:
        try:
            exe, args = self._resolve_command()
            self._trace(f'backend: spawn {exe} {" ".join(args)}')
            env = dict(os.environ)
            env['PYTHONIOENCODING'] = 'utf-8'
            env.setdefault('PYTHONUTF8', '1')
            # ⚠️ **显式告诉后端把运行时文件放哪**：后端自己会取 `sys.executable` 所在目录，
            # 而打包后它在 `backend\` 子目录里 ⇒ 它会战 config.json / logs 放进
            # `backend\`，与 README 说的"在 LearnHelper.exe 旁边"不符，用户找不到。
            # 这里统一钉到界面自己的目录（= exe 旁边），源码运行 = 仓库根，行为一致。
            env['LH_BASE_DIR'] = PROJECT_DIR
            self._trace(f'backend: LH_BASE_DIR={PROJECT_DIR}')
            self.proc = subprocess.Popen(
                [exe] + args,
                cwd=PROJECT_DIR,
                stdin=subprocess.PIPE,      # ⚠️ 必须保持打开：关了后端就自杀
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                if os.name == 'nt' else 0,
            )
        except Exception as e:                       # pragma: no cover
            self.failed.emit(f'后端启动失败: {e}')
            return

        threading.Thread(target=self._drain_stderr, daemon=True, name='backend-stderr').start()
        self._read_handshake()

    def _resolve_command(self):
        """决定用哪个后端可执行文件。顺序与 Rust 版一致，并**显式覆盖冻结场景**。

        1. **冻结（PyInstaller onefile）**：`sys.executable` 就是界面 exe，后端在它旁边
           （`backend\\learn-helper-core.exe`）。⚠️ 注意 onefile 下**不能**用 `sys._MEIPASS`
           去拼路径 —— 那是会被删掉的临时解包目录，后端不在那里。
        2. **源码运行**：`sys.executable` 是 python.exe，此时跑 `backend\\main.py`。
        3. 两者都不成立就抛异常（好过让 `Popen` 用错误的参数静默失败）。
        """
        # 1) 打包后：后端就在界面 exe 旁边
        for base in (os.path.dirname(sys.executable), PROJECT_DIR):
            cand = os.path.join(base, 'backend', 'learn-helper-core.exe')
            if os.path.exists(cand):
                return cand, ['--port', '0']
        # 2) 源码运行：用当前解释器跑后端脚本
        if os.path.exists(BACKEND_ENTRY):
            return sys.executable, [BACKEND_ENTRY, '--port', '0']
        raise FileNotFoundError(
            f'找不到后端：既没有打包的 backend\\learn-helper-core.exe，'
            f'也没有源码 {BACKEND_ENTRY}')

    def _drain_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        try:
            for line in self.proc.stderr:
                line = line.rstrip('\n')
                if line.strip():
                    self._trace(f'backend[stderr]: {_truncate(line, 200)}')
        except Exception:
            pass

    def _read_handshake(self) -> None:
        """读 stdout 第一行 JSON 握手；之后继续在后台把 stdout 抄进诊断日志。"""
        if not self.proc or not self.proc.stdout:
            self.failed.emit('后端没有可读的 stdout')
            return
        deadline = time.time() + 60
        got = None
        while time.time() < deadline and not self._stop.is_set():
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.rstrip('\n')
            if not line.strip():
                continue
            self._trace(f'backend[stdout#1]: {_truncate(line, 200)}')
            if READY_TOKEN in line:
                try:
                    got = json.loads(line)
                except Exception as e:
                    self.failed.emit(f'握手行解析失败: {e}')
                    return
                break
        if got is None:
            self.failed.emit('后端握手超时或未就绪')
            return

        self.port = int(got.get('port') or 0)
        self.pipe_name = got.get('pipe') or ''
        self.version = str(got.get('version') or '')
        self.base_url = f'http://127.0.0.1:{self.port}'
        self._trace(f'backend: handshake port={self.port} pipe={self.pipe_name}')

        # 健康检查（最多重试几次，后端刚起时 HTTP 还没 listen）
        for i in range(1, 11):
            try:
                r = requests.get(f'{self.base_url}/api/health', timeout=3, proxies=_NO_PROXY)
                if r.status_code == 200:
                    self._trace(f'backend: health attempt={i} status=200')
                    break
            except Exception as e:
                self._trace(f'backend: health attempt={i} 失败: {_truncate(str(e), 80)}')
                time.sleep(0.4)
        else:
            self.failed.emit('后端 HTTP 健康检查一直失败')
            return

        self.connected_ok = True
        self._trace('backend: connected; pipe reader + poller starting')
        self.connected.emit(self.base_url, self.pipe_name)

        threading.Thread(target=self._pipe_loop, daemon=True, name='backend-pipe').start()
        threading.Thread(target=self._drain_stdout, daemon=True, name='backend-stdout').start()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                            name='backend-poll')
        self._poll_thread.start()

    def _drain_stdout(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            for line in self.proc.stdout:
                line = line.rstrip('\n')
                if line.strip():
                    self._trace(f'backend[stdout]: {_truncate(line, 200)}')
        except Exception:
            pass
        self._trace('backend[stdout]: closed')

    # ---------------------------------------------------------------- 管道
    def _pipe_loop(self) -> None:
        """读命名管道（逐行 JSON 事件）。断线就重连，直到退出。"""
        path = rf'\\.\pipe\{self.pipe_name}'
        attempts = 0
        while not self._stop.is_set():
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                    attempts = 0
                    self._trace('backend: pipe connected')
                    for line in fh:
                        if self._stop.is_set():
                            return
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        self._on_event(evt)
            except Exception as e:
                if self._stop.is_set():
                    return
                attempts += 1
                if attempts <= 3:
                    self._trace(f'backend: pipe 断开/未就绪（{_truncate(str(e), 60)}），重试 {attempts}')
                time.sleep(0.6)

    def _on_event(self, evt: dict) -> None:
        kind = evt.get('type')
        if kind == 'log':
            self.logged.emit('log', str(evt.get('text') or ''))
        elif kind == 'hello':
            self._trace(f'backend: pipe hello version={evt.get("version")}')
        elif kind == 'status':
            self._last_status = evt
            self.status_changed.emit()
        elif kind == 'pages':
            pages = evt.get('pages') or []
            self._last_status['pages'] = pages
            self.pages_changed.emit(list(pages))
        elif kind == 'progress':
            self._last_status.update(evt)
            self.status_changed.emit()

    # ---------------------------------------------------------------- 轮询兜底
    def _poll_loop(self) -> None:
        """HTTP 轮询：**这是状态的主通道**，命名管道只当作加速。

        ⚠️ 为什么以轮询为主（2026-09-21 实测）：命名管道的服务端在推送几条事件后会
        **阻塞在 `WriteFile` 里**（最小复现：`PipeServer` + 一个 Python 读取端，
        只收到 `hello`/`status` 两条，之后 `emit_pages` 的写入永久阻塞）。
        直连 CDP 的 Rust 界面没暴露它，但绝不能把界面"能不能更新"押在这条通道上。
        本机回环一次 `/api/status` 是亚毫秒级，1.2 秒一次可以忽略不计。
        """
        while not self._stop.is_set():
            time.sleep(1.2)
            st = self.status()
            if st:
                self._last_status = st
                pages = st.get('pages') or []
                if list(pages) != list(self._last_status.get('_pages_seen') or []):
                    self._last_status['_pages_seen'] = list(pages)
                    self.pages_changed.emit(list(pages))
                self.status_changed.emit()
            # 管道若还活着，它推送的事件会比轮询更快到；两条路都更新同一个 model，无冲突。

    # ---------------------------------------------------------------- HTTP
    def _get(self, path: str, timeout: float = 8.0) -> dict:
        if not self.connected_ok:
            return {}
        try:
            r = requests.get(f'{self.base_url}{path}', timeout=timeout, proxies=_NO_PROXY)
            return r.json() if r.content else {}
        except Exception as e:
            self._trace(f'backend: GET {path} 失败: {_truncate(str(e), 80)}')
            return {}

    def status(self) -> dict:
        return self._get('/api/status', timeout=5)

    def settings(self) -> dict:
        return self._get('/api/settings')

    def logs_since(self, since: int = 0) -> list:
        data = self._get(f'/api/logs?since={int(since)}')
        return (data.get('data') or {}).get('entries') or []

    def control(self, action: str, params: Optional[dict] = None) -> tuple[bool, str]:
        """POST /api/control。返回 (ok, message)。**动作名与后端 `_control` 一一对应**。"""
        if not self.connected_ok:
            return False, '后端未就绪'
        body: dict[str, Any] = {'action': action}
        if params:
            body.update(params)
        try:
            r = requests.post(f'{self.base_url}/api/control', json=body,
                              timeout=300, proxies=_NO_PROXY)
            data = r.json() if r.content else {}
            return bool(data.get('ok')), str(data.get('message') or '')
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'

    def put_settings(self, patch: dict) -> tuple[bool, str]:
        if not self.connected_ok:
            return False, '后端未就绪'
        try:
            r = requests.put(f'{self.base_url}/api/settings', json=patch,
                             timeout=20, proxies=_NO_PROXY)
            data = r.json() if r.content else {}
            return bool(data.get('ok')), str(data.get('message') or '')
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'

    # ---------------------------------------------------------------- 退出
    def shutdown(self, timeout: float = 6.0) -> None:
        """优雅退出：先让后端自己收尾，再确保进程没了。

        ⚠️ 顺序不能反：先关 stdin 会让后端**立刻**析构（它就是靠这个自杀的），
        那样它会来不及杀掉它拉起的沙盒浏览器。
        ⚠️ 必须**幂等**：`closeEvent` 与「终止并退出」都会走到这里，重复调用不能出错。
        """
        if getattr(self, '_shutdown_done', False):
            return
        self._shutdown_done = True
        self._stop.set()
        if self.connected_ok:
            try:
                requests.post(f'{self.base_url}/api/shutdown', json={},
                              timeout=3, proxies=_NO_PROXY)
            except Exception:
                pass
        if self.proc:
            deadline = time.time() + timeout
            while time.time() < deadline and self.proc.poll() is None:
                time.sleep(0.1)
            if self.proc.poll() is None:
                self._terminate_process_tree(self.proc.pid)
            # ⚠️ 关掉 stdout/stderr：读取它们的后台线程会阻塞在 readline 上，
            # 不关的话线程退不出（自检里表现为 "backend-poll/backend-stdout 线程异常"）。
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    @staticmethod
    def _terminate_process_tree(pid: int) -> None:
        """杀进程树（后端自己可能拉起了浏览器/子进程）。"""
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            pass
