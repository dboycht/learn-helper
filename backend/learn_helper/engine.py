# -*- coding: utf-8 -*-
"""SolverEngine：刷课/答题的状态机与专用自动化线程。

## 线程铁律（本项目最值钱的一条，1.0.4 重构后更容易违反，务必盯住）

Playwright 的 **sync API 绑定创建它的线程**：

- 自动化主流程（锁定页面、翻卡片、截图、题型识别、填涂、翻页、视频/文档监控）
  全部运行在 ``engine.run()`` 所在的那条线程里 —— 由 ``start()`` 用一条
  daemon 线程拉起，线程内创建 ``sync_playwright()``。
- 只有**纯网络的求解**（``solve_question``）允许并发，走 ``core.run_parallel``（守护线程）。
- 界面/HTTP 线程**绝不**直接碰 Playwright：``list_pages()`` / ``diagnose()`` 用
  **短连接**（另开一条 CDP 连接，用完即关），且用 ``_io_lock`` 与主流程互斥。

## 状态归属

引擎是 UI 无关的：所有对外表现通过 ``hub`` 推送（日志 / 进度 / 状态快照），
前端只读快照、只发指令，不参与任何逻辑。
"""

import threading
import time

from . import core
from .config import (APP_VERSION, LOGGER, _as_bool, get_answer_cfg, get_run_cfg,
                     update_config)


