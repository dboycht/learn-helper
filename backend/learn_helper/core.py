# -*- coding: utf-8 -*-
"""UI 无关的刷课/答题核心（1.0.4 从旧 Tk 客户端 `learn_helper.py` 迁移而来）。

迁移纪律（逐条对应，便于核对）：
- 逻辑**逐字保真**，只把「界面调用」换成返回值 / 异常：
  旧代码里的 ``self.log(...)`` 由 engine 负责，本模块只保留返回值与 LOGGER 写盘。
- **线程铁律不变**：本模块里所有碰 Playwright 的函数（截图、题型识别、填涂、
  浏览器连接）都必须在**创建 Playwright 的那个线程**里调用；只有纯网络的
  ``solve_question`` 允许并发（``run_parallel`` 用守护线程）。
- 远端契约不变：``GET /check_version``、``POST /solve``（无卡密/点数）。
"""

import base64
import io
import json
import os
import re
import subprocess
import threading
import time

import requests

from .config import (
    ANSWER_MODE_LABELS,
    BASE_DIR,
    LOG_DIR,
    LOGGER,
    SCHOOL_ID,
    effective_server_url,
    get_answer_cfg,
    get_device_id,
    get_llm_cfg,
)

# 全局退出信号：置位后并发求解会放弃剩余在途请求（见 run_parallel / engine.stop）
SHUTDOWN = threading.Event()

# 上一次启动的浏览器进程（供退出时 terminate 兜底）
BROWSER_EXE = 'msedge.exe'
BROWSER_PATHS = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
]
CDP_URL = 'http://127.0.0.1:9222'


