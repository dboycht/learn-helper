"""learn-helper 界面（PySide6 / Qt6）。

**为什么换掉手写 Win32/GDI 界面**（用户反馈"拖动窗口一直在颤抖""UI 太过于性能浪费"）：
那版界面是我自己画的标题栏 + 自己实现的拖动/缩放/命中测试 + 自己做的全量 GDI 绘制。
拖动时每个 `WM_MOUSEMOVE` 都要同步搬窗口**并重画整窗**（1869×960 ≈ 180 万像素），
画面跟不上鼠标 ⇒ 看起来在颤；后来改走 `WM_SYSCOMMAND/SC_MOVE` 也不稳
（实测系统移动循环 1 毫秒就退出，窗口几乎不动）。

现在：**窗口由 Qt 托管** —— 用 Qt 自带的标题栏（`QMainWindow` 原生边框），
拖动、缩放、贴边分屏、边缘吸附、多显示器 DPI 全部由 Windows 自己处理，
我们一行相关代码都不写 ⇒ 不可能再抖。绘制交给 Qt 控件的原生绘制，
空闲时几乎不耗 CPU（实测 0.31%/10s）。

⚠️ **窗口类名改不掉（试过，不行）**：Qt 给的类名是 `Qt6100QWindowIcon`，而
`SetClassNameW` **在 user32 的导入表里根本不存在**（只有 MSDN 文档里有；实测调用报
"function 'SetClassNameW' not found"），所以既有的 `native/*.ps1` 探针（按类名枚举窗口）
**无法直接复用**。那些探针测的是 Rust 实现细节（自绘标题栏、自绘下拉窗口、自绘滚动条），
在 Qt 结构下已无意义；本前端的验证改由 `frontend/verify_qt.py` 承担
（Qt 可被内省，断言比"戳像素"更硬）。`native/` 与 Rust 探针保留作退路。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence, QPainter,
                           QPixmap, QTextCursor)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QStatusBar, QSystemTrayIcon, QVBoxLayout, QWidget,
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
    位置由 `backend_client._app_dir()` 决定 —— **冻结时必须落在 exe 旁边**，
    不能落在 `_MEIPASS` 临时目录（那里会被删掉，日志当场消失，实测踩到）。
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


def _make_tray_pixmap(size: int = 64) -> QPixmap:
    """现画一个托盘图标（圆角蓝底 + 白色对勾），不依赖任何外部文件。

    ⚠️ 用**代码画**而不是读 `logo.ico`：打包成单文件后资源路径会变，
    读文件最容易变成"托盘一片空白"，而画出来的永远在。
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor('#2f81f7'))
        p.setPen(Qt.NoPen)
        r = size * 0.18
        p.drawRoundedRect(int(size * 0.06), int(size * 0.06),
                          int(size * 0.88), int(size * 0.88), r, r)
        # 对勾
        pen = p.pen()
        pen.setColor(QColor('#ffffff'))
        pen.setWidthF(size * 0.11)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([
            _pt(size, 0.26, 0.54), _pt(size, 0.44, 0.71),
            _pt(size, 0.74, 0.33),
        ])
    finally:
        p.end()
    return pm


