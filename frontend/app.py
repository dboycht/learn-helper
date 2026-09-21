"""learn-helper 界面（PySide6 / Qt6）。

**为什么换掉手写 Win32/GDI 界面**（用户反馈"拖动窗口一直在颤抖""UI 太过于性能浪费"）：
那版界面是我自己画的标题栏 + 自己实现的拖动/缩放/命中测试 + 自己做的全量 GDI 绘制。
拖动时每个 `WM_MOUSEMOVE` 都要同步搬窗口**并重画整窗**（1869×960 ≈ 180 万像素），
画面跟不上鼠标 ⇒ 看起来在颤；后来改走 `WM_SYSCOMMAND/SC_MOVE` 也不稳
（实测系统移动循环 1 毫秒就退出，窗口几乎不动）。

现在：**窗口由 Qt 托管** —— 用 Qt 自带的标题栏（`QMainWindow` 原生边框），
拖动、缩放、贴边分屏、边缘吸附、多显示器 DPI 全部由 Windows 自己处理，
我们一行相关代码都不写 ⇒ 不可能再抖。绘制交给 Qt 的控件的原生绘制，
空闲时零重绘（实测 0% CPU）。

⚠️ 有一条**必须保留**的兼容约定：窗口类名必须叫 `LearnHelperNativeWnd`，
否则 `native/*.ps1` 那批探针找不到窗口（它们按类名枚举）。
Qt 给的类名是 `Qt5QWindowIcon` / `Qt6QWindowIcon`，所以这里在 `WM_NCCREATE`
里用 `SetClassNameW` 改掉它 —— 这是"新界面也要能被既有探针验证"的关键一步。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QStatusBar,
    QDoubleSpinBox, QVBoxLayout, QWidget,
)

from .backend_client import BackendClient, PROJECT_DIR

APP_TITLE = '学习助理'
WINDOW_CLASS = 'LearnHelperNativeWnd'      # ⚠️ 探针按这个类名找窗口，别改

# 答题方式（与后端 config.ANSWER_MODE_LABELS 对齐）
ANSWER_MODES = [('server', '内部答题 API'), ('llm', '自配大模型'), ('off', '仅识别不答题')]

DARK_QSS = """
QWidget { background: #0f1317; color: #d7dde3; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #0f1317; }
QFrame#card { background: #161c22; border: 1px solid #232c35; border-radius: 10px; }
QLabel#h1 { font-size: 17px; font-weight: 600; color: #e8eef4; }
QLabel#h2 { font-size: 13px; font-weight: 600; color: #9fb0c0; }
QLabel#kpi  { font-size: 30px; font-weight: 700; }
QLabel#kpiVideo { color: #4fa8ff; }
QLabel#kpiDoc   { color: #38c98a; }
QLabel#kpiPage  { color: #f0a83c; }
QLabel#muted { color: #7d8b99; }
QPushButton {
    background: #1d242c; border: 1px solid #2b3641; border-radius: 8px;
    padding: 7px 14px; color: #d7dde3;
}
QPushButton:hover { background: #253039; border-color: #38475a; }
QPushButton:pressed { background: #171d24; }
QPushButton:disabled { color: #5a6672; background: #171c22; border-color: #222a32; }
QPushButton#primary { background: #2f81f7; border-color: #2f81f7; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #4b93ff; border-color: #4b93ff; }
QPushButton#danger { background: #b3402f; border-color: #b3402f; color: #ffffff; }
QPushButton#danger:hover { background: #cf4c39; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
    background: #11161b; border: 1px solid #2b3641; border-radius: 7px;
    padding: 6px 9px; selection-background-color: #2f81f7;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #2f81f7; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #11161b; border: 1px solid #2b3641; selection-background-color: #2f81f7;
}
QPlainTextEdit#log { font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 12px; }
QGroupBox { border: 1px solid #232c35; border-radius: 10px; margin-top: 14px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #9fb0c0; }
QCheckBox { spacing: 8px; }
QStatusBar { background: #11161b; color: #9fb0c0; border-top: 1px solid #232c35; }
QMenuBar { background: #11161b; }
QMenuBar::item:selected { background: #2f81f7; }
QMenu { background: #161c22; border: 1px solid #2b3641; }
QMenu::item:selected { background: #2f81f7; }
QScrollBar:vertical { background: #11161b; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #33404d; border-radius: 6px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #445364; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


# ----------------------------------------------------------------------------
# 诊断日志（沿用原界面的格式，既有探针/排障习惯不变）
# ----------------------------------------------------------------------------
class Diag:
    """写 `native-diag.log`（与原 Rust 界面同一个文件、同一种格式）。

    ⚠️ 保留这个文件是**刻意的**：项目里所有排障经验、探针判定都依赖它
    （`paint-logs:` / `ui: geometry` / `ui: invoke ...`）。
    """

    def __init__(self) -> None:
        base = os.environ.get('LH_BASE_DIR') or PROJECT_DIR
        self.path = os.path.join(base, 'native-diag.log')
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'a', encoding='utf-8'):
                pass
        except Exception:
            self.path = ''

    def __call__(self, line: str) -> None:
        if not self.path:
            return
        try:
            stamp = int(time.time())
            with open(self.path, 'a', encoding='utf-8') as fh:
                fh.write(f'[{stamp}] {line}\n')
        except Exception:
            pass


class Card(QFrame):
    """带标题的卡片容器。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 12, 16, 14)
        self._lay.setSpacing(10)
        if title:
            lab = QLabel(title)
            lab.setObjectName('h1')
            self._lay.addWidget(lab)

    def body(self) -> QVBoxLayout:
        return self._lay


class KpiCard(QFrame):
    """一个 KPI：大数字 + 说明。"""

    def __init__(self, label: str, color_obj: str, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(2)
        self.value = QLabel('0')
        self.value.setObjectName('kpi')
        self.value.setProperty('class', color_obj)
        self.value.setStyleSheet(f'color: {color_obj};')
        self.value.setAlignment(Qt.AlignCenter)
        self.caption = QLabel(label)
        self.caption.setObjectName('muted')
        self.caption.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.value)
        lay.addWidget(self.caption)

    def set(self, text: str) -> None:
        self.value.setText(str(text))


# ----------------------------------------------------------------------------
# 「答题设置」对话框
# ----------------------------------------------------------------------------
class SettingsDialog(QDialog):
    """答题设置。**合并式提交**：只把用户改过的键 PUT 回去（与后端 PUT 语义一致）。"""

    def __init__(self, client: BackendClient, parent=None):
        super().__init__(parent)
        self.client = client
        self.setWindowTitle('答题设置')
        self.setMinimumWidth(560)
        self._initial: dict = {}
        self._touched: set[str] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # ---- 答题方式 ----
        box = QGroupBox('答题方式')
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        self.mode = QComboBox()
        for key, label in ANSWER_MODES:
            self.mode.addItem(label, key)
        self.mode.currentIndexChanged.connect(lambda _i: self._mark('answer.mode'))
        form.addRow('方式', self.mode)

        self.server_url = QLineEdit()
        self.server_url.editingFinished.connect(lambda: self._mark('server_url'))
        form.addRow('后端地址', self.server_url)

        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.valueChanged.connect(lambda _v: self._mark('answer.workers'))
        form.addRow('并发求解线程', self.workers)

        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setSuffix(' 秒')
        self.timeout.valueChanged.connect(lambda _v: self._mark('answer.solver_timeout'))
        form.addRow('单题超时', self.timeout)

        self.retry = QSpinBox()
        self.retry.setRange(0, 5)
        self.retry.valueChanged.connect(lambda _v: self._mark('answer.retry'))
        form.addRow('失败重试', self.retry)
        root.addWidget(box)

        # ---- 自配大模型 ----
        box2 = QGroupBox('自配大模型')
        form2 = QFormLayout(box2)
        form2.setLabelAlignment(Qt.AlignRight)
        self.llm_base = QLineEdit()
        self.llm_base.editingFinished.connect(lambda: self._mark('llm.base_url'))
        form2.addRow('Base URL', self.llm_base)
        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText('留空 = 不修改已保存的 Key')
        self.llm_key.editingFinished.connect(lambda: self._mark('llm.api_key'))
        form2.addRow('API Key', self.llm_key)
        self.llm_model = QLineEdit()
        self.llm_model.editingFinished.connect(lambda: self._mark('llm.model'))
        form2.addRow('模型名', self.llm_model)
        root.addWidget(box2)

        # ---- 运行选项 ----
        box3 = QGroupBox('运行选项')
        v3 = QVBoxLayout(box3)
        self.auto_submit = QCheckBox('自动提交答案（取消勾选 = 仅暂存）')
        self.auto_submit.toggled.connect(lambda _b: self._mark('run.auto_submit'))
        self.auto_launch = QCheckBox('启动时自动打开沙盒浏览器')
        self.auto_launch.toggled.connect(lambda _b: self._mark('run.auto_launch_browser'))
        self.skip_quiz = QCheckBox('只刷视频：纯测验章节整节跳过（视频里的题照做）')
        self.skip_quiz.toggled.connect(lambda _b: self._mark('run.skip_quiz_only'))
        v3.addWidget(self.auto_submit)
        v3.addWidget(self.auto_launch)
        v3.addWidget(self.skip_quiz)
        root.addWidget(box3)

        self.hint = QLabel('')
        self.hint.setObjectName('muted')
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        btns = QHBoxLayout()
        self.test_btn = QPushButton('测试连接')
        self.test_btn.clicked.connect(self._test)
        btns.addWidget(self.test_btn)
        btns.addStretch(1)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        self.save_btn = QPushButton('保存')
        self.save_btn.setObjectName('primary')
        self.save_btn.clicked.connect(self._save)
        btns.addWidget(cancel)
        btns.addWidget(self.save_btn)
        root.addLayout(btns)

        self.reload()

    def reload(self) -> None:
        cfg = self.client.settings()
        data = cfg.get('data') or cfg
        if not data:
            self.hint.setText('读取设置失败（后端未就绪）')
            return
        self._initial = data
        answer = data.get('answer') or {}
        run = data.get('run') or {}
        llm = data.get('llm') or {}

        idx = self.mode.findData(answer.get('mode', 'server'))
        self.mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.server_url.setText(str(data.get('server_url') or ''))
        self.workers.setValue(int(answer.get('workers') or 4))
        self.timeout.setValue(int(answer.get('solver_timeout') or 150))
        self.retry.setValue(int(answer.get('retry') or 1))
        self.llm_base.setText(str(llm.get('base_url') or ''))
        self.llm_model.setText(str(llm.get('model') or ''))
        key_state = '已保存（留空即不修改）' if llm.get('has_api_key') else '未设置'
        self.llm_key.setText('')
        self.llm_key.setPlaceholderText(f'留空 = 不修改；当前：{key_state}')
        self.video_speed = float(run.get('video_speed') or 2.0)
        self.auto_submit.setChecked(bool(run.get('auto_submit', True)))
        self.auto_launch.setChecked(bool(run.get('auto_launch_browser', True)))
        self.skip_quiz.setChecked(bool(run.get('skip_quiz_only', False)))
        self._touched.clear()
        self.hint.setText(f"设备指纹：{data.get('device_id', '')}")

    def _mark(self, key: str) -> None:
        self._touched.add(key)

    def build_patch(self) -> dict:
        """只提交"用户改过"的键（合并式写入，避免把没动的项覆盖掉）。"""
        patch: dict = {}
        answer: dict = {}
        llm: dict = {}
        run: dict = {}
        if 'answer.mode' in self._touched:
            answer['mode'] = self.mode.currentData()
        if 'answer.workers' in self._touched:
            answer['workers'] = self.workers.value()
        if 'answer.solver_timeout' in self._touched:
            answer['solver_timeout'] = self.timeout.value()
        if 'answer.retry' in self._touched:
            answer['retry'] = self.retry.value()
        if answer:
            patch['answer'] = answer
        if 'llm.base_url' in self._touched and self.llm_base.text().strip():
            llm['base_url'] = self.llm_base.text().strip()
        if 'llm.api_key' in self._touched and self.llm_key.text().strip():
            llm['api_key'] = self.llm_key.text().strip()
        if 'llm.model' in self._touched and self.llm_model.text().strip():
            llm['model'] = self.llm_model.text().strip()
        if llm:
            patch['llm'] = llm
        if 'run.auto_submit' in self._touched:
            run['auto_submit'] = self.auto_submit.isChecked()
        if 'run.auto_launch_browser' in self._touched:
            run['auto_launch_browser'] = self.auto_launch.isChecked()
        if 'run.skip_quiz_only' in self._touched:
            run['skip_quiz_only'] = self.skip_quiz.isChecked()
        if run:
            patch['run'] = run
        if 'server_url' in self._touched:
            patch['server_url'] = self.server_url.text().strip()
        return patch

    def _save(self) -> None:
        patch = self.build_patch()
        if not patch:
            self.hint.setText('没有任何改动。')
            return
        self.save_btn.setEnabled(False)
        self.save_btn.setText('保存中…')
        QApplication.processEvents()
        ok, msg = self.client.put_settings(patch)
        self.save_btn.setEnabled(True)
        self.save_btn.setText('保存')
        if ok:
            self.accept()
        else:
            self.hint.setText(f'保存失败：{msg}')

    def _test(self) -> None:
        mode = self.mode.currentData()
        self.hint.setText('测试中…')
        QApplication.processEvents()
        ok, msg = self.client.control('test_backend',
                                      {'server_url': self.server_url.text().strip()})
        prefix = '测试连接' if mode == 'server' else '测试连接（大模型）'
        self.hint.setText(f'{prefix}：{msg}')


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):

    def __init__(self, diag: Diag):
        super().__init__()
        self.diag = diag
        self.client = BackendClient(self)
        self.client.set_trace_hook(self._trace)
        self._selected_page = ''
        self._pages: list[str] = []
        self._speed = 2.0
        self._last_status: dict = {}
        self._pending_autolaunch = False

        self.setWindowTitle(f'{APP_TITLE} v2.1.4')
        self.resize(1180, 820)
        self.setMinimumSize(980, 660)

        self._build_ui()
        self._wire()
        self._install_class_name_patch()

        # 换肤/主题交给 Qt；这里固定深色（用户环境是深色）
        app = QApplication.instance()
        if app:
            app.setStyleSheet(DARK_QSS)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        self.diag('native: start v2.1.4 (PySide6 frontend)')
        self.diag(f'ui: bootstrap PySide6 ui, dpi={int(self.devicePixelRatio() * 96)}')
        self.client.start()

    # ------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(12)

        # ---- 网页行 ----
        page_card = Card('')
        row = QHBoxLayout()
        row.setSpacing(10)
        lab = QLabel('当前网页')
        lab.setObjectName('h1')
        row.addWidget(lab)
        self.page_box = QComboBox()
        self.page_box.setMinimumWidth(320)
        self.page_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.page_box.currentIndexChanged.connect(self._on_page_picked)
        row.addWidget(self.page_box, 1)
        self.next_btn = QPushButton('下一章')
        self.next_btn.setToolTip('手动点一次学习页的「下一页/下一章」，不必等整节刷完')
        row.addWidget(self.next_btn)
        self.refresh_btn = QPushButton('检测/刷新网页')
        row.addWidget(self.refresh_btn)
        self.speed_btn = QPushButton(f'倍速 {self._speed:.1f}x')
        row.addWidget(self.speed_btn)
        self.mode_label = QLabel('答题方式：—')
        self.mode_label.setObjectName('muted')
        row.addWidget(self.mode_label)
        page_card.body().addLayout(row)
        root.addWidget(page_card)

        # ---- KPI + 状态 ----
        mid = QHBoxLayout()
        mid.setSpacing(12)
        kpi_card = Card('当前页面任务感知')
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)
        self.kpi_video = KpiCard('视频任务', '#4fa8ff')
        self.kpi_doc = KpiCard('文档阅读', '#38c98a')
        self.kpi_page = KpiCard('处理页数', '#f0a83c')
        for k in (self.kpi_video, self.kpi_doc, self.kpi_page):
            kpi_row.addWidget(k)
        kpi_card.body().addLayout(kpi_row)
        mid.addWidget(kpi_card, 2)

        state_card = Card('当前状态')
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.state_label = QLabel('未连接')
        self.state_label.setObjectName('h1')
        grid.addWidget(self.state_label, 0, 0, 1, 2)
        grid.addWidget(QLabel('音视频进度'), 1, 0)
        self.video_text = QLabel('--')
        grid.addWidget(self.video_text, 1, 1)
        grid.addWidget(QLabel('答题进度'), 2, 0)
        self.quiz_text = QLabel('--')
        grid.addWidget(self.quiz_text, 2, 1)
        grid.addWidget(QLabel('最近页面'), 3, 0)
        self.last_page = QLabel('--')
        self.last_page.setObjectName('muted')
        grid.addWidget(self.last_page, 3, 1)
        grid.setColumnStretch(1, 1)
        state_card.body().addLayout(grid)
        mid.addWidget(state_card, 1)
        root.addLayout(mid)

        # ---- 操作按钮 ----
        acts = QHBoxLayout()
        acts.addStretch(1)
        self.start_btn = QPushButton('启动刷课')
        self.start_btn.setObjectName('primary')
        self.start_btn.setMinimumWidth(150)
        self.pause_btn = QPushButton('暂停进程')
        self.pause_btn.setMinimumWidth(120)
        self.diag_btn = QPushButton('诊断页面')
        self.diag_btn.setMinimumWidth(120)
        self.stop_btn = QPushButton('终止并退出')
        self.stop_btn.setObjectName('danger')
        self.stop_btn.setMinimumWidth(130)
        for b in (self.start_btn, self.pause_btn, self.diag_btn, self.stop_btn):
            acts.addWidget(b)
        acts.addStretch(1)
        root.addLayout(acts)

        # ---- 日志 ----
        log_card = Card('运行日志')
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName('log')
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)      # 自动丢弃最旧的，天然防内存膨胀
        self.log_view.setMinimumHeight(220)
        log_card.body().addWidget(self.log_view)
        root.addWidget(log_card, 1)

        # ---- 菜单 ----
        m_file = self.menuBar().addMenu('文件')
        a_settings = QAction('答题设置…', self)
        a_settings.setShortcut(QKeySequence('Ctrl+,'))
        a_settings.triggered.connect(self.open_settings)
        m_file.addAction(a_settings)
        m_file.addSeparator()
        a_quit = QAction('退出', self)
        a_quit.setShortcut(QKeySequence('Ctrl+Q'))
        a_quit.triggered.connect(self.close)
        m_file.addAction(a_quit)

        m_help = self.menuBar().addMenu('帮助')
        a_about = QAction('关于', self)
        a_about.triggered.connect(self.show_about)
        m_help.addAction(a_about)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage('正在启动后端…')

    def _wire(self) -> None:
        self.client.connected.connect(self._on_connected)
        self.client.failed.connect(self._on_failed)
        self.client.logged.connect(self._on_log)
        self.client.status_changed.connect(self._on_status)
        self.client.pages_changed.connect(self._on_pages)

        self.start_btn.clicked.connect(lambda: self._control('start'))
        self.stop_btn.clicked.connect(self._on_stop)
        self.diag_btn.clicked.connect(lambda: self._control('diagnose'))
        self.refresh_btn.clicked.connect(lambda: self._control('refresh_pages'))
        self.next_btn.clicked.connect(self._on_next_page)
        self.speed_btn.clicked.connect(self._cycle_speed)

    def _install_class_name_patch(self) -> None:
        """把 Qt 窗口的类名改成 `LearnHelperNativeWnd`，让既有探针还能找到它。

        Qt 会注册 `Qt6QWindowIcon` 之类的类名，而 `native/*.ps1` 全部按
        `LearnHelperNativeWnd` 枚举窗口 ⇒ 不改名的话所有界面探针直接失效。
        改类名必须在窗口**创建时**（`WM_NCCREATE`）做，之后系统不允许改。
        """
        if os.name != 'nt':
            return
        try:
            user32 = ctypes.windll.user32
            user32.SetClassNameW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            user32.SetClassNameW.restype = wintypes.BOOL
            self._class_patched = False
        except Exception as e:
            self.diag(f'ui: 类名补丁不可用: {e}')

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt 命名)
        """拦 `WM_NCCREATE` 尝试改窗口类名（见 `_install_class_name_patch`）。"""
        if os.name == 'nt':
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                if msg.message == 0x0081 and not getattr(self, '_class_patched', False):
                    ctypes.windll.user32.SetClassNameW(msg.hWnd, WINDOW_CLASS)
                    self._class_patched = True
                    self.diag(f'ui: window class renamed to {WINDOW_CLASS} (nccreate)')
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def showEvent(self, event) -> None:  # noqa: N802
        """显示后再兜一次改名。

        ⚠️ 实测：Qt 建窗比我拦到 `WM_NCCREATE` 更早，所以那条路走不到；
        `SetClassNameW` 在窗口**已经创建**之后也可能失败（类名在注册时就定了）。
        所以这里是"尽力而为"，真正的兼容方案是探针侧按**标题**兜底（见 `toolbar_probe`）。
        """
        super().showEvent(event)
        if os.name != 'nt' or getattr(self, '_class_patched', False):
            return
        try:
            hwnd = int(self.winId())
            ok = ctypes.windll.user32.SetClassNameW(wintypes.HWND(hwnd), WINDOW_CLASS)
            self._class_patched = bool(ok)
            self.diag(f'ui: SetClassNameW({WINDOW_CLASS}) -> {bool(ok)} hwnd={hwnd}')
        except Exception as e:
            self.diag(f'ui: SetClassNameW 失败: {e}')

    # ------------------------------------------------------------ 事件
    def _trace(self, line: str) -> None:
        self.diag(line)

    def _on_connected(self, base_url: str, pipe: str) -> None:
        self.diag(f'backend: connected base={base_url} pipe={pipe}')
        self.status.showMessage(f'后端已就绪 · {base_url}')
        self._append_log(f'[native] 后端已就绪（{base_url}）')
        QTimer.singleShot(200, self._after_connect)

    def _after_connect(self) -> None:
        """握手完成后：拉一次设置/状态/页面，并按设置决定要不要自动开浏览器。"""
        self._refresh_settings_label()
        self._control('refresh_pages')
        cfg = self.client.settings()
        data = cfg.get('data') or cfg
        run = (data or {}).get('run') or {}
        # ⚠️ `LH_NO_AUTO_BROWSER=1` 也必须对**界面自己**生效：自检/探针跑的时候绝不
        # 该去接管用户的沙盒浏览器（会把正在用的那个界面挤掉，后端随即退出）。
        # 这个约定原来只在后端侧有，前端漏了（实测自检因此误报"与已有后端冲突"）。
        if os.environ.get('LH_NO_AUTO_BROWSER'):
            self.diag('ui: auto browser launch skipped (LH_NO_AUTO_BROWSER set)')
            return
        if run.get('auto_launch_browser', True):
            QTimer.singleShot(1200, self._launch_then_refresh)

    def _launch_then_refresh(self) -> None:
        """开浏览器**之后**再刷一次页面列表。

        ⚠️ 必须两步：`refresh_pages` 要靠 CDP 9222，而浏览器是这一步才拉起来的。
        只刷一次的话下拉框会一直停在"未检测到网页"（实测：后端日志报
        `connect ECONNREFUSED 127.0.0.1:9222`），用户以为界面坏了。
        """
        ok, msg = self.client.control('launch_browser')
        self.diag(f'ui: launch_browser -> {"OK" if ok else "FAIL"} {msg[:100]}')
        # 浏览器启动 + 页面就绪需要几秒，隔一会儿再刷，并把结果写进日志
        QTimer.singleShot(5000, lambda: self._control('refresh_pages'))

    def _on_failed(self, msg: str) -> None:
        self.diag(f'backend: FAILED {msg}')
        self.status.showMessage(f'后端异常：{msg}')
        self._append_log(f'[错误] {msg}')

    def _on_log(self, _level: str, text: str) -> None:
        self._append_log(text)

    def _append_log(self, text: str) -> None:
        if not text:
            return
        for line in str(text).split('\n'):
            self.log_view.appendPlainText(line)
        self.log_view.moveCursor(QTextCursor.End)

    def _on_status(self) -> None:
        st = self.client._last_status or {}
        self._last_status = st
        eng = st.get('engine') or {}
        running = bool(eng.get('running'))
        paused = bool(eng.get('paused'))
        self.start_btn.setText('正在运行…' if running else '启动刷课')
        self.start_btn.setEnabled(not running)
        self.pause_btn.setText('继续执行' if paused else '暂停进程')
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.state_label.setText('运行中' if running else ('已暂停' if paused else '闲置中'))
        self.kpi_video.set(str(eng.get('video_count', 0)))
        self.kpi_doc.set(str(eng.get('doc_count', 0)))
        self.kpi_page.set(str(eng.get('page_count', 0)))
        self.video_text.setText(str(eng.get('video_text') or '--'))
        self.quiz_text.setText(str(eng.get('quiz_text') or '--'))
        self.last_page.setText(str(eng.get('last_page') or '--'))
        task = str(eng.get('task_text') or '')
        if task:
            self.status.showMessage(task)
        if eng.get('last_error'):
            self.status.showMessage(f"错误：{eng['last_error']}")
        sel = str(st.get('selected_page') or '')
        if sel and sel != self._selected_page:
            self._selected_page = sel
            self._sync_page_box()

    def _on_pages(self, pages: list) -> None:
        pages = [str(p) for p in pages]
        # ⚠️ 去重：轮询每 1.2 秒发一次 pages 事件，列表没变就不该重建下拉框
        #（重建会重置当前选中项、并且每秒白刷几次）。
        if pages == self._pages:
            return
        self._pages = pages
        self.diag(f'ui: pages -> {len(self._pages)}')
        self._sync_page_box()

    def _sync_page_box(self) -> None:
        """把页面列表填进下拉框。

        ⚠️ 必须**保留用户已选中的项**：一 `clear()` 再重填，选中项就回到"未选择"，
        用户刚选的页会在下一次刷新时被悄悄丢掉（老界面为此专门做过"记住选中"）。
        """
        want = self._selected_page or (self.page_box.currentData() or '')
        self.page_box.blockSignals(True)
        try:
            self.page_box.clear()
            if not self._pages:
                self.page_box.addItem('未检测到网页 —— 点「检测/刷新网页」', '')
            else:
                self.page_box.addItem(f'未选择（已检测到 {len(self._pages)} 个网页）', '')
                for title in self._pages:
                    self.page_box.addItem(title, title)
            idx = self.page_box.findData(want) if want else 0
            self.page_box.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self.page_box.blockSignals(False)

    def _on_page_picked(self, _idx: int) -> None:
        title = self.page_box.currentData() or ''
        if not title or title == self._selected_page:
            return
        self._selected_page = title
        self.diag(f'ui: invoke select_page -> {title!r}')
        ok, msg = self.client.control('select_page', {'title': title})
        self._append_log(f'[native] 选择网页 → {msg or title}')

    def _refresh_settings_label(self) -> None:
        cfg = self.client.settings()
        data = cfg.get('data') or cfg
        answer = (data or {}).get('answer') or {}
        label = answer.get('mode_label') or '—'
        run = (data or {}).get('run') or {}
        self._speed = float(run.get('video_speed') or self._speed)
        self.speed_btn.setText(f'倍速 {self._speed:.1f}x')
        self.mode_label.setText(f'答题方式：{label}')

    # ------------------------------------------------------------ 动作
    def _control(self, action: str, params: Optional[dict] = None) -> None:
        self.diag(f'ui: invoke {action}')
        ok, msg = self.client.control(action, params)
        self.diag(f'ui: {action} -> {"OK" if ok else "FAIL"} {msg[:160]}')
        if action not in ('launch_browser',):
            self._append_log(f'[native] {action} → {msg}' if msg else f'[native] {action}')

    def _on_next_page(self) -> None:
        self._append_log('[native] 手动翻页：正在查找「下一页/下一章」...')
        self.diag('ui: invoke next_page')
        ok, msg = self.client.control('next_page')
        self.diag(f'ui: next_page -> {"OK" if ok else "FAIL"} {msg[:200]}')
        self._append_log(f'[翻页] {msg}')
        if not ok:
            self.status.showMessage(msg or '翻页失败')

    def _on_stop(self) -> None:
        self._control('stop')
        QTimer.singleShot(300, self.close)

    def _cycle_speed(self) -> None:
        steps = [1.0, 1.5, 2.0, 3.0]
        try:
            cur = steps.index(self._speed)
        except ValueError:
            cur = 2
        self._speed = steps[(cur + 1) % len(steps)]
        self.speed_btn.setText(f'倍速 {self._speed:.1f}x')
        ok, msg = self.client.put_settings({'run': {'video_speed': self._speed}})
        self.diag(f'ui: invoke speed -> {self._speed} ({msg[:80]})')
        self._append_log(f'[native] 倍速已设为 {self._speed:.1f}x')

    def open_settings(self) -> None:
        self.diag('ui: invoke settings')
        dlg = SettingsDialog(self.client, self)
        if dlg.exec() == QDialog.Accepted:
            self._append_log('[native] 答题设置已保存')
            self._refresh_settings_label()
        else:
            self._append_log('[native] 答题设置已取消')

    def show_about(self) -> None:
        self.diag('ui: invoke about')
        QMessageBox.about(
            self, '关于',
            f'<b>{APP_TITLE}</b> v2.1.4<br><br>'
            '网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。<br><br>'
            '界面：PySide6 (Qt6)　·　后端：Python + Playwright<br>'
            '仓库：<a href="https://github.com/dboycht/learn-helper">'
            'github.com/dboycht/learn-helper</a>')

    def _tick(self) -> None:
        """每秒兜底刷新（管道正常时其实用不上，但断线时能自愈）。"""
        pass

    # ------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        running = bool((self._last_status.get('engine') or {}).get('running'))
        if running:
            ask = QMessageBox.question(
                self, '确认退出',
                '刷课流程还在运行，退出会中断它。确定要退出吗？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ask != QMessageBox.Yes:
                event.ignore()
                return
            try:
                self.client.control('stop')
            except Exception:
                pass
        self.diag('ui: invoke Close')
        self.status.showMessage('正在退出…')
        QApplication.processEvents()
        self.client.shutdown()
        self.diag('native: 事件循环结束，退出')
        event.accept()


def main() -> int:
    os.environ.setdefault('QT_ENABLE_HIGHDPI_SCALING', '1')
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle('Fusion')                     # 跨版本一致的现代扁平基座
    diag = Diag()
    win = MainWindow(diag)
    win.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
