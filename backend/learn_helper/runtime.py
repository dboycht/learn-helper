# -*- coding: utf-8 -*-
"""后端进程装配与生命周期。

启动握手（前端据此拿到随机端口，不需要猜）：

1. 先 ``bind`` HTTP socket（拿到确切端口，避免"先写文件后监听"的竞态）；
2. 启动命名管道监听；
3. 向 **stdout** 打一行纯 ASCII JSON（带哨兵 ``learn-helper-backend-ready``）；
4. 同时把同一份信息写进 ``<base>/.runtime/backend.json``（便于人工/脚本排查）。

父死子亡：守护 stdin —— 父进程一退出，stdin 读到 EOF 即自我了断（避免残留后端进程）。
"""

import argparse
import json
import os
import signal
import sys
import threading
import time

from .config import APP_VERSION, BASE_DIR, LOGGER, setup_logger
from .engine import SolverEngine
from .ipc import ApiServer, Hub, PipeServer, PROTOCOL_VERSION, dumps_ascii, find_free_port

READY_TOKEN = 'learn-helper-backend-ready'
EXIT_ALREADY_RUNNING = 3

RUNTIME_DIR = os.path.join(BASE_DIR, '.runtime')
LOCK_PATH = os.path.join(RUNTIME_DIR, 'backend.lock')
INFO_PATH = os.path.join(RUNTIME_DIR, 'backend.json')

# 退出时"停引擎 + 关浏览器"的上限（秒）。超过就放弃这一步继续收尾。
# 取值依据：CDP 连接超时 3s + Playwright 驱动启动余量（实测正常路径 ≈1~3s）。
ENGINE_STOP_TIMEOUT = 6.0


# ----------------------------------------------------------------------------
# 单实例锁
# ----------------------------------------------------------------------------
def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259      # STILL_ACTIVE
    except Exception:
        return False


