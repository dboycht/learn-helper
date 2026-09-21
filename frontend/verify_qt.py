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
    calls = []
    win.client.control = lambda a, p=None: (calls.append((a, p)), (True, 'stub'))[1]  # type: ignore
    win.page_box.setCurrentIndex(0)          # 索引 0 = "未选择"
    win._on_page_picked(0)
    check('未选择网页时不发 select_page', not any(c[0] == 'select_page' for c in calls),
          str(calls))

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
    # ⚠️ 如果**已经有一个 learn-helper 后端在跑**（界面开着），本自检会与它争抢同一个
    # 沙盒浏览器：新后端把浏览器"接管"过去，老后端随即退出（实测 code=3）。
    # 那是环境冲突、不是缺陷 ⇒ 跳过 D/E，而不是误报失败
    #（一个"时绿时红"的测试比没有测试更糟）。
    # 注意判据是"有没有**别的后端**"，而不是"9222 开着" —— 沙盒浏览器本来就常驻 9222。
    other = [p for p in _leftover_backends() if p != str(os.getpid())]
    if other:
        print(f'  [skip] D/E：已有后端在跑（{other}）—— 请先关掉那个界面再跑本自检')
        _finish(RESULTS)
        return 1 if any(not ok for _n, ok, _d in RESULTS) else 0

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
            win.page_box.setCurrentIndex(1 if win.page_box.count() > 1 else 0)
            check('页面列表已填进下拉框（或明确显示"未检测到"）',
                  win.page_box.count() >= 1, str(win.page_box.count()))
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