def _pt(size: int, fx: float, fy: float):
    from PySide6.QtCore import QPointF
    return QPointF(size * fx, size * fy)


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

        # ⚠️ `settings_read` 是**全局广播**信号，主窗口也连着它。对话框只在
        # "这次读取是我发起的" 时才吃这份数据，否则会拿别人的结果覆盖自己的表单。
        self._awaiting_settings = False
        self.client.settings_read.connect(self._maybe_apply_settings)

        # ⚠️ 打开对话框时**不要**同步读设置：`/api/settings` 是 HTTP（timeout 8s），
        # 后端慢的时候会连"对话框都弹不出来"。先让窗口显示出来，数据异步填。
        self._awaiting_settings = True
        self.client.read_settings_async()
        self.hint.setText('正在读取设置…')

    def _maybe_apply_settings(self, data: dict) -> None:
        if not self._awaiting_settings:
            return
        self._awaiting_settings = False
        self._on_settings_loaded(data)

    def _on_settings_loaded(self, data: dict) -> None:
        """异步拿到的设置快照填进表单（**在 UI 线程**）。"""
        if not data:
            self.hint.setText('读取设置失败（后端未就绪）')
            return
        self.apply(data)

    def apply(self, data: dict) -> None:
        """把一份设置快照填进表单。

        ⚠️ 这里**没有** `reload()` 这种"同步读设置"的方法：读设置是 HTTP，
        UI 线程上同步等会冻界面（本轮把唯一一处删掉了；自检要填表就直接调
        `apply({...})`，既更快也不依赖后端）。
        """
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
        # ⚠️ 异步保存：`put_settings` 是 HTTP，不能堵 UI 线程（否则对话框也"卡死一下"）
        self.client.call_async_result(
            'settings_save', patch, on_done=self._on_saved)

    def _on_saved(self, ok: bool, msg: str) -> None:
        self.save_btn.setEnabled(True)
        self.save_btn.setText('保存')
        if ok:
            self.accept()
        else:
            self.hint.setText(f'保存失败：{msg}')

    def _test(self) -> None:
        mode = self.mode.currentData()
        self.hint.setText('测试中…')
        self.test_btn.setEnabled(False)
        self.client.call_async(
            'test_backend', {'server_url': self.server_url.text().strip()},
            on_done=lambda ok, msg: self._on_tested(mode, ok, msg))

    def _on_tested(self, mode, ok: bool, msg: str) -> None:
        self.test_btn.setEnabled(True)
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
        self._pages_synced = False      # view 是否已按 `_pages` 同步过（见 _apply_pages）
        # 正在跑的后端动作（用于"忙"状态显示；见 _set_busy）
        self._busy_actions: set[str] = set()
        self._busy_prev = ''
        self._speed = 2.0
        self._last_status: dict = {}
        self._pending_autolaunch = False
        self.tray: Optional[QSystemTrayIcon] = None

        self.setWindowTitle(f'{APP_TITLE} v2.1.4')
        self.resize(1180, 820)
        self.setMinimumSize(980, 660)

        self._build_ui()
        self._wire()
        self._install_class_name_patch()
        self._build_tray()
        self._quitting = False

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
        self.client.action_done.connect(self._on_action_done)
        self.client.settings_read.connect(self._on_settings_read)

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
        self._control('refresh_pages')
        # 设置要异步取（`/api/settings` 也是个 HTTP 请求，后端卡住时一样会堵 UI 线程）
        self.client.read_settings_async()

    def _on_settings_read(self, data: dict) -> None:
        """拿到设置后：更新界面上的"倍速/答题方式"，并按需自动开浏览器。"""
        answer = (data or {}).get('answer') or {}
        run = (data or {}).get('run') or {}
        if answer.get('mode_label'):
            self.mode_label.setText(f"答题方式：{answer['mode_label']}")
        if run.get('video_speed'):
            self._speed = float(run['video_speed'])
            self.speed_btn.setText(f'倍速 {self._speed:.1f}x')
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
        ⚠️ 必须**异步**：`launch_browser` 要拉起整个浏览器（实测 4~8 秒），
        同步调用会把启动阶段的界面冻住 —— 这也是"卡死一下"的来源之一。
        ⚠️ 而且**不加忙状态**：这是启动时的自动动作，禁用"检测/刷新网页"按钮会让界面
        一开始就像坏的（用户还没做任何操作，按钮却是灰的）。
        """
        self.client.call_async('launch_browser', None, on_done=self._on_launch_done)

    def _on_launch_done(self, ok: bool, msg: str) -> None:
        # ⚠️ 这里**必须**清忙状态：`launch_browser` 走的是 `on_done` 回调通道，
        # 不会发 `action_done` 信号 ⇒ 如果只在 `_on_action_done` 里清，
        # 「检测/刷新网页」按钮会**一直保持禁用**（实测踩到）。
        self._set_busy('launch_browser', False)
        self.diag(f'ui: launch_browser done -> {"OK" if ok else "FAIL"} {msg[:80]}')
        self._append_log(f'[浏览器] {msg}' if msg else '[浏览器] 已启动')
        # 浏览器启动 + 页面就绪需要几秒，之后**连刷几次**把页面列表补上。
        # ⚠️ 实测只刷一次不够：浏览器"已打开"到标签页真正可枚举之间有窗口期，
        # 那一次可能仍然拿到空列表（后端报 ECONNREFUSED / 0 个标签页），
        # 于是下拉框一直停在"未检测到网页"，用户以为坏了。
        for delay in (4000, 8000, 14000):
            QTimer.singleShot(delay, self._refresh_if_no_pages)

    def _refresh_if_no_pages(self) -> None:
        """页面列表还是空就再刷一次（有列表就不再打扰后端）。"""
        if self._pages:
            return
        self.diag('ui: 页面列表仍为空 -> 再刷一次')
        self._control('refresh_pages')

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
        """管道推来的页面列表（走统一入口）。"""
        self._apply_pages(pages)

    def _apply_pages(self, pages: list) -> None:
        """**唯一**更新页面列表的入口：先更新 model，再同步 view。

        ⚠️ 这里修过两个"下拉框空着"的真 bug（2026-09-21）：
        1. `_poll_loop` 原来**直接写 `self._pages`**，于是随后管道推来的 `pages` 事件
           被判成"没变化"直接 return ⇒ **view 永远没被同步**，下拉框一直空白。
        2. 去重只比了 model：`_pages` 初值就是 `[]`，所以**第一次**推来空列表时
           被判成"没变化"⇒ 连"未检测到网页"这个占位都不会填，用户看到一个**空框**。
        **判据：去重必须同时考虑"view 是否已经同步过"**，否则 model 与 view 会脱节。
        """
        pages = [str(p) for p in pages]
        # 去重：轮询每 1.2 秒发一次，列表没变且 view 已同步过就不重建
        #（重建会重置当前选中项、并且每秒白刷几次）。
        if pages == self._pages and self._pages_synced:
            return
        self._pages = pages
        self._pages_synced = True
        self.diag(f'ui: pages -> {len(self._pages)}')
        self._sync_page_box()

    def _sync_pages_from_status(self, st: dict) -> None:
        """从 HTTP 状态里取页面列表（轮询兜底）。走同一个入口。"""
        self._apply_pages(st.get('pages') or [])

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
        # `select_page` 在后端也要连 CDP 落盘，属于慢调用 ⇒ 走异步（别堵 UI 线程）
        self._control('select_page', {'title': title}, busy_text=f'正在选择网页：{title}')

    def _refresh_settings_label(self) -> None:
        """重新读设置并刷新"倍速/答题方式"标签。**异步**（读设置也是 HTTP）。"""
        self.client.read_settings_async()

    # ------------------------------------------------------------ 动作
    #
    # ⚠️⚠️ **所有后端调用一律异步**（用户报"点「下一章」经常卡死一下"，2026-09-21）。
    # 原来这里是 `ok, msg = self.client.control(action)` —— 在 UI 线程上同步等 HTTP。
    # `next_page` 在后端要"连 CDP → 找按钮 → 点击 → 轮询最多 6s 等确认弹窗 → 等标题变化"，
    # 正常 2~8 秒，而 `control` 的 timeout 是 **300 秒**；这段时间 Qt 事件循环被完全堵住
    # ⇒ 不重绘、按钮点不动、拖动无反应。现在只投递请求，结果通过 `action_done` 回来。
    def _control(self, action: str, params: Optional[dict] = None,
                 busy_text: str = '') -> None:
        self._set_busy(action, True, busy_text)
        self.client.call_async(action, params)

    def _set_busy(self, action: str, busy: bool, text: str = '') -> None:
        """把"某个动作正在跑"体现在界面上（按钮禁用 + 状态栏提示）。

        没有这个的话，动作期间界面虽然不卡了，但用户会以为"点了没反应"。
        """
        self._busy_actions.add(action) if busy else self._busy_actions.discard(action)
        busy_now = bool(self._busy_actions)
        # 会阻塞的操作期间禁用对应按钮，避免重复提交
        for ctl_action, btn in (('next_page', self.next_btn),
                                ('refresh_pages', self.refresh_btn),
                                ('diagnose', self.diag_btn),
                                ('launch_browser', self.refresh_btn)):
            if ctl_action == action:
                btn.setEnabled(not busy)
        if busy:
            self._busy_prev = self.status.currentMessage()
            self.next_btn.setText('翻页中…' if action == 'next_page' else self.next_btn.text())
            self.status.showMessage(text or f'正在执行 {action} …')
        else:
            if self.next_btn.text() == '翻页中…':
                self.next_btn.setText('下一章')
            if not busy_now and getattr(self, '_busy_prev', ''):
                self.status.showMessage(self._busy_prev)
                self._busy_prev = ''

    def _on_action_done(self, action: str, ok: bool, msg: str) -> None:
        """后台动作回来了（**在 UI 线程**）。所有界面更新都只在这里做。"""
        self._set_busy(action, False)
        if action == 'next_page':
            self._append_log(f'[翻页] {msg}')
            if not ok:
                self.status.showMessage(msg or '翻页失败')
            return
        if action in ('launch_browser',):
            # 自动开浏览器是"后台动作"：只记日志，不打扰用户（沿用原设计）
            return
        if action == 'select_page':
            self._append_log(f'[native] 选择网页 → {msg}')
            return
        if msg:
            self._append_log(f'[native] {action} → {msg}')
        else:
            self._append_log(f'[native] {action}')

    def _on_next_page(self) -> None:
        self._append_log('[native] 手动翻页：正在查找「下一页/下一章」...')
        self._control('next_page', busy_text='正在翻页（查找「下一页/下一章」并处理确认弹窗）…')

    def _request_stop(self, wait_seconds: float = 0.0) -> bool:
        """请求后端停止，返回"停止是否已完成"。

        ⚠️ 同样不许在 UI 线程上"同步等 HTTP"：原来的写法是
        `ok, msg = self.client.control('stop')`，而 `stop` 在后端会去杀它拉起的
        沙盒浏览器，慢的时候要好几秒 ⇒ 点「终止并退出」界面也会僵住。
        现在：后台发请求；**只有调用方明确需要"停干净再继续"时才等**，
        而且是**有上限的等**（`QApplication.processEvents()` 让界面继续活着），
        等不到就照常往下走 —— 绝不无限期冻住界面。
        """
        done = threading.Event()
        box: dict = {}

        def _cb(ok: bool, msg: str) -> None:
            box['ok'], box['msg'] = ok, msg
            done.set()

        self.diag('ui: invoke stop')
        self.client.call_async('stop', None, on_done=_cb)
        if wait_seconds <= 0:
            return False
        deadline = time.time() + wait_seconds
        while not done.is_set() and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
        ok = bool(box.get('ok'))
        msg = str(box.get('msg') or '')
        if not done.is_set():
            self.diag(f'ui: stop 等待 {wait_seconds}s 未完成，继续退出')
            self._append_log('[native] stop 未在预期时间内完成，继续退出')
            return False
        self.diag(f'ui: stop -> {"OK" if ok else "FAIL"} {msg[:120]}')
        self._append_log(f'[native] stop → {msg}' if msg else '[native] stop')
        return ok

    def _on_stop(self) -> None:
        # 「终止并退出」：给它最多 8 秒停干净（要杀沙盒浏览器），等不到也照常关窗。
        self._request_stop(wait_seconds=8.0)
        QTimer.singleShot(200, self.close)

    def _cycle_speed(self) -> None:
        steps = [1.0, 1.5, 2.0, 3.0]
        try:
            cur = steps.index(self._speed)
        except ValueError:
            cur = 2
        self._speed = steps[(cur + 1) % len(steps)]
        self.speed_btn.setText(f'倍速 {self._speed:.1f}x')
        self._append_log(f'[native] 倍速已设为 {self._speed:.1f}x（保存中…）')
        self.client.call_async_result('speed', {'run': {'video_speed': self._speed}})

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

    # ------------------------------------------------------------ 托盘
    def _build_tray(self) -> None:
        """系统托盘：挂机时用户可以关掉窗口而不中断刷课。

        ⚠️ 托盘图标用**程序里现成的**绘制（不依赖外部 ico 文件），
        这样打包成单文件也不会因为资源路径变化而变成空白图标。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.diag('ui: 系统托盘不可用，跳过')
            return
        icon = QIcon(_make_tray_pixmap())
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f'{APP_TITLE} v2.1.4')

        menu = QMenu()
        self.act_show = QAction('显示主界面', self)
        self.act_show.triggered.connect(self._restore_from_tray)
        menu.addAction(self.act_show)
        menu.addSeparator()
        self.act_start = QAction('启动刷课', self)
        self.act_start.triggered.connect(lambda: self._control('start'))
        self.act_pause = QAction('暂停/继续', self)
        self.act_pause.triggered.connect(self._toggle_pause)
        self.act_stop = QAction('终止并退出', self)
        self.act_stop.triggered.connect(self._on_stop)
        menu.addAction(self.act_start)
        menu.addAction(self.act_pause)
        menu.addAction(self.act_stop)
        menu.addSeparator()
        act_quit = QAction('退出界面', self)
        act_quit.triggered.connect(self._quit_from_tray)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self.diag('ui: tray icon installed')

    def _toggle_pause(self) -> None:
        paused = bool((self._last_status.get('engine') or {}).get('paused'))
        self._control('resume' if paused else 'pause')

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        """托盘菜单里的"退出界面"：真正退出（不再弹确认，用户已经明确点了）。"""
        self._quitting = True
        self.close()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()

    # ------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        running = bool((self._last_status.get('engine') or {}).get('running'))
        # 托盘可用且流程在跑 ⇒ 默认**最小化到托盘**（挂机场景：用户想关窗口但不想中断刷课）。
        # 直接退出仍然是选项之一。没有托盘就退回原来的二选一确认。
        if running and not getattr(self, '_quitting', False) and getattr(self, 'tray', None):
            box = QMessageBox(self)
            box.setWindowTitle('关闭界面')
            box.setText('刷课流程还在运行。')
            box.setInformativeText('要最小化到系统托盘继续刷课，还是直接退出（会中断刷课）？')
            btn_tray = box.addButton('最小化到托盘', QMessageBox.AcceptRole)
            btn_quit = box.addButton('直接退出', QMessageBox.DestructiveRole)
            box.addButton('取消', QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_tray:
                self.hide()
                self.tray.showMessage(APP_TITLE, '已最小化到托盘，刷课继续进行。',
                                      QSystemTrayIcon.Information, 2500)
                self.diag('ui: close -> 最小化到托盘')
                event.ignore()
                return
            if clicked is btn_quit:
                # 有上限地等它停干净（后端要顺手杀沙盒浏览器），等不到也照常退出
                self._request_stop(wait_seconds=3.0)
            else:
                event.ignore()
                return
        elif running:
            ask = QMessageBox.question(
                self, '确认退出',
                '刷课流程还在运行，退出会中断它。确定要退出吗？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ask != QMessageBox.Yes:
                event.ignore()
                return
            self._request_stop(wait_seconds=3.0)

        self.diag('ui: invoke Close')
        self.status.showMessage('正在退出…')
        QApplication.processEvents()
        self.client.shutdown()
        if getattr(self, 'tray', None):
            self.tray.hide()
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