def _backend_responding(port, timeout=1.5):
    """该端口上是否真有我们自己的后端在服务（用于排除残留文件）。

    ⚠️ 只查 pid 存活是不够的：进程被杀后 pid 会被系统回收，
    ``_pid_alive`` 可能命中**另一个毫不相干的进程**，于是新实例误判"已有实例在运行"
    而拒绝启动（实测踩到，见 ERROR.md E32）。所以最终判据是"端口上能问到 /api/health"。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port <= 0:
        return False
    import json
    import socket
    import urllib.request

    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            pass
    except Exception:
        return False
    try:
        # 本机请求绕开代理（有系统代理时会拦 127.0.0.1）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return bool(data.get('ok'))
    except Exception:
        return False


def _clear_stale_runtime_files():
    for path in (LOCK_PATH, INFO_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def acquire_single_instance_lock(force=False):
    """返回 (ok, existing_info)。

    已有**真正在服务**的后端时返回 False（避免抢浏览器/端口）。
    ⚠️ 残留文件（上次崩溃/强杀留下）必须自动清掉，否则会永久卡死启动——
    这是实测踩到的坑（ERROR.md E32）。
    """
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    existing = read_runtime_info()
    if existing and not force:
        if _backend_responding(existing.get('port')):
            return False, existing
        # pid 活着但端口无响应 ⇒ 要么不是我们的进程（pid 被回收），
        # 要么是半死状态；两种情况都该清掉残留继续启动。
        LOGGER.warning(f'[启动] 发现残留运行时文件（{existing}），已清理后继续启动。')
        _clear_stale_runtime_files()
    elif existing and force:
        _clear_stale_runtime_files()

    with open(LOCK_PATH, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    return True, existing


def read_runtime_info():
    try:
        with open(INFO_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_runtime_info(info):
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(INFO_PATH, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=True, indent=2)
    except Exception as e:
        LOGGER.warning(f'[启动] 写运行时信息失败: {e}')


def release_runtime_info():
    for path in (LOCK_PATH, INFO_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 主服务
# ----------------------------------------------------------------------------
class Backend:
    def __init__(self, port=0, force=False):
        self.requested_port = int(port or 0)
        self.force = force
        self.hub = Hub()
        self.engine = None
        self.pipe = None
        self.api = None
        self.port = 0
        self._stopping = threading.Event()
        self._exit_code = 0

    # ---------------- 启动 ----------------
    def start(self):
        ok, existing = acquire_single_instance_lock(self.force)
        if not ok:
            LOGGER.warning(f'[启动] 已有后端在运行: {existing}')
            # 既写 stderr（人看）也写 stdout（前端可解析并直接复用已运行的实例，
            # 而不是只报一句"没收到握手行"——实测踩过，见 ERROR.md E32）。
            msg = dumps_ascii({
                'type': 'already-running',
                'token': 'learn-helper-backend-already-running',
                'ok': False,
                'pid': (existing or {}).get('pid'),
                'port': (existing or {}).get('port'),
                'pipe': (existing or {}).get('pipe'),
            }) + '\n'
            sys.stderr.write(msg)
            sys.stderr.flush()
            sys.stdout.write(msg)
            sys.stdout.flush()
            return None

        self.engine = SolverEngine(self.hub)
        self.pipe = PipeServer(self.hub)
        self.api = ApiServer(self.hub, self.requested_port or find_free_port(),
                             on_shutdown=self.shutdown)
        self.port = self.api.bind()                 # 先绑端口，握手才有准数
        self.hub.attach(self.engine, self.pipe)

        pipe_ok = self.pipe.start()
        if not pipe_ok:
            self.hub.emit_log(f'[启动] 命名管道不可用（{self.pipe.last_error}），'
                              f'日志推送降级为 HTTP 轮询。')
        # ⚠️ **不要再调 `self.api.start()`**（2026-09-16，ERROR.md E45）：
        # 那会再起一条 `http-api` 后台线程跑 serve_forever，于是**同一个 socket 上有两条
        # accept 循环**（后台线程 + 下面主线程各一条），退出时互相抢、
        # 实测出现"已请求退出但循环就是不返回"⇒ 收尾不执行 ⇒ 后端残留并占着端口，
        # 下次启动被判"已有实例在运行"。正式运行只由**主线程**的 serve_forever 服务；
        # `ApiServer.start()` 留给自测脚本用（它们不起主线程循环）。
        # 握手行在 serve_forever 之前就写出去了，前端本来就要轮询 /api/health，无竞态。

        info = {
            'type': 'ready',
            'token': READY_TOKEN,
            'ok': True,
            'version': APP_VERSION,
            'protocol': PROTOCOL_VERSION,
            'pid': os.getpid(),
            'port': self.port,
            'host': '127.0.0.1',
            'pipe': self.pipe.name if pipe_ok else '',
            'base_dir': BASE_DIR,
        }
        write_runtime_info(info)
        # 一行纯 ASCII JSON = 前端握手行
        sys.stdout.write(dumps_ascii(info) + '\n')
        sys.stdout.flush()
        self.hub.emit_log(f'[启动] learn-helper 后端 v{APP_VERSION} 已就绪'
                          f'（HTTP 127.0.0.1:{self.port}，管道 {info["pipe"] or "不可用"}）')
        return info

    # ---------------- 运行 ----------------
    def serve_forever(self):
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        except Exception:
            pass
        threading.Thread(target=self._watch_stdin, daemon=True, name='stdin-watch').start()
        # 阻塞在这里；收到退出请求后 serve_forever 返回，**由主线程**执行收尾。
        # （收尾放主线程、退出码由 main() 返回，才不会和解释器 finalize 抢 stdout/stderr。）
        LOGGER.info('[HTTP] 主线程进入 api.serve_forever()')
        self.api.serve_forever()
        LOGGER.info('[退出] HTTP 循环已返回，开始收尾 _cleanup')
        self._cleanup()
        LOGGER.info('[退出] 收尾完成')
        return self._exit_code

    def _watch_stdin(self):
        """父进程退出（stdin EOF）即退出，避免残留后端进程。"""
        try:
            while True:
                line = sys.stdin.readline()
                if line == '':
                    break
                cmd = line.strip().lower()
                if cmd in ('quit', 'shutdown', 'exit'):
                    break
        except Exception:
            pass
        LOGGER.info('[生命周期] stdin 关闭（父进程退出），后端自行退出。')
        self.shutdown()

    # ---------------- 停止 ----------------
    def shutdown(self):
        """请求退出（可从任意线程调用）。真正的收尾在 ``_cleanup``（主线程）。"""
        if self._stopping.is_set():
            return
        self._stopping.set()
        LOGGER.info('[退出] 收到退出请求...')
        try:
            self.hub.emit_log('[退出] 正在停止刷课流程...')
        except Exception:
            pass
        try:
            LOGGER.info('[退出] 正在唤醒 HTTP 服务...')
            self.api.request_stop()          # 只置位；主循环每 0.3s 自查
            LOGGER.info('[退出] HTTP 服务已唤醒')
        except Exception as e:
            LOGGER.warning(f'[退出] 唤醒 HTTP 服务失败: {e}')

    def _cleanup(self):
        """主线程收尾：停引擎与浏览器 → 关管道 → 关 HTTP → 释放运行时文件。

        ⚠️ **每一步都必须有界**。实测（2026-09-16，ERROR.md E45）：
        `engine.stop(close_browser=True)` 里的 Playwright `sync_playwright()` 收尾
        **会永远不返回**（日志停在"HTTP 服务已唤醒"就没了），于是收尾卡死
        ⇒ HTTP 端口不放、运行时文件不删 ⇒ **界面已退出、后端还挂着**，
        下一次启动被判定"已有实例在运行"而连不上。
        所以引擎那一步套一层**有超时的 daemon 线程**：超时就放弃关浏览器，
        继续把端口与运行时文件收干净（反正 `os._exit` 会终止进程）。
        """
        try:
            if self.engine is not None:
                LOGGER.info('[退出] 收尾①：停引擎/关浏览器')
                stopper = threading.Thread(target=self.engine.stop,
                                           kwargs={'close_browser': True},
                                           daemon=True, name='engine-stop')
                stopper.start()
                stopper.join(timeout=ENGINE_STOP_TIMEOUT)
                if stopper.is_alive():
                    LOGGER.warning('[退出] 停止引擎/关闭浏览器超时（放弃该步，继续收尾）')
                else:
                    LOGGER.info('[退出] 收尾①完成')
        except Exception as e:
            LOGGER.warning(f'[退出] 停止引擎异常: {e}')
        try:
            if self.pipe is not None:
                LOGGER.info('[退出] 收尾②：关闭命名管道')
                self.pipe.stop()
        except Exception:
            pass
        try:
            if self.api is not None:
                LOGGER.info('[退出] 收尾③：关闭 HTTP 监听')
                self.api.server_close()
        except Exception:
            pass
        release_runtime_info()
        LOGGER.info('[退出] 收尾④：运行时文件已释放')
        logger = LOGGER
        for h in list(logger.handlers):
            try:
                h.flush()
                h.close()
                logger.removeHandler(h)
            except Exception:
                pass
        try:
            import logging
            logging.shutdown()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog='learn-helper-backend',
                                description='学习助理 Python 后端服务')
    p.add_argument('--port', type=int, default=0,
                   help='HTTP 端口（默认 0 = 由系统随机分配）')
    p.add_argument('--diagnose', action='store_true',
                   help='命令行页面诊断（连接本机 CDP，dump 识别信息后退出）')
    p.add_argument('--print-config', action='store_true',
                   help='打印配置快照 JSON 后退出（排障用）')
    p.add_argument('--force', action='store_true',
                   help='忽略单实例锁，强制启动')
    p.add_argument('--check-browser', action='store_true',
                   help='自检：启动 Playwright 驱动并连接本机 CDP，打印结果后退出')
    p.add_argument('--launch-browser', action='store_true',
                   help='自检：先真的拉起沙盒浏览器，再做 --check-browser 的全套检查')
    p.add_argument('--version', action='store_true', help='打印版本后退出')
    return p


def _check_browser(do_launch=False):
    """`--check-browser`：把"Playwright 驱动能不能起来 + 能不能连上 9222"一次问清。

    为什么要有这个开关（2026-09-19）：发布包**刻意不带** Playwright 自带的
    88 MB `node.exe`，改用本机 Node（见 `_release/learn-helper-core.spec` 与
    `rthook_node.py`）。这条改动一旦出问题，表现是"点了启动刷课就没反应"，
    而冻结成 exe 之后**看不到 traceback**（stdout/stderr 都被界面收走了）。
    所以留一个能单独跑、逐步打印的自检入口 —— 排障时一行命令就能定位到
    "驱动没起来 / 拉不起浏览器 / 连不上浏览器"里的哪一步。

    `--launch-browser` 额外**真的拉一次沙盒浏览器**：这是冻结环境下最容易出事的
    一步（要 spawn Edge + 等 9222 就绪），而它在 HTTP 线程里跑，崩了看不到任何输出。
    """
    from . import core
    print(f'backend version : {APP_VERSION}')
    print(f'frozen          : {bool(getattr(sys, "frozen", False))}')
    print(f'executable      : {sys.executable}')
    print(f'PLAYWRIGHT_NODEJS_PATH : {os.environ.get("PLAYWRIGHT_NODEJS_PATH") or "(not set)"}')
    print(f'node candidates : {core._find_system_node() or "(none found)"}')
    print(f'cdp 9222 open   : {core.is_cdp_port_open()}')
    try:
        sync_playwright = core.require_playwright()
        print('import playwright : OK')
    except Exception as e:
        print(f'import playwright : FAILED -> {e}')
        return 2
    if do_launch:
        print('launch browser  : ...')
        try:
            ok, proc = core.kill_and_launch_browser()
            print(f'launch browser  : {"OK" if ok else "FAILED"}'
                  f'{"" if proc is None else f" (pid {proc.pid})"}')
        except Exception as e:
            print(f'launch browser  : EXCEPTION -> {type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()
            return 5
        print(f'cdp 9222 open   : {core.is_cdp_port_open()}')
    try:
        print('starting driver   : ...')
        with sync_playwright() as p:
            print('starting driver   : OK')
            try:
                browser = p.chromium.connect_over_cdp(core.CDP_URL, timeout=8000)
            except Exception as e:
                print(f'connect_over_cdp  : FAILED -> {e}')
                return 3
            print(f'connect_over_cdp  : OK (browser {browser.version}, '
                  f'{len(browser.contexts)} context(s))')
            browser.close()
    except Exception as e:
        print(f'playwright run    : FAILED -> {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()
        return 4
    print('RESULT: browser automation is sane')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    setup_logger()

    if args.version:
        print(APP_VERSION)
        return 0
    if args.check_browser:
        return _check_browser()
    if args.launch_browser:
        return _check_browser(do_launch=True)
    if args.print_config:
        from .ipc import settings_payload
        hub = Hub()
        print(dumps_ascii(settings_payload(hub)))
        return 0
    if args.diagnose:
        from . import core
        # 把诊断的真实结果作为退出码传出去（原来恒返回 0，脚本没法判成败）
        return core.run_diagnose_cli() or 0

    backend = Backend(port=args.port, force=args.force)
    if backend.start() is None:
        return EXIT_ALREADY_RUNNING
    code = backend.serve_forever()
    # 硬退：管道/监听线程是 daemon 没关系，但 greenlet(Playwright) 之类的非守护
    # 线程会拖住正常返回。收尾已在主线程做完，这里直接定退出码。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def run():
    sys.exit(main())


if __name__ == '__main__':  # pragma: no cover
    run()