def require_playwright():
    """按需导入 Playwright；缺依赖时抛可读异常（不阻塞服务启动）。"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as e:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError(
            f'缺少 Playwright（{e}）。请安装：pip install playwright && playwright install chromium；'
            f'发布版应自带后端 exe，无需本机 Python。') from e


# ----------------------------------------------------------------------------
# 注入页面的脚本（本地常量，非服务器下发）
# ----------------------------------------------------------------------------
HACK_SCRIPT = '''
try {
    // 伪装前台
    Object.defineProperty(document, 'hidden', { value: false, writable: false });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', writable: false });
    window.onblur = null;
    window.onfocus = null;
} catch (e) {}

// 倍速 + 静音播放，并挂结束监听
window.hackVideo = function(speed) {
    try {
        let target_speed = speed || 2.0;
        let videos = document.getElementsByTagName('video');
        let count = 0;
        for (let i = 0; i < videos.length; i++) {
            let v = videos[i];
            if (v) {
                v.muted = true;
                if (v.playbackRate !== target_speed) { v.playbackRate = target_speed; }
                if (!v.__my_lock_added) {
                    v.addEventListener('ended', () => { window.__my_video_task_done = true; });
                    v.addEventListener('timeupdate', () => {
                        if (v.duration > 0 && v.currentTime >= v.duration - 1.5) {
                            window.__my_video_task_done = true;
                        }
                    });
                    v.__my_lock_added = true;
                }
                count++;
            }
        }
        return count;
    } catch(e) { return 0; }
}

// 视频源异常时自动切换到其它线路
window.hackLineSwitch = function() {
    try {
        let bodyText = document.body ? document.body.innerText : "";
        if (!/格式不支持|网络的问题|无法加载|其他线路/.test(bodyText)) { return false; }
        let elements = Array.from(document.querySelectorAll('a, button, span, div.btn, li'));
        let targets = elements.filter(el => {
            let txt = el.innerText || "";
            return /公网|线路|推荐|默认|CDN|极速/.test(txt) && el.offsetHeight > 5 && el.offsetWidth > 5;
        });
        if (targets.length === 0) return false;
        if (window.__last_line_idx === undefined) { window.__last_line_idx = 0; }
        let idx = window.__last_line_idx % targets.length;
        targets[idx].click();
        window.__last_line_idx++;
        return true;
    } catch (e) { return false; }
}
'''

SCROLL_SCRIPT = '''
window.autoScrollDocument = function() {
    try {
        let pan = document.querySelector('#panView');
        if (pan && pan.contentWindow) {
            let doc = pan.contentWindow.document;
            let win = pan.contentWindow;
            let current = doc.documentElement.scrollTop || doc.body.scrollTop || 0;
            let maxScroll = doc.documentElement.scrollHeight || doc.body.scrollHeight || 0;
            let clientH = win.innerHeight || doc.documentElement.clientHeight || 0;
            if (maxScroll > clientH) {
                win.scrollTo(0, current + 400);
                let nextCurrent = doc.documentElement.scrollTop || doc.body.scrollTop || 0;
                let reached = (nextCurrent + clientH) >= (maxScroll - 30);
                return { ended: reached, percent: ((nextCurrent + clientH)/maxScroll*100).toFixed(1) };
            }
        }
        let container = document.querySelector('#container') || document.querySelector('#scrollBox');
        if (container) {
            let current = container.scrollTop;
            let maxScroll = container.scrollHeight;
            let clientH = container.clientHeight;
            if (maxScroll > clientH) {
                container.scrollTop = current + 400;
                let reached = (container.scrollTop + clientH) >= (maxScroll - 30);
                return { ended: reached, percent: ((container.scrollTop + clientH)/maxScroll*100).toFixed(1) };
            }
        }
        let current = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;
        let maxScroll = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
        let clientH = window.innerHeight || document.documentElement.clientHeight || 0;
        if (maxScroll > clientH) {
            window.scrollBy(0, 400);
            let nextCurrent = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;
            let reached = (nextCurrent + clientH) >= (maxScroll - 30);
            return { ended: true, percent: ((nextCurrent + clientH)/maxScroll*100).toFixed(1) };
        }
        return { ended: true, percent: "100.0" };
    } catch(e) {
        return { ended: true, percent: "100.0", error: e.toString() };
    }
}
'''

# 填空/简答答案注入脚本（UEditor / textarea / iframe / input / contenteditable 多轨写入）
FILL_TEXT_SCRIPT = '''

(element, answers) => {
    let filled_count = 0;
    if (!answers || answers.length === 0) return filled_count;
    let blankContainers = Array.from(element.querySelectorAll('.blankItemDiv'));
    if (blankContainers.length === 0) {
        let allInputs = Array.from(element.querySelectorAll('textarea[id^="answer"], textarea, input[type="text"], input.blank_input, div[contenteditable="true"]'));
        blankContainers = allInputs.length > 0 ? allInputs : [element];
    }
    function fillSingle(container, text) {
        if (!container || text === undefined || text === null) return false;
        let success = false;
        let cleanText = String(text);
        let textareas = Array.from(container.querySelectorAll ? container.querySelectorAll('textarea[id^="answer"], textarea') : []);
        if (container.tagName === 'TEXTAREA') textareas.push(container);
        for (let ta of textareas) {
            let taId = ta.id;
            if (window.UE) {
                try {
                    if (taId && window.UE.getEditor) { let ed = window.UE.getEditor(taId); if (ed && ed.setContent) { ed.setContent(cleanText); success = true; } }
                } catch(e) {}
                if (!success && window.UE.instants) {
                    for (let key in window.UE.instants) {
                        let inst = window.UE.instants[key];
                        if (inst && (inst.key === taId || inst.textarea === ta || (taId && inst.key && inst.key.includes(taId)))) {
                            try { inst.setContent(cleanText); success = true; break; } catch(e) {}
                        }
                    }
                }
            }
            try {
                ta.value = cleanText;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
                ta.dispatchEvent(new Event('blur', { bubbles: true }));
                success = true;
            } catch(e) {}
        }
        let iframes = Array.from(container.querySelectorAll ? container.querySelectorAll('iframe[id^="ueditor_"], iframe') : []);
        if (container.tagName === 'IFRAME') iframes.push(container);
        for (let ifr of iframes) {
            try {
                let doc = ifr.contentDocument || (ifr.contentWindow ? ifr.contentWindow.document : null);
                if (doc && doc.body) {
                    doc.body.innerHTML = '<p>' + cleanText.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</p>';
                    doc.body.dispatchEvent(new Event('input', { bubbles: true }));
                    doc.body.dispatchEvent(new Event('change', { bubbles: true }));
                    success = true;
                }
            } catch(e) {}
        }
        let inputs = Array.from(container.querySelectorAll ? container.querySelectorAll('input[type="text"], input.blank_input, input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])') : []);
        if (container.tagName === 'INPUT' && container.type !== 'hidden' && container.type !== 'radio' && container.type !== 'checkbox') inputs.push(container);
        for (let ipt of inputs) {
            try { ipt.focus(); ipt.value = cleanText; ipt.dispatchEvent(new Event('input', { bubbles: true })); ipt.dispatchEvent(new Event('change', { bubbles: true })); ipt.dispatchEvent(new Event('blur', { bubbles: true })); success = true; } catch(e) {}
        }
        let editables = Array.from(container.querySelectorAll ? container.querySelectorAll('div[contenteditable="true"]') : []);
        if (container.getAttribute && container.getAttribute('contenteditable') === 'true') editables.push(container);
        for (let ed of editables) {
            try { ed.focus(); ed.innerText = cleanText; ed.dispatchEvent(new Event('input', { bubbles: true })); ed.dispatchEvent(new Event('change', { bubbles: true })); ed.dispatchEvent(new Event('blur', { bubbles: true })); success = true; } catch(e) {}
        }
        return success;
    }
    for (let i = 0; i < answers.length; i++) {
        if (i < blankContainers.length && fillSingle(blankContainers[i], answers[i])) filled_count++;
    }
    if (filled_count === 0 && answers.length > 0 && fillSingle(element, answers[0])) filled_count++;
    return filled_count;
}

'''

LATEX_EXTRACT_JS = '''
(element) => {
    function decodeAndClean(latexData) {
        try {
            let decoded = decodeURIComponent(latexData);
            decoded = decoded.replace(/^"+|"+$/g, '').replace(/^%22+|%22+$/g, '');
            return decoded;
        } catch(e) { return latexData; }
    }
    function traverse(node) {
        let text = "";
        if (node.nodeType === 3) {
            text += node.textContent;
        } else if (node.nodeType === 1) {
            if (node.tagName === "INPUT" && node.type === "hidden") return "";
            if (node.tagName === "SCRIPT" || node.tagName === "STYLE") return "";
            if (node.tagName === "IMG" && node.classList.contains("ans-latex-moudle")) {
                let latexData = node.getAttribute("data") || node.getAttribute("data-original") || "";
                if (latexData) return " " + decodeAndClean(latexData) + " ";
            }
            for (let child of node.childNodes) text += traverse(child);
        }
        return text;
    }
    return traverse(element);
}
'''

# 页面关键元素探测用的选择器
DIAG_SELECTORS = [
    'div.ans-attach-ct', '.ans-attach-online', '.ans-cc', 'div[class*="attach"]',
    'iframe', 'video', 'audio', '#panView', '#container', '#scrollBox',
    'div.singlequesid', 'div.TiMu', '.question-card', '.prev_ul li', '.prev_tab li',
]


# ----------------------------------------------------------------------------
# 通用助手
# ----------------------------------------------------------------------------
def keep_computer_awake(release=False):
    """阻止/恢复系统自动休眠与熄屏（挂机必备）。

    老 Tk 版在启动时就调它（`learn_helper.py` 的启动序列），2.x 重构时**没搬过来**
    ⇒ 这个函数一直没人调用，等于"挂机一晚上、中途系统睡了"（见 ERROR.md E53
    同类问题：重构漏搬行为）。

    用法：**开始刷课时调一次**（`release=False`），结束时调 `release=True` 撤销。
    用 `ES_CONTINUOUS` 表示"这个状态一直有效，直到下次调用改掉"，所以不必轮询。
    """
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (0 if release else ES_SYSTEM_REQUIRED)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        return None
    return None


def clean_text_for_gui(text):
    if not text:
        return ''
    clean_text = text.replace('\xa0', ' ').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
    clean_text = ' '.join(clean_text.split())
    clean_chars = [c for c in clean_text if ord(c) < 65535]
    return ''.join(clean_chars).strip()


def is_cdp_port_open(port=9222, timeout=1.0):
    """探测本机 CDP 调试端口是否已开。"""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
    except Exception:
        return False
    return result == 0


def kill_and_launch_browser():
    """探测 9222 调试端口，必要时拉起沙盒浏览器（Edge/Chrome）。返回 (就绪, Popen)。"""
    if is_cdp_port_open():
        return (True, None)

    valid_path = None
    try:
        subprocess.run([BROWSER_EXE, '--version'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        valid_path = BROWSER_EXE
    except Exception:
        for path in BROWSER_PATHS:
            if os.path.exists(path):
                valid_path = path
                break
    if not valid_path:
        return (False, None)

    profile_path = os.path.join(BASE_DIR, 'browser_profile')
    if not os.path.exists(profile_path):
        try:
            os.makedirs(profile_path)
        except Exception:
            pass
    lock_file = os.path.join(profile_path, 'SingletonLock')
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    proc = None
    try:
        cmd_args = [
            valid_path,
            '--remote-debugging-port=9222',
            '--user-data-dir=' + profile_path,
            '--restore-last-session',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-session-crashed-bubble',
            '--disable-renderer-backgrounding',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-features=msEdgeFirstRunImport,SwitchIntranetSitesToWorkProfile,msEdgeSharedData,msEdgeDeleteBrowsingDataOnExit,CalculateWindowOcclusion,UseEcoQoSForBackgroundProcess',
        ]
        proc = subprocess.Popen(cmd_args, shell=False)
        for _ in range(10):
            time.sleep(1)
            if is_cdp_port_open():
                return (True, proc)
        # ⚠️ 拉了但 9222 一直没起来 ⇒ **必须把进程收掉**再返回失败：
        # 原来直接 `return (False, None)`，调用方拿不到句柄、也就永远杀不掉它，
        # 桌面上留一个"半死"的沙盒 Edge（用户视角：明明报失败，却多出一个浏览器窗口）。
        LOGGER.warning('[浏览器] 拉起后 10s 内 9222 未就绪，回收该进程。')
        return (False, None)
    except Exception as e:
        LOGGER.warning(f'[浏览器] 启动异常: {e}')
        return (False, None)
    finally:
        # 走到这里说明"没成功交给调用方"（成功路径在上面已 return）
        if proc is not None and not is_cdp_port_open():
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
            except Exception:
                pass


def collect_page_labels(context, attempts=10, interval=0.6):
    """读取沙盒浏览器里的标签页标题，返回 (labels, used_url_fallback)。

    冷启动（浏览器刚被拉起）时 Edge 的标签页还在恢复/加载，`pg.title()` 可能暂时为空，
    旧实现直接当成"没有标签页"并提示用户手动打开学习页。这里轮询等待标题，
    等满 attempts 次仍没有标题，才退回用 URL 兜底（至少界面能选到那个页面）。
    """
    titles, urls = [], []
    for _ in range(max(1, int(attempts))):
        titles, urls = [], []
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        for pg in pages:
            try:
                url = (pg.url or '').strip()
                if not url or url == 'about:blank':
                    continue
                if url not in urls:
                    urls.append(url)
                title = (pg.title() or '').strip()
                if title and title not in ('New Tab', '新建标签页', 'about:blank') \
                        and title not in titles:
                    titles.append(title)
            except Exception:
                pass
        if titles:
            return titles, False
        time.sleep(interval)
    return (titles or urls), (not titles and bool(urls))


def connect_over_cdp(sync_playwright=None):
    """连接本机沙盒浏览器的 CDP。返回 (playwright_cm, browser) —— 调用方负责 close。"""
    sp = sync_playwright or require_playwright()
    pw = sp().start()
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
    except Exception:
        pw.stop()
        raise
    return pw, browser


def list_page_titles():
    """短连接：列出浏览器标签页标题（供 HTTP /api/pages）。不用于自动化线程。"""
    pw, browser = connect_over_cdp()
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            return [], False
        return collect_page_labels(ctx, attempts=4, interval=0.4)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def collect_job_containers(page, cards_frame):
    """
    在 卡片帧 / 主帧 / 全部子帧 中寻找「任务容器」并返回 Locator 列表。
    命中即返回该帧的结果（避免跨帧混合）。全部未命中返回 []。
    """
    frames = []
    if cards_frame is not None:
        frames.append(cards_frame)
    if page.main_frame not in frames:
        frames.append(page.main_frame)
    for f in page.frames:
        if f not in frames:
            frames.append(f)
    sels = ['div.ans-attach-ct', '.ans-attach-online', '.ans-cc']
    for fr in frames:
        out = []
        for sel in sels:
            try:
                out.extend(fr.locator(sel).all())
            except Exception:
                pass
        if out:
            try:
                furl = fr.url
            except Exception:
                furl = '?'
            LOGGER.info(f'[识别] 任务容器命中 frame url={furl[:100]} 数量={len(out)}')
            return out
    return []


def diagnose_page(page, log):
    """把当前页面（含所有 frame）的关键元素计数与片段写入日志，便于排查"识别不到"的问题。"""
    try:
        try:
            title = page.title()
        except Exception:
            title = '?'
        log(f'[诊断] 页面标题: {title}')
        log(f'[诊断] 页面 URL : {page.url}')
        frames = [('main', page.main_frame)] + [(f'#{i + 1}', f) for i, f in enumerate(page.frames)]
        for name, fr in frames:
            try:
                furl = fr.url
            except Exception:
                furl = '?'
            counts = {}
            for sel in DIAG_SELECTORS:
                try:
                    counts[sel] = fr.locator(sel).count()
                except Exception:
                    counts[sel] = -1
            nonzero = ', '.join(f'{k}={v}' for k, v in counts.items() if v and v > 0)
            log(f'[诊断] frame<{name}> url={furl[:120]}')
            log(f'        命中: {nonzero if nonzero else "(无关键元素)"}')
            for sel in ('div.ans-attach-ct', '.ans-attach-online', "div[class*='attach']"):
                try:
                    if fr.locator(sel).count() > 0:
                        html = fr.locator(sel).first.evaluate('(e) => e.outerHTML')
                        html = ' '.join((html or '').split())
                        log(f'        [{sel}] 片段: {html[:500]}')
                        break
                except Exception as e:
                    log(f'        [{sel}] dump 失败: {e}')
    except Exception as e:
        log(f'[诊断] 异常: {e}')
    return None


def diagnose_cli_lines():
    """诊断模式下要打印的行（供 engine.diagnose 复用；不直接开窗口）。"""
    from .config import APP_VERSION
    lines = [f'[诊断] learn-helper v{APP_VERSION} 诊断模式',
             f'[诊断] 日志文件: {os.path.join(LOG_DIR, "learn_helper.log")}']
    return lines


def run_diagnose_cli():
    """命令行诊断：连接本机 CDP，dump 所有已打开页面的识别信息。

    用法：``python -m learn_helper --diagnose``
    """
    def log(msg):
        print(msg)
        LOGGER.info(msg)

    for line in diagnose_cli_lines():
        log(line)
    ok, _ = kill_and_launch_browser()
    if not ok:
        log('[诊断] 无法连接/拉起浏览器（9222 不可用）。')
        return
    sync_playwright = require_playwright()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0]
            if not ctx.pages:
                log('[诊断] 浏览器无已打开页面。')
                browser.close()
                return
            for i, pg in enumerate(ctx.pages):
                log(f'========== 页面 {i + 1} / {len(ctx.pages)} ==========')
                diagnose_page(pg, log)
            browser.close()
    except Exception as e:
        log(f'[诊断] 异常: {e}')
    return None


# ----------------------------------------------------------------------------
# 元素查找（多帧回退）
# ----------------------------------------------------------------------------
def find_button_in_frames(page, text_list):
    """在主框架+所有 iframe 中按文本找可见可用按钮，返回 (元素, frame)。"""
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            for text in text_list:
                locators = [
                    frame.locator(f"button:has-text('{text}')"),
                    frame.locator(f"a:has-text('{text}')"),
                    frame.locator(f"input[value*='{text}']"),
                    frame.locator(f"span:has-text('{text}')"),
                    frame.locator(f"div:has-text('{text}')"),
                ]
                for loc in locators:
                    try:
                        count = loc.count()
                    except Exception:
                        continue
                    for idx in range(count):
                        el = loc.nth(idx)
                        try:
                            if el.is_visible() and el.is_enabled():
                                box = el.bounding_box()
                                if box and box['height'] > 5 and box['width'] > 5:
                                    tag_name = el.evaluate('node => node.tagName')
                                    if tag_name == 'DIV' and (box['height'] > 80 or box['width'] > 300):
                                        continue
                                    return (el, frame)
                        except Exception:
                            continue
        except Exception:
            pass
    return (None, None)


def find_next_button(page):
    """找「下一页/下一章/下一节/下一题/下一步」按钮。"""
    el, frame = find_button_in_frames(page, ['下一页', '下一章', '下一节', '下一题', '下一步'])
    if el:
        return (el, frame)
    fallback_selectors = [
        '.next-chapter', "[title='下一节']", ".jb_btn:has-text('下一节')", "span:has-text('下一节')",
        '.next', "[class*='next']", '.btn-next', '#nextChapter', '.prev_next .next',
    ]
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            for sel in fallback_selectors:
                loc = frame.locator(sel)
                count = loc.count()
                for idx in range(count):
                    el = loc.nth(idx)
                    try:
                        if el.is_visible() and el.is_enabled():
                            box = el.bounding_box()
                            if box and box['height'] > 5 and box['width'] > 5:
                                return (el, frame)
                    except Exception:
                        continue
        except Exception:
            pass
    return (None, None)


def find_tab_buttons(cards_frame):
    """找章节卡片 Tab（至少 2 个可见）。"""
    selectors = ['.prev_ul li', '.prev_tab li', '.prev_tab_ul li', "ul[class*='prev'] li", "div[class*='tab'] li"]
    for sel in selectors:
        try:
            locs = cards_frame.locator(sel).all()
            if len(locs) > 1:
                valid_locs = [loc for loc in locs if loc.is_visible()]
                if len(valid_locs) > 1:
                    return valid_locs
        except Exception:
            pass
    return []


def find_confirmation_bypass_button(page):
    """检测「还有任务点未完成」等确认弹窗，尝试点强跳按钮。"""
    bypass_selectors = [
        '.popDiv.wid440.popMove .nextChapter', '.popDiv .nextChapter',
        ".popDiv a:has-text('下一节')", ".popDiv a:has-text('确定')", ".popDiv button:has-text('确定')",
        'a.nextChapter', "[class*='pop'] a:has-text('确定')", "[class*='pop'] button:has-text('确定')",
    ]
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            body_text = frame.evaluate("document.body ? document.body.innerText : ''")
            if any(kw in body_text for kw in ('还有任务点未完成', '未完成的任务点', '确认离开', '当前章节还有', '是否去完成')):
                for sel in bypass_selectors:
                    loc = frame.locator(sel)
                    if loc.count() > 0:
                        btn = loc.first
                        if btn.is_visible() and btn.is_enabled():
                            return (btn, frame)
                fallback_locs = [
                    frame.locator(".popDiv button:has-text('确定')"),
                    frame.locator(".popDiv a:has-text('确定')"),
                    frame.locator(".popDiv a:has-text('继续下一节')"),
                    frame.locator(".popDiv a:has-text('强行下一节')"),
                ]
                for loc in fallback_locs:
                    if loc.count() > 0:
                        btn = loc.first
                        if btn.is_visible():
                            return (btn, frame)
        except Exception:
            pass
    return (None, None)


def robust_wait_for_tasks_to_render(page, check_func, timeout=8000):
    """等待任务卡片渲染（找到 video / 题目 / 测验 iframe 任一）。"""
    for _ in range(5):
        if check_func():
            return False
        time.sleep(0.1)
    start_time = time.time() * 1000
    while time.time() * 1000 - start_time < timeout:
        if check_func():
            return False
        try:
            frames = [page.main_frame] + page.frames
            for f in frames:
                if f.locator('video').count() > 0:
                    time.sleep(0.5)
                    return True
                for quiz_sel in ('div.singlequesid', 'div.singleQuesId', 'div.TiMu',
                                 '.question-card', '.question-item', '.ti-q-c', '.que'):
                    if f.locator(quiz_sel).count() > 0:
                        time.sleep(0.5)
                        return True
                if f.locator('.ans-attach-online').count() > 0:
                    time.sleep(0.5)
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ----------------------------------------------------------------------------
# 求解：内部答题 API / 自配大模型
# ----------------------------------------------------------------------------
SOLVE_PROMPT = (
    '你是一名网课答题助手。请根据附带的题目截图作答。\n'
    '题目类型 {question_type}（choice=单选, multi_choice=多选, blank=填空, essay=简答）。\n'
    '题干文本：{text_source}\n'
    '数量信息：{num_info}（值 0 表示未知）\n'
    '请只输出一个 JSON 对象，禁止任何多余文字：\n'
    '{{"question_type": "{question_type}", '
    '"answer_key": "选择/多选答案为A-F大写字母组合，例如 A 或 ACD；填空/简答填空留空", '
    '"text_answers": ["填空/简答答案数组，填将按顺序填入各空，简答放一个元素"]}}\n'
    '规则：choice/multi_choice 必须给出 answer_key；blank 的 text_answers 长度等于填空个数；essay 的 '
    'text_answers 为一个元素。无法确定时给出你的最佳判断。'
)


def solve_with_llm(image_bytes, q_type, num_blanks, text_source, timeout=180, llm_cfg=None):
    """把题目截图 + 题干发给自己配置的大模型（OpenAI 兼容接口），返回结果 dict。

    图片以 `image_url` 的 data URL 形式放进 user 消息的 content 数组，
    并带 `detail: "high"`（保留原分辨率）—— 题目截图字小且密，低分辨率会看错。
    `llm_cfg` 用于「测试」按钮直接测输入框里当前填的值（可不保存）。
    """
    cfg = llm_cfg or get_llm_cfg()
    if not cfg['api_key']:
        raise ValueError('未配置大模型 API Key，请在「答题设置 → 自配大模型」填写 Base URL / API Key / 模型名')
    base = cfg['base_url'].rstrip('/')
    b64 = base64.b64encode(image_bytes).decode()
    num_info = f'填空数/选项数: {num_blanks}' if (q_type in ('blank', 'essay')) else f'选项数: {num_blanks}'
    prompt = SOLVE_PROMPT.format(question_type=q_type, text_source=text_source or '(无题干)',
                                 num_info=num_info)
    headers = {
        'Authorization': f'Bearer {cfg["api_key"]}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': cfg['model'],
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url',
                 'image_url': {'url': f'data:image/png;base64,{b64}', 'detail': 'high'}},
            ],
        }],
        'temperature': 0.1,
    }
    LOGGER.info(f'[LLM] 调用 {base}/chat/completions model={cfg["model"]} q_type={q_type} '
                f'img={len(image_bytes)}B')
    try:
        res = requests.post(f'{base}/chat/completions', json=payload, headers=headers,
                            timeout=timeout)
    except Exception as e:
        LOGGER.warning(f'[LLM] 请求异常: {e}')
        raise
    if res.status_code != 200:
        LOGGER.warning(f'[LLM] HTTP {res.status_code}: {(res.text or "")[:300]}')
        raise RuntimeError(f'大模型返回 {res.status_code}: {res.text[:300]}')
    try:
        content = res.json()['choices'][0]['message']['content']
    except Exception as e:
        LOGGER.warning(f'[LLM] 响应格式异常: {e}；body={(res.text or "")[:300]}')
        raise RuntimeError(f'大模型响应格式异常: {e}')
    ans = parse_llm_answer(content, q_type)
    LOGGER.info(f'[LLM] 命中 answer_key={ans["answer_key"]!r} '
                f'texts={len(ans["text_answers"])}')
    return ans


def parse_llm_answer(content, q_type):
    """把大模型返回的文本解析成统一答案格式（尽力鲁棒）。"""
    content = (content or '').strip()
    # ⚠️ 用贪心 `\\{.*\\}` 而不是 `\\{[^{}]*\\}`：后者匹配不了任何**嵌套**的 JSON
    # （`{"answer_key":"AC","text_answers":["x"]}` 里的方括号没问题，但只要模型
    # 顺手包一层 `{"result":{...}}` 就直接失配），失配后又掉进下面"抓字母"的兜底。
    m = re.search(r'\{.*\}', content, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            at = obj.get('answer_key')
            ta = obj.get('text_answers')
            qt = str(obj.get('question_type') or q_type)
            # ⚠️ 后端/模型可能给 `"answer_key": null` 或数字 ⇒ 原来只判了
            # `isinstance(at, str)` 才清洗，但下面 `len(at)` 对 None 会抛 TypeError。
            at = normalize_choice_keys(at) if isinstance(at, str) else ''
            if not isinstance(ta, (list, tuple)):
                ta = [ta] if ta else []
            ta = [str(x) for x in ta if x is not None]
            if qt in ('blank', 'essay'):
                return {'question_type': qt, 'answer_key': '', 'text_answers': ta}
            return {'question_type': 'multi_choice' if len(at) > 1 else 'choice',
                    'answer_key': at, 'text_answers': []}
    # ⚠️ 兜底**只从"像答案的片段"里取字母**：原来是把整段文本里所有 A-F 都抓出来
    # （"The answer is AC" 会抓成 `THANSA` 之类），然后当成答案交上去 —— 比"答不出"
    # 更糟。这里只认这几类明确形式：
    #   · `答案：AC` / `答案是 B` / `answer: A`
    #   · 整段就是一个/多个选项字母（可带分隔符）
    if q_type in ('blank', 'essay'):
        return {'question_type': q_type, 'answer_key': '', 'text_answers': [content]}
    for pat in (r'(?:答案|answer)\s*[:：是]\s*([A-Fa-f][A-Fa-f\s,、，]*)',
                r'^\s*([A-Fa-f](?:\s*[,、，]?\s*[A-Fa-f])*)\s*$'):
        mm = re.search(pat, content, re.I | re.M)
        if mm:
            keys = normalize_choice_keys(mm.group(1))
            # 抽出 "AC" 这种多选形式时，里面的分隔符已经被清掉
            keys = re.sub(r'[^A-F]', '', keys)
            if keys:
                return {'question_type': 'multi_choice' if len(keys) > 1 else 'choice',
                        'answer_key': keys, 'text_answers': []}
    return {'question_type': q_type, 'answer_key': '', 'text_answers': []}


def _normalize_answer(data, q_type):
    """把后端返回统一成 {'question_type','answer_key','text_answers'}（与自配大模型对齐）。"""
    data = data if isinstance(data, dict) else {}
    at = data.get('answer_key')
    # 与 `parse_llm_answer` 走同一套规范化（判断题、多选字母都不会被改错）
    at = normalize_choice_keys(at) if isinstance(at, str) else ''
    ta = data.get('text_answers')
    if not isinstance(ta, (list, tuple)):
        ta = [ta] if ta else []
    ta = [str(x) for x in ta if x is not None and str(x) != '']
    qt = str(data.get('question_type') or q_type)
    if at:
        return {'question_type': 'multi_choice' if len(at) > 1 else 'choice',
                'answer_key': at, 'text_answers': []}
    if ta:
        return {'question_type': qt if qt in ('blank', 'essay') else q_type,
                'answer_key': '', 'text_answers': ta}
    return {'question_type': q_type, 'answer_key': '', 'text_answers': []}


class SolverHTTPError(RuntimeError):
    """4xx（路由不存在 / 鉴权失败 / 请求体不合规范）：重试无意义，调用方应立即放弃。"""


def _post_json(url, payload, timeout):
    """统一的 POST 封装：把 HTTP 错误码翻译成可读中文异常。

    返回 (status_code, data_dict)。
    · 4xx → 抛 SolverHTTPError（**不可重试**）
    · 5xx → 抛 RuntimeError（可重试）
    · requests 层异常（超时/断连）原样抛出，由调用方重试
    """
    res = requests.post(url, json=payload, timeout=timeout)
    detail = ''
    try:
        data = res.json()
        detail = data.get('detail') or data.get('message') or ''
    except Exception:
        data = {}
        detail = (res.text or '')[:200]
    if res.status_code == 200:
        return 200, data
    if res.status_code in (401, 403):
        raise SolverHTTPError(f'后端拒绝访问（HTTP {res.status_code}）：'
                              f'{detail or "接口可能需要鉴权或设备未授权"}')
    if res.status_code == 404:
        raise SolverHTTPError(f'后端没有该接口（HTTP 404）：{detail or url}')
    if 400 <= res.status_code < 500:
        raise SolverHTTPError(f'请求被后端拒绝（HTTP {res.status_code}）：{detail}')
    raise RuntimeError(f'HTTP {res.status_code}: {detail}')


def solve_with_server(image_bytes, q_type, num_blanks, text_source,
                      timeout=None, retry=None, device_id=None, base=None):
    """向自建后端答题模型请求单题答案（内部答题 API）。

    返回 {'question_type','answer_key','text_answers','hash_id','cached'}。
    只重试网络抖动与 5xx；退出过程中（SHUTDOWN 置位）不再发起新请求。
    """
    cfg = get_answer_cfg()
    timeout = timeout or cfg['solver_timeout']
    retry = cfg['retry'] if retry is None else retry
    base = (base or effective_server_url()).rstrip('/')
    payload = {
        'image': base64.b64encode(image_bytes).decode(),
        'question_type': q_type,
        'num_blanks': num_blanks,
        'text_hash_source': text_source or '',
        'school_id': SCHOOL_ID,
        'device_id': device_id or get_device_id(),
    }
    LOGGER.info(f'[内部答题] POST {base}/solve type={q_type} num={num_blanks} '
                f'img={len(image_bytes)}B')
    last_err = None
    for attempt in range(retry + 1):
        if SHUTDOWN.is_set():
            raise RuntimeError('已请求退出，放弃求解')
        try:
            _, data = _post_json(f'{base}/solve', payload, timeout)
            ans = _normalize_answer(data, q_type)
            ans['hash_id'] = data.get('hash_id') or ''
            ans['cached'] = bool(data.get('cached'))
            LOGGER.info(f'[内部答题] 命中 answer_key={ans["answer_key"]!r} '
                        f'texts={len(ans["text_answers"])} cached={ans["cached"]}')
            return ans
        except SolverHTTPError:
            raise                      # 4xx：重试没有意义，立即放弃
        except Exception as e:
            last_err = e
            if attempt < retry:
                wait = 1.5 * (attempt + 1)
                LOGGER.warning(f'[内部答题] 第 {attempt + 1} 次失败({e})，{wait:.1f}s 后重试')
                time.sleep(wait)
    raise RuntimeError(f'内部答题接口连续 {retry + 1} 次失败：{last_err}')


def probe_backend(timeout=8, base=None):
    """「测试连接」用：探活自建后端（GET /check_version）。返回 (ok, message)。

    `base` 用于直接测界面输入框里当前填的地址（可不保存）。
    """
    from .config import APP_VERSION
    base = (base or effective_server_url()).rstrip('/')
    try:
        res = requests.get(f'{base}/check_version',
                           params={'ver': APP_VERSION, 'school_id': SCHOOL_ID},
                           timeout=timeout)
    except Exception as e:
        return False, f'无法连接 {base}\n{e}'
    detail = ''
    try:
        data = res.json()
        detail = data.get('detail') or ''
    except Exception:
        data = {}
        detail = (res.text or '')[:200]
    if res.status_code == 200:
        notice = str(data.get('notice') or '（后端未返回公告）').strip()
        return True, (f'连接成功 ✓\n\n后端地址: {base}\n公告: {notice}\n'
                      f'强制更新: {"是" if data.get("force_update") else "否"}\n'
                      f'设备指纹: {get_device_id()}')
    return False, f'后端返回 HTTP {res.status_code}\n{detail}'


def solve_question(image_bytes, q_type, num_blanks, text_source, mode, cfg=None):
    """统一求解入口：按「答题方式」分派到内部答题 API / 自配大模型。

    ⚠️ 纯网络调用，**不碰 Playwright**，因此可以在守护线程里并发（run_parallel）。
    """
    cfg = cfg or get_answer_cfg()
    if mode == 'server':
        return solve_with_server(image_bytes, q_type, num_blanks, text_source,
                                 timeout=cfg['solver_timeout'], retry=cfg['retry'])
    if mode == 'llm':
        return solve_with_llm(image_bytes, q_type, num_blanks, text_source,
                              timeout=cfg['solver_timeout'])
    raise RuntimeError('当前答题方式为「仅识别不答题」，不应调用求解')


# ----------------------------------------------------------------------------
# 「测试图片」自检：题目图片在内存里合成，不依赖任何外部素材
# ----------------------------------------------------------------------------
TEST_CASES = (
    {
        'name': '单选题',
        'question_type': 'choice',
        'num_blanks': 4,
        'zh': {'stem': '中国的首都是哪座城市？',
               'choices': ['A. 上海', 'B. 北京', 'C. 广州', 'D. 深圳']},
        'ascii': {'stem': 'Which city is the capital of China?',
                  'choices': ['A. Shanghai', 'B. Beijing', 'C. Guangzhou', 'D. Shenzhen']},
        'expect_key': 'B',
    },
    {
        'name': '多选题',
        'question_type': 'multi_choice',
        'num_blanks': 4,
        'zh': {'stem': '下列哪些属于哺乳动物？（多选）',
               'choices': ['A. 鲸鱼', 'B. 鲨鱼', 'C. 蝙蝠', 'D. 鳄鱼']},
        'ascii': {'stem': 'Which of these are mammals? (select all)',
                  'choices': ['A. Whale', 'B. Shark', 'C. Bat', 'D. Crocodile']},
        'expect_key': 'AC',
    },
    {
        'name': '填空题',
        'question_type': 'blank',
        'num_blanks': 1,
        'zh': {'stem': '填空题：一年有 ____ 个月。', 'choices': []},
        'ascii': {'stem': 'Fill in the blank: there are ____ months in a year.',
                  'choices': []},
        'expect_texts_any': ('12', '十二', 'twelve'),
    },
)

_TEST_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhbd.ttc',
    'C:/Windows/Fonts/simhei.ttf', 'C:/Windows/Fonts/simsun.ttc',
)


def _test_font(size=20):
    """找能渲染中文的字体；返回 (font, has_cjk)。找不到就退化为默认字体 + 纯英文题。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None, False
    for path in _TEST_FONT_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size), True
        except Exception:
            pass
    try:
        return ImageFont.load_default(), False
    except Exception:
        return None, False


