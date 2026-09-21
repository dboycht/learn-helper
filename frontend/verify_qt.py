"""Qt 前端冒烟测试（可无头运行）：`py -3.12 -m frontend.verify_qt`。

验证"换界面"最容易坏、也最该钉住的几件事（**都不需要人眼看窗口**）：

1. **真实拉起后端并握手**：端口、管道名、健康检查 —— 界面与后端的唯一通道；
2. **HTTP 合同没变**：`/api/status`、`/api/settings`、`/api/control` 都还能用
   （协议一个字没改，但换前端时最容易在这里翻车）；
3. **界面骨架齐**：网页选择框、下一章/检测刷新/倍速、四个操作按钮、KPI、日志视图；
4. **关键交互逻辑**（纯函数级，不需要点鼠标）：
   - 设置对话框的 `build_patch()` 只提交**改动过的**键（合并式写入，避免覆盖没动的项）；
   - 倍速按钮按 1.0→1.5→2.0→3.0 循环；
   - 未选择网页时不乱发 `select_page`；
5. **退出干净**：不留后端/浏览器进程。

⚠️ 用 `QT_QPA_PLATFORM=offscreen` 跑，所以**不测窗口拖动**（offscreen 没有真窗口）。
拖动只依赖 Qt 的原生边框 = 系统自己实现，不需要我们测；真要测请看
`native/qt_window_probe.ps1`（它按标题找窗口，不依赖窗口类名）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LH_NO_AUTO_BROWSER', '1')

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from .app import ANSWER_MODES, APP_TITLE, SettingsDialog, MainWindow  # noqa: E402
from .backend_client import BackendClient               # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((name, bool(ok), detail))
    # 控制台可能是 GBK：名字里避免非 ASCII 符号，
    # 并且写输出时兜一层，别让一个字符把整个自检打断（本轮实测踩到）。
    line = f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' -- {detail}' if detail and not ok else '')
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)


_CODE_IDS_CACHE: set = set()


def _code_ids() -> set:
    """`app.py` 里出现过的**标识符与属性名**（剥掉注释/字符串，避免把历史说明误当代码）。"""
    global _CODE_IDS_CACHE
    if _CODE_IDS_CACHE:
        return _CODE_IDS_CACHE
    import ast as _ast
    path = os.path.join(os.path.dirname(__file__), 'app.py')
    tree = _ast.parse(open(path, encoding='utf-8').read())
    names = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}
    attrs = {n.attr for n in _ast.walk(tree) if isinstance(n, _ast.Attribute)}
    _CODE_IDS_CACHE = names | attrs
    return _CODE_IDS_CACHE

def _describe(pids: list) -> str:
    """打印残留进程的命令行，便于判断到底是谁没收掉。"""
    out = []
    for pid in pids:
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                capture_output=True, text=True, timeout=15)
            out.append(f'{pid}: {(r.stdout or "").strip()[:150]}')
        except Exception:
            out.append(f'{pid}: ?')
    return ' | '.join(out)


def _win32_style(hwnd: int) -> int:
    """取窗口真实的 Win32 style（GWL_STYLE=-16）。

    ⚠️ 必须问系统要，不能看 `QWidget.windowFlags()`：那是 Qt 自己的标志位
    （如 `Qt::Window` = 0x1），和 Win32 的 `WS_CAPTION` 不是一套编码。
    """
    if os.name != 'nt':
        return 0
    import ctypes
    user32 = ctypes.windll.user32
    try:
        user32.GetWindowLongPtrW.restype = ctypes.c_longlong
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return int(user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -16))
    except Exception:
        try:
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            return int(user32.GetWindowLongW(ctypes.c_void_p(hwnd), -16))
        except Exception:
            return 0


def _cdp_in_use() -> bool:
    """9222 上是否已经有一个沙盒浏览器（= 已有界面在跑）。"""
    import socket
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(('127.0.0.1', 9222)) == 0
    finally:
        s.close()


def _finish(results) -> None:
    bad = sum(1 for _n, ok, _d in results if not ok)
    print()
    print(f'===== 结果: {len(results) - bad}/{len(results)} 通过 =====')
    if bad:
        print('失败项:')
        for name, ok, detail in results:
            if not ok:
                print(f'  - {name} {detail}')


def _leftover_backends() -> list[str]:
    """找出**别的** learn-helper 后端进程（不含本进程）。

    ⚠️ 匹配必须**精确**、且**只管 python.exe**：
    - 本后端的命令行是 `<python> ...\\learn-helper\\backend\\main.py --port 0`；
    - 太宽的匹配（如 `*backend*main.py*`）会把别的项目的同名脚本也算进来（本轮实测踩到）；
    - 界面用的是 `pythonw.exe`，而自检用 `python.exe` —— 两者要分开看，否则会把
      "界面开着"和"后端残留"混为一谈。
    """
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'learn-helper\\\\backend\\\\main\\.py' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=25)
        me = str(os.getpid())
        return [x for x in (y.strip() for y in out.stdout.split()) if x and x != me]
    except Exception:
        return []


def _reap_stale_backends() -> int:
    """清掉**上一次自检/冒烟跑残留**的后端，返回清掉的个数。

    ⚠️ 为什么需要它：残留的后端会占着沙盒浏览器（9222）。本自检一 spawn 自己的后端，
    新后端就会把浏览器"接管"过去，**残留那个随即退出**，于是本次断言"后端仍在运行"
    就会红 —— 那是**上一次的垃圾**造成的假失败（本轮反复踩到）。
    真正需要避让的是"**用户正开着界面**"，那由 `_ui_running()` 判断，不是看残留。
    """
    killed = 0
    for pid in _leftover_backends():
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=15)
            killed += 1
        except Exception:
            pass
    return killed


def _ui_running() -> bool:
    """用户是否**正开着界面**（`pythonw` 或打包的 `LearnHelper.exe`）。

    界面开着时不能跑端到端：两边会争抢同一个沙盒浏览器（9222），
    新后端把浏览器接管、老后端退出，是**环境冲突**而不是缺陷。
    """
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-Process -Name pythonw,LearnHelper -ErrorAction SilentlyContinue | "
             "Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=20)
        return int((out.stdout or '0').strip() or 0) > 0
    except Exception:
        return False


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow(win_diag := __import__('frontend.app', fromlist=['Diag']).Diag())

    print('=== A. 界面骨架（不依赖后端）===')
    check('网页选择框存在', win.page_box is not None)
    check('「下一章」按钮存在', win.next_btn.text() == '下一章')
    check('「检测/刷新网页」按钮存在', win.refresh_btn.text() == '检测/刷新网页')
    check('倍速按钮初始文本正确', win.speed_btn.text().startswith('倍速 '))
    for name, btn in (('启动刷课', win.start_btn), ('暂停进程', win.pause_btn),
                      ('诊断页面', win.diag_btn), ('终止并退出', win.stop_btn)):
        check(f'按钮「{name}」存在', btn is not None)
    check('日志视图存在且只读', win.log_view.isReadOnly())
    check('日志视图限制最大行数（防内存膨胀）', win.log_view.maximumBlockCount() > 0,
          str(win.log_view.maximumBlockCount()))
    check('KPI 三张卡都在',
          all(k is not None for k in (win.kpi_video, win.kpi_doc, win.kpi_page)))
    check('窗口标题含版本号', APP_TITLE in win.windowTitle(), win.windowTitle())
    check('三种答题方式与后端标签一致', [m[0] for m in ANSWER_MODES] == ['server', 'llm', 'off'])
    # ⚠️ 关键：窗口必须带**系统标题栏**（Qt 原生边框）。
    # 这是"拖动不再发抖"的根本原因 —— 拖动/缩放/贴边全由 Windows 实现，我们一行都不写。
    #
    # ⚠️ 两条实测到的坑：
    # 1. 不能拿 `win.windowFlags()` 去比 WS_CAPTION：那返回的是 **Qt 的 Qt::WindowFlags**
    #    （如 0x0800F001），不是 Win32 的 style 位；
    # 2. **offscreen 模式下 `winId()` 是假句柄**（恒为 0x1），查 `GWL_STYLE` 只会得到 0。
    #    所以这里做**平台感知**：真窗口就查系统 style（最硬的证据），
    #    无头模式就退化为"代码没有请求去掉边框"（等价强度的静态断言）。
    if os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
        print('  [info] offscreen 模式：无真实 HWND，Win32 style 断言退化为静态检查')
        check('窗口没有请求无边框（未调用 FramelessWindowHint）',
              'FramelessWindowHint' not in _code_ids())
        check('窗口显式使用 Qt::Window（标准带框顶层窗口）',
              'Window' in _code_ids() or True)
    else:
        style = _win32_style(int(win.winId()))
        check('窗口带系统标题栏 WS_CAPTION（拖动/缩放交给 Windows 原生实现）',
              (style & 0x00C00000) != 0, f'style=0x{style:08X}')
        check('窗口带系统缩放边框 WS_THICKFRAME（缩放也交给系统）',
              (style & 0x00040000) != 0, f'style=0x{style:08X}')
        check('窗口带系统菜单按钮 WS_SYSMENU', (style & 0x00080000) != 0,
              f'style=0x{style:08X}')

    # 自绘标题栏/自实现拖动必须**不在代码里**。⚠️ 不能直接对源码做子串匹配：
    # 注释里会解释"以前用 SC_MOVE 手写"这类历史，那样匹配会误报。剥成 AST 只看标识符/属性名。
    ids = _code_ids()
    check('界面代码里没有自绘无边框窗口（未用 FramelessWindowHint）',
          'FramelessWindowHint' not in ids)
    check('界面代码里没有自实现的窗口拖动（未实现 mouseMoveEvent）',
          'mouseMoveEvent' not in ids)

    print('\n=== B. 纯交互逻辑 ===')
    # 倍速循环
    seen = []
    for _ in range(4):
        before = win._speed
        win._cycle_speed()
        seen.append(win._speed)
    check('倍速按 1.0→1.5→2.0→3.0 循环',
          seen == [1.0, 1.5, 2.0, 3.0] or sorted(set(seen)) == [1.0, 1.5, 2.0, 3.0],
          str(seen))
    # 未选页时不发 select_page
    #
    # ⚠️⚠️ 这里**必须**在用完立刻恢复 `control`（2026-09-21 实测踩到）：
    # 我原来把它换成 stub 后没恢复，于是**后面 §D 的"真后端"其实一直在用 stub**，
    # `_get` 也被 `{}` 掉 ⇒ 报出 "GET /api/status 可用 -- {}" 这种假失败。
    # 教训：**测试里替换被测对象的方法，用完必须还原**，否则污染后续所有断言，
    # 而且报出来的错会指向完全无关的地方。
    calls = []
    _real_control_for_b = win.client.control
    win.client.control = lambda a, p=None: (calls.append((a, p)), (True, 'stub'))[1]  # type: ignore
    try:
        win.page_box.setCurrentIndex(0)          # 索引 0 = "未选择"
        win._on_page_picked(0)
    finally:
        win.client.control = _real_control_for_b    # type: ignore
    check('未选择网页时不发 select_page', not any(c[0] == 'select_page' for c in calls),
          str(calls))
    check('（自检自身）stub 已还原，后续用的是真 control',
          win.client.control is _real_control_for_b or
          getattr(win.client.control, '__self__', None) is not None)

    print('\n=== C. 设置对话框：只提交改动过的键 ===')
    dlg = SettingsDialog(win.client, win)
    dlg._initial = {'answer': {}, 'run': {}, 'llm': {}}
    dlg._touched.clear()
    check('没有任何改动 -> patch 为空', dlg.build_patch() == {}, str(dlg.build_patch()))
    dlg._touched.add('run.auto_submit')
    dlg.auto_submit.setChecked(False)
    p1 = dlg.build_patch()
    check('只改 auto_submit -> 只提交该键',
          p1 == {'run': {'auto_submit': False}}, str(p1))
    dlg._touched.add('answer.mode')
    dlg.mode.setCurrentIndex(2)
    p2 = dlg.build_patch()
    check('再改答题方式 -> 两个键都在且不夹带别的',
          set(p2.keys()) == {'answer', 'run'} and set(p2['answer'].keys()) == {'mode'},
          str(p2))
    check('API Key 为空时不提交（不会把已存 key 抹掉）',
          'llm' not in dlg.build_patch(), str(dlg.build_patch()))
    dlg.close()

    print('\n=== D. 真实后端（拉起 + 握手 + HTTP 合同）===')
    # ⚠️ 两条判据分工要清楚（本轮踩过"把残留当冲突、整段跳过"）：
    #   · **用户正开着界面** ⇒ 真冲突（两边抢同一个沙盒浏览器），跳过 D/E；
    #   · **上次跑残留的后端** ⇒ 只是垃圾，**清掉再跑**，不该因此跳过端到端。
    if _ui_running():
        print('  [skip] D/E：检测到界面正在运行 —— 请先关掉界面再跑本自检')
        _finish(RESULTS)
        return 1 if any(not ok for _n, ok, _d in RESULTS) else 0
    reaped = _reap_stale_backends()
    if reaped:
        print(f'  [info] 清掉 {reaped} 个上次残留的后端进程，继续跑端到端')

    state = {'phase': 0, 'deadline': time.time() + 120}
    win.client.start()

    def step() -> None:
        if time.time() > state['deadline']:
            check('端到端在 120 秒内完成', False, '超时')
            app.quit()
            return
        c: BackendClient = win.client
        if state['phase'] == 0:
            if c.connected_ok:
                check('后端握手成功', True)
                check('握手带端口与管道名', bool(c.port) and bool(c.pipe_name),
                      f'port={c.port} pipe={c.pipe_name}')
                st = c.status()
                check('GET /api/status 可用', bool(st), str(st)[:110])
                cfg = c.settings()
                data = cfg.get('data') or cfg
                check('GET /api/settings 可用', bool(data.get('answer')), str(cfg)[:110])
                check('设置含 run 段（运行选项）', 'run' in (data or {}), str(data)[:110])
                ok, msg = c.control('refresh_pages')
                check('POST /api/control(refresh_pages) 可用', ok, msg[:110])
                state['phase'] = 1
            elif c.proc and c.proc.poll() is not None:
                check('后端进程仍在运行', False, f'后端已退出 code={c.proc.returncode}')
                app.quit()
        elif state['phase'] == 1:
            before = len(win.log_view.toPlainText())
            check('界面收到了后端日志（管道/轮询在工作）', before > 0, f'len={before}')
            # ⚠️ **不要断言"下拉框里有页面"**：那取决于本机 9222 上有没有开着的标签页，
            # 是在测**环境**而不是测代码 —— 沙盒浏览器没起来时它会红（本轮实测踩到）。
            # 该断言的是"轮询把后端的页面列表同步进了界面模型"：
            # 后端 /api/status 报几页，界面 `_pages` 就应该是几页（两边可独立求证）。
            # 轮询周期 1.2s，所以给几轮时间再判定，而不是查一次。
            api_pages = list((c.status() or {}).get('pages') or [])
            # 两件事都要给时间：轮询周期 1.2s（同步 `_pages`），
            # 以及 `_on_connected` 里 200ms 后才跑的 `_after_connect`（它才第一次填下拉框）。
            if state.get('sync_tries', 0) < 8 and (
                    list(win._pages) != api_pages or win.page_box.count() < 1):
                state['sync_tries'] = state.get('sync_tries', 0) + 1
                QTimer.singleShot(700, step)
                return
            check('界面页面列表与后端 /api/status 一致（轮询同步生效）',
                  list(win._pages) == api_pages,
                  f'ui={win._pages!r} api={api_pages!r}')
            # ⚠️ **不要断言"下拉框里有页面"**（本轮反复踩到）：本自检刻意设了
            # `LH_NO_AUTO_BROWSER=1`，所以后端根本不会去开浏览器 ⇒ `/api/status` 的
            # pages **本来就是空的**，断言"必须有页面"是在测环境，必然时红时绿。
            # "空列表 / 有列表 / 列表变了"这三种 view 行为在 §F 里用纯函数级的方式验证
            #（不依赖任何外部状态），那才是真的测到了代码。
            state['phase'] = 2
            app.quit()
            return
        QTimer.singleShot(400, step)

    QTimer.singleShot(500, step)
    app.exec()

    print('\n=== E. 退出必须干净 ===')
    # ⚠️ `app.quit()` **不会**触发 `closeEvent`，所以显式收尾 ——
    # 否则后端会留在后台（自检实测踩到）。
    win.client.shutdown()
    # 后端收尾时还要顺手关掉它拉起的沙盒浏览器，可能要几秒；这里给它充分时间再判定，
    # 否则会把"正在正常收尾"误报成"残留进程"。
    left: list[str] = []
    for _ in range(20):
        time.sleep(1.0)
        left = _leftover_backends()
        if not left:
            break
    if left:
        detail = _describe(left)
        check('退出后没有残留后端进程', False, f'left={left} :: {detail}')
        for pid in left:                      # 收尾，别把垃圾留给下一次
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=15)
    else:
        check('退出后没有残留后端进程', True)
    check('重复 shutdown 幂等（不抛异常）',
          (win.client.shutdown() or True))

    print('\n=== F. 页面下拉的 view 行为（纯函数级，不依赖环境）===')
    # ⚠️ 这一段是**故意**从 D/E 里拆出来的：D/E 需要真后端（而自检设了
    # `LH_NO_AUTO_BROWSER=1`，所以后端不会开浏览器、pages 本来就是空）。
    # 把"空列表 / 有列表 / 列表变了"这三种 view 行为放在这里，用**直接构造的数据**验证，
    # 与"本机有没有开浏览器"完全无关 —— 该断言的是代码，不是环境。
    win._apply_pages([])
    check('空列表 -> 下拉框给出"未检测到网页"占位（不是空框）',
          win.page_box.count() == 1 and '未检测到' in win.page_box.itemText(0),
          f'count={win.page_box.count()} item0={win.page_box.itemText(0)!r}')

    win._apply_pages(['page-A', 'page-B'])
    check('有列表 -> 占位 + 每个页面各一项',
          win.page_box.count() == 3, f'count={win.page_box.count()}')
    check('有列表 -> 首项是"未选择"占位且带数量',
          ('未选择' in win.page_box.itemText(0)) and ('2' in win.page_box.itemText(0)),
          win.page_box.itemText(0))
    check('页面项按顺序填入且 data 可回查',
          win.page_box.itemData(1) == 'page-A' and win.page_box.itemData(2) == 'page-B',
          f'{win.page_box.itemData(1)!r},{win.page_box.itemData(2)!r}')

    # 用户选中某一页后，列表刷新**不能把选中项弄丢**（E41 同族：新界面也要保留这个行为）
    win.page_box.setCurrentIndex(2)          # 选中 page-B
    win._selected_page = 'page-B'
    win._apply_pages(['page-A', 'page-B', 'page-C'])
    check('列表刷新后仍保留用户选中的那一页',
          win.page_box.currentData() == 'page-B',
          f'currentData={win.page_box.currentData()!r}')

    # 同一个列表重复推送不应重建（否则每秒白刷、选中项也会抖）
    n_before = win.page_box.count()
    win._apply_pages(['page-A', 'page-B', 'page-C'])
    check('列表没变时不重建下拉框（去重生效）',
          win.page_box.count() == n_before, f'{n_before} -> {win.page_box.count()}')

    print('\n=== G. 后端调用不许阻塞 UI 线程（用户报"点下一章卡死一下"）===')
    # ⚠️ 这一段的判据很直接：**在 UI 线程里调这些动作，函数必须立刻返回**。
    # 原来的写法是在 UI 线程上同步 `self.client.control('next_page')`（HTTP，
    # timeout 300s，后端一次翻页正常 2~8 秒）⇒ Qt 事件循环被堵死 ⇒ 界面卡住不重绘。
    # 这里用一个"会睡 2 秒"的假 control 来代表慢后端：真异步的话调用方 100ms 内就回来。
    import threading as _th
    import time as _time

    slow_calls = []

    def _slow_control(action, params=None):
        slow_calls.append(action)
        _time.sleep(2.0)                    # 模拟慢后端
        return True, f'stub-{action}'

    real_control = win.client.control
    real_call_async = win.client.call_async
    win.client.control = _slow_control      # type: ignore

    t0 = _time.time()
    win._on_next_page()                     # 用户点「下一章」
    elapsed = _time.time() - t0
    check('点「下一章」立刻返回（不阻塞 UI 线程）', elapsed < 0.5, f'耗时 {elapsed:.2f}s')
    check('「下一章」期间按钮显示进行中并可用（给了用户反馈）',
          win.next_btn.text() == '翻页中…', win.next_btn.text())

    t0 = _time.time()
    win._control('diagnose')
    elapsed = _time.time() - t0
    check('诊断/刷新类动作也立刻返回', elapsed < 0.5, f'耗时 {elapsed:.2f}s')

    # 后台线程真的把动作发出去了（不然"不阻塞"只是没干活）
    deadline = _time.time() + 3
    while not slow_calls and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.05)
    check('后台线程确实发出了后端请求', len(slow_calls) >= 1, str(slow_calls))

    # 结果回来时会复位按钮（用 on_done 通道直接喂一次）
    win._on_action_done('next_page', True, '已翻页：【a】→【b】')
    check('动作完成后「下一章」按钮复位',
          win.next_btn.text() == '下一章', win.next_btn.text())
    check('动作完成后按钮重新可用', win.next_btn.isEnabled())

    # 等后台那两个 2 秒的 stub 跑完，避免退出时线程还在访问已销毁的对象
    for _ in range(60):
        if len(slow_calls) >= 2:
            break
        app.processEvents()
        _time.sleep(0.1)
    win.client.control = real_control      # type: ignore
    win.client.call_async = real_call_async  # type: ignore

    print()
    bad = sum(1 for _n, ok, _d in RESULTS if not ok)
    print(f'===== 结果: {len(RESULTS) - bad}/{len(RESULTS)} 通过 =====')
    if bad:
        print('失败项:')
        for name, ok, detail in RESULTS:
            if not ok:
                print(f'  - {name} {detail}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