class SolverEngine:
    """刷课/答题引擎。``hub`` 是外部提供的推送总线，要求实现：

    - ``emit_log(text)``         推一条日志
    - ``emit_progress(task=None, video=None, quiz=None)`` 推进度
    - ``emit_status()``          推一帧完整状态快照（返回值会被 ipc 复用）
    - ``pages``                  属性：当前页面列表；``pages_at`` 时间戳
    - ``selected_title``         属性：当前选中的网页标题
    """

    def __init__(self, hub):
        self.hub = hub
        self.settings = dict(get_run_cfg())          # video_speed / auto_submit
        self.stop_requested = False
        self.pause_requested = False
        self.solver_running = False
        self.solver_thread = None
        self.browser_proc = None
        self.accumulated_video_seconds = 0.0
        self.last_error = ''                          # 最近一次失败提示（前端可展示）

        # 进度与 KPI（供快照）
        self.page_counter = 0
        self.video_count = 0
        self.doc_count = 0
        self.quiz_text = '--'
        self.task_text = '闲置中'
        self.video_text = '--'
        self.last_page = ''
        self.started_at = None

        self._io_lock = threading.Lock()              # 短连接（页面列表/诊断）互斥
        self._cleanup_lock = threading.Lock()

    # ---------------- 运行期设置 ----------------
    def set_video_speed(self, speed):
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return False, '倍速必须是数字'
        speed = max(1.0, min(4.0, speed))
        self.settings['video_speed'] = speed
        update_config({'run': {'video_speed': speed}})
        return True, f'倍速已设为 {speed}x'

    def set_auto_submit(self, auto_submit):
        # ⚠️ 不能直接 `bool(auto_submit)`：`bool("false") is True`（实测），
        # 别的客户端发 `{"auto_submit":"false"}` 会**打开**自动提交。
        auto_submit = _as_bool(auto_submit, self.settings.get('auto_submit', True))
        self.settings['auto_submit'] = auto_submit
        update_config({'run': {'auto_submit': auto_submit}})
        return True, '提交模式已切换为「自动提交」' if auto_submit else '提交模式已切换为「仅暂存」'

    def set_auto_launch_browser(self, enabled):
        enabled = _as_bool(enabled, self.settings.get('auto_launch_browser', True))
        self.settings['auto_launch_browser'] = enabled
        update_config({'run': {'auto_launch_browser': enabled}})
        return True, ('启动时将自动打开浏览器' if enabled
                      else '启动时不再自动打开浏览器（可随时点「检测/刷新网页」手动打开）')

    def set_skip_quiz_only(self, enabled):
        """「只刷视频」开关：只跳"纯测验章节"，视频里弹的题不受影响。"""
        enabled = _as_bool(enabled, self.settings.get('skip_quiz_only', False))
        self.settings['skip_quiz_only'] = enabled
        update_config({'run': {'skip_quiz_only': enabled}})
        return True, ('已开启「只刷视频」：只有题目、没有视频/文档的章节会整节跳过'
                      if enabled
                      else '已关闭「只刷视频」：测验章节恢复自动答题')

    def auto_launch_browser(self):
        """界面启动后的延时动作：配置开着就拉起沙盒浏览器。

        为什么放在后端而不是界面侧：拉起浏览器属于业务（`kill_and_launch_browser`），
        界面只该发一条控制指令。返回 `(ok, message)`；**失败只记日志、不弹错误**。
        """
        if not self.settings.get('auto_launch_browser', True):
            return True, '启动自动打开浏览器已关闭（跳过）'
        return self.ensure_browser()

    def ensure_browser(self):
        """确保沙盒浏览器可用：没有 9222 就拉起（幂等，短连接，可被 HTTP 线程调用）。"""
        with self._io_lock:
            try:
                if core.is_cdp_port_open():
                    self.browser_proc = None     # 复用用户/上次留下的浏览器，不持有进程句柄
                    return True, '浏览器已在运行（复用 9222）'
                ok, proc = core.kill_and_launch_browser()
                if not ok:
                    return False, '无法拉起浏览器（9222 不可用）'
                self.browser_proc = proc
                return True, '已打开沙盒浏览器，请登录并点开学习页'
            except Exception as e:
                LOGGER.warning(f'[浏览器] 拉起失败: {e}')
                return False, f'拉起浏览器异常: {e}'

    def reload_settings(self):
        self.settings = dict(get_run_cfg())

    # ---------------- 控制 ----------------
    def start(self, selected_title=None):
        """启动刷课流程（专用自动化线程）。返回 (ok, message)。"""
        if self.solver_running:
            return False, '刷课流程已在运行中'
        if selected_title:
            # ⚠️ **一定要转成 str**：`/api/control` 的 `params.page` 是外部输入，
            # 传 `{"page": 123}` 或 `{"page": {...}}` 时原来的代码会把它原样塞进
            # `hub.selected_title`，随后 `run()` 里的 `.strip()` / `'[请点击' in title`
            # 抛 TypeError/AttributeError。后者尤其致命：异常抛在 `run()` 的 try
            # **之外**，`finally: self.solver_running = False` 永远不执行 ⇒
            # 引擎**永久卡在"正在运行"**，之后 start/diagnose 全被拒（实测，见 E61）。
            selected_title = str(selected_title).strip()
            self.hub.selected_title = selected_title
        title = getattr(self.hub, 'selected_title', None) or ''
        title = title if isinstance(title, str) else ''
        if (not title) or '[请点击' in title or '[安全浏览器' in title:
            self.last_error = '请先在浏览器中点开学习页，并在「当前网页」中选择要刷的网页'
            self.hub.emit_log(f'[启动] 未选择学习页：{self.last_error}')
            self.hub.emit_status()
            return False, self.last_error

        core.SHUTDOWN.clear()
        self.stop_requested = False
        self.pause_requested = False
        self.solver_running = True
        self.last_error = ''
        self.page_counter = 0
        self.video_count = 0
        self.doc_count = 0
        self.quiz_text = '--'
        self.video_text = '--'
        self.started_at = time.time()
        self.solver_thread = threading.Thread(target=self.run, daemon=True,
                                              name='solver-engine')
        self.solver_thread.start()
        self.hub.emit_log('================================================')
        self.hub.emit_log('[启动] 纯刷课流程已就绪，正在准备...')
        self.hub.emit_status()
        return True, '刷课流程已启动'

    def stop(self, close_browser=False):
        """请求停止（幂等）。close_browser=False 保留沙盒浏览器便于查看页面。"""
        was = self.solver_running
        self.stop_requested = True
        core.SHUTDOWN.set()
        self.hub.emit_log('[系统] 已请求停止刷课流程...')
        if close_browser:
            thread = getattr(self, 'solver_thread', None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=3.0)
            self.close_browser()
        self.hub.emit_status()
        return was

    def pause(self):
        self.pause_requested = True
        self.hub.emit_log('[系统] 已暂停（视频/文档监控会停在下一轮循环）。')
        self.hub.emit_status()
        return True, '已暂停'

    def resume(self):
        self.pause_requested = False
        self.hub.emit_log('[系统] 已继续。')
        self.hub.emit_status()
        return True, '已继续'

    def check_pause_and_stop(self):
        """循环守卫：暂停时自旋等待，停止/退出信号返回 True。"""
        while self.pause_requested and not self.stop_requested:
            time.sleep(0.25)
        return self.stop_requested

    def close_browser(self):
        """退出流程用：关闭沙盒浏览器（含兜底 terminate 自拉起的进程）。"""
        # 同样要拿 `_io_lock`：`core.close_sandbox_browser` 会**另开一条 CDP 连接**，
        # 而 `/api/pages`、`diagnose()` 也在这把锁下面用短连接 —— 不互斥的话
        # `Browser.close` 可能在别人正查询时把浏览器抽走（虽然只记日志，但会误报）。
        with self._cleanup_lock, self._io_lock:
            try:
                acted = core.close_sandbox_browser(self.browser_proc)
                self.hub.emit_log('[退出] 沙盒浏览器已关闭。' if acted
                                  else '[退出] 未发现可关闭的沙盒浏览器。')
            except Exception as e:
                self.hub.emit_log(f'[退出] 关闭沙盒浏览器异常: {e}')
            self.browser_proc = None

    # ---------------- 只读操作（HTTP 线程，短连接） ----------------
    def list_pages(self):
        """列出浏览器标签页。与自动化主流程互斥（_io_lock）。"""
        with self._io_lock:
            try:
                labels, used_url = core.list_page_titles()
            except Exception as e:
                LOGGER.warning(f'[页面列表] 读取失败: {e}')
                return {'ok': False, 'message': f'读取失败: {e}', 'pages': []}
        self.hub.pages = labels
        self.hub.pages_at = time.time()
        msg = '（已用 URL 兜底）' if used_url else ''
        return {'ok': True, 'message': f'检测到 {len(labels)} 个标签页{msg}',
                'pages': labels, 'used_url_fallback': used_url}

    def diagnose(self):
        """诊断当前学习页（短连接；与主流程互斥）。"""
        if self.solver_running:
            return {'ok': False,
                    'message': '刷课运行中，请先停止再做诊断（避免两条 CDP 通道互相干扰）'}
        with self._io_lock:
            lines = []
            try:
                ok, proc = core.kill_and_launch_browser()
                if not ok:
                    lines.append('[诊断] 无法连接/拉起浏览器（9222 不可用）。')
                    return {'ok': True, 'message': '\n'.join(lines)}
                self.browser_proc = proc
                sync_playwright = core.require_playwright()
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(core.CDP_URL)
                    ctx = browser.contexts[0]
                    if not ctx.pages:
                        lines.append('[诊断] 浏览器无已打开页面。')
                    else:
                        for i, pg in enumerate(ctx.pages):
                            lines.append(f'========== 页面 {i + 1} / {len(ctx.pages)} ==========')
                            core.diagnose_page(pg, lines.append)
                    browser.close()
            except Exception as e:
                lines.append(f'[诊断] 异常: {e}')
        text = '\n'.join(lines)
        self.hub.emit_log(text)
        return {'ok': True, 'message': text}

    def test_backend(self, base=None):
        """测试后端连接（纯网络）。"""
        ok, message = core.probe_backend(base=base)
        self.hub.emit_log(message)
        return {'ok': ok, 'message': message}

    def run_self_test(self, mode='server', base=None):
        """跑「测试图片」自检（纯网络，放守护线程执行）。"""
        def _work():
            try:
                ok_all, lines, results = core.run_solve_self_test(
                    mode=mode, retry=1, base=base)
            except Exception as e:
                self.hub.emit_log(f'[自检] 执行异常: {e}')
                return
            for ln in lines:
                self.hub.emit_log(f'[自检] {ln}')
            self.hub.emit_log(f'[自检] 结论: {core.summarize_self_test(results)}')

        threading.Thread(target=_work, daemon=True, name='self-test').start()
        return True, '自检已开始（结果见日志）'

    def _safe_title(self, page, fallback='(未知页面)'):
        """读页面标题，**任何异常都不许打断主流程**。

        `page.title()` 在"标签页正被关闭 / 正在导航"时会抛 `TargetClosedError`，
        而调用点（锁定页面、重连后重新锁定）恰恰是最容易出现这种时序的地方；
        原来它裸调用，一抛就冒到 `run()` 的兜底 except ⇒ 整条刷课流程"异常中断"。
        """
        try:
            return (page.title() or '').strip() or fallback
        except Exception:
            return fallback

    def _register_dialog_handler(self, page):
        """给页面挂"自动接受 alert/confirm"的处理器 —— **同一页面只挂一次**。

        ⚠️ Playwright 的 `page.on('dialog', ...)` 是**追加**语义，且没有去重。
        主循环里每次重新锁定页面都挂一遍，同一个 page 对象就会被挂上多个处理器
        （周期性重建标签页时旧 page 被 close，但重连/复用同一 page 的场景会累积）。
        这里按对象 id 记一笔，重复调用直接返回。
        """
        key = id(page)
        seen = getattr(self, '_dialog_pages', None)
        if seen is None:
            seen = set()
            self._dialog_pages = seen
        if key in seen:
            return
        try:
            page.on('dialog', lambda dialog: dialog.accept())
            seen.add(key)
        except Exception as e:
            LOGGER.info(f'[页面] 注册对话框处理器失败: {e}')

    # ---------------- 状态快照 ----------------
    def status(self):
        return {
            'version': APP_VERSION,
            'running': self.solver_running,
            'paused': self.pause_requested,
            'stop_requested': self.stop_requested,
            'selected_page': getattr(self.hub, 'selected_title', None) or '',
            'page_count': self.page_counter,
            'video_count': self.video_count,
            'doc_count': self.doc_count,
            'task_text': self.task_text,
            'video_text': self.video_text,
            'quiz_text': self.quiz_text,
            'last_page': self.last_page,
            'last_error': self.last_error,
            'uptime': round(time.time() - self.started_at, 1) if self.started_at else 0.0,
        }

    # ====================================================================
    # 自动化主流程（专用线程内执行；不要从其它线程调用）
    # ====================================================================
    def run(self):
        """刷课主流程：锁定页面 → 逐卡片（答题 → 音视频/文档）→ 翻页 → 防弹窗。"""
        hub = self.hub
        try:
            # ⚠️ 这一行**必须留在 try 里面**：它原来在 try 之前，一旦
            # `hub.selected_title` 是异常类型（外部输入未转 str）就会在这里抛出，
            # `finally` 里的 `solver_running = False` 不执行 ⇒ 引擎永久假"运行中"。
            selected_title = (getattr(hub, 'selected_title', None) or '').strip()
            self.task_text = '正在准备...'
            # 挂机必备：阻止系统自动休眠/熄屏（老 Tk 版有、2.x 漏搬，见 core.keep_computer_awake）。
            core.keep_computer_awake()
            hub.emit_status()
            completed_video_urls = set()
            completed_doc_urls = set()

            ok, proc = core.kill_and_launch_browser()
            if ok:
                self.browser_proc = proc
            else:
                hub.emit_log('[错误] 浏览器挂载失败，请先手动打开一个 Edge 窗口。')
                self.last_error = '浏览器挂载失败，请先手动打开一个 Edge 窗口'
                return

            start_time = time.time()
            hub.emit_log('[系统] 正在连接浏览器 CDP 通道...')

            sync_playwright = core.require_playwright()
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(core.CDP_URL)
                context = browser.contexts[0]

                target_page = self._locate_page(context, selected_title)
                if target_page is None:
                    target_page = self._reopen_last_page(context)
                if target_page is None:
                    self.last_error = f'找不到网页「{selected_title}」且无法自动跳回上次的学习页'
                    hub.emit_log(f'[错误] {self.last_error}')
                    return

                self.remember_page(target_page)
                hub.emit_log(f'[系统] 锁定当前网页: 【{self._safe_title(target_page)}】')
                self._register_dialog_handler(target_page)
                page_counter = 1
                saved_url = None
                restore_fails = 0
                # 内层 while 的 break 只跳出一层（= 进入下一页）；整个流程是否收尾由
                # end_flow 决定，否则「终止退出」和「刷完最后一页」都会在外层空转（ERROR.md E6）。
                end_flow = False

                while True:
                    if self.stop_requested:
                        break
                    # 浏览器上下文丢失时自动重连，避免 list index out of range 中断流程
                    if not browser.contexts:
                        hub.emit_log('[警告] 浏览器上下文丢失，正在重新拉起浏览器...')
                        try:
                            core.kill_and_launch_browser()
                            time.sleep(1.0)
                            browser = p.chromium.connect_over_cdp(core.CDP_URL)
                        except Exception as e:
                            hub.emit_log(f'[错误] 浏览器重连失败: {e}')
                            break
                    try:
                        context = browser.contexts[0]
                    except Exception:
                        context = None
                    if context is None:
                        hub.emit_log('[错误] 浏览器上下文不可用，流程结束。')
                        break
                    if not context.pages:
                        try:
                            target_page = context.new_page()
                        except Exception as e:
                            hub.emit_log(f'[错误] 新建标签页失败: {e}')
                            break
                        time.sleep(0.5)
                    elif saved_url:
                        target_page = context.pages[-1] if context.pages else context.new_page()
                        try:
                            target_page.goto(saved_url)
                            hub.emit_log('[系统] 🌟 浏览器重启完毕，已返回目标页，继续刷课...')
                        except Exception as e:
                            # ⚠️ **失败也必须消费掉 saved_url**：原来无论成功失败都在
                            # 后面无条件 `saved_url = None`，那反而没错；真正会死循环的是
                            # "失败却保留"的写法。这里显式在两条路径上都清掉，并在
                            # 失败时计数，连续失败就不再尝试（否则外层 `while True`
                            # 会一直回到这里重试同一件事，用户只能手动停止）。
                            restore_fails += 1
                            hub.emit_log(f'[警告] 返回目标页失败（第 {restore_fails} 次）: {e}')
                        saved_url = None
                        if restore_fails >= 2:
                            hub.emit_log('[错误] 多次无法返回学习页，结束本次刷课。')
                            end_flow = True
                            break
                    else:
                        found = self._locate_page(context, selected_title)
                        if found is not None:
                            target_page = found
                        elif not context.pages:
                            target_page = context.new_page()
                        elif target_page is not None:
                            try:
                                target_page.url          # 还在就继续用它
                            except Exception:
                                target_page = context.pages[-1]
                        else:
                            target_page = context.pages[-1]
                    self.remember_page(target_page)
                    hub.emit_log(f'[系统] 锁定当前网页: 【{self._safe_title(target_page)}】')
                    self._register_dialog_handler(target_page)

                    while True:
                        if self.check_pause_and_stop():
                            hub.emit_log('[系统] 任务因用户请求退出。')
                            end_flow = True
                            break
                        self.page_counter = page_counter
                        hub.emit_log(f'\n--- [ 正在处理第 {page_counter} 页 ] ---')
                        try:
                            target_page.wait_for_load_state('load', timeout=15000)
                        except Exception:
                            pass
                        hub.emit_log('[系统] 正在等待任务卡片加载...')
                        core.robust_wait_for_tasks_to_render(target_page, self.check_pause_and_stop)

                        LOGGER.info(f'[识别] 页面帧数={1 + len(target_page.frames)}')
                        for i, fr in enumerate(target_page.frames):
                            try:
                                LOGGER.info(f'[识别]   frame#{i + 1} url={fr.url[:120]}')
                            except Exception:
                                pass
                        cards_frame = None
                        for frame in target_page.frames:
                            if 'knowledge/cards' in frame.url:
                                cards_frame = frame
                                break
                        if not cards_frame:
                            for frame in target_page.frames:
                                if frame != target_page.main_frame:
                                    cards_frame = frame
                                    break
                        LOGGER.info(f'[识别] cards_frame={"命中" if cards_frame else "未命中"}')

                        tab_buttons = core.find_tab_buttons(cards_frame) if cards_frame else []
                        total_tabs = max(1, len(tab_buttons))
                        hub.emit_log('[系统] 本节包含多个卡片，逐个处理...' if total_tabs > 1
                                     else '[系统] 本节包含单个卡片。')

                        for tab_idx in range(total_tabs):
                            if self.check_pause_and_stop():
                                break
                            hub.emit_log(f'\n   --- [ 任务卡片 {tab_idx + 1} / {total_tabs} ] ---')
                            if len(tab_buttons) > 1:
                                try:
                                    current_tabs = core.find_tab_buttons(cards_frame)
                                    if tab_idx < len(current_tabs):
                                        target_tab = current_tabs[tab_idx]
                                        target_tab.scroll_into_view_if_needed()
                                        time.sleep(0.3)
                                        target_tab.click(True, force=True)
                                        hub.emit_log(f'      [卡片] 已切换至卡片 {tab_idx + 1}...')
                                        time.sleep(1.5)
                                        core.robust_wait_for_tasks_to_render(
                                            target_page, self.check_pause_and_stop)
                                except Exception as tab_ex:
                                    hub.emit_log(f'      [警告] 切换卡片失败: {tab_ex}')

                            # 曝光懒加载
                            try:
                                active_cards_frame = cards_frame if cards_frame else target_page.main_frame
                                for el in active_cards_frame.locator(
                                        "div.ans-attach-ct, .ans-attach-online, .ans-cc, iframe, div[class*='attach']").all():
                                    try:
                                        if el.is_visible():
                                            el.scroll_into_view_if_needed()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            time.sleep(0.3)

                            # ---- 先扫音视频/文档任务（多选择器 + 多帧回退）----
                            #
                            # ⚠️ **顺序很重要**：这一段原来在"答题"之后。为了让「只刷视频」
                            # （`run.skip_quiz_only`）能判断"本节到底有没有视频"，
                            # 必须**先**知道本节有哪些媒体任务点，再决定要不要答题（2.1.3）。
                            containers = core.collect_job_containers(target_page, cards_frame)
                            hub.emit_log(f'      [识别] 发现候选任务容器 {len(containers)} 个')
                            valid_jobs = []
                            for ph in containers:
                                try:
                                    if not ph.is_visible():
                                        LOGGER.info('[识别] 跳过容器：不可见')
                                        continue
                                    box = ph.bounding_box()
                                    if not box or box['height'] < 10 or box['width'] < 10:
                                        LOGGER.info(f'[识别] 跳过容器：尺寸过小 box={box}')
                                        continue
                                    html = ph.inner_html().lower()
                                    matched = [kw for kw in
                                               ('video', 'audio', 'fastforward', 'insertvideo',
                                                'pdf', 'ppt', 'doc', 'preview') if kw in html]
                                    if not matched:
                                        LOGGER.info('[识别] 跳过容器：未见媒体关键字 '
                                                    f'html={" ".join(html.split())[:200]}')
                                        continue
                                    LOGGER.info(f'[识别] 接受容器 命中={matched} '
                                                f'size=({int(box["width"])}x{int(box["height"])})')
                                    valid_jobs.append(ph)
                                except Exception as ex:
                                    LOGGER.info(f'[识别] 容器检查异常: {ex}')

                            has_media = any(
                                any(k in ph.inner_html().lower() for k in
                                    ('video', 'audio', 'fastforward', 'insertvideo',
                                     'pdf', 'ppt', 'doc', 'preview'))
                                for ph in valid_jobs)

                            # ---- 答题（内部答题 API / 自配大模型 / 仅识别）----
                            questions, target_frame = core.scan_page_recursively(target_page)
                            if questions:
                                done = core.check_quiz_completed(questions, target_frame)
                                if done:
                                    hub.emit_log('      [跳过] 该测验任务点已被平台标记完成。')
                                    self.quiz_text = '已完成'
                                elif self.settings.get('skip_quiz_only', False) and not has_media:
                                    # 「只刷视频」：本节**只有题目、没有任何媒体任务点**
                                    # ⇒ 这就是章节列表里那种"测验/作业"任务点，整节跳过。
                                    # ⚠️ 明确**不跳**的情况：本节有视频/文档 ⇒ 题目照做
                                    # （视频里弹出来的题也照做，用户明确要求）。
                                    hub.emit_log(
                                        f'      [跳过] 「只刷视频」已开启：本节是纯测验任务点'
                                        f'（{len(questions)} 题、无视频/文档），整节跳过不答题。')
                                    self.quiz_text = f'已跳过测验 {len(questions)} 题'
                                else:
                                    self._solve_question_batch(questions, target_page)
                            else:
                                self.quiz_text = '无题目'
                            hub.emit_status()

                            if not valid_jobs:
                                hub.emit_log('      [系统] 当前卡片无音视频/文档任务。'
                                             '（可点「诊断页面」查看命中详情）')
                                self.video_count = 0
                                self.doc_count = 0
                                LOGGER.info('[识别] 未识别到任务容器，自动输出页面诊断：')
                                core.diagnose_page(target_page, hub.emit_log)
                            else:
                                v_count = sum(1 for ph in valid_jobs
                                              if any(k in ph.inner_html().lower() for k in
                                                     ('video', 'audio', 'fastforward', 'insertvideo')))
                                d_count = len(valid_jobs) - v_count
                                self.video_count = v_count
                                self.doc_count = d_count
                                hub.emit_log(f'      [系统] 检测到 {len(valid_jobs)} 个任务点，开始监控...')

                                for task_idx, target_container in enumerate(valid_jobs):
                                    if self.check_pause_and_stop():
                                        break
                                    hub.emit_log(
                                        f'\n         [对焦] 目标任务 {task_idx + 1}/{len(valid_jobs)} ...')
                                    try:
                                        target_container.scroll_into_view_if_needed()
                                        time.sleep(0.5)
                                    except Exception:
                                        pass
                                    task_sig = self.generate_task_signature(
                                        page_counter, tab_idx, task_idx, target_container)
                                    if task_sig in completed_video_urls or task_sig in completed_doc_urls:
                                        hub.emit_log('         [跳过] 已完成过此任务。')
                                        continue
                                    try:
                                        if target_container.evaluate(
                                                '(c) => c.classList.contains("ans-job-finished") || '
                                                '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(c.className || \'\');'):
                                            hub.emit_log('         [跳过] 平台已标记完成。')
                                            completed_video_urls.add(task_sig)
                                            completed_doc_urls.add(task_sig)
                                            continue
                                    except Exception:
                                        pass

                                    task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        hub.emit_log('         [等待] 任务容器初始化中，等 2 秒...')
                                        time.sleep(2.0)
                                        task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        hub.emit_log('         [警告] 容器穿透失败，跳过该任务。')
                                        continue

                                    has_video = False
                                    has_doc = False
                                    for _ in range(25):
                                        u = (task_frame.url or '').lower()
                                        has_video = (task_frame.locator('video, audio').count() > 0
                                                     or any(k in u for k in ('/video/', '/audio/')))
                                        has_doc = (task_frame.locator('#panView, #container, #scrollBox').count() > 0
                                                   or any(k in u for k in ('/pdf/', '/ppt/', '/doc/', '/pub/preview')))
                                        if has_video or has_doc:
                                            break
                                        time.sleep(0.2)

                                    try:
                                        if has_video:
                                            hub.emit_log('         [视频] 开始倍速静音播放...')
                                            self.run_video_task(target_page, task_frame,
                                                                target_container, task_sig,
                                                                completed_video_urls)
                                        elif has_doc:
                                            hub.emit_log('         [文档] 开始步进滚动阅读...')
                                            self.run_doc_task(target_page, task_frame,
                                                              target_container, task_sig,
                                                              completed_doc_urls)
                                        else:
                                            hub.emit_log('         [系统] 未发现视频/文档，标记已读。')
                                            self.video_text = '已完成'
                                            try:
                                                target_container.evaluate(
                                                    "(c) => c.classList.add('ans-job-finished')")
                                            except Exception:
                                                pass
                                        hub.emit_log('         [等待] 平台状态同步中...')
                                        time.sleep(1.5)
                                    except Exception as eval_ex:
                                        hub.emit_log(f'         [警告] 任务异常: {eval_ex}')
                                        try:
                                            target_container.evaluate(
                                                "(c) => c.classList.add('ans-job-finished')")
                                        except Exception:
                                            pass
                                        time.sleep(1.0)
                                    hub.emit_status()

                            if self.check_pause_and_stop():
                                break

                        if self.check_pause_and_stop():
                            end_flow = True
                            break

                        # 翻页
                        hub.emit_log('[导航] 正在查找下一页按钮...')
                        next_btn, next_frame = core.find_next_button(target_page)
                        if not next_btn:
                            hub.emit_log('[系统] 未找到下一页按钮，刷课流程结束。')
                            end_flow = True
                            break
                        try:
                            next_btn.scroll_into_view_if_needed()
                            time.sleep(0.5)
                            next_btn.click(True, force=True)
                            hub.emit_log('[导航] 已翻页，检查是否有确认弹窗...')
                            for _ in range(5):
                                if self.check_pause_and_stop():
                                    break
                                time.sleep(0.2)
                                bypass_btn, _bf = core.find_confirmation_bypass_button(target_page)
                                if bypass_btn:
                                    hub.emit_log('[系统] 检测到未完成提示弹窗，已强制跳过。')
                                    bypass_btn.click(True, force=True)
                                    break
                            hub.emit_log('[导航] 等待页面载入...')
                            time.sleep(0.8)
                            if self.stop_requested:
                                end_flow = True
                                break
                            page_counter += 1
                            if page_counter % 10 == 0:
                                hub.emit_log('[系统] 🌟 已连刷 10 页，重建标签页释放内存...')
                                saved_url = target_page.url
                                new_page = context.new_page()
                                self._register_dialog_handler(new_page)
                                target_page.close()
                                new_page.goto(saved_url)
                                target_page = new_page
                                hub.emit_log('[系统] 🌟 内存清理完毕，继续刷课...')
                            completed_video_urls.clear()
                            completed_doc_urls.clear()
                            hub.emit_log('[系统] 已清理上一页去重缓存。')
                        except Exception as ex:
                            hub.emit_log(f'   [警告] 翻页受阻: {ex}')
                            end_flow = True
                            break

                    if end_flow:
                        break

                self.page_counter = page_counter
                hub.emit_log(f'\n[系统] 刷课流程运行完毕，共处理页面数: {page_counter}')
                hub.emit_log(f'总计耗时: {time.time() - start_time:.2f}s')
                if self.stop_requested:
                    hub.emit_log('[系统] 已按用户请求停止，保留沙盒浏览器（不关闭，便于查看页面）。')
                else:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            hub.emit_log(f'[错误] 流程异常中断: {e}')
            self.last_error = f'流程异常中断: {e}'
        finally:
            # 撤销"阻止休眠"，把电源策略还给系统（与 run() 开头的 keep_computer_awake 配对）
            core.keep_computer_awake(release=True)
            self.solver_running = False
            self.task_text = '闲置中'
            self.hub.emit_status()
        return None

    # ---------------- 页面锁定 / 恢复 ----------------
    def _locate_page(self, context, selected_title):
        """按标题匹配页面；冷启动兜底时按 URL 匹配（ERROR.md E8 的配套）。"""
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        for pg in pages:
            try:
                if (pg.title() or '').strip() == (selected_title or '').strip():
                    return pg
            except Exception:
                pass
        # 按 URL 兜底（冷启动标题未就绪时）
        for pg in pages:
            try:
                if selected_title and selected_title in (pg.url or ''):
                    return pg
            except Exception:
                pass
        return None

    def remember_page(self, page):
        """把当前学习页写进 config.json: last_page_url / last_page_title（同址不重复写）。"""
        try:
            url = page.url
        except Exception:
            return None
        title = self._safe_title(page, fallback='')
        hub = self.hub
        if getattr(hub, '_last_saved_url', None) == url:
            return None
        hub._last_saved_url = url
        try:
            # `update_config` 已经模块级 import；原来这里又局部 import 一次，
            # 而且把写入失败也吞掉了。写失败至少留一条 warning 便于定位。
            if not update_config({'last_page_url': url, 'last_page_title': title}):
                LOGGER.warning('[页面] last_page_* 写入 config.json 失败')
        except Exception as e:
            LOGGER.warning(f'[页面] 记忆当前页失败: {e}')
        return None

    def _reopen_last_page(self, context):
        """页面没了就 goto 回上次记住的学习页并等标题就绪。"""
        try:
            from .config import load_config
            cfg = load_config()
            url = cfg.get('last_page_url') or ''
            if not url:
                return None
            self.hub.emit_log('[系统] 页面被关闭，尝试自动跳回上次的学习页...')
            page = context.new_page()
            self._register_dialog_handler(page)
            page.goto(url)
            for _ in range(30):
                try:
                    if page.title():
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            self.remember_page(page)
            return page
        except Exception as e:
            self.hub.emit_log(f'[错误] 自动跳回上次学习页失败: {e}')
            return None

    # ---------------- 答题批次（识别 → 并发求解 → 填涂 → 提交/暂存） ----------------
    def _solve_question_batch(self, questions, target_page):
        """一批题目的完整处理。截图/识别/填涂留在本线程；求解走 run_parallel（守护线程）。"""
        hub = self.hub
        acfg = get_answer_cfg()
        mode = acfg['mode']
        total_q = len(questions)

        if mode == 'off':
            hub.emit_log(f'      [测验] 探测到文字题 {total_q} 道；当前为「仅识别不答题」，只记录不填涂。')
            self.quiz_text = f'仅识别 {total_q} 题'
            for i, q in enumerate(questions):
                try:
                    q_type, num_inputs = core.detect_question_type_and_inputs(q)
                    text_source = core.extract_clean_text_with_latex(q)
                    hub.emit_log(f'         [题 {i + 1}/{total_q}] 类型={q_type} 数量={num_inputs} '
                                 f'题干={(text_source or "(无题干)")[:60]}')
                except Exception as e:
                    hub.emit_log(f'         [题 {i + 1}/{total_q}] 识别失败: {e}')
            return None

        from .config import ANSWER_MODE_LABELS
        label = ANSWER_MODE_LABELS.get(mode, mode)
        hub.emit_log(f'      [测验] 探测到文字题 {total_q} 道，使用「{label}」求解'
                     f'（并发 {acfg["workers"]}，单题超时 {acfg["solver_timeout"]}s）...')
        self.task_text = f'自动做题中（{label}）'
        self.quiz_text = f'识别到 {total_q} 题'
        hub.emit_status()

        # ---- 阶段 1（本线程）：截图 + 题型/题干识别 ----
        items = []
        for i, q in enumerate(questions):
            if self.check_pause_and_stop():
                break
            try:
                q.scroll_into_view_if_needed()
                time.sleep(0.05)
                img_bytes = q.screenshot()
                q_type, num_inputs = core.detect_question_type_and_inputs(q)
                text_source = core.extract_clean_text_with_latex(q)
                items.append({
                    'idx': i, 'locator': q, 'image': img_bytes,
                    'q_type': q_type, 'num': num_inputs, 'text': text_source,
                    'answer': None, 'error': None,
                })
                hub.emit_log(f'         [题 {i + 1}/{total_q}] 类型={q_type} 数量={num_inputs} '
                             f'题干={(text_source or "(无题干)")[:40]}')
            except Exception as e:
                hub.emit_log(f'         [题 {i + 1}/{total_q}] 截图/识别失败: {e}')
        if not items:
            hub.emit_log('      [警告] 本卡片题目截图/识别全部失败，跳过答题。')
            self.quiz_text = '识别失败'
            return None

        # ---- 阶段 2（守护线程并发）：求解 ----
        def _work(it):
            try:
                it['answer'] = core.solve_question(it['image'], it['q_type'], it['num'],
                                                   it['text'], mode, acfg)
            except Exception as e:
                it['error'] = ('error', str(e))
            return it

        self.quiz_text = f'求解 0/{len(items)}'
        core.run_parallel(items, _work, workers=acfg['workers'],
                          on_progress=lambda n: setattr(self, 'quiz_text', f'求解 {n}/{len(items)}'))
        if core.SHUTDOWN.is_set():
            hub.emit_log('      [退出] 已放弃剩余在途求解请求。')
            return None

        # ---- 阶段 3（本线程）：填涂 ----
        nc = 0
        ok_q = 0
        for it in items:
            if self.check_pause_and_stop():
                break
            if it['error']:
                hub.emit_log(f'         [题 {it["idx"] + 1}] 求解失败: {it["error"][1]}')
                continue
            resp = it['answer'] or {}
            # ⚠️ **空答案不算"求解成功"**：`run_parallel` 在退出时会直接 return，
            # 那些没轮到的题 `answer` 还是 None；后端返回 200 但内容是空/无字母时
            # `_normalize_answer` 也会给出空答案。原来这两种都按"已求解"计数，
            # 一旦本批里**有任意一题**填涂成功（`nc > 0`）就会触发自动提交 ——
            # 交上去的是一份"其余题目全空白"的答卷。
            if not (resp.get('answer_key') or resp.get('text_answers')):
                hub.emit_log(f'         [题 {it["idx"] + 1}] 未取得有效答案，跳过填涂。')
                continue
            show = resp.get('answer_key') or ', '.join(resp.get('text_answers', []))
            tag = ' [后端缓存命中]' if resp.get('cached') else ''
            hub.emit_log(f'         [题 {it["idx"] + 1}] 答案: {show or "(空)"}{tag}')
            ok_q += 1
            try:
                if core.fill_and_click_smart(it['locator'], resp):
                    nc += 1
                    self.quiz_text = f'已填涂 {nc}/{total_q}'
                    time.sleep(0.1)
                else:
                    hub.emit_log(f'         [题 {it["idx"] + 1}] 答案未能写入页面（选择器可能已失效）')
            except Exception as e:
                hub.emit_log(f'         [题 {it["idx"] + 1}] 填涂异常: {e}')

        hub.emit_log(f'      [完成] 本卡片求解 {ok_q} 题、成功填涂 {nc}/{total_q} 题。')
        self.quiz_text = f'完成 {nc}/{total_q}'

        # ---- 阶段 4：提交 / 暂存 ----
        # ⚠️ **只有整批全部填涂成功才自动提交**（2026-09-19）：原来的判据是 `nc > 0`，
        # 于是 8 道题里填对 1 道也会把整份测验交上去，其余 7 道留空。
        # 有题没填上就**降级为暂存**——答案留在页面上，用户可以自己补完再交。
        if nc == 0:
            hub.emit_log('      [跳过] 本卡片没有任何题目填涂成功，不提交。')
            return None
        if self.check_pause_and_stop():
            return None
        # `len(items)` 才是"真正识别成功、参与本批"的题数；`total_q` 是扫描到的题数，
        # 截图失败被丢掉的题会让两者不等，所以两个都要比。
        if nc < len(items) or len(items) < total_q:
            hub.emit_log(f'      [提示] 未全部填涂（{nc}/{total_q}），本次不自动提交，'
                         f'改为暂存以免交出空白答卷。')
            self._do_save_target_page(target_page)
            return None
        if self.settings.get('auto_submit', True):
            self._do_submit_target_page(target_page)
        else:
            self._do_save_target_page(target_page)
        return None

    def _do_submit_target_page(self, target_page):
        """点「提交」并处理二次确认；失败自动降级为暂存（与旧客户端语义一致）。"""
        self.hub.emit_log('      [提交] 执行自动提交...')
        submit_btn, _ = core.find_submit_button(target_page)
        if not submit_btn:
            self.hub.emit_log('         [警告] 未找到提交按钮，自动降级为暂存。')
            return self._do_save_target_page(target_page)
        try:
            submit_btn.scroll_into_view_if_needed()
            time.sleep(0.3)
            submit_btn.click(True, force=True)
            self.hub.emit_log('         [提交] 已点击提交，等待二次确认弹窗...')
            time.sleep(0.8)
            confirm_btn = None
            sels = ['#popok', 'a#popok', '.jb_btn_92', "a:has-text('确定')", "button:has-text('确定')"]
            for f in [target_page.main_frame] + target_page.frames:
                try:
                    for sel in sels:
                        loc = f.locator(sel)
                        if loc.count() > 0:
                            el = loc.first
                            if el.is_visible() and el.is_enabled():
                                confirm_btn = el
                                break
                    if confirm_btn:
                        break
                except Exception:
                    pass
            if confirm_btn:
                confirm_btn.scroll_into_view_if_needed()
                confirm_btn.click(True, force=True)
                self.hub.emit_log('         [提交] 二次确认完成，任务点已提交。')
            else:
                self.hub.emit_log('         [提示] 未检测到确认弹窗（可能已被浏览器自动放行）。')
            for _ in range(10):
                if self.stop_requested:
                    break
                time.sleep(0.2)
        except Exception as e:
            self.hub.emit_log(f'         [警告] 自动提交失败: {e}，降级为暂存。')
            return self._do_save_target_page(target_page)
        return None

    def _do_save_target_page(self, target_page):
        """点「暂存/保存」留存答案。"""
        self.hub.emit_log('      [暂存] 执行自动暂存...')
        save_btn, _ = core.find_save_button(target_page)
        if not save_btn:
            self.hub.emit_log('         [系统] 未找到暂存按钮，跳过暂存。')
            return None
        try:
            save_btn.scroll_into_view_if_needed()
            time.sleep(0.3)
            save_btn.click(True, force=True)
            self.hub.emit_log('         [存档] 暂存成功，答案已留存。')
            for _ in range(10):
                if self.stop_requested:
                    break
                time.sleep(0.2)
        except Exception as e:
            self.hub.emit_log(f'         [警告] 暂存失败: {e}')
        return None

    # ---------------- 视频 / 文档任务 ----------------
    def traverse_to_leaf_frame(self, container_locator):
        """穿透 iframe，找到承载 video/文档的最内层 frame。"""
        iframe_loc = container_locator.locator('iframe').first
        if iframe_loc.count() == 0:
            return None
        try:
            handle = iframe_loc.element_handle()
            current_frame = handle.content_frame() if handle else None
            if not current_frame:
                return None
            for _ in range(25):
                if current_frame.url and current_frame.url != 'about:blank':
                    break
                time.sleep(0.2)
            url_lower = (current_frame.url or '').lower()
            has_media = current_frame.locator('video, audio').count() > 0
            has_doc = current_frame.locator('#panView, #container, #scrollBox').count() > 0
            is_task_url = any(kw in url_lower for kw in ('/video/', '/audio/'))
            if has_media or has_doc or is_task_url:
                return current_frame
            nested_iframe_loc = current_frame.locator('iframe').first
            if nested_iframe_loc.count() > 0:
                handle = nested_iframe_loc.element_handle()
                nested_frame = handle.content_frame() if handle else None
                if nested_frame:
                    current_frame = nested_frame
            return current_frame
        except Exception:
            return None

    def generate_task_signature(self, page_counter, tab_idx, task_idx, container_locator):
        """任务去重签名（页/卡/序号 + 清洗后 iframe src）。"""
        try:
            iframe_el = container_locator.locator('iframe').first
            src = ''
            for _ in range(12):
                if iframe_el.count() > 0:
                    raw_src = iframe_el.get_attribute('src') or ''
                    if raw_src and raw_src != 'about:blank':
                        src = raw_src
                        break
                time.sleep(0.2)
            return f'p{page_counter}_t{tab_idx}_i{task_idx}_{(src or "unloaded")[:120]}'
        except Exception:
            return f'p{page_counter}_t{tab_idx}_i{task_idx}_fallback'

    # 单个任务点"完全没进展"多久就放弃（秒）。
    # ⚠️ 没有这个上限时：视频永久缓冲（`paused=True, ended=False`，且线路切换函数
    # 没报错）会让 `while not media_completed` 每 0.5s 空转**永远不退出**；
    # 文档 `percent` 卡在 99.x 或滚动高度一直在长也一样。用户在界面看到的是
    # "一直不动也不结束"，只能手动点停止。
    TASK_STALL_TIMEOUT = 300.0

    def run_video_task(self, target_page, task_frame, target_container, task_sig,
                       completed_video_urls):
        media_completed = False
        inner_err_count = 0
        last_percent = -1
        line_switch_count = 0
        hub = self.hub
        self.task_text = '正在播放音视频'
        last_progress_at = time.time()
        try:
            task_frame.evaluate(core.HACK_SCRIPT)
        except Exception:
            pass

        while not media_completed:
            if self.check_pause_and_stop() or target_page.is_closed():
                return None
            # 进展看门狗：进度百分比变了就刷新时间戳，长时间不变即放弃该任务点。
            if time.time() - last_progress_at > self.TASK_STALL_TIMEOUT:
                hub.emit_log(f'         [放弃] 视频 {int(self.TASK_STALL_TIMEOUT)}s 无进展'
                             f'（仍停在 {self.video_text}），跳过该任务点。')
                completed_video_urls.add(task_sig)
                return None
            try:
                is_finished = target_container.evaluate(
                    '(container) => container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\');')
                if is_finished:
                    hub.emit_log('         [完成] 平台已标记完成。')
                    completed_video_urls.add(task_sig)
                    self.video_text = '已完成'
                    media_completed = True
                    return None
                try:
                    current_speed = float(self.settings.get('video_speed', 2.0))
                except Exception:
                    current_speed = 2.0
                task_frame.evaluate(core.HACK_SCRIPT)
                task_frame.evaluate(f'window.hackVideo({current_speed})')
                status = task_frame.evaluate(
                    "() => { try { let v = document.querySelector('video, audio'); "
                    "let ended_by_event = window.__my_video_task_done === true; "
                    "if (!v) return {ended: true, percent: '100.0', paused: false}; "
                    "let duration = v.duration || 0; let currentTime = v.currentTime || 0; "
                    "let percent = '0.0'; if (duration > 0) { percent = (currentTime / duration * 100).toFixed(1); } "
                    "let ended = ended_by_event || v.ended || (duration > 0 && currentTime >= v.duration - 1.5); "
                    "return { ended: ended, percent: percent, paused: v.paused }; "
                    "} catch(e) { return {error: e.toString()}; } }")
                if 'error' in status:
                    raise Exception(status['error'])
                inner_err_count = 0
                # ⚠️ 进程退出后 `evaluate` 可能返回 None；`status['percent']` 这种下标
                # 取值一旦缺键/为 None 就会抛 KeyError/TypeError（虽然被下面兜住，
                # 但会白白吃掉 5 次容错额度并最终"假装完成"）。统一走 `.get`。
                status = status or {}
                if not isinstance(status, dict):
                    raise Exception('视频状态未返回有效结果')
                percent_txt = str(status.get('percent', '0.0'))
                if percent_txt != f'{last_percent}.0' and percent_txt != str(last_percent):
                    last_progress_at = time.time()
                if not status.get('paused', True) and not status.get('ended', False):
                    self.accumulated_video_seconds += 0.5
                    if self.accumulated_video_seconds >= 600.0:
                        self.accumulated_video_seconds -= 600.0
                        hub.emit_log('      [看课] 已累计观看满 10 分钟（仅本地计时，无任何上报）。')

                if status.get('paused') and not status.get('ended'):
                    is_line_error = task_frame.evaluate('window.hackLineSwitch()')
                    if is_line_error:
                        hub.emit_log('         [警告] 视频源异常，正在自动切换线路...')
                        line_switch_count += 1
                        last_progress_at = time.time()   # 切线路也算"有动作"，别被看门狗误杀
                        recovered = False
                        for _ in range(15):
                            if self.check_pause_and_stop():
                                break
                            time.sleep(0.2)
                            is_playing = task_frame.evaluate(
                                "() => { let v = document.querySelector('video, audio'); "
                                "return v ? (!v.paused && v.readyState >= 2) : false; }")
                            if is_playing:
                                hub.emit_log('         [自愈] 已恢复播放。')
                                recovered = True
                                break
                        if not recovered and line_switch_count >= 3:
                            hub.emit_log('         [警告] 线路频繁更换，重载播放容器...')
                            try:
                                target_container.evaluate(
                                    "(container) => { let iframe = container.querySelector('iframe'); "
                                    "if (iframe) { let src = iframe.src; iframe.src = ''; "
                                    "setTimeout(() => { iframe.src = src; }, 100); } }")
                                line_switch_count = 0
                                time.sleep(2.0)
                            except Exception:
                                pass
                        continue
                    task_frame.evaluate("() => { let v = document.querySelector('video, audio'); if(v) v.play(); }")
                else:
                    try:
                        current_percent_float = float(percent_txt)
                        if int(current_percent_float) != last_percent:
                            hub.emit_log(f'         [视频] 播放进度: {percent_txt}%')
                            self.video_text = f'{percent_txt}%'
                            last_percent = int(current_percent_float)
                            last_progress_at = time.time()
                    except (TypeError, ValueError):
                        pass
                if status.get('ended'):
                    hub.emit_log('         [完成] 音视频播放结束。')
                    self.video_text = '已完成'
                    completed_video_urls.add(task_sig)
                    try:
                        task_frame.evaluate("() => { let v = document.querySelector('video, audio'); "
                                            "if (v) { v.pause(); v.src = ''; v.load(); v.remove(); } }")
                    except Exception:
                        pass
                    media_completed = True
                    return None
            except Exception as loop_ex:
                inner_err_count += 1
                hub.emit_log(f'         [警告] 状态监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_video_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.5)
        return None

    def run_doc_task(self, target_page, task_frame, target_container, task_sig,
                     completed_doc_urls):
        doc_completed = False
        inner_err_count = 0
        hub = self.hub
        self.task_text = '步进阅读文档'
        last_progress_at = time.time()
        last_percent = ''

        def _percent_value(text):
            try:
                return float(str(text).replace('%', ''))
            except (TypeError, ValueError):
                return -1.0

        while not doc_completed:
            if self.check_pause_and_stop() or target_page.is_closed():
                return None
            # 看门狗：`percent` 长时间不动（滚动高度一直在长、或卡在 99.x）
            # 就放弃该任务点，否则外层 while 永远转下去（用户只能手动停止）。
            if time.time() - last_progress_at > self.TASK_STALL_TIMEOUT:
                hub.emit_log(f'         [放弃] 文档 {int(self.TASK_STALL_TIMEOUT)}s 无进展'
                             f'（停在 {last_percent or "?"}%），跳过该任务点。')
                completed_doc_urls.add(task_sig)
                return None
            try:
                is_finished = target_container.evaluate(
                    '(container) => container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\');')
                if is_finished:
                    hub.emit_log('         [完成] 平台已标记文档完成。')
                    completed_doc_urls.add(task_sig)
                    self.video_text = '已完成'
                    doc_completed = True
                    return None
                task_frame.evaluate(core.SCROLL_SCRIPT)
                status = task_frame.evaluate('window.autoScrollDocument()')
                # ⚠️ `evaluate` 在 frame 已导航/脚本未注入时返回 None；原来直接
                # `'error' in status` ⇒ `TypeError: argument of type 'NoneType' is
                # not iterable`，被下面当成"普通异常"吃掉 5 次后**把任务标记成已完成**
                # （等于没读就跳过）。
                if not isinstance(status, dict):
                    raise Exception('滚动脚本未返回结果（frame 可能已失效）')
                if status.get('error'):
                    raise Exception(status['error'])
                inner_err_count = 0
                percent_txt = str(status.get('percent', '0.0'))
                if percent_txt != last_percent:
                    last_percent = percent_txt
                    last_progress_at = time.time()
                hub.emit_log(f'         [文档] 阅读进度: {percent_txt}%')
                self.video_text = f'阅读进度 {percent_txt}%'
                # 触底判定：脚本说 ended，或进度已经到 99% 以上（`toFixed(1)` 常常
                # 停在 99.9/99.8 而永远不出现字面量 "100.0"），或已到 100%。
                if status.get('ended') or _percent_value(percent_txt) >= 99.0:
                    hub.emit_log('         [完成] 文档已触底。')
                    self.video_text = '已完成'
                    completed_doc_urls.add(task_sig)
                    doc_completed = True
                    return None
            except Exception as loop_ex:
                inner_err_count += 1
                hub.emit_log(f'         [警告] 文档监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_doc_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.4)
        return None
