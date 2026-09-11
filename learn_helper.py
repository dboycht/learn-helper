# -*- coding: utf-8 -*-
"""
学习助理 (learn-helper)
版本: 1.0.2      刷课（视频/文档）+ 答题（默认接入自建后端答题模型）。

设计要点
- 控制逻辑（点击/翻页/刷视频/滚文档）全部本地实现；浏览器内脚本为本地常量注入。
- **不含任何计费 / 卡密 / 点数逻辑**（2026-09-11 按要求移除）：不调 `/points`、
  不调 `/solve/checkout`、无充值与商城入口，`/solve` 也不再发送 `card_key`。
- 远端调用点只剩两个，都指向自建后端（地址由使用者自己配置）：
    GET  /check_version     公告 / 强制更新
    POST /solve             内部答题模型：题目截图 + 题干求解（带设备指纹）
- 答题方式三选一（「答题设置」窗口，存 config.json）：内部答题 API（默认）/
  自配大模型（OpenAI 兼容）/ 仅识别不答题。
- 远端地址优先级：环境变量 LH_SERVER_URL > 同目录 config.json 的 server_url > 默认 127.0.0.1:8000。
- 退出：关闭窗口或点「终止并退出」→ 运行中才二次确认 → 停自动化 → 放弃在途请求
  → 断开 CDP → 关闭沙盒浏览器 → 退出进程。
"""

import base64
import io
import json
import os
import re
import sys
import subprocess
import time
import ctypes
import logging
import threading
from logging.handlers import RotatingFileHandler

try:
    import requests
    from playwright.sync_api import sync_playwright
except Exception as _imp_err:
    _msg = (f'依赖导入失败: {_imp_err}\n\n'
            f'请使用 Python 3.10 运行，并安装依赖:\n    pip install playwright requests\n\n'
            f'当前解释器: {sys.executable}')
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk()
        _r.withdraw()
        _mb.showerror('学习助理 · 启动失败', _msg)
        _r.destroy()
    except Exception:
        print(_msg)
    sys.exit(1)

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

APP_VERSION = '1.0.2'
SCHOOL_ID = 'nuaa'

# 全局退出信号：置位后并发求解会放弃剩余在途请求（见 run_parallel / shutdown_and_exit）
SHUTDOWN = threading.Event()

BROWSER_EXE = 'msedge.exe'
BROWSER_PATHS = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
]


def _load_server_url():
    default = 'http://127.0.0.1:8000'
    try:
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        if os.path.exists(cfg):
            with open(cfg, 'r', encoding='utf-8') as f:
                url = (json.load(f) or {}).get('server_url')
            if url:
                return url.rstrip('/')
    except Exception:
        pass
    return os.environ.get('LH_SERVER_URL', default).rstrip('/')


# 启动时快照（兼容旧引用）；运行期统一走 get_server_url()，以便 UI 改址即时生效
SERVER_URL = _load_server_url()

# ----------------------------------------------------------------------------
# 日志系统：写文件（logs/learn_helper.log，滚动）+ 可镜像到 GUI
# ----------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger('learn_helper')
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        path = os.path.join(LOG_DIR, 'learn_helper.log')
        fh = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
        lg.addHandler(fh)
    return lg


LOGGER = setup_logger()


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


# ----------------------------------------------------------------------------
# 通用助手
# ----------------------------------------------------------------------------
def keep_computer_awake():
    """通过 Windows API 阻止系统自动休眠/熄屏（挂机必备）。"""
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 1
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
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


def kill_and_launch_browser():
    """探测本机 9222 调试端口，必要时拉起沙盒浏览器（Edge/Chrome）。返回 (就绪, Popen)。"""
    import socket

    def is_port_open():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', 9222))
            sock.close()
        except Exception:
            return False
        return result == 0

    if is_port_open():
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

    project_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(project_dir, 'browser_profile')
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
            if is_port_open():
                return (True, proc)
        return (False, None)
    except Exception:
        return (False, None)


def get_device_id():
    """基于机器指纹的稳定设备标识（供后端限设备）。"""
    try:
        import uuid as machine_uuid
        import hashlib
        return 'DEV-' + hashlib.md5(str(machine_uuid.getnode()).encode()).hexdigest()[:12].upper()
    except Exception:
        return 'DEV-UNKNOWN'


# 页面关键元素探测用的选择器
DIAG_SELECTORS = [
    'div.ans-attach-ct', '.ans-attach-online', '.ans-cc', 'div[class*="attach"]',
    'iframe', 'video', 'audio', '#panView', '#container', '#scrollBox',
    'div.singlequesid', 'div.TiMu', '.question-card', '.prev_ul li', '.prev_tab li',
]


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
            # 若存在任务容器，dump 一段 outerHTML
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


def run_diagnose_cli():
    """命令行诊断：连接本机 CDP，dump 所有已打开页面的识别信息。用法：python learn_helper.py --diagnose"""
    def log(msg):
        print(msg)
        LOGGER.info(msg)

    log(f'[诊断] learn-helper v{APP_VERSION} 诊断模式')
    log(f'[诊断] 日志文件: {os.path.join(LOG_DIR, "learn_helper.log")}')
    ok, _ = kill_and_launch_browser()
    if not ok:
        log('[诊断] 无法连接/拉起浏览器（9222 不可用）。')
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
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
# 答题：题目识别 / 截图 / 求解（内部答题 API 或自配大模型）/ 填涂 / 提交
# ----------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

DEFAULT_LLM = {
    'base_url': 'https://api.openai.com/v1',
    'api_key': '',
    'model': 'gpt-4o',
}


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    """整体写入 config.json（保留原有内容由调用方负责，见 update_config）。"""
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        LOGGER.error(f'保存 config.json 失败: {e}')
        return False


def update_config(patch):
    """**合并式**写入：只覆盖 patch 里给出的键，其余段落原样保留。

    修复 1.0.2 的缺陷——原 save_config 直接把整个 config.json 覆盖成 {'llm': {...}}，
    会把用户填过的 server_url 等键整段抹掉。
    """
    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(cfg[k])
            merged.update(v)
            cfg[k] = merged
        else:
            cfg[k] = v
    return save_config(cfg)


def get_server_url():
    """每次调用都重读配置：在「答题设置」里改完地址即时生效，无需重启。"""
    return _load_server_url()


def get_llm_cfg():
    cfg = load_config().get('llm', {}) or {}
    return {
        'base_url': cfg.get('base_url') or DEFAULT_LLM['base_url'],
        'api_key': cfg.get('api_key') or '',
        'model': cfg.get('model') or DEFAULT_LLM['model'],
    }


# ----------------------------------------------------------------------------
# 答题方式（接入自建后端答题模型；契约已去掉原程序的卡密/点数计费）
#   server —— 走自建后端 POST /solve：截图 + 题干 + 设备指纹，客户端零配置（默认）
#   llm    —— 自配大模型通道（OpenAI 兼容 /chat/completions），保留可切换
#   off    —— 只识别题目并记录，不求解、不填涂
# ----------------------------------------------------------------------------
DEFAULT_ANSWER = {
    'mode': 'server',
    'solver_timeout': 240,   # /solve 单题超时（原程序 150~300s）
    'workers': 4,            # 并发求解线程数（原程序 ThreadPoolExecutor(max_workers=4)）
    'retry': 2,              # 单题失败重试次数
}

ANSWER_MODES = (
    ('server', '内部答题 API',
     '走自建后端 POST /solve：题目截图 + 题干发给自己的答题模型（推荐）'),
    ('llm', '自配大模型',
     '直连你自己的 OpenAI 兼容接口（Base URL / API Key / 模型名）'),
    ('off', '仅识别不答题',
     '只识别题型与题干并写入日志，不求解、不填涂、不提交'),
)

ANSWER_MODE_LABELS = {m: label for m, label, _ in ANSWER_MODES}
ANSWER_MODE_TIPS = {m: tip for m, _, tip in ANSWER_MODES}


def get_answer_cfg():
    """读取答题配置（缺项自动补默认值）。"""
    cfg = load_config().get('answer', {}) or {}
    out = dict(DEFAULT_ANSWER)
    for k, default in DEFAULT_ANSWER.items():
        v = cfg.get(k, default)
        if isinstance(default, bool):
            out[k] = bool(v)
        elif isinstance(default, int):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = default
        else:
            out[k] = v if v else default
    if out['mode'] not in ANSWER_MODE_LABELS:
        out['mode'] = DEFAULT_ANSWER['mode']
    out['solver_timeout'] = max(10, min(600, out['solver_timeout']))
    out['workers'] = max(1, min(16, out['workers']))
    out['retry'] = max(0, min(5, out['retry']))
    return out


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


def solve_with_llm(image_bytes, q_type, num_blanks, text_source):
    """把题目截图 + 题干发给自己配置的大模型（OpenAI 兼容接口），返回结果 dict。"""
    cfg = get_llm_cfg()
    if not cfg['api_key']:
        raise ValueError('未配置大模型 API Key，请点击「答题设置」→「自配大模型」填写 Base URL / API Key / 模型名')
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
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ],
        }],
        'temperature': 0.1,
    }
    LOGGER.info(f'[LLM] 调用 {base}/chat/completions model={cfg["model"]} q_type={q_type}')
    res = requests.post(f'{base}/chat/completions', json=payload, headers=headers, timeout=180)
    if res.status_code != 200:
        raise RuntimeError(f'大模型返回 {res.status_code}: {res.text[:300]}')
    content = res.json()['choices'][0]['message']['content']
    return parse_llm_answer(content, q_type)


