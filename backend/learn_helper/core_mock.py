# -*- coding: utf-8 -*-
"""自测用的 mock 答题后端（本地 http.server，纯内存，不发任何外网请求）。

契约与真后端一致：
    GET  /check_version  → 200 {"notice": str, "force_update": bool}
    POST /solve          → 200 {"answer_key"?, "text_answers"?, "hash_id", "cached"}

用作 ``verify_backend.py`` 的对照实验：证明「后端通道打通 + 答案解析正确」，
而不是证明"某个远端服务可用"（探针纪律，见 rules/01 §8.4）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 内置三道自检题的期望答案（与 core.TEST_CASES 对齐）
_EXPECT = {
    'choice': {'answer_key': 'B'},
    'multi_choice': {'answer_key': 'AC'},
    'blank': {'text_answers': ['12']},
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=True).encode('ascii')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/check_version'):
            return self._json(200, {'notice': 'mock 后端在线', 'force_update': False})
        return self._json(404, {'detail': 'no such endpoint'})

    def do_POST(self):
        if not self.path.startswith('/solve'):
            return self._json(404, {'detail': 'no such endpoint'})
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            return self._json(400, {'detail': 'bad json'})
        q_type = payload.get('question_type') or 'choice'
        self.server.seen.append(payload)
        ans = dict(_EXPECT.get(q_type, {'answer_key': 'A'}))
        ans['hash_id'] = f'mock-{len(self.server.seen)}'
        ans['cached'] = False
        return self._json(200, ans)


class MockAnswerBackend:
    def __init__(self, port=0):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.seen = []
        self.port = self._httpd.server_address[1]
        self._thread = None

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}'

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={'poll_interval': 0.2}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """停掉 mock 服务。**只在确实跑起来之后**才 shutdown。

        ⚠️ `socketserver.shutdown()` 会等 `serve_forever()` 把它的事件置位；
        如果 `serve_forever()` 从没跑过，这个调用**永远不返回**（自测会在退出时挂住）。
        异常也不再 `pass` 掉 —— 自测工具的失败必须看得见（E74）。
        """
        try:
            if getattr(self, '_thread', None) is not None and self._thread.is_alive():
                self._httpd.shutdown()
            self._httpd.server_close()
        except Exception as e:
            try:
                from learn_helper.config import LOGGER
                LOGGER.warning(f'[mock] 停止 mock 后端时异常: {e}')
            except Exception:
                pass

    def run_self_test_via(self):
        """把请求指向 mock 后端跑一遍内置三道题自检。返回 (ok_all, lines, results)。

        ⚠️ 两处**必须**做，否则结果不可信：
        1. 先清 ``SHUTDOWN`` —— 引擎在"未选页/未启动"分支里会把它置位，留着会让本次
           求解全部立刻放弃（表现为三题 request_fail，看着像 mock 坏了）；
        2. 不改 config.json：把 base 直接传给 ``run_solve_self_test``，避免污染用户配置。

        （原来这里有个没人用的 `engine` 参数，会让人误以为它走的是引擎链路，已删。）
        """
        from learn_helper import core
        core.SHUTDOWN.clear()
        return core.run_solve_self_test(mode='server', retry=0, base=self.base_url)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
