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

    # B3e/B3f：只刷视频（开启后，只有题目、没有视频/文档的章节整节跳过）
    check('B3e 默认 skip_quiz_only=False（不改变现有行为）',
          get_run_cfg()['skip_quiz_only'] is False,
          f"got={get_run_cfg()['skip_quiz_only']!r}")
    ok, msg = eng.set_skip_quiz_only(True)
    check('B3f skip_quiz_only 开启并写回 config',
          ok and eng.settings['skip_quiz_only'] is True
          and get_run_cfg()['skip_quiz_only'] is True,
          f'settings={eng.settings.get("skip_quiz_only")!r} cfg={get_run_cfg()["skip_quiz_only"]!r}')
    eng.set_skip_quiz_only(False)

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
        ok_all, lines, results = mock.run_self_test_via()
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
# D. 界面新增的「下一章」按钮所依赖的控制动作
# ----------------------------------------------------------------------------
def test_next_page_action():
    """`next_page` 动作必须存在、可达、并且**如实**回答（找不到按钮就明说）。

    这个动作是给界面「下一章」按钮用的：它让用户不必等整节刷完就能验证翻页链路
    （翻页曾经坏了一整个版本，见 ERROR.md E69）。这里不依赖真实课程页 ——
    只要后端把动作接对了，无论有没有浏览器，回答都应该是"那几句可读的话"之一，
    而不是「未知动作」。
    """
    print('\n=== D. next_page 控制动作 ===')
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['LH_BASE_DIR'] = _TEST_BASE
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND_DIR, 'main.py'), '--port', '0'],
        cwd=PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE, env=env, text=True, encoding='utf-8', errors='replace')
    try:
        hs = read_handshake(proc)
        if 'learn-helper-backend-ready' not in hs:
            check('D1 next_page：后端可启动', False, hs[:120])
            return
        check('D1 next_page：后端可启动', True)
        base = f'http://127.0.0.1:{json.loads(hs)["port"]}'

        # 先确认"未知动作"确实会被拒 —— 否则下面的断言可能永远通过（假绿）
        r0 = http_post(f'{base}/api/control', json={'action': 'no_such_action_xyz'},
                       timeout=15)
        j0 = r0.json()
        check('D2 未知动作被明确拒绝（对照，证明 D3 不是假绿）',
              (not j0.get('ok')) and ('未知' in str(j0.get('message', ''))),
              str(j0)[:140])

        r = http_post(f'{base}/api/control', json={'action': 'next_page'},
                      timeout=120)
        j = r.json()
        msg = str(j.get('message') or '')
        print(f'  [info] next_page -> ok={j.get("ok")} msg={msg[:120]}')
        # 四种合法回答：翻过去了 / 点了但标题没变 / 没有按钮 / 连不上浏览器
        ok_shapes = ('已翻页' in msg) or ('已点击' in msg) or ('没有找到' in msg) or ('无法连接' in msg)
        check('D3 next_page 被路由到引擎并如实回答', ok_shapes, msg[:140])
        check('D4 next_page 不是未知动作', '未知' not in msg, msg[:140])
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=25)
        except Exception:
            proc.kill()
            proc.wait(timeout=10)


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

    # C12：两个通道对"同一份数据"必须给出一致结论（E73）。
    #   选择题但只有 text_answers（模型常见偏差）时，`/solve` 通道能救回来，
    #   `parse_llm_answer` 原来把非空内容丢掉 ⇒ 该题静默跳过。
    ans = core.parse_llm_answer('{"question_type":"choice","text_answers":["B"]}', 'choice')
    check('C12 parse_llm_answer 保留选择题的 text_answers（与 server 通道一致）',
          ans['text_answers'] == ['B'], str(ans))
    srv = core._normalize_answer({'question_type': 'choice', 'text_answers': ['B']}, 'choice')
    check('C12b 两个通道结论一致', srv['text_answers'] == ans['text_answers'],
          f'llm={ans} server={srv}')

    # C13：「仅识别不答题」下跑自检**不得发起任何求解请求**（E73）。
    #   原来写成"不是 server 就走大模型" ⇒ 拿空 key 请求 3 次全失败，
    #   汇总成"接口或地址不可用"，把用户引去查后端地址。
    ok_all, lines, results = core.run_solve_self_test(mode='off')
    check('C13 mode=off 自检不发请求且说明原因',
          ok_all is False and results and results[0].get('verdict') == 'mode_not_solvable'
          and '仅识别' in (results[0].get('detail') or ''),
          f'verdict={results[0].get("verdict") if results else None}')
    check('C13b mode=off 自检只产出 1 条结论（没有逐题请求）', len(results) == 1,
          f'results={len(results)}')

    acfg = get_answer_cfg()
    check('C6 答题配置钳位', 10 <= acfg['solver_timeout'] <= 600
          and 1 <= acfg['workers'] <= 16 and acfg['mode'] in ('server', 'llm', 'off'), str(acfg))

    # C9/C10：发布包瘦身相关（2026-09-19，见 ERROR.md E68）
    #   C9  node 探测：发布包**刻意不带** Playwright 自带的 88 MB node.exe，改用本机
    #       Node（`PLAYWRIGHT_NODEJS_PATH`）。探测逻辑错了 ⇒ 发布版一启动刷课就报
    #       "浏览器挂载失败"，而冻结成 exe 后看不到 traceback，所以必须在这里钉住。
    #   C10 配置扩展名必须能被 JSON 解析回来（`.bad` 备份/原子替换的配套）。
    from learn_helper import core as _core

    real_env = os.environ.pop('PLAYWRIGHT_NODEJS_PATH', None)
    real_node = os.environ.pop('LH_NODE_PATH', None)
    try:
        # 显式 override 优先，且必须是**存在的文件**才被采纳
        os.environ['LH_NODE_PATH'] = sys.executable
        check('C9 LH_NODE_PATH 覆盖生效', _core._find_system_node() == sys.executable,
              f'got={_core._find_system_node()!r}')
        # 指向不存在的路径时不能瞎认，必须回落（本机有 Node 就返回它，否则 None）
        os.environ['LH_NODE_PATH'] = r'C:\definitely\not\here\node.exe'
        fallback = _core._find_system_node()
        check('C9b 无效路径不被采纳（回落或 None）',
              fallback is None or os.path.isfile(fallback), f'got={fallback!r}')
        # 环境里已有 PLAYWRIGHT_NODEJS_PATH 时，ensure_node_available 不得覆盖它
        os.environ.pop('LH_NODE_PATH', None)
        os.environ['PLAYWRIGHT_NODEJS_PATH'] = r'C:\already\set\node.exe'
        _core._NODE_RESOLVED = False
        _core.ensure_node_available()
        check('C9c 已显式指定的 PLAYWRIGHT_NODEJS_PATH 不被覆盖',
              os.environ.get('PLAYWRIGHT_NODEJS_PATH') == r'C:\already\set\node.exe',
              f'got={os.environ.get("PLAYWRIGHT_NODEJS_PATH")!r}')
    finally:
        os.environ.pop('LH_NODE_PATH', None)
        os.environ.pop('PLAYWRIGHT_NODEJS_PATH', None)
        if real_env is not None:
            os.environ['PLAYWRIGHT_NODEJS_PATH'] = real_env
        if real_node is not None:
            os.environ['LH_NODE_PATH'] = real_node
        _core._NODE_RESOLVED = False

    # C10：正常写入**不该**产生 .bad 备份（.bad 只在"读到坏文件"时出现）
    from learn_helper import config as _cfgmod
    _CP = _cfgmod.CONFIG_PATH
    _bad = _CP + '.bad'
    if os.path.exists(_bad):
        os.remove(_bad)
    _cfgmod.update_config({'healthcheck': 'ok'})
    check('C10 正常写入不产生 .bad 备份',
          os.path.exists(_CP) and not os.path.exists(_bad),
          f'bad_exists={os.path.exists(_bad)}')

    # C11 ⭐⭐：**不许给 Playwright 的 keyword-only 方法传位置参数**
    #   教训（2026-09-19，用户报"无法自动切换页面"）：整套刷课里**所有点击**都写成
    #   `el.click(True, force=True)`，而 `Locator.click` 的签名是 `click(self, *, ...)`
    #   —— 多出来的位置参数直接抛
    #   `TypeError: click() takes 1 positional argument but 2 positional arguments
    #   (and 1 keyword-only argument) were given`。于是**翻页、切卡片、提交、暂存、
    #   点选项全部静默失败**（各自被 try/except 吞掉，只留一行"翻页受阻"）。
    #   这个 bug 从 2.1.1 重构起就在，而"没有真实浏览器"的自测照不到它 ⇒
    #   在源码层面直接扫一遍是最便宜、最可靠的回归（不需要浏览器、不需要网络）。
    _scan_ok = True
    try:
        import inspect as _inspect

        from playwright.sync_api import ElementHandle as _EH
        from playwright.sync_api import Frame as _FR
        from playwright.sync_api import Locator as _LOC
        from playwright.sync_api import Page as _PG

        _kwonly = set()
        for _cls in (_LOC, _PG, _FR, _EH):
            for _name, _member in _inspect.getmembers(_cls):
                if _name.startswith('_') or not callable(_member):
                    continue
                try:
                    _params = list(_inspect.signature(_member).parameters.values())[1:]
                except (TypeError, ValueError):
                    continue
                if _params and not any(
                    p.kind in (_inspect.Parameter.POSITIONAL_ONLY,
                               _inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    for p in _params
                ):
                    _kwonly.add(_name)
    except Exception as e:
        _scan_ok = False
        check('C11 扫描 Playwright keyword-only 调用', False, f'取签名失败: {e}')

    if _scan_ok:
        import re as _re
        _bad_calls = []
        _lh_dir = os.path.join(BACKEND_DIR, 'learn_helper')
        for _fname in sorted(os.listdir(_lh_dir)):
            if not _fname.endswith('.py'):
                continue
            with open(os.path.join(_lh_dir, _fname), encoding='utf-8') as _fh:
                for _lineno, _line in enumerate(_fh, 1):
                    for _m in _re.finditer(r'\.([a-z_]+)\(([^)]*)\)', _line):
                        _meth, _args = _m.group(1), _m.group(2).strip()
                        if _meth not in _kwonly or not _args:
                            continue
                        # 取第一个参数；以 `name=` 形式出现的就是关键字参数（合法）
                        _depth, _first = 0, ''
                        for _ch in _args:
                            if _ch in '([{':
                                _depth += 1
                            elif _ch in ')]}':
                                _depth -= 1
                            elif _ch == ',' and _depth == 0:
                                break
                            _first += _ch
                        _first = _first.strip()
                        if _first and not _re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=', _first):
                            _bad_calls.append(f'{_fname}:{_lineno} .{_meth}({_first})')
        check('C11 无位置参数传给 Playwright keyword-only 方法（click 等）',
              not _bad_calls, '; '.join(_bad_calls[:5]))

    # C7/C8：config.json 的写入健壮性（2026-09-19 修，见 ERROR.md E60）
    #   C7 想证明的是**落盘方式**本身：旧实现 `open(path,'w')` 先截断再 dump，
    #      中途失败就留下半截文件；`save_config` 的原子替换无论成功失败都不该留半截。
    #      （故意不用 update_config 单独做这件事 —— 把新 config.json 内容整段替换后
    #        写回是一个"合法写入"，那样测不出原子性，只能测出它真的写了。）
    #   C8 想证明的是**并发安全**：界面线程与引擎线程会同时改不同键，一个都不能丢。
    import json as _json
    import tempfile
    import threading

    from learn_helper.config import CONFIG_PATH, load_config, update_config

    original_bytes = b''
    had_config = os.path.exists(CONFIG_PATH)
    if had_config:
        with open(CONFIG_PATH, 'rb') as f:
            original_bytes = f.read()
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            _json.dump({'server_url': 'http://keep.me', 'llm': {'api_key': 'sk-x'}}, f)
        # 模拟"写到一半失败"：目标文件先被写成半截，再让 save_config 正常写入。
        # 期望：坏文件被**备份**成 config.json.bad（保住现场），然后重建一份可用配置。
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write('{')
        update_config({'run': {'video_speed': 3.0}})
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = f.read()
        try:
            parsed = _json.loads(raw)
        except Exception:
            parsed = None
        check('C7 半截 config 被重建且原文件备份为 .bad（原子写不留半截）',
              isinstance(parsed, dict) and parsed.get('run', {}).get('video_speed') == 3.0
              and os.path.exists(CONFIG_PATH + '.bad'),
              f'raw={raw[:80]!r} bad={os.path.exists(CONFIG_PATH + ".bad")}')
        check('C7b 写入后不留 .tmp 残留',
              not os.path.exists(CONFIG_PATH + '.tmp'))

        # 备份目录里那份必须是"坏掉的原文"，而不是新内容（否则现场就丢了）
        with open(CONFIG_PATH + '.bad', 'r', encoding='utf-8') as f:
            check('C7c .bad 里保存的是坏掉的原文件', f.read().strip() == '{')

        # C8：并发合并写入。先写回一份**正常**配置（C7 已经把坏文件挪走并重建了），
        # 然后再让多线程同时改**不同**的键 —— 一个都不能丢。
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            _json.dump({'server_url': 'http://keep.me', 'llm': {'api_key': 'sk-x'}}, f)
        lost = []
        errs = []

        def _writer(n):
            try:
                for _ in range(25):
                    update_config({f't{n}': n})
            except Exception as exc:          # pragma: no cover - 失败即断言失败
                errs.append(repr(exc))

        threads = [threading.Thread(target=_writer, args=(n,)) for n in range(1, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        final = load_config()
        lost = [f't{n}' for n in range(1, 7) if final.get(f't{n}') != n]
        check('C8 并发合并写入不丢键（界面线程 vs 引擎线程）',
              not lost and not errs
              and final.get('llm', {}).get('api_key') == 'sk-x'
              and final.get('server_url') == 'http://keep.me',
              f'lost={lost} errs={errs[:2]} keys={sorted(final)[:10]}')
    finally:
        # 恢复测试前的 config（自测目录本来也是临时的，这里只是保持"自测不留副作用"）
        if had_config:
            with open(CONFIG_PATH, 'wb') as f:
                f.write(original_bytes)


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
        test_next_page_action()
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