def parse_llm_answer(content, q_type):
    """把大模型返回的文本解析成统一答案格式（尽力鲁棒）。"""
    content = (content or '').strip()
    m = re.search(r'\{[^{}]*\}', content, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            at = obj.get('answer_key')
            ta = obj.get('text_answers')
            qt = str(obj.get('question_type') or q_type)
            if isinstance(at, str):
                at = re.sub(r'[^A-Fa-f]', '', at).upper()
            if not isinstance(ta, (list, tuple)):
                ta = [ta] if ta else []
            ta = [str(x) for x in ta if x is not None]
            if qt in ('blank', 'essay'):
                return {'question_type': qt, 'answer_key': '', 'text_answers': ta}
            return {'question_type': 'multi_choice' if len(at) > 1 else 'choice',
                    'answer_key': at, 'text_answers': []}
    # 兜底：抽字母或整段当文本
    letters = re.findall(r'[A-Fa-f]', content)
    if q_type in ('blank', 'essay'):
        return {'question_type': q_type, 'answer_key': '', 'text_answers': [content]}
    return {'question_type': 'choice', 'answer_key': ''.join(letters[:6]).upper(), 'text_answers': []}


# ----------------------------------------------------------------------------
# 内部答题 API 客户端（自建后端答题模型）
# 本项目简化后的契约（**已移除原程序的卡密 / 点数 / 结算计费**）：
#   POST /solve   {image(base64),question_type,num_blanks,
#                  text_hash_source,school_id,device_id}       timeout = 配置项
#        200 {answer_key?,text_answers[]?,hash_id?,cached?}
#        非 200 {"detail": str}
# ----------------------------------------------------------------------------


def _normalize_answer(data, q_type):
    """把后端返回统一成 {'question_type','answer_key','text_answers'}（与自配大模型对齐）。"""
    data = data if isinstance(data, dict) else {}
    at = data.get('answer_key')
    at = re.sub(r'[^A-Fa-f]', '', at).upper() if isinstance(at, str) else ''
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


def _post_json(url, payload, timeout):
    """统一的 POST 封装：把 HTTP 错误码翻译成可读中文异常。

    返回 (status_code, data_dict)。requests 层异常原样抛出，由调用方重试。
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
        raise RuntimeError(f'后端拒绝访问（HTTP {res.status_code}）：'
                           f'{detail or "接口可能需要鉴权或设备未授权"}')
    if res.status_code == 404:
        raise RuntimeError(f'后端没有该接口（HTTP 404）：{detail or url}')
    raise RuntimeError(f'HTTP {res.status_code}: {detail}')


def solve_with_server(image_bytes, q_type, num_blanks, text_source,
                      timeout=None, retry=None, device_id=None):
    """向自建后端答题模型请求单题答案（内部答题 API）。

    返回 {'question_type','answer_key','text_answers','hash_id','cached'}。
    只重试网络抖动与 5xx；退出过程中（SHUTDOWN 置位）不再发起新请求。
    """
    cfg = get_answer_cfg()
    timeout = timeout or cfg['solver_timeout']
    retry = cfg['retry'] if retry is None else retry
    base = get_server_url()
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
        except Exception as e:
            last_err = e
            if attempt < retry:
                wait = 1.5 * (attempt + 1)
                LOGGER.warning(f'[内部答题] 第 {attempt + 1} 次失败({e})，{wait:.1f}s 后重试')
                time.sleep(wait)
    raise RuntimeError(f'内部答题接口连续 {retry + 1} 次失败：{last_err}')


def probe_backend(timeout=8):
    """「测试连接」用：探活自建后端（GET /check_version）。返回 (ok, message)。"""
    base = get_server_url()
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
    """统一求解入口：按「答题方式」分派到内部答题 API / 自配大模型。"""
    cfg = cfg or get_answer_cfg()
    if mode == 'server':
        return solve_with_server(image_bytes, q_type, num_blanks, text_source,
                                 timeout=cfg['solver_timeout'], retry=cfg['retry'])
    if mode == 'llm':
        return solve_with_llm(image_bytes, q_type, num_blanks, text_source)
    raise RuntimeError('当前答题方式为「仅识别不答题」，不应调用求解')


# ----------------------------------------------------------------------------
# 「测试图片」自检：题目图片在内存里合成，不依赖任何外部素材
#   点一下按钮 → 生成 3 张内置测试题图片 → 逐张 POST /solve → 报告
#   "接口通不通 / 模型认不认得 / 答案对不对"（三种结果分开报，便于定位问题）
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


def run_solve_self_test(timeout=None, retry=1):
    """用内置测试图跑一遍当前「内部答题 API」，逐题给出结论。

    返回 (ok_all, lines, results)；纯网络调用，界面侧请放到后台线程执行。
    """
    acfg = get_answer_cfg()
    limit = acfg['solver_timeout'] if timeout is None else int(timeout)
    limit = max(10, min(limit, 120))          # 自检不必等满 240s
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
            ans = solve_with_server(image, case['question_type'], case['num_blanks'],
                                    text_source, timeout=limit, retry=retry)
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
    for t in threads:
        while t.is_alive():
            t.join(0.2)
            if SHUTDOWN.is_set():
                return None      # 放弃在途请求：daemon 线程随进程结束
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
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=3000)
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
            LOGGER.info(f'[退出] 终止浏览器进程失败: {e}')
    return acted


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


def fill_and_click_smart(question_locator, response_data):
    """按大模型返回的答案填涂题目。response_data 形如
       {'question_type':..., 'answer_key':'AC', 'text_answers':[...]}。"""
    q_type = response_data.get('question_type', 'choice')
    text_answers = response_data.get('text_answers') or []
    answer_key = response_data.get('answer_key') or ''
    try:
        if q_type in ('choice', 'multi_choice') or text_answers:
            if not answer_key:
                return False
            clean_str = (answer_key.upper().replace('对', 'A').replace('TRUE', 'A')
                         .replace('T', 'A').replace('正确', 'A'))
            clean_str = (clean_str.replace('错', 'B').replace('FALSE', 'B')
                         .replace('F', 'B').replace('错误', 'B'))
            clean_keys = re.sub(r'[^A-F]', '', clean_str)
            target_set = set(clean_keys)

            def parse_option_letter(el, idx):
                txt = ''
                data_val = ''
                try:
                    if not el.is_visible():
                        pass
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
                # 先判断是否已全对
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
                # 修正勾选
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


# ----------------------------------------------------------------------------
# 主面板
# ----------------------------------------------------------------------------
class AppConsole:
    def __init__(self, root):
        self.root = root
        self.root.title(f'学习助理 · 纯刷课 v{APP_VERSION}')
        self.root.geometry('680x560')
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close_window)

        self.COLOR_BG = '#F8F9FA'
        self.COLOR_CARD_BG = '#FFFFFF'
        self.COLOR_PRIMARY = '#1E88E5'
        self.COLOR_PRIMARY_DARK = '#1565C0'
        self.COLOR_CARD_BORDER = '#E2E8F0'
        self.COLOR_TEXT_MAIN = '#2C3E50'
        self.COLOR_TEXT_MUTED = '#7F8C8D'
        self.root.configure(bg=self.COLOR_BG)

        self.browser_proc = None
        self.stop_requested = False
        self.pause_requested = False
        self.log_visible = False
        self.accumulated_video_seconds = 0.0
        self.solver_thread = None
        self.solver_running = False
        self.shutting_down = False

        self.notice_text = '正在连接服务器并同步版本信息...'
        self.scroll_index = 0
        self.notice_loop_id = None

        self.create_widgets()
        self.refresh_answer_mode_label()
        # 先让控制面板显示并短暂置顶，避免被随后拉起的浏览器窗口盖住
        self.root.update_idletasks()
        self.root.lift()
        try:
            self.root.attributes('-topmost', True)
            self.root.after(900, lambda: self.root.attributes('-topmost', False))
        except Exception:
            pass
        self.root.after(500, self.auto_launch_browser_on_start)
        self.check_server_version()
        LOGGER.info('[系统] 控制面板已显示（若被浏览器盖住，请查看任务栏）。')

    def on_close_window(self):
        """窗口关闭（X / Alt+F4）：与「终止并退出」走同一条出口。"""
        return self.confirm_exit()

    def confirm_exit(self):
        """统一的退出入口：运行中先二次确认，再执行完整退出流程。"""
        if getattr(self, 'shutting_down', False):
            return None                     # 防重复点击 / 重复关闭
        if getattr(self, 'solver_running', False):
            if not messagebox.askyesno(
                    '退出确认',
                    '刷课流程仍在运行。\n\n确定要停止并退出吗？\n\n'
                    '退出会放弃在途的答题请求，并关闭本程序拉起的沙盒浏览器。'):
                return None
        self.shutdown_and_exit()
        return None

    def shutdown_and_exit(self):
        """完整退出流程：停自动化 → 放弃在途请求 → 关闭沙盒浏览器 → 退出。"""
        if getattr(self, 'shutting_down', False):
            return None
        self.shutting_down = True
        SHUTDOWN.set()                      # 让并发求解放弃剩余在途请求
        self.stop_requested = True
        self.pause_requested = False

        def _log(msg):
            try:
                self.log(msg)
            except Exception:
                pass

        _log('[退出] 正在停止刷课流程...')
        thread = getattr(self, 'solver_thread', None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)        # 守护线程 + SHUTDOWN：最多等 3 秒
            if thread.is_alive():
                _log('[退出] 刷课线程未在 3s 内收尾，放弃等待（在途请求随进程结束）。')

        # 关掉可能开着的子窗口（答题中心等）
        try:
            for w in list(self.root.winfo_children()):
                if isinstance(w, tk.Toplevel):
                    w.destroy()
        except Exception:
            pass
        # 停掉滚动公告的 after 轮询
        try:
            if self.notice_loop_id:
                self.root.after_cancel(self.notice_loop_id)
                self.notice_loop_id = None
        except Exception:
            pass

        _log('[退出] 正在关闭沙盒浏览器...')
        try:
            acted = close_sandbox_browser(self.browser_proc)
            _log('[退出] 沙盒浏览器已关闭。' if acted else '[退出] 未发现可关闭的沙盒浏览器。')
        except Exception as e:
            _log(f'[退出] 关闭沙盒浏览器异常: {e}')

        _log('[退出] 再见。')
        try:
            LOGGER.info('[退出] 进程退出')
            logging.shutdown()              # 刷盘日志，避免最后几行丢失
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        return None

    def bind_hover(self, widget, hover_bg=None, normal_bg=None, hover_fg=None, normal_fg=None):
        widget.bind('<Enter>', lambda e: widget.config(bg=hover_bg, fg=hover_fg))
        widget.bind('<Leave>', lambda e: widget.config(bg=normal_bg, fg=normal_fg))
        return None

    # ---------------- 界面 ----------------
    def create_widgets(self):
        header_frame = tk.Frame(self.root, bg=self.COLOR_BG, height=40)
        header_frame.pack(fill=tk.X)
        header_frame.pack_propagate(False)

        tk.Frame(self.root, bg=self.COLOR_CARD_BORDER, height=1).pack(fill=tk.X, pady=(0, 10))

        tk.Label(header_frame, text='系统公告', font=('Microsoft YaHei', 8, 'bold'),
                 bg='#E0F2FE', fg='#0369A1', padx=6, pady=2).pack(side=tk.LEFT, padx=(18, 8), pady=8)
        self.lbl_notice = tk.Label(header_frame, text='正在连接服务器并同步版本信息...',
                                   fg='#475569', bg=self.COLOR_BG, font=('Microsoft YaHei', 9))
        self.lbl_notice.pack(side=tk.LEFT, pady=8)
        self.start_scrolling_notice()

        main = tk.Frame(self.root, bg=self.COLOR_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 5))

        # 运行环境状态（原「账户卡密 / 账户余额」付费 UI 已按要求移除）
        row1 = tk.Frame(main, bg=self.COLOR_CARD_BG, highlightthickness=1,
                        highlightbackground=self.COLOR_CARD_BORDER)
        row1.pack(fill=tk.X, pady=6, ipady=6)
        tk.Label(row1, text='后端', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(18, 10))
        self.lbl_backend = tk.Label(row1, text='--', font=('Segoe UI', 9),
                                    bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN)
        self.lbl_backend.pack(side=tk.LEFT)
        self.lbl_device = tk.Label(row1, text='', font=('Segoe UI', 8),
                                   bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MUTED)
        self.lbl_device.pack(side=tk.RIGHT, padx=(0, 15))
        self.lbl_answer_mode = tk.Label(row1, text='答题方式: --', font=('Segoe UI', 9, 'bold'),
                                        bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY)
        self.lbl_answer_mode.pack(side=tk.RIGHT, padx=(0, 20))

        # 网页选择
        row2 = tk.Frame(main, bg=self.COLOR_CARD_BG, highlightthickness=1,
                        highlightbackground=self.COLOR_CARD_BORDER)
        row2.pack(fill=tk.X, pady=(4, 6), ipady=6)
        tk.Label(row2, text='当前网页', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(18, 8))
        self.cb_pages = ttk.Combobox(row2, state='readonly', width=34, font=('Microsoft YaHei', 9))
        self.cb_pages.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.cb_pages.set('[请点击右侧刷新选择网页]')
        self.btn_refresh = tk.Button(row2, text='检测/刷新网页', font=('Microsoft YaHei', 9, 'bold'),
                                     bg=self.COLOR_PRIMARY_DARK, fg='white', relief=tk.FLAT, cursor='hand2',
                                     command=self.refresh_pages, activebackground=self.COLOR_PRIMARY)
        self.btn_refresh.pack(side=tk.RIGHT, padx=(10, 15))
        self.bind_hover(self.btn_refresh, self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)

        # 视频速度
        row3 = tk.Frame(main, bg=self.COLOR_BG)
        row3.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row3, text='视频速度:', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(10, 6))
        self.var_video_speed = tk.StringVar(value='2.0')
        self.cb_speed = ttk.Combobox(row3, textvariable=self.var_video_speed,
                                     values=('1.0', '1.5', '2.0'), state='readonly', width=5,
                                     font=('Microsoft YaHei', 9))
        self.cb_speed.pack(side=tk.LEFT)

        # 做题提交模式 + 答题中心（新版本：内部答题模型接入）
        row4 = tk.Frame(main, bg=self.COLOR_BG)
        row4.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row4, text='做题提交:', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(10, 6))
        self.var_auto_submit = tk.IntVar(value=1)
        self.rad_submit = tk.Radiobutton(row4, text='自动提交', value=1, variable=self.var_auto_submit,
                                         bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN, selectcolor='white',
                                         font=('Microsoft YaHei', 9))
        self.rad_submit.pack(side=tk.LEFT)
        self.rad_save = tk.Radiobutton(row4, text='仅暂存', value=0, variable=self.var_auto_submit,
                                       bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN, selectcolor='white',
                                       font=('Microsoft YaHei', 9))
        self.rad_save.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_llm = tk.Button(row4, text='答题设置', bg=self.COLOR_PRIMARY, fg='white',
                                 font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT, cursor='hand2',
                                 command=self.open_answer_center, activebackground=self.COLOR_PRIMARY_DARK)
        self.btn_llm.pack(side=tk.RIGHT, padx=(0, 10))
        self.bind_hover(self.btn_llm, self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)

        # KPI：视频 / 文档
        dash = tk.Frame(main, bg=self.COLOR_BG)
        dash.pack(fill=tk.X, pady=10)
        left = tk.Frame(dash, bg=self.COLOR_CARD_BG, highlightthickness=1,
                        highlightbackground=self.COLOR_CARD_BORDER, width=315, height=150)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        left.pack_propagate(False)
        tk.Label(left, text='当前页面任务感知', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(anchor=tk.W, padx=15, pady=(12, 4))
        kpis = tk.Frame(left, bg=self.COLOR_CARD_BG)
        kpis.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        def _kpi(parent, desc, attr):
            f = tk.Frame(parent, bg=self.COLOR_CARD_BG)
            f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            lbl = tk.Label(f, text='--', font=('Segoe UI', 18, 'bold'), bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY)
            lbl.pack(pady=(4, 0))
            setattr(self, attr, lbl)
            tk.Label(f, text=desc, font=('Microsoft YaHei', 8), bg=self.COLOR_CARD_BG,
                     fg=self.COLOR_TEXT_MUTED).pack(pady=(2, 0))

        _kpi(kpis, '视频任务', 'lbl_task_video_num')
        _kpi(kpis, '文档阅读', 'lbl_task_doc_num')

        right = tk.Frame(dash, bg=self.COLOR_CARD_BG, highlightthickness=1,
                         highlightbackground=self.COLOR_CARD_BORDER, width=315, height=150)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        right.pack_propagate(False)
        tk.Label(right, text='实时执行进度', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(anchor=tk.W, padx=15, pady=(12, 4))
        self.lbl_prog_task = tk.Label(right, text='当前状态: 闲置中', bg=self.COLOR_CARD_BG,
                                      fg=self.COLOR_PRIMARY_DARK, font=('Microsoft YaHei', 9, 'bold'))
        self.lbl_prog_task.pack(anchor=tk.W, padx=18, pady=(6, 4))
        self.lbl_prog_video = tk.Label(right, text='音视频进度: --', bg=self.COLOR_CARD_BG,
                                       fg=self.COLOR_TEXT_MAIN, font=('Segoe UI', 9))
        self.lbl_prog_video.pack(anchor=tk.W, padx=18, pady=3)
        self.lbl_prog_quiz = tk.Label(right, text='答题进度: --', bg=self.COLOR_CARD_BG,
                                      fg=self.COLOR_TEXT_MAIN, font=('Segoe UI', 9))
        self.lbl_prog_quiz.pack(anchor=tk.W, padx=18, pady=3)

        # 控制按钮
        controls = tk.Frame(main, bg=self.COLOR_BG)
        controls.pack(fill=tk.X, pady=6)
        self.btn_run = tk.Button(controls, text='启动刷课', bg=self.COLOR_PRIMARY_DARK, fg='white',
                                 font=('Microsoft YaHei', 10, 'bold'), relief=tk.FLAT, cursor='hand2',
                                 command=self.start_solver_thread, activebackground='#0D47A1')
        self.btn_run.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5), ipady=5)
        self.bind_hover(self.btn_run, '#0D47A1', self.COLOR_PRIMARY_DARK)
        self.btn_pause = tk.Button(controls, text='暂停进程', bg=self.COLOR_TEXT_MUTED, fg='white',
                                   font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                                   state='disabled', command=self.toggle_pause, activebackground='#95A5A6')
        self.btn_pause.pack(side=tk.LEFT, padx=3, ipady=5)
        self.bind_hover(self.btn_pause, '#95A5A6', self.COLOR_TEXT_MUTED)
        self.btn_stop = tk.Button(controls, text='终止并退出', bg='#D35400', fg='white',
                                  font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                                  state='disabled', command=self.confirm_exit, activebackground='#E67E22')
        self.btn_stop.pack(side=tk.RIGHT, padx=(3, 0), ipady=5)
        self.bind_hover(self.btn_stop, '#E67E22', '#D35400')

        self.btn_diag = tk.Button(controls, text='诊断页面', bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY,
                                  font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                                  command=self.diagnose_current_page, activebackground='#E3F2FD')
        self.btn_diag.pack(side=tk.RIGHT, padx=(3, 6), ipady=5)
        self.bind_hover(self.btn_diag, '#E3F2FD', self.COLOR_CARD_BG)

        self.btn_toggle_log = tk.Button(main, text='展开详细运行日志 ∨', bg=self.COLOR_BG,
                                        fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 9),
                                        relief=tk.FLAT, cursor='hand2', command=self.toggle_log,
                                        activebackground='#E5E7E9')
        self.btn_toggle_log.pack(fill=tk.X, pady=(4, 5))

        self.log_frame = tk.Frame(main, bg=self.COLOR_BG)
        self.log_area = scrolledtext.ScrolledText(self.log_frame, height=9, font=('Consolas', 9),
                                                  bg='#FFFFFF', fg=self.COLOR_TEXT_MAIN, highlightthickness=1,
                                                  highlightbackground=self.COLOR_CARD_BORDER,
                                                  highlightcolor=self.COLOR_PRIMARY)
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.insert(tk.END, f'[系统] 服务就绪。当前版本: {APP_VERSION}\n')
        self.log_area.configure(state='disabled')

    def toggle_log(self):
        if self.log_visible:
            self.log_frame.pack_forget()
            self.btn_toggle_log.configure(text='展开详细运行日志 ∨')
            self.root.geometry('680x560')
            self.log_visible = False
        else:
            self.log_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 5))
            self.btn_toggle_log.configure(text='收起详细运行日志 ∧')
            self.root.geometry('680x800')
            self.log_visible = True
        return None

    def log(self, text):
        """统一日志出口：写文件 + 镜像到 GUI（GUI 操作自动切回主线程）。"""
        try:
            LOGGER.info(text)
        except Exception:
            pass
        if threading.current_thread() is threading.main_thread():
            self._append_log(text)
        else:
            try:
                self.root.after(0, self._append_log, text)
            except Exception:
                pass
        return None

    def _append_log(self, text):
        try:
            self.log_area.configure(state='normal')
            self.log_area.insert(tk.END, text + '\n')
            self.log_area.see(tk.END)
            self.log_area.configure(state='disabled')
        except Exception:
            pass
        return None

    def start_scrolling_notice(self, text_content=None):
        if text_content is not None:
            self.notice_text = text_content
            self.scroll_index = 0
        if self.notice_loop_id is not None:
            self.root.after_cancel(self.notice_loop_id)
            self.notice_loop_id = None
        clean = clean_text_for_gui(self.notice_text)
        max_len = 35
        if len(clean) <= max_len:
            self.lbl_notice.configure(text=clean)
            return None
        padded = clean + '          '
        display = padded[self.scroll_index:] + padded[:self.scroll_index]
        self.lbl_notice.configure(text=display[:max_len])
        self.scroll_index = (self.scroll_index + 1) % len(padded)
        self.notice_loop_id = self.root.after(250, self.start_scrolling_notice)
        return None

    # ---------------- 远端调用（自建后端；已无任何计费接口） ----------------
    def check_server_version(self):
        """GET /check_version：公告与强制更新（后台线程执行，界面不卡）。

        原 query_points / deduct_video_heartbeat（/points、/video_heartbeat 扣点）
        已随计费逻辑一并移除。
        """
        def run():
            try:
                res = requests.get(f'{get_server_url()}/check_version',
                                   params={'ver': APP_VERSION, 'school_id': SCHOOL_ID},
                                   timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    notice = data.get('notice')
                    self.root.after(0, lambda n=notice: self.start_scrolling_notice(n))
                    if data.get('force_update'):
                        def warn():
                            messagebox.showerror('更新提示', '检测到强制更新，请获取新版本。')
                            self.btn_run.configure(state='disabled',
                                                   text='版本已过期，请更新后使用', bg='#BDC3C7')
                        self.root.after(0, warn)
                else:
                    self.root.after(0, lambda: self.start_scrolling_notice('[提示] 无法获取后端公告。'))
            except Exception:
                self.root.after(0, lambda: self.start_scrolling_notice(
                    '[提示] 未连接后端（可在「答题设置」里配置地址，或忽略）。'))
            return None

        threading.Thread(target=run, daemon=True).start()
        return None

    # ---------------- 浏览器管理 ----------------
    def auto_launch_browser_on_start(self):
        self.log('[系统] 正在自动唤醒安全沙盒浏览器...')

        def do_launch():
            ok, proc = kill_and_launch_browser()
            if ok:
                self.browser_proc = proc
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                        context = browser.contexts[0]
                        if not context.pages:
                            context.new_page()
                            time.sleep(0.5)
                        pages_list = []
                        for pg in context.pages:
                            try:
                                title = pg.title()
                                if title and title.strip():
                                    pages_list.append(title.strip())
                            except Exception:
                                pass
                        browser.close()
                        self.root.after(0, lambda: self.update_pages_dropdown_silent(pages_list))
                except Exception:
                    self.root.after(0, lambda: self.log('[系统] 浏览器已就绪，请登录并打开做题/学习页后点刷新。'))
            else:
                self.root.after(0, lambda: self.log('[警告] 浏览器自动打开失败，请确认 9222 端口可用。'))
            return None

        import threading
        threading.Thread(target=do_launch, daemon=True).start()
        return None

    def refresh_pages(self):
        self.btn_refresh.configure(state='disabled', text='正在检测...')
        self.log('[系统] 正在检测并读取浏览器标签页...')
        if not kill_and_launch_browser():
            self.log('[警告] 页面探测失败，浏览器未开启或连接断开。')
            self.btn_refresh.configure(state='normal', text='检测/刷新网页')
            return None

        def do_refresh():
            try:
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                    context = browser.contexts[0]
                    if not context.pages:
                        context.new_page()
                        time.sleep(0.5)
                    pages_list = []
                    for pg in context.pages:
                        try:
                            title = pg.title()
                            if title and title.strip():
                                pages_list.append(title.strip())
                        except Exception:
                            pass
                    browser.close()
                    self.root.after(0, lambda: self.update_pages_dropdown(pages_list))
            except Exception as ex:
                self.log(f'[错误] 页面探测失败: {ex}')
                self.btn_refresh.configure(state='normal', text='检测/刷新网页')
            return None

        import threading
        threading.Thread(target=do_refresh, daemon=True).start()
        return None

    @staticmethod
    def _clean_pages(pages_list):
        return [p.strip() for p in pages_list
                if p and p.strip() and p != 'about:blank' and p != 'New Tab' and p != '新建标签页']

    def _fill_dropdown(self, clean_pages, quiet):
        if not clean_pages:
            self.cb_pages.configure(values=['[安全浏览器当前无活动标签页]'])
            self.cb_pages.set('[安全浏览器当前无活动标签页]')
            self.log('[警告] 安全浏览器内未找到有效标签页，请先在浏览器中点开学习页。'
                     if not quiet else '[系统] 请先在浏览器中点开学习页。')
            return
        self.cb_pages.configure(values=clean_pages)
        keywords = ['学习', '作业', '检测', '测试', '考试', '评估']
        default_idx = 0
        for idx, title in enumerate(clean_pages):
            if any(kw in title for kw in keywords):
                default_idx = idx
                break
        self.cb_pages.current(default_idx)
        self.log('[系统] 页面已锁定，可直接启动刷课。' if not quiet else
                 '[系统] 自动恢复历史页面成功。')

    def update_pages_dropdown_silent(self, pages_list):
        self._fill_dropdown(self._clean_pages(pages_list), quiet=True)
        return None

    def update_pages_dropdown(self, pages_list):
        self.btn_refresh.configure(state='normal', text='检测/刷新网页')
        self._fill_dropdown(self._clean_pages(pages_list), quiet=False)
        return None

    # ---------------- 运行控制 ----------------
    # ---------------- 答题中心（新版本：内部答题模型接入） ----------------
    def open_llm_settings(self):
        """兼容旧入口（主界面旧按钮名），统一跳到「答题中心」。"""
        return self.open_answer_center()

    def open_answer_center(self):
        """答题中心窗口：答题方式切换 / 内部答题 API / 自配大模型，保存到 config.json。

        保存走 update_config（合并式），不会再像 1.0.2 那样把 server_url 等键整段抹掉。
        """
        acfg = get_answer_cfg()
        lcfg = get_llm_cfg()
        v_mode = tk.StringVar(value=acfg['mode'])
        v_server = tk.StringVar(value=load_config().get('server_url') or get_server_url())
        v_timeout = tk.StringVar(value=str(acfg['solver_timeout']))
        v_workers = tk.StringVar(value=str(acfg['workers']))
        v_retry = tk.StringVar(value=str(acfg['retry']))
        v_url = tk.StringVar(value=lcfg['base_url'])
        v_key = tk.StringVar(value=lcfg['api_key'])
        v_model = tk.StringVar(value=lcfg['model'])
        v_show_key = tk.BooleanVar(value=False)

        win = tk.Toplevel(self.root)
        win.title(f'答题中心 · learn-helper v{APP_VERSION}')
        win.geometry('740x700')
        win.resizable(False, False)
        win.configure(bg=self.COLOR_BG)

        # 标题区
        head = tk.Frame(win, bg=self.COLOR_BG)
        head.pack(fill=tk.X, padx=24, pady=(16, 6))
        tk.Label(head, text='答题中心', bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN,
                 font=('Microsoft YaHei', 13, 'bold')).pack(anchor='w')
        tk.Label(head, text='配置题目的求解方式。保存后写入同目录 config.json（已在 .gitignore 中，不会上传）。',
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(anchor='w', pady=(2, 0))

        style = ttk.Style(win)
        try:
            style.configure('Answer.TNotebook', background=self.COLOR_BG, borderwidth=0)
            style.configure('Answer.TNotebook.Tab', font=('Microsoft YaHei', 9), padding=(14, 5))
        except Exception:
            pass
        nb = ttk.Notebook(win, style='Answer.TNotebook')
        nb.pack(fill=tk.BOTH, expand=True, padx=20, pady=(4, 0))

        def _entry(parent, var, show=None, width=None):
            return tk.Entry(parent, textvariable=var, show=show, width=width,
                            font=('Segoe UI', 10), bg='#FFFFFF', fg=self.COLOR_TEXT_MAIN,
                            highlightthickness=1, highlightbackground=self.COLOR_CARD_BORDER,
                            highlightcolor=self.COLOR_PRIMARY)

        def _tip(parent, text):
            return tk.Label(parent, text=text, bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED,
                            font=('Microsoft YaHei', 8), justify='left', anchor='w')

        # ================= 页签 1：答题方式 =================
        tab_mode = tk.Frame(nb, bg=self.COLOR_BG)
        nb.add(tab_mode, text='  答题方式  ')
        tk.Label(tab_mode, text='选择客户端如何得到答案（三种方式互斥，随时可切换）：',
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN,
                 font=('Microsoft YaHei', 9, 'bold')).pack(anchor='w', padx=18, pady=(14, 8))
        for mode, label, tip in ANSWER_MODES:
            card = tk.Frame(tab_mode, bg=self.COLOR_CARD_BG, highlightthickness=1,
                            highlightbackground=self.COLOR_CARD_BORDER)
            card.pack(fill=tk.X, padx=18, pady=5)
            rb = tk.Radiobutton(card, text=label, value=mode, variable=v_mode,
                                bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN, selectcolor='white',
                                activebackground=self.COLOR_CARD_BG, cursor='hand2',
                                font=('Microsoft YaHei', 10, 'bold'))
            rb.pack(anchor='w', padx=12, pady=(9, 0))
            tk.Label(card, text=tip, bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MUTED,
                     font=('Microsoft YaHei', 8), justify='left', anchor='w',
                     wraplength=630).pack(anchor='w', padx=34, pady=(0, 9))
        tk.Label(tab_mode, text='说明：方式为「内部答题 API」时，未识别到答案解析失败不会中断刷课；\n'
                                '方式为「仅识别不答题」时，程序只把题型与题干写进日志，便于排查平台改版。',
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 8),
                 justify='left').pack(anchor='w', padx=18, pady=(10, 0))

        # ================= 页签 2：内部答题 API =================
        tab_api = tk.Frame(nb, bg=self.COLOR_BG)
        nb.add(tab_api, text='  内部答题 API  ')
        grid = tk.Frame(tab_api, bg=self.COLOR_BG)
        grid.pack(fill=tk.X, padx=18, pady=(14, 0))
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text='后端地址', bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN,
                 font=('Microsoft YaHei', 9, 'bold')).grid(row=0, column=0, sticky='w', pady=(8, 0))
        _entry(grid, v_server).grid(row=0, column=1, sticky='ew', padx=(10, 0), pady=(8, 0), ipady=3)
        _tip(grid, '你的答题模型服务地址，例如 http://127.0.0.1:8000'
                   '（也可用环境变量 LH_SERVER_URL，或 config.json 的 server_url）'
             ).grid(row=1, column=1, sticky='w', padx=(10, 0), pady=(2, 0))

        box = tk.Frame(tab_api, bg=self.COLOR_CARD_BG, highlightthickness=1,
                       highlightbackground=self.COLOR_CARD_BORDER)
        box.pack(fill=tk.X, padx=18, pady=(14, 0))
        tk.Label(box, text='请求参数', bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN,
                 font=('Microsoft YaHei', 9, 'bold')).grid(row=0, column=0, columnspan=6,
                                                           sticky='w', padx=12, pady=(10, 6))
        _spin = lambda var, lo, hi: tk.Spinbox(box, from_=lo, to=hi, textvariable=var, width=5,
                                               font=('Segoe UI', 10), bg='#FFFFFF',
                                               highlightthickness=1,
                                               highlightbackground=self.COLOR_CARD_BORDER)
        for col, (lab, var, lo, hi) in enumerate((
                ('单题超时(秒)', v_timeout, 10, 600),
                ('并发求解', v_workers, 1, 16),
                ('失败重试', v_retry, 0, 5))):
            tk.Label(box, text=lab, bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN,
                     font=('Microsoft YaHei', 9)).grid(row=1, column=col * 2, sticky='w',
                                                       padx=(12 if col == 0 else 14, 4), pady=(2, 12))
            _spin(var, lo, hi).grid(row=1, column=col * 2 + 1, sticky='w', pady=(2, 12))
        tk.Label(box, text='客户端只发送题目截图与题干（image / question_type / num_blanks / '
                           'text_hash_source / school_id / device_id），不含任何账号或计费字段。',
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 8),
                 justify='left', anchor='w', wraplength=620
                 ).grid(row=2, column=0, columnspan=6, sticky='w', padx=12, pady=(0, 10))

        row_test = tk.Frame(tab_api, bg=self.COLOR_BG)
        row_test.pack(fill=tk.X, padx=18, pady=(12, 0))
        btn_probe = tk.Button(row_test, text='测试连接', bg=self.COLOR_PRIMARY, fg='white',
                              font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT, cursor='hand2',
                              activebackground=self.COLOR_PRIMARY_DARK)
        btn_probe.pack(side=tk.LEFT, ipady=3, padx=(0, 10))
        self.bind_hover(btn_probe, self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)
        btn_imgtest = tk.Button(row_test, text='测试图片', bg='#2E7D32', fg='white',
                                font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT, cursor='hand2',
                                activebackground='#1B5E20')
        btn_imgtest.pack(side=tk.LEFT, ipady=3, padx=(0, 10))
        self.bind_hover(btn_imgtest, '#1B5E20', '#2E7D32')
        tk.Label(row_test, text='（合成 3 张内置测试题图片发给后端，逐题报告能否识别）',
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(side=tk.LEFT)

        lbl_probe = tk.Label(tab_api, text='尚未测试。点「测试连接」确认地址可达；点「测试图片」验证模型能否识图作答。',
                             bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 8),
                             justify='left', anchor='w', wraplength=650)
        lbl_probe.pack(anchor='w', padx=18, pady=(8, 4))

        # 测试详情（等宽只读文本框，带颜色标记）
        detail_wrap = tk.Frame(tab_api, bg=self.COLOR_BG)
        detail_wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        detail = tk.Text(detail_wrap, height=7, wrap='word', font=('Consolas', 9),
                         bg='#FFFFFF', fg=self.COLOR_TEXT_MAIN, relief=tk.FLAT,
                         highlightthickness=1, highlightbackground=self.COLOR_CARD_BORDER,
                         state='disabled')
        detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail.tag_configure('ok', foreground='#1E7E34')
        detail.tag_configure('warn', foreground='#B7791F')
        detail.tag_configure('bad', foreground='#C0392B')
        detail.tag_configure('dim', foreground=self.COLOR_TEXT_MUTED)
        sb = tk.Scrollbar(detail_wrap, command=detail.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        detail.configure(yscrollcommand=sb.set)

        def _write_detail(lines, status='', ok=True):
            detail.configure(state='normal')
            detail.delete('1.0', tk.END)
            if status:
                detail.insert(tk.END, status + '\n', ('ok' if ok else 'bad'))
            for ln in (lines or []):
                tag = 'ok' if ln.startswith('✓') else ('bad' if ln.startswith('✗') else (
                    'warn' if ln.startswith('⚠') else 'dim'))
                detail.insert(tk.END, ln + '\n', tag)
            detail.configure(state='disabled')
            return None

        def _start_test(btn, label, task, pending):
            """后台执行 task() -> (ok, status, detail_lines)，完成后回写界面。"""
            btn.configure(state='disabled', text=pending)
            lbl_probe.configure(text=pending, fg=self.COLOR_TEXT_MUTED)
            _write_detail([], pending, True)

            def worker():
                try:
                    ok, status, lines = task()
                except Exception as e:
                    ok, status, lines = False, f'测试异常：{e}', []

                def done():
                    btn.configure(state='normal', text=label)
                    lbl_probe.configure(text=status, fg=('#1E7E34' if ok else '#C0392B'))
                    _write_detail(lines, status, ok)
                try:
                    self.root.after(0, done)
                except Exception:
                    pass
                return None

            threading.Thread(target=worker, daemon=True).start()
            return None

        def _unsaved_guard():
            """后端地址改了但没保存时，提示先保存（否则测的还是旧地址）。"""
            saved = load_config().get('server_url') or ''
            typed = v_server.get().strip().rstrip('/')
            if typed and typed != saved:
                return (f'后端地址已改但尚未保存\n\n请先点「保存」，再测试。\n'
                        f'当前生效地址: {saved or get_server_url()}')
            return None

        def _task_probe():
            guard = _unsaved_guard()
            if guard:
                return False, '地址未保存，未执行测试', guard.split('\n')
            ok, msg = probe_backend()
            lines = [ln for ln in msg.split('\n') if ln.strip()]
            return ok, ('连接成功 ✓' if ok else '连接失败 ✗'), lines

        def _task_image():
            if get_answer_cfg()['mode'] != 'server':
                return (False, '当前答题方式不是「内部答题 API」',
                        ['请先到「答题方式」页选择「内部答题 API」，再回来测试图片。'])
            guard = _unsaved_guard()
            if guard:
                return False, '地址未保存，未执行测试', guard.split('\n')
            _ok, lines, results = run_solve_self_test()
            status = summarize_self_test(results)
            lines = list(lines)
            lines.append(f'后端: {get_server_url()}')
            lines.append('说明：✗=接口/网络失败，⚠=接口通但答案不符或为空，✓=识别正确。')
            return status.startswith('自检通过'), status, lines

        btn_probe.configure(command=lambda: _start_test(btn_probe, '测试连接', _task_probe, '测试中…'))
        btn_imgtest.configure(command=lambda: _start_test(btn_imgtest, '测试图片', _task_image,
                                                          '识别中…'))
        _write_detail(['点「测试图片」会合成下面 3 道题并发给后端 /solve：',
                       '  · 单选题：中国的首都是哪座城市？ → 期望 B',
                       '  · 多选题：下列哪些属于哺乳动物？（多选）→ 期望 AC',
                       '  · 填空题：一年有 ____ 个月。→ 期望 12',
                       '结果显示具体返回内容与耗时，便于判断是"接口不通"还是"识图不准"。'],
                      '尚未测试', True)

        # ================= 页签 3：自配大模型 =================
        tab_llm = tk.Frame(nb, bg=self.COLOR_BG)
        nb.add(tab_llm, text='  自配大模型  ')
        g2 = tk.Frame(tab_llm, bg=self.COLOR_BG)
        g2.pack(fill=tk.X, padx=18, pady=(14, 0))
        g2.columnconfigure(1, weight=1)
        for idx, (lab, var, tip, is_key) in enumerate((
                ('Base URL', v_url, 'OpenAI 兼容地址，需带 /v1，例如 https://api.openai.com/v1', False),
                ('API Key', v_key, 'Bearer 令牌，如 sk-...（仅存本地 config.json）', True),
                ('模型名', v_model, '需支持图片输入，例如 gpt-4o / qwen-vl-max / glm-4v', False))):
            tk.Label(g2, text=lab, bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN,
                     font=('Microsoft YaHei', 9, 'bold')).grid(row=idx * 2, column=0,
                                                               sticky='w', pady=(10, 0))
            ent = _entry(g2, var, show='*' if is_key else None)
            ent.grid(row=idx * 2, column=1, sticky='ew', padx=(10, 0), pady=(10, 0), ipady=3)
            if is_key:
                tk.Checkbutton(g2, text='显示', variable=v_show_key, bg=self.COLOR_BG,
                               activebackground=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED,
                               command=lambda e=ent: e.config(show='' if v_show_key.get() else '*')
                               ).grid(row=idx * 2, column=2, sticky='w', padx=(6, 0))
            _tip(g2, tip).grid(row=idx * 2 + 1, column=1, sticky='w', padx=(10, 0), pady=(2, 0))
        row_llm = tk.Frame(tab_llm, bg=self.COLOR_BG)
        row_llm.pack(fill=tk.X, padx=18, pady=(14, 0))
        btn_llm_test = tk.Button(row_llm, text='测试连接', bg=self.COLOR_PRIMARY, fg='white',
                                 font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT, cursor='hand2',
                                 activebackground=self.COLOR_PRIMARY_DARK)
        btn_llm_test.pack(side=tk.LEFT, ipady=3, padx=(0, 10))
        self.bind_hover(btn_llm_test, self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)
        lbl_llm_test = tk.Label(tab_llm, text='尚未测试。该通道直连上面的接口，不经过自建后端。',
                                bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 8),
                                justify='left', anchor='w', wraplength=640)
        lbl_llm_test.pack(anchor='w', padx=18, pady=(8, 0))

        # ================= 底部按钮 =================
        btns = tk.Frame(win, bg=self.COLOR_BG)
        btns.pack(fill=tk.X, pady=(14, 14))
        tk.Label(btns, text='Ctrl+S 保存 · Esc 关闭', bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(side=tk.RIGHT, padx=(0, 22))

        def _save(_event=None):
            url = v_server.get().strip().rstrip('/')
            if v_mode.get() == 'server' and not url:
                messagebox.showwarning('答题中心', '选择「内部答题 API」时必须填写后端地址。')
                return None
            try:
                timeout = max(10, min(600, int(v_timeout.get())))
                workers = max(1, min(16, int(v_workers.get())))
                retry = max(0, min(5, int(v_retry.get())))
            except (TypeError, ValueError):
                messagebox.showwarning('答题中心', '单题超时 / 并发求解 / 失败重试 必须填整数。')
                return None
            ok = update_config({
                'server_url': url,
                'answer': {'mode': v_mode.get(), 'solver_timeout': timeout,
                           'workers': workers, 'retry': retry},
                'llm': {'base_url': v_url.get().strip().rstrip('/'), 'api_key': v_key.get().strip(),
                        'model': v_model.get().strip()},
            })
            self.refresh_answer_mode_label()
            self.log(f'[答题中心] 已保存：方式={ANSWER_MODE_LABELS.get(v_mode.get(), v_mode.get())} | '
                     f'后端={url or "(空)"} | 超时={timeout}s 并发={workers} 重试={retry}')
            if not ok:
                messagebox.showerror('答题中心', '写入 config.json 失败，详见 logs/learn_helper.log。')
                return None
            win.destroy()
            return None

        def _probe_thread(target, btn, lbl, pending):
            btn.configure(state='disabled', text=pending)
            lbl.configure(text=pending, fg=self.COLOR_TEXT_MUTED)

            def worker():
                ok, msg = target()
                def done():
                    btn.configure(state='normal', text='测试连接')
                    lbl.configure(text=msg, fg=('#1E7E34' if ok else '#C0392B'))
                try:
                    self.root.after(0, done)
                except Exception:
                    pass
                return None

            threading.Thread(target=worker, daemon=True).start()
            return None

        def _test_llm():
            base = v_url.get().strip().rstrip('/')
            key = v_key.get().strip()
            model = v_model.get().strip()
            if not base or not key or not model:
                return False, '请先填写 Base URL / API Key / 模型名。'
            try:
                r = requests.post(f'{base}/chat/completions',
                                  headers={'Authorization': f'Bearer {key}',
                                           'Content-Type': 'application/json'},
                                  json={'model': model,
                                        'messages': [{'role': 'user', 'content': '回复 OK 即可'}],
                                        'max_tokens': 16},
                                  timeout=30)
            except Exception as e:
                return False, f'连接失败：{e}'
            if r.status_code == 200:
                return True, f'连接成功 ✓  模型：{model}'
            return False, f'HTTP {r.status_code}：{r.text[:200]}'

        btn_llm_test.configure(command=lambda: _probe_thread(_test_llm, btn_llm_test, lbl_llm_test, '测试中…'))

        tk.Button(btns, text='保存', command=_save, bg=self.COLOR_PRIMARY_DARK, fg='white',
                  font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT, cursor='hand2',
                  activebackground=self.COLOR_PRIMARY).pack(side=tk.LEFT, padx=(22, 6), ipady=4)
        tk.Button(btns, text='取消', command=win.destroy, bg=self.COLOR_TEXT_MUTED, fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                  activebackground='#95A5A6').pack(side=tk.LEFT, padx=6, ipady=4)
        win.bind('<Escape>', lambda e: win.destroy())
        win.bind('<Control-s>', _save)
        win.bind('<Control-S>', _save)

        win.update_idletasks()
        try:
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            w, h = win.winfo_width(), win.winfo_height()
            win.geometry(f'+{max(0, px + (pw - w) // 2)}+{max(0, py + (ph - h) // 3)}')
        except Exception:
            pass
        win.focus_force()
        return None

    def refresh_answer_mode_label(self):
        """把「答题方式 / 后端地址 / 设备指纹」刷新到主界面状态行。"""
        mode = get_answer_cfg()['mode']
        color = self.COLOR_PRIMARY if mode == 'server' else (
            '#B7791F' if mode == 'llm' else self.COLOR_TEXT_MUTED)
        if hasattr(self, 'lbl_answer_mode'):
            self.lbl_answer_mode.configure(
                text=f'答题方式: {ANSWER_MODE_LABELS.get(mode, mode)}', fg=color)
        if hasattr(self, 'lbl_backend'):
            self.lbl_backend.configure(text=get_server_url())
        if hasattr(self, 'lbl_device'):
            self.lbl_device.configure(text=f'设备指纹 {get_device_id()}')
        return None

    def diagnose_current_page(self):
        """连接浏览器，把当前页（或所选网页）的识别详情写入日志/界面。"""
        selected = self.cb_pages.get().strip()
        self.log('[诊断] 正在连接浏览器并分析页面...（详情同时写入 logs/learn_helper.log）')

        def run():
            ok, _ = kill_and_launch_browser()
            if not ok:
                self.log('[诊断] 浏览器不可用（9222 未就绪）。')
                return None
            try:
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                    ctx = browser.contexts[0]
                    pages = list(ctx.pages)
                    target = None
                    for pg in pages:
                        try:
                            if pg.title() == selected:
                                target = pg
                                break
                        except Exception:
                            pass
                    if target is None and pages:
                        target = pages[-1]
                    if target is None:
                        self.log('[诊断] 浏览器无已打开页面。')
                        browser.close()
                        return None
                    diagnose_page(target, self.log)
                    browser.close()
            except Exception as e:
                self.log(f'[诊断] 异常: {e}')
            return None

        threading.Thread(target=run, daemon=True).start()
        return None

    def _do_submit_target_page(self, target_page):
        """点「提交」并处理二次确认；失败自动降级为暂存。"""
        self.log('      [提交] 执行自动提交...')
        submit_btn, _ = find_submit_button(target_page)
        if not submit_btn:
            self.log('         [警告] 未找到提交按钮，自动降级为暂存。')
            return self._do_save_target_page(target_page)
        try:
            submit_btn.scroll_into_view_if_needed()
            time.sleep(0.3)
            submit_btn.click(True, force=True)
            self.log('         [提交] 已点击提交，等待二次确认弹窗...')
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
                self.log('         [提交] 二次确认完成，任务点已提交。')
            else:
                self.log('         [提示] 未检测到确认弹窗（可能已被浏览器自动放行）。')
            for _ in range(10):
                if self.stop_requested:
                    break
                time.sleep(0.2)
        except Exception as e:
            self.log(f'         [警告] 自动提交失败: {e}，降级为暂存。')
            return self._do_save_target_page(target_page)
        return None

    def _do_save_target_page(self, target_page):
        """点「暂存/保存」留存答案。"""
        self.log('      [暂存] 执行自动暂存...')
        save_btn, _ = find_save_button(target_page)
        if not save_btn:
            self.log('         [系统] 未找到暂存按钮，跳过暂存。')
            return None
        try:
            save_btn.scroll_into_view_if_needed()
            time.sleep(0.3)
            save_btn.click(True, force=True)
            self.log('         [存档] 暂存成功，答案已留存。')
            for _ in range(10):
                if self.stop_requested:
                    break
                time.sleep(0.2)
        except Exception as e:
            self.log(f'         [警告] 暂存失败: {e}')
        return None

    def check_pause_and_stop(self):
        if self.stop_requested:
            return True
        while self.pause_requested and not self.stop_requested:
            time.sleep(0.2)
        return self.stop_requested

    def toggle_pause(self):
        if self.pause_requested:
            self.pause_requested = False
            self.btn_pause.configure(text='暂停进程', bg='#E67E22')
            self.log('[系统] 已恢复执行。')
        else:
            self.pause_requested = True
            self.btn_pause.configure(text='恢复执行', bg='#2980B9')
            self.log('[系统] 已挂起，可点击恢复或终止。')
        return None

    def set_running_ui_state(self):
        self.solver_running = True
        self.btn_run.configure(state='disabled', text='正在运行...')
        self.btn_pause.configure(state='normal', text='暂停进程', bg='#E67E22')
        self.btn_stop.configure(state='normal')
        return None

    def reset_control_buttons(self):
        self.solver_running = False
        self.btn_run.configure(state='normal', text='启动刷课')
        self.btn_pause.configure(state='disabled', text='暂停进程', bg=self.COLOR_TEXT_MUTED)
        self.btn_stop.configure(state='disabled')
        self.update_task_perception(0, 0)
        self.update_progress_task('闲置中')
        self.update_progress_video('--')
        self.update_progress_quiz('--')
        return None

    def start_solver_thread(self):
        """启动刷课线程（守护线程：退出时不会拖住进程）。"""
        SHUTDOWN.clear()
        self.solver_running = True
        self.solver_thread = threading.Thread(target=self.run_solver_process, daemon=True)
        self.solver_thread.start()
        return None

    def update_task_perception(self, video_count, doc_count):
        def update():
            if video_count is not None:
                self.lbl_task_video_num.configure(text=str(video_count))
            if doc_count is not None:
                self.lbl_task_doc_num.configure(text=str(doc_count))

        self.root.after(0, update)
        return None

    def update_progress_task(self, task_name):
        self.root.after(0, lambda: self.lbl_prog_task.configure(text=f'当前状态: {task_name}'))
        return None

    def update_progress_video(self, percent_text):
        self.root.after(0, lambda: self.lbl_prog_video.configure(text=f'音视频进度: {percent_text}'))
        return None

    def update_progress_quiz(self, text):
        """答题进度（1.0.2 答题流程调用但此前漏定义，会抛 AttributeError）。"""
        self.root.after(0, lambda: self.lbl_prog_quiz.configure(text=f'答题进度: {text}'))
        return None

    # ---------------- 答题批次（内部答题 API / 自配大模型） ----------------
    def _solve_question_batch(self, questions, target_page):
        """一批题目的完整处理：识别 → 并发求解 → 填涂 → 提交/暂存。

        线程约定：Playwright 的 sync API 绑定创建它的线程，因此**截图、题型识别与填涂
        都留在本方法所在线程（刷课线程）**，只有纯网络的求解交给 run_parallel（守护线程）并发。
        """
        acfg = get_answer_cfg()
        mode = acfg['mode']
        total_q = len(questions)

        # 仅识别：把题型/题干写进日志，便于平台改版后维护选择器
        if mode == 'off':
            self.log(f'      [测验] 探测到文字题 {total_q} 道；当前为「仅识别不答题」，只记录不填涂。')
            self.update_progress_quiz(f'仅识别 {total_q} 题')
            for i, q in enumerate(questions):
                try:
                    q_type, num_inputs = detect_question_type_and_inputs(q)
                    text_source = extract_clean_text_with_latex(q)
                    self.log(f'         [题 {i + 1}/{total_q}] 类型={q_type} 数量={num_inputs} '
                             f'题干={(text_source or "(无题干)")[:60]}')
                except Exception as e:
                    self.log(f'         [题 {i + 1}/{total_q}] 识别失败: {e}')
            return None

        label = ANSWER_MODE_LABELS.get(mode, mode)
        self.log(f'      [测验] 探测到文字题 {total_q} 道，使用「{label}」求解'
                 f'（并发 {acfg["workers"]}，单题超时 {acfg["solver_timeout"]}s）...')
        self.update_progress_task(f'自动做题中（{label}）')
        self.update_progress_quiz(f'识别到 {total_q} 题')

        # ---- 阶段 1（刷课线程）：截图 + 题型/题干识别 ----
        items = []
        for i, q in enumerate(questions):
            if self.check_pause_and_stop():
                break
            try:
                q.scroll_into_view_if_needed()
                time.sleep(0.05)
                img_bytes = q.screenshot()
                q_type, num_inputs = detect_question_type_and_inputs(q)
                text_source = extract_clean_text_with_latex(q)
                items.append({
                    'idx': i, 'locator': q, 'image': img_bytes,
                    'q_type': q_type, 'num': num_inputs, 'text': text_source,
                    'answer': None, 'error': None,
                })
                self.log(f'         [题 {i + 1}/{total_q}] 类型={q_type} 数量={num_inputs} '
                         f'题干={(text_source or "(无题干)")[:40]}')
            except Exception as e:
                self.log(f'         [题 {i + 1}/{total_q}] 截图/识别失败: {e}')
        if not items:
            self.log('      [警告] 本卡片题目截图/识别全部失败，跳过答题。')
            self.update_progress_quiz('识别失败')
            return None

        # ---- 阶段 2（守护线程并发）：求解 ----
        def _work(it):
            try:
                it['answer'] = solve_question(it['image'], it['q_type'], it['num'], it['text'],
                                              mode, acfg)
            except Exception as e:
                it['error'] = ('error', str(e))
            return it

        self.update_progress_quiz(f'求解 0/{len(items)}')
        run_parallel(items, _work, workers=acfg['workers'],
                     on_progress=lambda n: self.update_progress_quiz(f'求解 {n}/{len(items)}'))
        if SHUTDOWN.is_set():
            self.log('      [退出] 已放弃剩余在途求解请求。')
            return None

        # ---- 阶段 3（刷课线程）：填涂 ----
        nc = 0
        ok_q = 0
        for it in items:
            if self.check_pause_and_stop():
                break
            if it['error']:
                self.log(f'         [题 {it["idx"] + 1}] 求解失败: {it["error"][1]}')
                continue
            resp = it['answer'] or {}
            show = resp.get('answer_key') or ', '.join(resp.get('text_answers', []))
            tag = ' [后端缓存命中]' if resp.get('cached') else ''
            self.log(f'         [题 {it["idx"] + 1}] 答案: {show or "(空)"}{tag}')
            ok_q += 1
            try:
                if fill_and_click_smart(it['locator'], resp):
                    nc += 1
                    self.update_progress_quiz(f'已填涂 {nc}/{total_q}')
                    time.sleep(0.1)
                else:
                    self.log(f'         [题 {it["idx"] + 1}] 答案未能写入页面（选择器可能已失效）')
            except Exception as e:
                self.log(f'         [题 {it["idx"] + 1}] 填涂异常: {e}')

        self.log(f'      [完成] 本卡片求解 {ok_q} 题、成功填涂 {nc}/{total_q} 题。')
        self.update_progress_quiz(f'完成 {nc}/{total_q}')

        # ---- 阶段 4：提交 / 暂存 ----
        if nc > 0 and not self.check_pause_and_stop():
            if self.var_auto_submit.get():
                self._do_submit_target_page(target_page)
            else:
                self._do_save_target_page(target_page)
        return None

    # ---------------- 自动化核心 ----------------
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

    def run_video_task(self, target_page, task_frame, target_container, task_sig, completed_video_urls):
        media_completed = False
        inner_err_count = 0
        last_percent = -1
        line_switch_count = 0
        self.update_progress_task('正在播放音视频')
        try:
            task_frame.evaluate(HACK_SCRIPT)
        except Exception:
            pass

        while not media_completed:
            if self.check_pause_and_stop() or target_page.is_closed():
                return None
            try:
                is_finished = target_container.evaluate(
                    '(container) => container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\');')
                if is_finished:
                    self.log('         [完成] 平台已标记完成。')
                    completed_video_urls.add(task_sig)
                    self.update_progress_video('已完成')
                    media_completed = True
                    return None
                try:
                    current_speed = float(self.var_video_speed.get() or '2.0')
                except Exception:
                    current_speed = 2.0
                task_frame.evaluate(HACK_SCRIPT)
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
                if status and not status.get('paused', True) and not status.get('ended', False):
                    self.accumulated_video_seconds += 0.5
                    if self.accumulated_video_seconds >= 600.0:
                        self.accumulated_video_seconds -= 600.0
                        self.log('      [看课] 已累计观看满 10 分钟（仅本地计时，无任何上报）。')

                if status.get('paused') and not status.get('ended'):
                    is_line_error = task_frame.evaluate('window.hackLineSwitch()')
                    if is_line_error:
                        self.log('         [警告] 视频源异常，正在自动切换线路...')
                        line_switch_count += 1
                        recovered = False
                        for _ in range(15):
                            if self.check_pause_and_stop():
                                break
                            time.sleep(0.2)
                            is_playing = task_frame.evaluate(
                                "() => { let v = document.querySelector('video, audio'); "
                                "return v ? (!v.paused && v.readyState >= 2) : false; }")
                            if is_playing:
                                self.log('         [自愈] 已恢复播放。')
                                recovered = True
                                break
                        if not recovered and line_switch_count >= 3:
                            self.log('         [警告] 线路频繁更换，重载播放容器...')
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
                        current_percent_float = float(status['percent'])
                        if int(current_percent_float) != last_percent:
                            self.log(f'         [视频] 播放进度: {status["percent"]}%')
                            self.update_progress_video(f'{status["percent"]}%')
                            last_percent = int(current_percent_float)
                    except Exception:
                        pass
                if status.get('ended'):
                    self.log('         [完成] 音视频播放结束。')
                    self.update_progress_video('已完成')
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
                self.log(f'         [警告] 状态监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_video_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.5)
        return None

    def run_doc_task(self, target_page, task_frame, target_container, task_sig, completed_doc_urls):
        doc_completed = False
        inner_err_count = 0
        self.update_progress_task('步进阅读文档')

        while not doc_completed:
            if self.check_pause_and_stop() or target_page.is_closed():
                return None
            try:
                is_finished = target_container.evaluate(
                    '(container) => container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\');')
                if is_finished:
                    self.log('         [完成] 平台已标记文档完成。')
                    completed_doc_urls.add(task_sig)
                    self.update_progress_video('已完成')
                    doc_completed = True
                    return None
                task_frame.evaluate(SCROLL_SCRIPT)
                status = task_frame.evaluate('window.autoScrollDocument()')
                if 'error' in status:
                    raise Exception(status['error'])
                inner_err_count = 0
                self.log(f'         [文档] 阅读进度: {status["percent"]}%')
                self.update_progress_video(f'阅读进度 {status["percent"]}%')
                if status.get('ended') or status.get('percent') == '100.0':
                    self.log('         [完成] 文档已触底。')
                    self.update_progress_video('已完成')
                    completed_doc_urls.add(task_sig)
                    doc_completed = True
                    return None
            except Exception as loop_ex:
                inner_err_count += 1
                self.log(f'         [警告] 文档监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_doc_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.4)
        return None

    def run_solver_process(self):
        """
        刷课主流程：
          锁定页面 → 逐卡片（答题 → 音视频/文档任务）→ 翻页 → 防弹窗 → 每 10 页重建内存。
        """
        selected_title = self.cb_pages.get().strip()
        if (not selected_title) or '[请点击' in selected_title or '[安全浏览器' in selected_title:
            messagebox.showwarning('提示', '请先在浏览器中点开学习页，并在下拉框中选择要刷的网页！')
            self.solver_running = False
            return None

        self.stop_requested = False
        self.pause_requested = False
        self.root.after(0, self.set_running_ui_state)
        self.log('================================================')
        self.log('[启动] 纯刷课流程已就绪，正在准备...')

        completed_video_urls = set()
        completed_doc_urls = set()

        ok, proc = kill_and_launch_browser()
        if ok:
            self.browser_proc = proc
        else:
            self.log('[错误] 浏览器挂载失败，请先手动打开一个 Edge 窗口。')
            self.root.after(0, self.reset_control_buttons)
            return None

        start_time = time.time()
        self.log('[系统] 正在连接浏览器 CDP 通道...')

        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                context = browser.contexts[0]

                if not context.pages:
                    self.log('[警告] 浏览器无活动标签页，正在自动新建...')
                    target_page = context.new_page()
                    time.sleep(0.5)
                else:
                    target_page = None
                    for pg in context.pages:
                        try:
                            if pg.title() == selected_title:
                                target_page = pg
                                break
                        except Exception:
                            pass
                    if not target_page:
                        target_page = context.pages[-1]

                self.log(f'[系统] 锁定当前网页: 【{target_page.title()}】')
                target_page.on('dialog', lambda dialog: dialog.accept())
                page_counter = 1
                saved_url = None
                # 内层 while 的 break 只跳出一层（= 进入下一页）；整个流程是否收尾由它决定，
                # 否则「终止退出」和「刷完最后一页」都会在外层 while 里无限空转（见 ERROR.md E6）。
                end_flow = False

                while True:
                    if self.stop_requested:
                        break
                    # 浏览器上下文丢失时自动重连，避免 list index out of range 中断流程
                    if not browser.contexts:
                        self.log('[警告] 浏览器上下文丢失，正在重新拉起浏览器...')
                        try:
                            kill_and_launch_browser()
                            time.sleep(1.0)
                            browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                        except Exception as e:
                            self.log(f'[错误] 浏览器重连失败: {e}')
                            break
                    try:
                        context = browser.contexts[0]
                    except Exception:
                        context = None
                    if context is None:
                        self.log('[错误] 浏览器上下文不可用，流程结束。')
                        break
                    if not context.pages:
                        try:
                            target_page = context.new_page()
                        except Exception as e:
                            self.log(f'[错误] 新建标签页失败: {e}')
                            break
                        time.sleep(0.5)
                    elif saved_url:
                        target_page = context.pages[-1] if context.pages else context.new_page()
                        try:
                            target_page.goto(saved_url)
                        except Exception as e:
                            self.log(f'[警告] 返回目标页失败: {e}')
                        self.log('[系统] 🌟 浏览器重启完毕，已返回目标页，继续刷课...')
                        saved_url = None
                    else:
                        target_page = None
                        for pg in context.pages:
                            try:
                                if pg.title() == selected_title:
                                    target_page = pg
                                    break
                            except Exception:
                                pass
                        if not target_page:
                            target_page = context.pages[-1] if context.pages else context.new_page()
                    self.log(f'[系统] 锁定当前网页: 【{target_page.title()}】')
                    target_page.on('dialog', lambda dialog: dialog.accept())

                    while True:
                        if self.check_pause_and_stop():
                            self.log('[系统] 任务因用户请求退出。')
                            end_flow = True
                            break
                        self.log(f'\n--- [ 正在处理第 {page_counter} 页 ] ---')
                        try:
                            target_page.wait_for_load_state('load', timeout=15000)
                        except Exception:
                            pass
                        self.log('[系统] 正在等待任务卡片加载...')
                        robust_wait_for_tasks_to_render(target_page, self.check_pause_and_stop)

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

                        tab_buttons = find_tab_buttons(cards_frame) if cards_frame else []
                        total_tabs = max(1, len(tab_buttons))
                        self.log('[系统] 本节包含多个卡片，逐个处理...' if total_tabs > 1
                                 else '[系统] 本节包含单个卡片。')

                        for tab_idx in range(total_tabs):
                            if self.check_pause_and_stop():
                                break
                            self.log(f'\n   --- [ 任务卡片 {tab_idx + 1} / {total_tabs} ] ---')
                            if len(tab_buttons) > 1:
                                try:
                                    current_tabs = find_tab_buttons(cards_frame)
                                    if tab_idx < len(current_tabs):
                                        target_tab = current_tabs[tab_idx]
                                        target_tab.scroll_into_view_if_needed()
                                        time.sleep(0.3)
                                        target_tab.click(True, force=True)
                                        self.log(f'      [卡片] 已切换至卡片 {tab_idx + 1}...')
                                        time.sleep(1.5)
                                        robust_wait_for_tasks_to_render(target_page, self.check_pause_and_stop)
                                except Exception as tab_ex:
                                    self.log(f'      [警告] 切换卡片失败: {tab_ex}')

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

                            # ---- 答题（新版本：内部答题模型 / 自配大模型 / 仅识别）----
                            questions, target_frame = scan_page_recursively(target_page)
                            if questions:
                                if check_quiz_completed(questions, target_frame):
                                    self.log('      [跳过] 该测验任务点已被平台标记完成。')
                                    self.update_progress_quiz('已完成')
                                else:
                                    self._solve_question_batch(questions, target_page)
                            else:
                                self.update_progress_quiz('无题目')

                            # 扫描音视频/文档任务（多选择器 + 多帧回退）
                            containers = collect_job_containers(target_page, cards_frame)
                            self.log(f'      [识别] 发现候选任务容器 {len(containers)} 个')
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

                            if not valid_jobs:
                                self.log('      [系统] 当前卡片无音视频/文档任务。（可点「诊断页面」查看命中详情）')
                                self.update_task_perception(0, 0)
                                LOGGER.info('[识别] 未识别到任务容器，自动输出页面诊断：')
                                diagnose_page(target_page, self.log)
                            else:
                                v_count = sum(1 for ph in valid_jobs
                                              if any(k in ph.inner_html().lower() for k in
                                                     ('video', 'audio', 'fastforward', 'insertvideo')))
                                d_count = len(valid_jobs) - v_count
                                self.update_task_perception(v_count, d_count)
                                self.log(f'      [系统] 检测到 {len(valid_jobs)} 个任务点，开始监控...')

                                for task_idx, target_container in enumerate(valid_jobs):
                                    if self.check_pause_and_stop():
                                        break
                                    self.log(f'\n         [对焦] 目标任务 {task_idx + 1}/{len(valid_jobs)} ...')
                                    try:
                                        target_container.scroll_into_view_if_needed()
                                        time.sleep(0.5)
                                    except Exception:
                                        pass
                                    task_sig = self.generate_task_signature(page_counter, tab_idx, task_idx, target_container)
                                    if task_sig in completed_video_urls or task_sig in completed_doc_urls:
                                        self.log('         [跳过] 已完成过此任务。')
                                        continue
                                    try:
                                        if target_container.evaluate(
                                                '(c) => c.classList.contains("ans-job-finished") || '
                                                '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(c.className || \'\');'):
                                            self.log('         [跳过] 平台已标记完成。')
                                            completed_video_urls.add(task_sig)
                                            completed_doc_urls.add(task_sig)
                                            continue
                                    except Exception:
                                        pass

                                    task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        self.log('         [等待] 任务容器初始化中，等 2 秒...')
                                        time.sleep(2.0)
                                        task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        self.log('         [警告] 容器穿透失败，跳过该任务。')
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
                                            self.log('         [视频] 开始倍速静音播放...')
                                            self.run_video_task(target_page, task_frame, target_container,
                                                                task_sig, completed_video_urls)
                                        elif has_doc:
                                            self.log('         [文档] 开始步进滚动阅读...')
                                            self.run_doc_task(target_page, task_frame, target_container,
                                                              task_sig, completed_doc_urls)
                                        else:
                                            self.log('         [系统] 未发现视频/文档，标记已读。')
                                            self.update_progress_video('已完成')
                                            try:
                                                target_container.evaluate("(c) => c.classList.add('ans-job-finished')")
                                            except Exception:
                                                pass
                                        self.log('         [等待] 平台状态同步中...')
                                        time.sleep(1.5)
                                    except Exception as eval_ex:
                                        self.log(f'         [警告] 任务异常: {eval_ex}')
                                        try:
                                            target_container.evaluate("(c) => c.classList.add('ans-job-finished')")
                                        except Exception:
                                            pass
                                        time.sleep(1.0)

                            if self.check_pause_and_stop():
                                break

                        if self.check_pause_and_stop():
                            end_flow = True
                            break

                        # 翻页
                        self.log('[导航] 正在查找下一页按钮...')
                        next_btn, next_frame = find_next_button(target_page)
                        if not next_btn:
                            self.log('[系统] 未找到下一页按钮，刷课流程结束。')
                            end_flow = True
                            break
                        try:
                            next_btn.scroll_into_view_if_needed()
                            time.sleep(0.5)
                            next_btn.click(True, force=True)
                            self.log('[导航] 已翻页，检查是否有确认弹窗...')
                            for _ in range(5):
                                if self.check_pause_and_stop():
                                    break
                                time.sleep(0.2)
                                bypass_btn, _bf = find_confirmation_bypass_button(target_page)
                                if bypass_btn:
                                    self.log('[系统] 检测到未完成提示弹窗，已强制跳过。')
                                    bypass_btn.click(True, force=True)
                                    break
                            self.log('[导航] 等待页面载入...')
                            time.sleep(0.8)
                            if self.stop_requested:
                                end_flow = True
                                break
                            page_counter += 1
                            if page_counter % 10 == 0:
                                self.log('[系统] 🌟 已连刷 10 页，重建标签页释放内存...')
                                saved_url = target_page.url
                                new_page = context.new_page()
                                new_page.on('dialog', lambda dialog: dialog.accept())
                                target_page.close()
                                new_page.goto(saved_url)
                                target_page = new_page
                                self.log('[系统] 🌟 内存清理完毕，继续刷课...')
                            completed_video_urls.clear()
                            completed_doc_urls.clear()
                            self.log('[系统] 已清理上一页去重缓存。')
                        except Exception as ex:
                            self.log(f'   [警告] 翻页受阻: {ex}')
                            end_flow = True
                            break

                    if end_flow:
                        break

                self.log(f'\n[系统] 刷课流程运行完毕，共处理页面数: {page_counter}')
                self.log(f'总计耗时: {time.time() - start_time:.2f}s')
                if self.stop_requested:
                    self.log('[系统] 已按用户请求停止，保留沙盒浏览器（不关闭，便于查看页面）。')
                else:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log(f'[错误] 流程异常中断: {e}')
        self.solver_running = False
        try:
            self.root.after(0, self.reset_control_buttons)
        except Exception:
            pass
        return None


if __name__ == '__main__':
    if '--diagnose' in sys.argv:
        run_diagnose_cli()
        sys.exit(0)
    LOGGER.info(f'===== 启动 学习助理·纯刷课 v{APP_VERSION} (日志: {os.path.join(LOG_DIR, "learn_helper.log")}) =====')
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    keep_computer_awake()
    root = tk.Tk()
    app = AppConsole(root)
    root.mainloop()