def build_test_question_image(case):
    """把内置测试题渲染成 PNG 字节（模拟真实题目截图）。

    返回 (png_bytes, text_source, used_cjk)；无中文字体时自动改用英文题面。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        raise RuntimeError(f'缺少 Pillow，无法生成测试图片（pip install pillow）：{e}')
    font, has_cjk = _test_font(20)
    body = case['zh'] if has_cjk else case['ascii']
    stem = body['stem']
    choices = list(body.get('choices') or [])
    text_source = (stem + ' ' + ' '.join(choices)).strip()

    width = 840
    height = 110 + 38 * max(1, len(choices))
    img = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((18, 18), '1. ' + stem, font=font, fill=(0, 0, 0))
    y = 66
    for choice in choices:
        draw.text((42, y), choice, font=font, fill=(0, 0, 0))
        y += 38
    draw.rectangle([0, 0, width - 1, height - 1], outline=(190, 195, 205), width=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue(), text_source, has_cjk


def _grade_case(case, ans):
    """判定单题结果，返回 (ok, got_text, verdict)。verdict: pass | mismatch | empty。"""
    got = ans.get('answer_key') or ' / '.join(ans.get('text_answers') or []) or ''
    got = str(got).strip()
    if not got:
        return False, got, 'empty'
    if case.get('expect_key'):
        want = set(str(case['expect_key']).upper())
        good = set(got.upper()) == want
        return good, got, ('pass' if good else 'mismatch')
    low = got.lower()
    good = any(str(k).lower() in low for k in (case.get('expect_texts_any') or ()))
    return good, got, ('pass' if good else 'mismatch')


def run_solve_self_test(mode='server', timeout=None, retry=1, base=None, llm_cfg=None):
    """用内置测试图跑一遍指定通道（server=内部答题 API / llm=自配大模型），逐题给出结论。

    返回 (ok_all, lines, results)；纯网络调用，调用方应放到后台线程执行。
    """
    acfg = get_answer_cfg()
    limit = acfg['solver_timeout'] if timeout is None else int(timeout)
    limit = max(10, min(limit, 120))          # 自检不必等满 240s
    channel = ANSWER_MODE_LABELS.get(mode, mode)
    lines, results = [], []
    for case in TEST_CASES:
        try:
            image, text_source, _has_cjk = build_test_question_image(case)
        except Exception as e:
            results.append({'name': case['name'], 'ok': False, 'verdict': 'build_fail',
                            'got': '', 'ms': 0, 'detail': str(e)})
            lines.append(f'✗ {case["name"]}：测试图生成失败 — {e}')
            continue
        t0 = time.time()
        try:
            if mode == 'server':
                ans = solve_with_server(image, case['question_type'], case['num_blanks'],
                                        text_source, timeout=limit, retry=retry, base=base)
            else:
                ans = solve_with_llm(image, case['question_type'], case['num_blanks'],
                                     text_source, timeout=limit, llm_cfg=llm_cfg)
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            results.append({'name': case['name'], 'ok': False, 'verdict': 'request_fail',
                            'got': '', 'ms': ms, 'detail': str(e)})
            lines.append(f'✗ {case["name"]}：请求失败（{ms} ms）— {e}')
            continue
        ms = int((time.time() - t0) * 1000)
        ok, got, verdict = _grade_case(case, ans)
        want = case.get('expect_key') or '/'.join(case.get('expect_texts_any') or ())
        tags = []
        if ans.get('cached'):
            tags.append('后端缓存命中')
        if ans.get('hash_id'):
            tags.append(f'hash_id={ans["hash_id"]}')
        suffix = f'（{ms} ms' + ('，' + '，'.join(tags) if tags else '') + '）'
        if verdict == 'empty':
            lines.append(f'⚠ {case["name"]}：接口通了，但没返回可用的答案{suffix}')
        elif ok:
            lines.append(f'✓ {case["name"]}：识别正确，返回 {got}（期望 {want}）{suffix}')
        else:
            lines.append(f'⚠ {case["name"]}：接口正常但答案不符，返回 {got}（期望 {want}）{suffix}')
        results.append({'name': case['name'], 'ok': ok, 'verdict': verdict,
                        'got': got, 'ms': ms, 'detail': ''})
    if lines:
        lines.append(f'（本次通过「{channel}」通道测试）')
    for ln in lines:                      # 写进日志文件，事后可追溯
        LOGGER.info(f'[自检/{mode}] {ln}')
    return (bool(results) and all(r['ok'] for r in results)), lines, results


def summarize_self_test(results):
    """把自检结果汇成一句话：区分"接口不通"和"接口通但答得不对"。"""
    total = len(results)
    if not total:
        return '自检未执行'
    passed = sum(1 for r in results if r['ok'])
    reached = sum(1 for r in results if r['verdict'] in ('pass', 'mismatch', 'empty'))
    if reached == 0:
        return f'自检失败 ✗：{total} 题全部请求失败（接口或地址不可用）'
    if passed == total:
        return f'自检通过 ✓：{passed}/{total} 题识别正确'
    return (f'自检部分通过 ⚠：{passed}/{total} 题识别正确；'
            f'另有 {reached - passed} 题接口返回正常但答案不符或为空')


# ----------------------------------------------------------------------------
# 并发工具：用**守护线程**实现，不用 ThreadPoolExecutor
#   ThreadPoolExecutor 的工作线程是非守护的，解释器退出时会 join 它们；
#   退出时若有在途 /solve 请求（最长 solver_timeout 秒）就会卡住进程，
#   而守护线程随进程结束，能保证"点了退出就立刻退"。
# ----------------------------------------------------------------------------
def run_parallel(items, worker, workers=4, on_progress=None):
    """并发对 items 执行 worker(item)，最多 workers 个同时进行。

    · 线程全为 daemon：退出时不等在途请求；
    · SHUTDOWN 置位后立即返回，未开工的项直接放弃。
    """
    workers = max(1, min(16, int(workers or 1)))
    sem = threading.Semaphore(workers)
    lock = threading.Lock()
    state = {'done': 0}

    def runner(item):
        with sem:
            if SHUTDOWN.is_set():
                return
            try:
                worker(item)
            except Exception as e:
                item['error'] = ('error', str(e))
            finally:
                with lock:
                    state['done'] += 1
                    done = state['done']
                if on_progress is not None:
                    try:
                        on_progress(done)
                    except Exception:
                        pass

    threads = [threading.Thread(target=runner, args=(it,), daemon=True) for it in items]
    for t in threads:
        t.start()
    # ⚠️ 等待必须有**上限**：worker 里是网络调用，`requests` 的 timeout 覆盖不了
    # "DNS 卡住 / 代理黑洞"这类情况，而主流程是在自动化线程里等这个函数的 ⇒
    # 没有上限就等于"刷课永久卡在求解阶段"，用户只能点停止。
    # 上限取"单题超时 + 60s 余量"：正常情况（含重试）一定在这个范围内结束，
    # 真到了说明有请求僵死，放弃它们（daemon 线程随进程结束）比卡死好。
    try:
        per_item = float(get_answer_cfg().get('solver_timeout') or 240)
    except Exception:
        per_item = 240.0
    deadline = time.time() + per_item + 60.0
    for t in threads:
        while t.is_alive():
            t.join(0.2)
            if SHUTDOWN.is_set():
                return None      # 放弃在途请求：daemon 线程随进程结束
            if time.time() > deadline:
                LOGGER.warning('[求解] 等待并发求解超时，放弃剩余在途请求')
                return None
    return None


# ----------------------------------------------------------------------------
# 关闭沙盒浏览器（退出流程用）
# ----------------------------------------------------------------------------
def close_sandbox_browser(browser_proc=None, timeout=5.0):
    """关闭由本程序拉起的沙盒浏览器（独立 `browser_profile`，不碰用户正常 Edge）。

    1) 优先走 CDP 的 `Browser.close`：能覆盖"复用已有 9222"的情况；
    2) 兜底 terminate 我们自己 Popen 出来的进程。
    返回是否执行过关闭动作。
    """
    acted = False
    try:
        sync_playwright = require_playwright()
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(CDP_URL, timeout=3000)
            session = b.new_browser_cdp_session()
            session.send('Browser.close')
            acted = True
            LOGGER.info('[退出] 已通过 CDP Browser.close 关闭沙盒浏览器')
    except Exception as e:
        LOGGER.info(f'[退出] CDP 关闭浏览器未成功（{e}），改用进程方式')
    if browser_proc is not None:
        try:
            if browser_proc.poll() is None:
                browser_proc.terminate()
                try:
                    browser_proc.wait(timeout=timeout)
                except Exception:
                    browser_proc.kill()
                acted = True
                LOGGER.info('[退出] 已终止沙盒浏览器进程')
        except Exception as e:
            LOGGER.info(f'[终止浏览器进程失败: {e}]')
    return acted


# ----------------------------------------------------------------------------
# 题目识别 / 填涂 / 提交
# ----------------------------------------------------------------------------
def scan_page_recursively(page):
    """在主框架与所有 iframe 中递归寻找题目节点。返回 ([locators], frame)。"""
    class_selectors = [
        'div.singleQuesId', 'div.singlequesid', 'div.TiMu', '.question-card',
        '.question-item', '.test-item', '.Tm_cont', '.problem', '.exercise',
        '.ti-q-c', '.que', '.multiquesid',
    ]
    xpath_selector = ("//input[(@type='radio' or @type='checkbox')]/ancestor::div[contains(@class, 'que') "
                      "or contains(@class, 'item') or contains(@class, 'box') or string-length(@class)>2]")
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            for sel in class_selectors:
                found = frame.locator(sel).all()
                if len(found) > 0:
                    return (found, frame)
            with_xpath = frame.locator(xpath_selector).all()
            if len(with_xpath) > 0:
                valid_qs = []
                for q in with_xpath:
                    try:
                        box = q.bounding_box()
                        if box and box['height'] > 50:
                            valid_qs.append(q)
                    except Exception:
                        pass
                if len(valid_qs) > 0:
                    return (valid_qs, frame)
        except Exception:
            pass
    return ([], None)


def extract_clean_text_with_latex(question_locator):
    """提取题干文本（含 LaTeX 图片内容）并清洗。"""
    try:
        raw = question_locator.evaluate(LATEX_EXTRACT_JS)
        clean = clean_text_for_gui(raw)
        clean = re.sub(r'^\d+[\s\.、]*', '', clean)
        clean = re.sub(r'[\(（]\s*\d+(\.\d+)?\s*分\s*[\)）]', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean
    except Exception as e:
        LOGGER.info(f'[题干] LaTeX 提取失败，回退 inner_text: {e}')
        try:
            return clean_text_for_gui(question_locator.inner_text())
        except Exception:
            return ''


def detect_question_type_and_inputs(question_locator):
    """识别题型，返回 (类型, 数量)。类型：choice / multi_choice / blank / essay。"""
    try:
        timu_el = question_locator.locator('div.TiMu').first
        if timu_el.count() > 0:
            timu_type = timu_el.get_attribute('data')
            if timu_type in ('0', '1', '3'):
                options_count = question_locator.locator(
                    "li.before-after, li[role='radio'], li[role='checkbox']").count()
                is_multi = (timu_type == '1') or (question_locator.locator(
                    "li[role='checkbox'], input[type='checkbox']").count() > 0)
                q_type_res = 'multi_choice' if is_multi else 'choice'
                return (q_type_res, options_count if options_count > 0 else 4)
            if timu_type == '2':
                blanks_count = question_locator.locator('.blankItemDiv').count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("textarea[id^='answer']").count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("iframe[id^='ueditor_']").count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("input[type='text'], input.blank_input").count()
                return ('blank', blanks_count if blanks_count > 0 else 1)
            if timu_type in ('4', '5', '6'):
                return ('essay', 1)
        options = question_locator.locator("li.before-after, li[role='radio'], li[role='checkbox']").all()
        if len(options) > 0:
            return ('choice', len(options))
        blanks = question_locator.locator(
            ".blankItemDiv, textarea[id^='answer'], iframe[id^='ueditor_'], input[type='text']").all()
        if len(blanks) > 0:
            return ('blank', len(blanks))
        return ('essay', 1)
    except Exception:
        return ('essay', 1)


def normalize_choice_keys(answer_key):
    """把答案键规范成"A"~"F"的字符串（选择题用）。

    ⚠️ **必须整体判定，不能逐个字母替换**（2026-09-19 修，见 ERROR.md E64）。
    原来的写法是：
        `answer_key.upper().replace('对','A').replace('TRUE','A').replace('T','A')...`
    这些都是**整串替换**，于是 `'AF'` 里的 `F` 被换成了 `B`（`'AF' → 'AB'`），
    多选答案"选 A 和 F"会被填成"选 A 和 B"——**答案被静默改错还报"填涂成功"**。
    `'T'`/`'F'`（判断题的"对/错"）同理。
    正确做法：先看整串是不是"真/假"这类词，是就映射成单个 A/B；否则只做
    "保留 A-F、转大写"的清洗。
    """
    text = str(answer_key or '').strip().upper()
    if not text:
        return ''
    # 判断题映射**只在整串就是那个词时**生效。原来的写法是整串 replace，
    # 会把多选答案里的字母也换掉：`'AF' → 'AB'`（F 被当成"错"）、
    # `'ABF' → 'ABB'` —— 答案被静默改错，还照样报"填涂成功"、照样提交。
    # 注意：选项字母只有 A~F，所以清洗时 `'T'` 会被丢掉（`'TF' → 'F'`）——
    # 这是对的：题面里根本没有 T 选项。
    if text in ('对', '正确', 'TRUE', 'T', '√', 'YES', 'Y', '1'):
        return 'A'
    if text in ('错', '错误', 'FALSE', 'F', '×', 'NO', 'N', '0'):
        return 'B'
    # 多选/单选：只保留 A-F 并去重保序（'A,C' → 'AC'）
    out = []
    for ch in text:
        if ch in 'ABCDEF' and ch not in out:
            out.append(ch)
    return ''.join(out)


def fill_and_click_smart(question_locator, response_data):
    """按答题结果填涂题目。response_data 形如
       {'question_type':..., 'answer_key':'AC', 'text_answers':[...]}。"""
    q_type = response_data.get('question_type', 'choice')
    text_answers = response_data.get('text_answers') or []
    answer_key = response_data.get('answer_key') or ''
    try:
        if q_type in ('choice', 'multi_choice') or text_answers:
            if not answer_key:
                return False
            clean_keys = normalize_choice_keys(answer_key)
            target_set = set(clean_keys)

            def parse_option_letter(el, idx):
                txt = ''
                data_val = ''
                try:
                    txt = (el.inner_text() or '').strip().upper()
                except Exception:
                    pass
                match = re.search(r'^[A-F]', txt)
                if match:
                    return match.group(0)
                if any(kw in txt for kw in ('对', '正确', 'TRUE', '√')):
                    return 'A'
                if any(kw in txt for kw in ('错', '错误', 'FALSE', '×')):
                    return 'B'
                try:
                    data_val = (el.get_attribute('data') or '').strip().upper()
                except Exception:
                    pass
                if data_val in ('A', 'B', 'C', 'D', 'E', 'F'):
                    return data_val
                if data_val in ('TRUE', '对'):
                    return 'A'
                if data_val in ('FALSE', '错'):
                    return 'B'
                alphabet = ['A', 'B', 'C', 'D', 'E', 'F']
                return alphabet[idx] if idx < len(alphabet) else ''

            spans = question_locator.locator(
                'span.num_option, span.num_option_dx, span.check_answer, span.check_answer_dx').all()
            if not spans:
                spans = question_locator.locator(
                    "li.before-after, li[role='radio'], li[role='checkbox']").all()

            if len(spans) > 0:
                all_correct = True
                for idx, el in enumerate(spans):
                    try:
                        letter = parse_option_letter(el, idx)
                        classes = el.get_attribute('class') or ''
                        is_selected = ('check_answer' in classes) or ('check_answer_dx' in classes)
                        should_select = letter in target_set
                        if is_selected != should_select:
                            all_correct = False
                            break
                    except Exception:
                        all_correct = False
                        break
                if all_correct:
                    return True
                for idx, el in enumerate(spans):
                    try:
                        letter = parse_option_letter(el, idx)
                        classes = el.get_attribute('class') or ''
                        is_selected = ('check_answer' in classes) or ('check_answer_dx' in classes)
                        should_select = letter in target_set
                        if should_select and not is_selected:
                            el.scroll_into_view_if_needed()
                            time.sleep(0.05)
                            el.click(True, force=True)
                            time.sleep(0.12)
                        elif not should_select and is_selected and q_type == 'multi_choice':
                            el.scroll_into_view_if_needed()
                            time.sleep(0.05)
                            el.click(True, force=True)
                            time.sleep(0.12)
                    except Exception as e:
                        LOGGER.info(f'[填涂] 选项 {idx + 1} 异常: {e}')
                return True
            else:
                inputs = question_locator.locator("input[type='radio'], input[type='checkbox']").all()
                if inputs:
                    alphabet = ['A', 'B', 'C', 'D', 'E', 'F']
                    for char in target_set:
                        if char in alphabet:
                            target_idx = alphabet.index(char)
                            if target_idx < len(inputs):
                                target_ipt = inputs[target_idx]
                                try:
                                    if not target_ipt.is_checked():
                                        target_ipt.scroll_into_view_if_needed()
                                        target_ipt.click(True, force=True)
                                        time.sleep(0.12)
                                except Exception:
                                    pass
                    return True
                return False
        if q_type in ('blank', 'essay') and text_answers:
            try:
                filled_ok = question_locator.evaluate(FILL_TEXT_SCRIPT, text_answers)
                return filled_ok > 0
            except Exception as e:
                LOGGER.info(f'[填涂] {q_type} JS 写入异常: {e}')
                return False
        return False
    except Exception as ex:
        LOGGER.info(f'[填涂] 智能填涂异常: {ex}')
        return False


def _find_by_selectors(page, selectors):
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            for sel in selectors:
                loc = frame.locator(sel)
                count = loc.count()
                for idx in range(count):
                    el = loc.nth(idx)
                    try:
                        if el.is_visible() and el.is_enabled():
                            box = el.bounding_box()
                            if box and box['height'] > 5 and box['width'] > 5:
                                return (el, frame)
                    except Exception:
                        continue
        except Exception:
            pass
    return (None, None)


def find_submit_button(page):
    """找「提交作业/提交」按钮。"""
    el, frame = find_button_in_frames(page, ['提交作业', '提交', '确认提交'])
    if el:
        return (el, frame)
    fallback = ['.submit', "[class*='submit']", '.btn-submit', '#submitButton', '.btn_ok']
    return _find_by_selectors(page, fallback)


def find_save_button(page):
    """找「暂存/保存」按钮。"""
    el, frame = find_button_in_frames(page, ['暂存', '保存答案', '保存'])
    if el:
        return (el, frame)
    fallback = ['.save', "[class*='save']", '.btn-save', '#saveButton']
    return _find_by_selectors(page, fallback)


def check_quiz_completed(questions, target_frame):
    """判断测试页是否已被平台标记完成/已作答。"""
    try:
        if target_frame:
            done = target_frame.evaluate(
                '() => { let b = document.body ? document.body.innerText : ""; '
                'let t = /得分：|成绩：|已提交|已完成|我的答案|正确答案|查看作答|已批阅|本题得\\s*\\d+/.test(b); '
                'let i = document.querySelectorAll(\'input[type="radio"], input[type="checkbox"], textarea\'); '
                'let d = i.length > 0 && Array.from(i).every(e => e.disabled); '
                'let m = document.querySelectorAll(\'.answer-right, .score, .scoreNum, .dui, .cuo, [class*="score"]\').length > 0; '
                'return t || d || m; }')
            if done:
                return True
            fe = target_frame.frame_element()
            if fe:
                return fe.evaluate(
                    '(iframe) => { let p = iframe.closest(\'div.ans-attach-ct\') || iframe.closest(\'.ans-attach-online\'); '
                    'return p ? (p.classList.contains("ans-job-finished") || /ans-job-finished|icon_Completed|jobFinish|job-finished/.test(p.className || \'\')) : false; }')
    except Exception as e:
        LOGGER.info(f'[答题] 完成态检查异常: {e}')
    return False
