# -*- coding: utf-8 -*-
"""1.0.4 后端无头自测（不依赖浏览器、不依赖 Tk、不碰用户真实 config.json）。

覆盖：
  A. 进程级：子进程启动 → stdout 握手行 → /api/health → /api/status →
     /api/control（未选页拒绝 / select_page / refresh_pages）→ /api/settings 读写钳位 →
     命名管道收到 hello/status/log → POST /api/shutdown → 进程自然退出
  B. 引擎级：设置钳位 / start 守卫 / stop 幂等 / 诊断不崩溃 / 自带 mock 后端跑通自检
  C. 纯逻辑：parse_llm_answer / _normalize_answer / 配置钳位

运行：py -3.10 backend\\verify_backend.py   （全绿退出码 0）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# ⚠️ 必须在导入 learn_helper 之前设好：config.BASE_DIR 在导入期计算。
# 这样自测写配置/日志都落在临时目录，绝不污染开发副本的 config.json。
_TEST_BASE = tempfile.mkdtemp(prefix='lh-verify-')
os.environ['LH_BASE_DIR'] = _TEST_BASE
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

# 本机有系统代理时会拦截 127.0.0.1 请求（实测 Invoke-RestMethod / requests 都中招）：
# 一律直连，别让代理把 localhost 请求转发出去。
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.abspath(os.path.join(HERE, '..', 'backend'))
PROJECT_DIR = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, BACKEND_DIR)

import requests  # noqa: E402


def http_get(url, **kw):
    """本机请求一律绕开代理（用空 proxies 显式直连）。"""
    kw.setdefault('timeout', 10)
    return requests.get(url, proxies={'http': None, 'https': None}, **kw)


def http_post(url, **kw):
    return requests.post(url, proxies={'http': None, 'https': None}, **kw)


def http_put(url, **kw):
    return requests.put(url, proxies={'http': None, 'https': None}, **kw)

FAILS = []
CHECKS = []


def check(name, cond, detail=''):
    CHECKS.append(name)
    mark = 'PASS' if cond else 'FAIL'
    print(f'  [{mark}] {name}' + (f' — {detail}' if detail and not cond else ''), flush=True)
    if not cond:
        FAILS.append(name)


def step(text):
    """Progress marker: makes a hang obvious (and shows where) in the captured output."""
    print(f'  [step] {text}', flush=True)


def seed_config():
    os.makedirs(_TEST_BASE, exist_ok=True)
    with open(os.path.join(_TEST_BASE, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'server_url': 'http://127.0.0.1:9', 'answer': {'mode': 'server'}}, f)


# ----------------------------------------------------------------------------
# A. 进程级端到端
# ----------------------------------------------------------------------------
def read_handshake(proc, timeout=25):
    line = ''
    deadline = time.time() + timeout
    while time.time() < deadline:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch == '\n':
            break
        line += ch
    return line


def wait_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return http_get(url, timeout=2).status_code
        except Exception:
            time.sleep(0.2)
    return None


def test_process_lifecycle():
    print('\n=== A. 进程级端到端 ===')
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['LH_BASE_DIR'] = _TEST_BASE
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND_DIR, 'main.py'), '--port', '0'],
        cwd=PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, env=env, text=True, encoding='utf-8', errors='replace')
    try:
        handshake = read_handshake(proc)
        print(f'  [info] handshake={handshake[:150]}')
        ok_handshake = ('learn-helper-backend-ready' in handshake
                        and '"port"' in handshake and '"pipe"' in handshake)
        check('A1 握手行含哨兵/port/pipe', ok_handshake, handshake[:200])
        if not ok_handshake:
            err = proc.stderr.read() if proc.poll() is not None else ''
            print(f'  [info] stderr={err[:400]}')
            return
        info = json.loads(handshake)
        base = f'http://127.0.0.1:{info["port"]}'

        code = wait_http(f'{base}/api/health')
        check('A2 /api/health 可达(200)', code == 200, f'code={code}')
        if code != 200:
            return

        r = http_get(f'{base}/api/health', timeout=5)
        check('A3 health 字段 ok/pipe/protocol',
              r.json().get('ok') and r.json().get('protocol') == 1 and r.json().get('pipe'))

        st = http_get(f'{base}/api/status', timeout=5).json()
        # 版本号不写死：从后端自身读，避免每次升版本都要改测试
        from learn_helper.config import APP_VERSION as backend_version
        check('A4 status 快照字段齐全',
              st.get('type') == 'status' and st.get('version') == backend_version
              and 'engine' in st and st['engine']['running'] is False, str(list(st.keys())))

        body = http_post(f'{base}/api/control', json={'action': 'start'}, timeout=10).json()
        check('A5 未选页 start 被拒且带中文提示',
              body.get('action_ok') is False and '网页' in body.get('action_message', ''),
              f"action_ok={body.get('action_ok')} msg={body.get('action_message','')[:60]}")

        body = http_post(f'{base}/api/control',
                             json={'action': 'select_page', 'params': {'page': '测试学习页'}},
                             timeout=10).json()
        check('A6 select_page 生效', body.get('selected_page') == '测试学习页', str(body)[:150])
        # A6b：选择的网页要**落盘记住**（下次启动 / 后端重启后仍然选中它）
        from learn_helper.config import load_config as _load_cfg
        check('A6b select_page 写入 config.last_page_title',
              _load_cfg().get('last_page_title') == '测试学习页',
              f"last_page_title={_load_cfg().get('last_page_title')!r}")

        r = http_post(f'{base}/api/control', json={'action': 'refresh_pages'}, timeout=30)
        body = r.json()
        check('A7 refresh_pages 不崩溃(HTTP200+message)',
              r.status_code == 200 and body.get('message'), str(body)[:150])

        r = http_put(f'{base}/api/settings', json={'video_speed': 8.0, 'auto_submit': False},
                         timeout=10)
        check('A8 settings 写入 ok', r.status_code == 200 and r.json().get('ok'), r.text[:150])
        run_cfg = http_get(f'{base}/api/settings', timeout=10).json()['data']['run']
        check('A9 video_speed 钳位到 4.0', abs(run_cfg['video_speed'] - 4.0) < 1e-6, str(run_cfg))
        check('A10 auto_submit 写入生效', run_cfg['auto_submit'] is False)

        r = http_post(f'{base}/api/control', json={'action': 'no_such'}, timeout=5)
        check('A11 未知指令返回 400', r.status_code == 400)

        step('连接命名管道读事件')
        events = read_pipe_events(info['pipe'], want=3, timeout=10)
        types = [e.get('type') for e in events]
        check('A12 管道收到 hello + 历史日志 + status',
              types[0] == 'hello' and 'status' in types,
              f'types={types[:5]}')

        step('触发一条推送日志')
        http_put(f'{base}/api/settings', json={'video_speed': 1.5}, timeout=10)
        time.sleep(0.8)
        # want 取大一点：连接时会先补发历史日志，光看前几条会把新日志挤掉。
        events2 = read_pipe_events(info['pipe'], want=25, timeout=10)
        types2 = [e.get('type') for e in events2]
        check('A14 管道收到 log 事件',
              types2 and types2[0] == 'hello' and any(
                  e.get('type') == 'log' and '倍速已设为 1.5x' in (e.get('text') or '')
                  for e in events2),
              f'types={types2[:8]} texts={[e.get("text","")[:16] for e in events2 if e.get("type")=="log"][:6]}')

        step('请求后端退出')
        try:
            http_post(f'{base}/api/shutdown', timeout=8)
        except Exception:
            pass
        code = proc.wait(timeout=20)
        check('A15 后端进程在 shutdown 后自行退出', code == 0, f'exit={code}')

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # ---- A16~A18：残留运行时文件不能卡死启动（ERROR.md E32，实测踩过）----
    lock = os.path.join(_TEST_BASE, '.runtime', 'backend.lock')
    info = os.path.join(_TEST_BASE, '.runtime', 'backend.json')
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    # 造一份"指向不存在 pid 的残留文件"
    with open(lock, 'w', encoding='utf-8') as f:
        f.write('999999')
    with open(info, 'w', encoding='utf-8') as f:
        json.dump({'type': 'ready', 'ok': True, 'pid': 999999, 'port': 9,
                   'pipe': 'learn-helper-stale'}, f)
    stale = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND_DIR, 'main.py'), '--port', '0'],
        cwd=PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, env=env, text=True, encoding='utf-8', errors='replace')
    try:
        line = read_handshake(stale, timeout=30)
        check('A16 残留运行时文件被自动清理并正常启动',
              'learn-helper-backend-ready' in line, line[:160] or '(无握手行)')
        if 'learn-helper-backend-ready' in line:
            st = json.loads(line)
            base2 = f'http://127.0.0.1:{st["port"]}'
            check('A17 清理后服务可用', wait_http(f'{base2}/api/health') == 200)
            try:
                http_post(f'{base2}/api/shutdown', timeout=8)
            except Exception:
                pass
        else:
            check('A17 清理后服务可用', False, '未启动成功')
        stale.wait(timeout=20)
    finally:
        if stale.poll() is None:
            stale.kill()
            stale.wait(timeout=10)


def read_pipe_events(pipe_name, want=3, timeout=8):
    """管道测试客户端（与 C# NamedPipeClientStream 同语义：连接后逐行读 JSON）。

    ⚠️ 必须用 ``PeekNamedPipe`` **非阻塞**轮询：直接 ``ReadFile`` 会在"事件不够"
    时阻塞在句柄上，超时逻辑永远轮不到（本轮自测就卡在这里）。
    """
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p]
    handle = kernel32.CreateFileW(fr'\\.\pipe\{pipe_name}', 0xC0000000, 0, None, 3, 0, None)
    if handle in (None, -1, 0xFFFFFFFFFFFFFFFF):
        return []
    kernel32.PeekNamedPipe.restype = ctypes.c_int
    kernel32.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.POINTER(ctypes.c_uint32)]
    kernel32.ReadFile.restype = ctypes.c_int
    kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    events, data = [], b''
    buf = ctypes.create_string_buffer(65536)
    read = ctypes.c_uint32(0)
    avail = ctypes.c_uint32(0)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ok = kernel32.PeekNamedPipe(ctypes.c_void_p(handle), None, 0, None,
                                        ctypes.byref(avail), None)
            if not ok:
                break                       # 服务端已断开
            if avail.value == 0:
                time.sleep(0.05)
                continue
            if not kernel32.ReadFile(ctypes.c_void_p(handle), buf, 65536,
                                     ctypes.byref(read), None):
                break
            if read.value == 0:
                time.sleep(0.05)
                continue
            data += buf.raw[:read.value]
            while b'\n' in data:
                line, data = data.split(b'\n', 1)
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line.decode('ascii')))
                    except Exception:
                        pass
            if len(events) >= want:
                break
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    return events


# ----------------------------------------------------------------------------
# B. 引擎级
# ----------------------------------------------------------------------------
def test_engine_unit():
    print('\n=== B. 引擎级 ===')
    from learn_helper import core_mock
    from learn_helper.config import update_config
    from learn_helper.engine import SolverEngine
    from learn_helper.ipc import Hub

    # B0：新会话要**恢复** config 里记住的网页（配合 select_page 的落盘，
    #     否则用户每次重开界面都得重选一次）
    update_config({'last_page_title': '记住的学习页'})
    hub_restored = Hub()
    check('B0 启动时恢复 config.last_page_title',
          hub_restored.selected_title == '记住的学习页',
          f'selected_title={hub_restored.selected_title!r}')

    # 后面的 B 用例都在"没有记住任何网页"的前提下跑，避免被 B0 的写入影响
    update_config({'last_page_title': ''})
    hub = Hub()
    eng = SolverEngine(hub)
    hub.attach(eng, None)

    ok, msg = eng.set_video_speed('abc')
    check('B1 非法倍速被拒', not ok and '数字' in msg)
    ok, msg = eng.set_video_speed(99)
    check('B2 倍速钳位到 4.0', ok and eng.settings['video_speed'] == 4.0)
    eng.set_video_speed(2.0)
    ok, msg = eng.set_auto_submit(False)
    check('B3 auto_submit 切换', ok and eng.settings['auto_submit'] is False)
    eng.set_auto_submit(True)

    # B3b/B3c：启动自动开浏览器（老 Tk 版 auto_launch_browser_on_start 的行为）
    #   默认必须是**开**（用户打开界面就能用上次的学习页）；
    #   关掉之后 `auto_launch_browser()` 必须**不发任何浏览器指令**（直接返回"跳过"）。
    from learn_helper.config import get_run_cfg
    check('B3b 默认 auto_launch_browser=True', get_run_cfg()['auto_launch_browser'] is True,
          f"got={get_run_cfg()['auto_launch_browser']!r}")
    ok, msg = eng.set_auto_launch_browser(False)
    check('B3c 关掉后 auto_launch_browser() 直接跳过（不拉浏览器）',
          ok and eng.settings['auto_launch_browser'] is False
          and (lambda r: r[0] is True and '跳过' in r[1])(eng.auto_launch_browser()),
          f'settings={eng.settings.get("auto_launch_browser")!r}')
    eng.set_auto_launch_browser(True)
    check('B3d 重新打开后设置写回 config',
          get_run_cfg()['auto_launch_browser'] is True,
          f"got={get_run_cfg()['auto_launch_browser']!r}")

    ok, msg = eng.start()
    check('B4 start(未选页) 返回 False + 提示', not ok and '网页' in msg)
    hub.selected_title = '[请点击右侧刷新选择网页]'
    ok, msg = eng.start()
    check('B5 start(占位标题) 被拒', not ok)

    # 真实浏览器路径是**可选**的：默认不碰 CDP（避免连上残留沙盒浏览器后真的开始刷课，
    # 那会让自测跑成"真任务"并长时间不返回）。要跑就设 LH_VERIFY_BROWSER=1。
    if os.environ.get('LH_VERIFY_BROWSER') == '1':
        hub.selected_title = '__lh-verify-nonexistent-page__'
        ok, msg = eng.start()
        check('B6 start(有选页) 接受指令', ok)
        deadline = time.time() + 90
        while time.time() < deadline and eng.solver_running:
            time.sleep(0.5)
        st = hub.snapshot()['engine']
        check('B7 流程结束后 running=False', st['running'] is False, str(st)[:200])
        check('B8 找不到页面时给出可读错误', bool(st['last_error']),
              f'last_error={st["last_error"][:80]!r}')
        res = eng.diagnose()
        check('B9 diagnose 返回结构化结果', isinstance(res, dict) and 'message' in res,
              str(res)[:150])
    else:
        print('  [skip] B6~B9 真实浏览器路径（设 LH_VERIFY_BROWSER=1 可跑）')
        hub.selected_title = ''
        res = eng.list_pages()
        check('B6 无浏览器时 list_pages 不崩溃', isinstance(res, dict) and 'pages' in res,
              str(res)[:150])

    check('B9b stop 幂等（重复调用不抛）', (lambda: (eng.stop(), eng.stop(), True)[2])())

    mock = core_mock.MockAnswerBackend().start()
    try:
        print(f'  [info] mock 后端起在 {mock.base_url}', flush=True)
        ok_all, lines, results = mock.run_self_test_via(eng)
        for line in lines:
            print(f'         {line}', flush=True)
        for r in results:
            if r.get('detail'):
                print(f'  [info] {r["name"]} detail={r["detail"]}', flush=True)
        check('B10 经 mock 后端自检三题全 pass', ok_all,
              f'{[ (r["name"], r["verdict"]) for r in results ]}')
        check('B11 自检确实打到了 mock /solve',
              len(mock._httpd.seen) >= 3 and 'image' in mock._httpd.seen[0],
              f'solved={len(mock._httpd.seen)}')
    finally:
        mock.stop()


# ----------------------------------------------------------------------------
# C. 纯逻辑
# ----------------------------------------------------------------------------
def test_logic():
    print('\n=== C. 纯逻辑 ===')
    from learn_helper import core
    from learn_helper.config import get_answer_cfg

    ans = core.parse_llm_answer('好的，答案是 {"question_type":"choice","answer_key":"ACD"}', 'choice')
    check('C1 parse_llm_answer 抽 key', ans['answer_key'] == 'ACD'
          and ans['question_type'] == 'multi_choice', str(ans))
    ans = core.parse_llm_answer('答案是 B 选项', 'choice')
    check('C2 parse_llm_answer 兜底抽字母', ans['answer_key'] == 'B', str(ans))
    ans = core.parse_llm_answer('{"text_answers":["12个月"]}', 'blank')
    check('C3 parse_llm_answer 填空', ans['text_answers'] == ['12个月'], str(ans))

    norm = core._normalize_answer({'answer_key': 'A,C'}, 'choice')
    check('C4 normalize 去逗号', norm['answer_key'] == 'AC', str(norm))
    norm = core._normalize_answer({'text_answers': ['春秋', '战国']}, 'blank')
    check('C5 normalize 文本答案', norm['text_answers'] == ['春秋', '战国'], str(norm))

    acfg = get_answer_cfg()
    check('C6 答题配置钳位', 10 <= acfg['solver_timeout'] <= 600
          and 1 <= acfg['workers'] <= 16 and acfg['mode'] in ('server', 'llm', 'off'), str(acfg))


def main():
    seed_config()
    # 输出固定 UTF-8：用例文案里有 ✓/⚠ 这类字符，本机默认控制台是 GBK，
    # 直接 print 会 UnicodeEncodeError 让自测中途崩掉（实测踩过）。
    # 这里自己兜住，避免依赖调用方是否设了 PYTHONIOENCODING。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        test_process_lifecycle()
        test_engine_unit()
        test_logic()
    finally:
        print(f'\n[info] 测试用临时目录: {_TEST_BASE}')
        shutil.rmtree(_TEST_BASE, ignore_errors=True)
    print(f'\n===== 结果: {len(CHECKS) - len(FAILS)}/{len(CHECKS)} 通过 =====')
    if FAILS:
        print('失败项: ' + ', '.join(FAILS))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
