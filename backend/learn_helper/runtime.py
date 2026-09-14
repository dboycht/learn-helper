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


def acquire_single_instance_lock(force=False):
    """返回 (ok, existing_info)。已有活着的后端时返回 False（避免抢浏览器/端口）。"""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    existing = read_runtime_info()
    if existing and _pid_alive(existing.get('pid')):
        if not force:
            return False, existing
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass
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
            sys.stderr.write(dumps_ascii({
                'type': 'already-running', 'pid': (existing or {}).get('pid'),
                'port': (existing or {}).get('port')}) + '\n')
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
        self.api.start()

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
        self.api.serve_forever()
        self._cleanup()
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
        """主线程收尾：停引擎与浏览器 → 关管道 → 关 HTTP → 释放运行时文件。"""
        try:
            if self.engine is not None:
                self.engine.stop(close_browser=True)
        except Exception as e:
            LOGGER.warning(f'[退出] 停止引擎异常: {e}')
        try:
            if self.pipe is not None:
                self.pipe.stop()
        except Exception:
            pass
        try:
            if self.api is not None:
                self.api.server_close()
        except Exception:
            pass
        release_runtime_info()
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
    p.add_argument('--version', action='store_true', help='打印版本后退出')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    setup_logger()

    if args.version:
        print(APP_VERSION)
        return 0
    if args.print_config:
        from .ipc import settings_payload
        hub = Hub()
        print(dumps_ascii(settings_payload(hub)))
        return 0
    if args.diagnose:
        from . import core
        core.run_diagnose_cli()
        return 0

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
