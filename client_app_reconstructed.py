# -*- coding: utf-8 -*-
# 反编译重建（PyInstaller 单文件，Python 3.10，入口 client_app.pyc）
# 来源: 学习助理.exe —— 一个基于 playwright + tkinter 的"青年大学习/网课"挂机刷课客户端
# 说明: 本文件为对字节码的忠实反编译重建，注释为研究用途。

import base64
import time
import re
import os
import random
import hashlib
import subprocess
import requests
import ctypes

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from playwright.sync_api import sync_playwright
from concurrent.futures import ThreadPoolExecutor
import io
import urllib.request as urllib

urllib.request.getproxies = lambda: {}

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

VERSION = '1.0.0'
SERVER_URL = 'http://1.13.15.42:8000'          # 兴趣小组自建任务服务器
SHOP_URL = 'https://www.kufaka.com/shop/BXZ53P9P'  # 卡法卡商城（自动发卡平台，售卖卡密）
SCHOOL_ID = 'nuaa'                              # 学校标识：南京航空航天大学
MAX_WORKERS = 4
BROWSER_EXE = 'msedge.exe'
BROWSER_PATHS = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
]


def keep_computer_awake():
    """
    通过 Windows API 阻止系统自动进入睡眠或熄灭屏幕（挂机刷课必备）
    """
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 1
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        return None
    return None


# 注入页面的一段 JS：伪装前台、加速视频、自动换线路、暴力试答随堂弹题
HACK_SCRIPT = '''

try {
    // 欺骗浏览器：始终在前台
    Object.defineProperty(document, 'hidden', { value: false, writable: false });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', writable: false });
    window.onblur = null;
    window.onfocus = null;
} catch (e) {}

// 🌟 动态接收速度参数 speed，如果没有传入则默认使用 2.0
window.hackVideo = function(speed) {
    try {
        let target_speed = speed || 2.0;
        let videos = document.getElementsByTagName('video');
        let count = 0;
        for (let i = 0; i < videos.length; i++) {
            let v = videos[i];
            if (v) {
                v.muted = true;
                
                // 🌟 动态首充赋速：只有当速度不等于目标速度时才赋值，防止事件洪水
                if (v.playbackRate !== target_speed) {
                    v.playbackRate = target_speed;
                }
                if (!v.__my_lock_added) {
                    v.addEventListener('ended', () => {
                        window.__my_video_task_done = true;
                    });
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
    } catch(e) {
        return 0;
    }
}

// 视频流播放报错自动换线路
window.hackLineSwitch = function() {
    try {
        let bodyText = document.body ? document.body.innerText : "";
        if (!/格式不支持|网络的问题|无法加载|其他线路/.test(bodyText)) {
            return false;
        }
        let elements = Array.from(document.querySelectorAll('a, button, span, div.btn, li'));
        let targets = elements.filter(el => {
            let txt = el.innerText || "";
            return /公网|线路|推荐|默认|CDN|极速/.test(txt) && el.offsetHeight > 5 && el.offsetWidth > 5;
        });
        if (targets.length === 0) return false;
        if (window.__last_line_idx === undefined) {
            window.__last_line_idx = 0;
        }
        let idx = window.__last_line_idx % targets.length;
        targets[idx].click();
        window.__last_line_idx++;
        return true;
    } catch (e) {
        return false;
    }
}

// 暴力随堂弹题快速试错 (兼容单选、多选与判断)
window.hackQuiz = function() {
    try {
        let inputs = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
        if (inputs.length === 0) return false;
        
        let submitBtn = null;
        let allBtns = document.querySelectorAll('a, button, div.btn, span');
        for (let btn of allBtns) {
            let text = btn.innerText || "";
            if (text.includes('提交') || text.includes('确定') || text.includes('继续')) {
                submitBtn = btn; break;
            }
        }
        if (!submitBtn) return false;

        // 多选题随机选2个，单选题随机选1个
        let clickCount = inputs[0].type === "checkbox" ? Math.floor(Math.random() * 2) + 2 : 1;
        let indices = Array.from({length: inputs.length}, (_, i) => i);
        for (let c = 0; c < clickCount; c++) {
            if (indices.length === 0) break;
            let randIdx = indices.splice(Math.floor(Math.random() * indices.length), 1)[0];
            inputs[randIdx].click();
        }
        
        // 400ms 极速缓冲提交
        setTimeout(() => { 
            try { submitBtn.click(); } catch(e){}
        }, 400);
        return true;
    } catch(e) {
        return false;
    }
}

'''

# 长页面（文档/PDF 预览）自动滚屏
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
                let step = 400;
                win.scrollTo(0, current + step);
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
# 填空题答案多轨写入脚本（UEditor / textarea / iframe / input / contenteditable）
FILL_TEXT_SCRIPT = '''

(element, answers) => {
    let filled_count = 0;
    if (!answers || answers.length === 0) return filled_count;

    // 1. 寻找所有独立的填空容器（.blankItemDiv）
    let blankContainers = Array.from(element.querySelectorAll('.blankItemDiv'));
    
    // 如果没有 .blankItemDiv 容器，则直接抓取所有潜在的输入控件
    if (blankContainers.length === 0) {
        let allInputs = Array.from(element.querySelectorAll('textarea[id^="answer"], textarea, input[type="text"], input.blank_input, div[contenteditable="true"]'));
        blankContainers = allInputs.length > 0 ? allInputs : [element];
    }

    // 辅助函数：针对单个输入容器执行 UEditor / Textarea / Iframe / Input 多轨写入
    function fillSingle(container, text) {
        if (!container || text === undefined || text === null) return false;
        let success = false;
        let cleanText = String(text);

        // A. 轨道一：百度 UEditor 官方实例与底座 Textarea 同步
        let textareas = Array.from(container.querySelectorAll ? container.querySelectorAll('textarea[id^="answer"], textarea') : []);
        if (container.tagName === 'TEXTAREA') {
            textareas.push(container);
        }

        for (let ta of textareas) {
            let taId = ta.id;
            // 1. 调用 UE.getEditor(id).setContent
            if (window.UE) {
                try {
                    if (taId && window.UE.getEditor) {
                        let editor = window.UE.getEditor(taId);
                        if (editor && editor.setContent) {
                            editor.setContent(cleanText);
                            success = true;
                        }
                    }
                } catch(e) {}
                
                // 2. 遍历 UE.instants 查找匹配
                if (!success && window.UE.instants) {
                    for (let key in window.UE.instants) {
                        let inst = window.UE.instants[key];
                        if (inst && (inst.key === taId || inst.textarea === ta || (taId && inst.key && inst.key.includes(taId)))) {
                            try {
                                inst.setContent(cleanText);
                                success = true;
                                break;
                            } catch(e) {}
                        }
                    }
                }
            }

            // 3. 原生同步更新 textarea 的 value 并派发事件 (保障提交和暂存能够捕获)
            try {
                ta.value = cleanText;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
                ta.dispatchEvent(new Event('blur', { bubbles: true }));
                success = true;
            } catch(e) {}
        }

        // B. 轨道二：同步写入 Iframe 内部 body (视觉更新)
        let iframes = Array.from(container.querySelectorAll ? container.querySelectorAll('iframe[id^="ueditor_"], iframe') : []);
        if (container.tagName === 'IFRAME') {
            iframes.push(container);
        }
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

        // C. 轨道三：普通 input 文本框 (单行填空题)
        let inputs = Array.from(container.querySelectorAll ? container.querySelectorAll('input[type="text"], input.blank_input, input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])') : []);
        if (container.tagName === 'INPUT' && container.type !== 'hidden' && container.type !== 'radio' && container.type !== 'checkbox') {
            inputs.push(container);
        }
        for (let ipt of inputs) {
            try {
                ipt.focus();
                ipt.value = cleanText;
                ipt.dispatchEvent(new Event('input', { bubbles: true }));
                ipt.dispatchEvent(new Event('change', { bubbles: true }));
                ipt.dispatchEvent(new Event('blur', { bubbles: true }));
                success = true;
            } catch(e) {}
        }

        // D. 轨道四：div[contenteditable="true"] 现代富文本
        let editables = Array.from(container.querySelectorAll ? container.querySelectorAll('div[contenteditable="true"]') : []);
        if (container.getAttribute && container.getAttribute('contenteditable') === 'true') {
            editables.push(container);
        }
        for (let ed of editables) {
            try {
                ed.focus();
                ed.innerText = cleanText;
                ed.dispatchEvent(new Event('input', { bubbles: true }));
                ed.dispatchEvent(new Event('change', { bubbles: true }));
                ed.dispatchEvent(new Event('blur', { bubbles: true }));
                success = true;
            } catch(e) {}
        }

        return success;
    }

    // 循环依次填入各个空的答案
    for (let i = 0; i < answers.length; i++) {
        let ans = answers[i];
        if (i < blankContainers.length) {
            if (fillSingle(blankContainers[i], ans)) {
                filled_count++;
            }
        }
    }

    // 兜底：如果没匹配到容器，直接对整个题目根节点尝试写入第一个答案（简答题保底）
    if (filled_count === 0 && answers.length > 0) {
        if (fillSingle(element, answers[0])) {
            filled_count++;
        }
    }

    return filled_count;
}

'''


def clean_text_for_gui(text):
    if not text:
        return ''
    clean_text = text.replace('\xa0', ' ').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
    clean_text = ' '.join(clean_text.split())
    clean_chars = [c for c in clean_text if ord(c) < 65535]
    return ''.join(clean_chars).strip()


def kill_and_launch_browser():
    """
    杀死已有浏览器（保证 9222 调试端口空闲）并以远程调试模式重新拉起 Edge/Chrome。
    返回 (是否就绪, subprocess.Popen 对象)
    """
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
def scan_page_recursively(page):
    """
    在页面主框架与所有 iframe 中递归寻找"题目"节点。
    返回 ([locators], frame)；找不到则返回 ([], None)。
    """
    class_selectors = [
        'div.singleQuesId', 'div.singlequesid', 'div.TiMu', '.question-card',
        '.question-item', '.test-item', '.Tm_cont', '.problem', '.exercise',
        '.ti-q-c', '.que', '.multiquesid',
    ]
    xpath_selector = "//input[(@type='radio' or @type='checkbox')]/ancestor::div[contains(@class, 'que') or contains(@class, 'item') or contains(@class, 'box') or string-length(@class)>2]"

    frames_to_scan = [page.main_frame] + page.frames
    for i, frame in enumerate(frames_to_scan):
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
    """
    提取题干文本（含 LaTeX 图片里的内容），去掉题号、"（N 分）"并压缩空白。
    """
    try:
        raw_extracted = question_locator.evaluate('''
(element) => {
            function decodeAndClean(latexData) {
                try {
                    let decoded = decodeURIComponent(latexData);
                    decoded = decoded.replace(/^"+|"+$/g, '').replace(/^%22+|%22+$/g, '');
                    return decoded;
                } catch(e) {
                    return latexData;
                }
            }

            function traverse(node) {
                let text = "";
                if (node.nodeType === 3) { 
                    text += node.textContent;
                } else if (node.nodeType === 1) { 
                    if (node.tagName === "INPUT" && node.type === "hidden") {
                        return "";
                    }
                    if (node.tagName === "SCRIPT" || node.tagName === "STYLE") {
                        return "";
                    }
                    if (node.tagName === "IMG" && node.classList.contains("ans-latex-moudle")) {
                        let latexData = node.getAttribute("data") || node.getAttribute("data-original") || "";
                        if (latexData) {
                            return " " + decodeAndClean(latexData) + " ";
                        }
                    }
                    for (let child of node.childNodes) {
                        text += traverse(child);
                    }
                }
                return text;
            }
            return traverse(element);
        }
''')
        clean_text = clean_text_for_gui(raw_extracted)
        clean_text = re.sub(r'^\d+[\s\.、]*', '', clean_text)
        clean_text = re.sub(r'[\(（]\s*\d+(\.\d+)?\s*分\s*[\)）]', '', clean_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        return clean_text
    except Exception as e:
        print(f'提取LaTeX文本异常: {e}')
        try:
            return clean_text_for_gui(question_locator.inner_text())
        except Exception:
            return ''


def detect_question_type_and_inputs(question_locator):
    """
    识别题目类型，返回 (类型, 数量)：
      ('choice', n) 单选 / ('multi_choice', n) 多选 / ('blank', n) 填空 / ('essay', 1) 简答
    """
    try:
        timu_el = question_locator.locator('div.TiMu').first
        if timu_el.count() > 0:
            timu_type = timu_el.get_attribute('data')
            if timu_type in ('0', '1', '3'):
                options_count = question_locator.locator("li.before-after, li[role='radio'], li[role='checkbox']").count()
                is_multi = (timu_type == '1') or (question_locator.locator("li[role='checkbox'], input[type='checkbox']").count() > 0)
                q_type_res = 'multi_choice' if is_multi else 'choice'
                if options_count > 0:
                    return (q_type_res, options_count)
                return ('choice', 4)
            elif timu_type == '2':
                blanks_count = question_locator.locator('.blankItemDiv').count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("textarea[id^='answer']").count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("iframe[id^='ueditor_']").count()
                if blanks_count == 0:
                    blanks_count = question_locator.locator("input[type='text'], input.blank_input").count()
                if blanks_count > 0:
                    return ('blank', blanks_count)
                return ('blank', 1)
            elif timu_type in ('4', '5', '6'):
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
    """
    根据后端返回的题目数据智能填涂/作答。
      response_data: {'question_type': 'choice'|'multi_choice'|'blank'|'essay',
                      'text_answers': [...], 'answer_key': 'ABD'}
    返回成功与否。
    """
    q_type = response_data.get('question_type', 'choice')
    text_answers = response_data.get('text_answers', [])
    answer_key = response_data.get('answer_key', '')
    try:
        if q_type in ('choice', 'multi_choice') or text_answers:
            if not answer_key:
                return False
            clean_str = (answer_key
                         .upper().replace('对', 'A').replace('TRUE', 'A')
                         .replace('T', 'A').replace('正确', 'A'))
            clean_str = (clean_str
                         .replace('错', 'B').replace('FALSE', 'B')
                         .replace('F', 'B').replace('错误', 'B'))
            clean_keys = re.sub(r'[^A-F]', '', clean_str)
            target_set = set(clean_keys)

            def parse_option_letter(el, idx):
                try:
                    txt = (el.inner_text() or '').strip().upper()
                    match = re.search(r'^[A-F]', txt)
                    if match:
                        return match.group(0)
                    if any(kw in txt for kw in ('对', '正确', 'TRUE', '√')):
                        return 'A'
                    if any(kw in txt for kw in ('错', '错误', 'FALSE', '×')):
                        return 'B'
                    data_val = (el.get_attribute('data') or '').strip().upper()
                    if data_val in ('A', 'B', 'C', 'D', 'E', 'F'):
                        return data_val
                    if data_val in ('TRUE', '对'):
                        return 'A'
                    if data_val in ('FALSE', '错'):
                        return 'B'
                except Exception:
                    pass
                alphabet = ['A', 'B', 'C', 'D', 'E', 'F']
                if idx < len(alphabet):
                    return alphabet[idx]
                return ''

            spans = question_locator.locator(
                'span.num_option, span.num_option_dx, span.check_answer, span.check_answer_dx').all()
            if not spans:
                spans = question_locator.locator("li.before-after, li[role='radio'], li[role='checkbox']").all()

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

                # 已有选择但答案不对 → 修正勾选
                clicked_count = 0
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
                            clicked_count += 1
                            time.sleep(0.12)
                        elif not should_select and is_selected and q_type == 'multi_choice':
                            el.scroll_into_view_if_needed()
                            time.sleep(0.05)
                            el.click(True, force=True)
                            clicked_count += 1
                            time.sleep(0.12)
                    except Exception as e:
                        print(f'[调试] 处理选项第 {idx + 1} 个异常: {e}')
                return clicked_count > 0 or len(spans) > 0
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
        if q_type == 'blank':
            try:
                filled_ok = question_locator.evaluate(FILL_TEXT_SCRIPT, text_answers)
                return filled_ok > 0
            except Exception as e:
                print(f'[调试] 填空题原生JS写入异常: {e}')
                return False
        if q_type == 'essay' and text_answers:
            try:
                filled_ok = question_locator.evaluate(FILL_TEXT_SCRIPT, text_answers)
                return filled_ok > 0
            except Exception as e:
                print(f'[调试] 简答题原生JS写入异常: {e}')
                return False
        return False
    except Exception as ex:
        print(f'智能填涂异常: {ex}')
        return False
def find_button_in_frames(page, text_list):
    """
    在主框架 + 所有 iframe 中按文本定位"可见、可用、尺寸合理"的按钮。
    返回 (元素, 所在frame)；找不到返回 (None, None)。
    """
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
                                    if tag_name == 'DIV':
                                        if box['height'] > 80 or box['width'] > 300:
                                            continue
                                    return (el, frame)
                        except Exception:
                            continue
        except Exception:
            pass
    return (None, None)


def find_save_button(page):
    """找"暂存/保存"按钮（先按文本，再按常见 class 选择器）。"""
    el, frame = find_button_in_frames(page, ['暂存', '保存答案', '保存'])
    if el:
        return (el, frame)
    fallback_selectors = ['.save', "[class*='save']", '.btn-save', '#saveButton']
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


def find_submit_button(page):
    """找"提交作业/提交"按钮。"""
    el, frame = find_button_in_frames(page, ['提交作业', '提交', '确认提交'])
    if el:
        return (el, frame)
    fallback_selectors = ['.submit', "[class*='submit']", '.btn-submit', '#submitButton', '.btn_ok']
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


def find_next_button(page):
    """找"下一页/下一章/下一节/下一题/下一步"按钮。"""
    el, frame = find_button_in_frames(page, ['下一页', '下一章', '下一节', '下一题', '下一步'])
    if el:
        return (el, frame)
    fallback_selectors = [
        '.next-chapter',
        "[title='下一节']",
        ".jb_btn:has-text('下一节')",
        "span:has-text('下一节')",
        '.next',
        "[class*='next']",
        '.btn-next',
        '#nextChapter',
        '.prev_next .next',
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
    """找页面顶部章节 Tab 按钮列表（至少 2 个可见）。"""
    selectors = ['.prev_ul li', '.prev_tab li', '.prev_tab_ul li', "ul[class*='prev'] li", "div[class*='tab'] li"]
    for sel in selectors:
        try:
            locs = cards_frame.locator(sel).all()
            if len(locs) > 1:
                valid_locs = []
                for loc in locs:
                    if loc.is_visible():
                        valid_locs.append(loc)
                if len(valid_locs) > 1:
                    return valid_locs
        except Exception:
            pass
    return []


def robust_wait_for_tasks_to_render(page, check_func, timeout=8000):
    """
    等待任务列表渲染完成。
    前 5 次快速探测 check_func()，若为真则视为"没任务"直接返回 False；
    之后在 timeout(ms) 内轮询页面，找到 <video>/题目/随堂测验 iframe 任一即返回 True。
    """
    for _ in range(5):
        if check_func():
            return False
        time.sleep(0.1)
    start_time = time.time() * 1000
    while time.time() * 1000 - start_time < timeout:
        if check_func():
            return False
        video_found = False
        questions_found = False
        quiz_iframe_found = False
        try:
            frames = [page.main_frame] + page.frames
            for f in frames:
                if f.locator('video').count() > 0:
                    video_found = True
                    break
                for quiz_sel in ('div.singlequesid', 'div.singleQuesId', 'div.TiMu',
                                 '.question-card', '.question-item', '.ti-q-c', '.que'):
                    if f.locator(quiz_sel).count() > 0:
                        questions_found = True
                        break
                if f.locator('.ans-attach-online').count() > 0:
                    quiz_iframe_found = True
                    break
        except Exception:
            pass
        if video_found or questions_found or quiz_iframe_found:
            time.sleep(0.5)
            return True
        time.sleep(0.4)
    return False


def get_device_id():
    """基于 MAC 地址指纹生成设备标识，用于服务端限制设备数/做标记。"""
    try:
        import uuid as machine_uuid
        return 'DEV-' + hashlib.md5(str(machine_uuid.getnode()).encode()).hexdigest()[:12].upper()
    except Exception:
        return 'DEV-UNKNOWN'


def clean_task_url(url):
    """
    把任务 URL 归一化成"类型 + 资源ID"的稳定签名（video/file/job），
    用于去重；解析失败则去掉时间戳等易变参数后返回。
    """
    try:
        url_lower = url.lower()
        objectid_match = re.search(r'objectid=([a-f0-9\-_]+)', url_lower)
        fileid_match = re.search(r'fileid=([a-f0-9\-_]+)', url_lower)
        jobid_match = re.search(r'jobid=([a-f0-9\-_]+)', url_lower)
        key_parts = []
        if '/video/' in url_lower:
            key_parts.append('video')
        elif '/audio/' in url_lower:
            key_parts.append('audio')
        elif '/pdf/' in url_lower:
            key_parts.append('pdf')
        elif '/ppt/' in url_lower:
            key_parts.append('ppt')
        elif '/doc/' in url_lower:
            key_parts.append('doc')
        if objectid_match:
            key_parts.append(f"obj_{objectid_match.group(1)}")
        if fileid_match:
            key_parts.append(f"file_{fileid_match.group(1)}")
        if jobid_match:
            key_parts.append(f"job_{jobid_match.group(1)}")
        if key_parts:
            return '_'.join(key_parts)
        cleaned = re.sub(r'[\?&](_dc|v|t|k|cpi|ut|enc)=[^&]*', '', url, flags=re.I)
        cleaned = cleaned.rstrip('?&').lower()
        return cleaned
    except Exception:
        return url.lower()


def find_confirmation_bypass_button(page):
    """检测"离开/完成"确认弹窗并尝试点击绕过按钮（切到下节）。"""
    bypass_selectors = [
        '.popDiv.wid440.popMove .nextChapter',
        '.popDiv .nextChapter',
        ".popDiv a:has-text('下一节')",
        ".popDiv a:has-text('确定')",
        ".popDiv button:has-text('确定')",
        'a.nextChapter',
        "[class*='pop'] a:has-text('确定')",
        "[class*='pop'] button:has-text('确定')",
    ]
    frames_to_scan = [page.main_frame] + page.frames
    for frame in frames_to_scan:
        try:
            body_text = frame.evaluate("document.body ? document.body.innerText : ''")
            if any(kw in body_text for kw in ('还有任务点未完成', '未完成的任务点',
                                              '确认离开', '当前章节还有', '是否去完成')):
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


def solve_single_question_client(args, card_key, log_func, timeout=150):
    """
    把一道题的截图等发给后端 /solve 求解。
    成功返回 (index, {'hash_id', 'answer_key', 'text_answers', 'question_type',
                      'image', 'remaining_points'})；失败返回 (index, None)。
    """
    index, task_data = args
    image_bytes = task_data['image_bytes']
    q_type = task_data['question_type']
    num_blanks = task_data['num_blanks']
    text_source = task_data['text_hash_source']

    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    url = f'{SERVER_URL}/solve'
    payload = {
        'card_key': card_key,
        'image': base64_image,
        'question_type': q_type,
        'num_blanks': num_blanks,
        'text_hash_source': text_source,
        'school_id': SCHOOL_ID,
        'device_id': get_device_id(),
    }
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code == 200:
            data = response.json()
            is_cached = ' *' if data.get('cached') else ''
            if q_type in ('choice', 'multi_choice'):
                ans_str = data.get('answer_key')
            else:
                ans_str = ', '.join(data.get('text_answers', []))
            log_func(f'   [成功] 第 {index + 1} 题 -> {ans_str}{is_cached}')
            return (index, {
                'hash_id': data.get('hash_id'),
                'answer_key': data.get('answer_key'),
                'text_answers': data.get('text_answers', []),
                'question_type': q_type,
                'image': base64_image,
                'remaining_points': data.get('remaining_points'),
            })
        else:
            err_detail = response.json().get('detail', '未知网络错误')
            log_func(f'   [错误] 第 {index + 1} 题: {err_detail}')
            return (index, None)
    except Exception as e:
        log_func(f'   [网络异常] 第 {index + 1} 题请求超时/无法连接到服务器: {e}')
        return (index, None)
class AppConsole:
    """
    主控制面板。核心业务：
      - 卡密登录（保存在 %APPDATA%/StudyAssistant/card_key.txt）
      - 点数系统（查余额 / 合并结算 / 看课心跳核销 / 卡密充值 / 官方发卡平台跳转）
      - 安全沙盒浏览器（CDP 端口 9222）的自动拉起、标签页检测与锁定
      - 一键刷课：自动做视频、文档、测验、提交/暂存、翻页、防弹窗
      - 错题人工申诉/云端审计退点
    """

    def __init__(self, root):
        self.root = root
        self.root.title(f'控制面板 v{VERSION}')
        self.root.geometry('680x595')
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

        self.session_history = {}            # 章节名 -> [{index,hash_id,image,answer_key,snippet,...}]
        self.appealing_hashes = set()        # 正在云端审计退点的题目
        self.browser_proc = None
        self.stop_requested = False
        self.pause_requested = False
        self.log_visible = False

        self.notice_text = '正在连接服务器并同步版本信息...'
        self.scroll_index = 0
        self.notice_loop_id = None
        self.appeal_canvas = None
        self.appeal_frame = None

        self.saved_key = ''
        appdata_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'StudyAssistant')
        if not os.path.exists(appdata_dir):
            try:
                os.makedirs(appdata_dir)
            except Exception:
                pass
        self.key_file_path = os.path.join(appdata_dir, 'card_key.txt')
        if os.path.exists(self.key_file_path):
            try:
                with open(self.key_file_path, 'r', encoding='utf-8') as f:
                    val = f.read().strip()
                if val:
                    self.saved_key = val
            except Exception:
                pass

        self.create_widgets()
        self.auto_launch_browser_on_start()
        self.query_points()
        self.accumulated_video_seconds = 0.0

    def on_close_window(self):
        # 存在云端审计中的题时，警告后由用户选择是否强行关闭
        if hasattr(self, 'appealing_hashes') and self.appealing_hashes:
            confirm = messagebox.askyesno(
                '云端审计未完警告',
                '警告：当前有错题正在云端审计退点中（退款处理中）。\n\n'
                '如果此时强行关闭程序，您将失去本批次错题卡片的申诉上下文，导致无法直接在本地发起人工复审'
                '（但云端退点加分仍会自动在服务器执行并到账）。\n\n'
                '建议等待审计完毕再关闭！是否确认强行关闭程序？')
            if not confirm:
                return None
        self.root.destroy()
        return None

    def bind_hover(self, widget, hover_bg=None, normal_bg=None, hover_fg=None, normal_fg=None):
        def on_enter(e):
            widget.config(bg=hover_bg, fg=hover_fg)

        def on_leave(e):
            widget.config(bg=normal_bg, fg=normal_fg)

        widget.bind('<Enter>', on_enter)
        widget.bind('<Leave>', on_leave)
        return None

    def create_widgets(self):
        header_frame = tk.Frame(self.root, bg=self.COLOR_BG, height=40)
        header_frame.pack(fill=tk.X)
        header_frame.pack_propagate(False)

        separator = tk.Frame(self.root, bg=self.COLOR_CARD_BORDER, height=1)
        separator.pack(fill=tk.X, pady=(0, 10))

        lbl_tag = tk.Label(header_frame, text='系统公告', font=('Microsoft YaHei', 8, 'bold'),
                           bg='#E0F2FE', fg='#0369A1', padx=6, pady=2)
        lbl_tag.pack(side=tk.LEFT, padx=(18, 8), pady=8)

        self.lbl_notice = tk.Label(header_frame, text='正在连接服务器并同步版本信息...',
                                   fg='#475569', bg=self.COLOR_BG, font=('Microsoft YaHei', 9))
        self.lbl_notice.pack(side=tk.LEFT, pady=8)
        self.start_scrolling_notice()

        main_container = tk.Frame(self.root, bg=self.COLOR_BG)
        main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 5))

        # ---- 账户卡密行 ----
        frame_auth = tk.Frame(main_container, bg=self.COLOR_CARD_BG, highlightthickness=1,
                              highlightbackground=self.COLOR_CARD_BORDER)
        frame_auth.pack(fill=tk.X, pady=6, ipady=6)

        lbl_key = tk.Label(frame_auth, text='账户卡密', font=('Microsoft YaHei', 9, 'bold'),
                           bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN)
        lbl_key.pack(side=tk.LEFT, padx=(18, 10))

        self.ent_key = tk.Entry(frame_auth, font=('Segoe UI', 10), bd=0, bg='#F1F5F9',
                                fg=self.COLOR_TEXT_MAIN, highlightthickness=1,
                                highlightbackground=self.COLOR_CARD_BORDER, highlightcolor=self.COLOR_PRIMARY,
                                insertbackground=self.COLOR_PRIMARY)
        self.ent_key.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, ipady=3)
        self.ent_key.insert(0, self.saved_key)
        self.ent_key.bind('<FocusOut>', lambda e: self.query_points())
        self.ent_key.bind('<Return>', lambda e: self.query_points())

        self.btn_buy_card = tk.Button(frame_auth, text='[购买卡密]', font=('Microsoft YaHei', 8, 'bold'),
                                      bg=self.COLOR_CARD_BG, fg='#E65100', relief=tk.FLAT, cursor='hand2',
                                      activebackground='#FFF3E0', command=self.open_shop_url)
        self.btn_buy_card.pack(side=tk.RIGHT, padx=(0, 12))
        self.bind_hover(self.btn_buy_card, '#FFF3E0', self.COLOR_CARD_BG, '#BF360C', '#E65100')

        self.btn_recharge_trigger = tk.Button(frame_auth, text='[充点]', font=('Microsoft YaHei', 8, 'bold'),
                                              bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY, relief=tk.FLAT,
                                              cursor='hand2', activebackground='#E3F2FD',
                                              command=self.open_recharge_dialog)
        self.btn_recharge_trigger.pack(side=tk.RIGHT, padx=(0, 6))
        self.bind_hover(self.btn_recharge_trigger, '#E3F2FD', self.COLOR_CARD_BG,
                        self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)

        self.lbl_points = tk.Label(frame_auth, text='点数余额: --', font=('Segoe UI', 9, 'bold'),
                                   bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY)
        self.lbl_points.pack(side=tk.RIGHT, padx=(0, 6))

        # ---- 网页锁定行 ----
        lbl_page = tk.Label(frame_auth, text='当前网页', font=('Microsoft YaHei', 9, 'bold'),
                            bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN)
        lbl_page.pack(side=tk.LEFT, padx=(18, 5), pady=(0, 14))

        self.cb_pages = ttk.Combobox(frame_auth, state='readonly', width=38, font=('Microsoft YaHei', 9))
        self.cb_pages.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=(0, 14))
        self.cb_pages.set('[请点击右侧刷新选择网页]')

        self.btn_refresh = tk.Button(frame_auth, text='检测/刷新网页', font=('Microsoft YaHei', 9, 'bold'),
                                     bg=self.COLOR_PRIMARY_DARK, fg='white', relief=tk.FLAT, cursor='hand2',
                                     command=self.refresh_pages, activebackground=self.COLOR_PRIMARY,
                                     activeforeground='white')
        self.btn_refresh.pack(side=tk.RIGHT, padx=(10, 15), pady=(0, 14))
        self.bind_hover(self.btn_refresh, self.COLOR_PRIMARY_DARK, self.COLOR_PRIMARY)

        # ---- 做题提交模式 ----
        self.var_auto_submit = tk.IntVar(value=1)
        frame_options = tk.Frame(main_container, bg=self.COLOR_BG)
        frame_options.pack(fill=tk.X, pady=(2, 4))

        lbl_mode_title = tk.Label(frame_options, text='做题提交模式:', font=('Microsoft YaHei', 9, 'bold'),
                                  bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN)
        lbl_mode_title.pack(side=tk.LEFT, padx=(10, 15))

        self.rad_submit = tk.Radiobutton(frame_options, text='自动提交', value=1,
                                         variable=self.var_auto_submit, font=('Microsoft YaHei', 9),
                                         bg=self.COLOR_BG, activebackground=self.COLOR_BG,
                                         fg=self.COLOR_TEXT_MAIN, selectcolor='white')
        self.rad_submit.pack(side=tk.LEFT, padx=(0, 30))

        self.rad_save = tk.Radiobutton(frame_options, text='仅暂存不提交', value=0,
                                       variable=self.var_auto_submit, font=('Microsoft YaHei', 9),
                                       bg=self.COLOR_BG, activebackground=self.COLOR_BG,
                                       fg=self.COLOR_TEXT_MAIN, selectcolor='white')
        self.rad_save.pack(side=tk.LEFT)

        self.var_video_speed = tk.StringVar(value='2.0')
        tk.Label(frame_options, text=' ｜ 视频速度:', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(15, 5))
        self.cb_speed = ttk.Combobox(frame_options, textvariable=self.var_video_speed,
                                     values=('1.0', '1.5', '2.0'), state='readonly',
                                     width=5, font=('Microsoft YaHei', 9))
        self.cb_speed.pack(side=tk.LEFT)

        # ---- 任务感知 KPI ----
        dash_frame = tk.Frame(main_container, bg=self.COLOR_BG)
        dash_frame.pack(fill=tk.X, pady=10)

        def _kpi(c, title, desc, set_attr):
            frame = tk.Frame(c, bg=self.COLOR_CARD_BG)
            frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            lbl_v = tk.Label(frame, text='--', font=('Segoe UI', 18, 'bold'), bg=self.COLOR_CARD_BG,
                             fg=self.COLOR_PRIMARY)
            lbl_v.pack(pady=(4, 0))
            setattr(self, set_attr, lbl_v)
            tk.Label(frame, text=desc, font=('Microsoft YaHei', 8), bg=self.COLOR_CARD_BG,
                     fg=self.COLOR_TEXT_MUTED).pack(pady=(2, 0))
            return title

        dash_left = tk.Frame(dash_frame, bg=self.COLOR_CARD_BG, highlightthickness=1,
                             highlightbackground=self.COLOR_CARD_BORDER, width=315, height=165)
        dash_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        dash_left.pack_propagate(False)
        tk.Label(dash_left, text='当前页面任务感知', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(anchor=tk.W, padx=15, pady=(12, 4))
        kpi_container_left = tk.Frame(dash_left, bg=self.COLOR_CARD_BG)
        kpi_container_left.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        _kpi(kpi_container_left, None, '视频任务', 'lbl_task_video_num')
        _kpi(kpi_container_left, None, '文档阅读', 'lbl_task_doc_num')  # placeholder (real: below)
        _kpi(kpi_container_left, None, '测验题目', 'lbl_task_quiz_num')

        dash_right = tk.Frame(dash_frame, bg=self.COLOR_CARD_BG, highlightthickness=1,
                              highlightbackground=self.COLOR_CARD_BORDER, width=315, height=165)
        dash_right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        dash_right.pack_propagate(False)
        tk.Label(dash_right, text='实时执行进度', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MAIN).pack(anchor=tk.W, padx=15, pady=(12, 4))
        self.lbl_prog_task = tk.Label(dash_right, text='当前状态: 闲置中', bg=self.COLOR_CARD_BG,
                                      fg=self.COLOR_PRIMARY_DARK, font=('Microsoft YaHei', 9, 'bold'))
        self.lbl_prog_task.pack(anchor=tk.W, padx=18, pady=(6, 4))
        self.lbl_prog_video = tk.Label(dash_right, text='音视频进度: --', bg=self.COLOR_CARD_BG,
                                       fg=self.COLOR_TEXT_MAIN, font=('Segoe UI', 9))
        self.lbl_prog_video.pack(anchor=tk.W, padx=18, pady=3)
        self.lbl_prog_quiz = tk.Label(dash_right, text='测验进度: --', bg=self.COLOR_CARD_BG,
                                      fg=self.COLOR_TEXT_MAIN, font=('Segoe UI', 9))
        self.lbl_prog_quiz.pack(anchor=tk.W, padx=18, pady=3)

        # ---- 控制按钮 ----
        controls_frame = tk.Frame(main_container, bg=self.COLOR_BG)
        controls_frame.pack(fill=tk.X, pady=6)

        self.btn_run = tk.Button(controls_frame, text='启动自动化刷课与测验', bg=self.COLOR_PRIMARY_DARK,
                                 fg='white', font=('Microsoft YaHei', 10, 'bold'), relief=tk.FLAT,
                                 cursor='hand2', command=self.start_solver_thread,
                                 activebackground='#0D47A1', activeforeground='white')
        self.btn_run.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5), ipady=5)
        self.bind_hover(self.btn_run, '#0D47A1', self.COLOR_PRIMARY_DARK)

        self.btn_pause = tk.Button(controls_frame, text='暂停进程', bg=self.COLOR_TEXT_MUTED, fg='white',
                                   font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                                   state='disabled', command=self.toggle_pause,
                                   activebackground='#95A5A6', activeforeground='white')
        self.btn_pause.pack(side=tk.LEFT, padx=3, ipady=5)
        self.bind_hover(self.btn_pause, '#95A5A6', self.COLOR_TEXT_MUTED)

        self.btn_stop = tk.Button(controls_frame, text='终止退出', bg='#D35400', fg='white',
                                  font=('Microsoft YaHei', 9), relief=tk.FLAT, cursor='hand2',
                                  state='disabled', command=self.trigger_stop,
                                  activebackground='#E67E22', activeforeground='white')
        self.btn_stop.pack(side=tk.RIGHT, padx=(3, 0), ipady=5)
        self.bind_hover(self.btn_stop, '#E67E22', '#D35400')

        self.btn_appeal = tk.Button(main_container, text='错题在线申诉与退点', bg=self.COLOR_CARD_BG,
                                    fg=self.COLOR_PRIMARY, font=('Microsoft YaHei', 9, 'bold'),
                                    relief=tk.FLAT, cursor='hand2', command=self.open_appeal_window,
                                    activebackground='#E3F2FD', activeforeground=self.COLOR_PRIMARY_DARK,
                                    highlightthickness=1, highlightbackground=self.COLOR_CARD_BORDER)
        self.btn_appeal.pack(fill=tk.X, pady=6, ipady=5)
        self.bind_hover(self.btn_appeal, '#E3F2FD', self.COLOR_CARD_BG)

        self.btn_toggle_log = tk.Button(main_container, text='展开详细运行日志 ∨', bg=self.COLOR_BG,
                                        fg=self.COLOR_TEXT_MUTED, font=('Microsoft YaHei', 9),
                                        relief=tk.FLAT, cursor='hand2', command=self.toggle_log,
                                        activebackground='#E5E7E9', activeforeground=self.COLOR_TEXT_MUTED)
        self.btn_toggle_log.pack(fill=tk.X, pady=(2, 5))
        self.bind_hover(self.btn_toggle_log, '#E5E7E9', self.COLOR_BG)

        self.log_frame = tk.Frame(main_container, bg=self.COLOR_BG)
        self.log_area = scrolledtext.ScrolledText(self.log_frame, height=9, font=('Consolas', 9),
                                                  bg='#FFFFFF', fg=self.COLOR_TEXT_MAIN, highlightthickness=1,
                                                  highlightbackground=self.COLOR_CARD_BORDER,
                                                  highlightcolor=self.COLOR_PRIMARY)
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.insert(tk.END, f'[系统] 服务就绪。当前版本: {VERSION}\n')
        self.log_area.configure(state='disabled')

    def toggle_log(self):
        if self.log_visible:
            self.log_frame.pack_forget()
            self.btn_toggle_log.configure(text='展开详细运行日志 ∨')
            self.root.geometry('680x595')
            self.log_visible = False
        else:
            self.log_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 5))
            self.btn_toggle_log.configure(text='收起详细运行日志 ∧')
            self.root.geometry('680x835')
            self.log_visible = True
        return None

    def log(self, text):
        self.log_area.configure(state='normal')
        self.log_area.insert(tk.END, text + '\n')
        self.log_area.see(tk.END)
        self.log_area.configure(state='disabled')
        return None

    def start_scrolling_notice(self, text_content=None):
        if text_content is not None:
            self.notice_text = text_content
            self.scroll_index = 0
        if self.notice_loop_id is not None:
            self.root.after_cancel(self.notice_loop_id)
            self.notice_loop_id = None
        clean_notice = clean_text_for_gui(self.notice_text)
        max_visible_len = 35
        if len(clean_notice) <= max_visible_len:
            self.lbl_notice.configure(text=clean_notice)
            return None
        padded_text = clean_notice + '          '
        display_part = padded_text[self.scroll_index:] + padded_text[:self.scroll_index]
        self.lbl_notice.configure(text=display_part[:max_visible_len])
        self.scroll_index = (self.scroll_index + 1) % len(padded_text)
        self.notice_loop_id = self.root.after(250, self.start_scrolling_notice)
        return None

    # ---------------- 点数 / 充值 / 商城 ----------------

    def query_points(self):
        card_key = self.ent_key.get().strip()
        if not card_key:
            self.root.after(0, lambda: self.lbl_points.configure(text='请输入账户卡密',
                                                                 fg=self.COLOR_PRIMARY))
            self.btn_run.configure(state='disabled', bg='#BDC3C7')
            self.btn_recharge_trigger.configure(state='disabled')
            self.root.after(0, self.check_server_version)
            return None
        device_id = get_device_id()

        def run_query():
            success = False
            try:
                res = requests.get(
                    f'{SERVER_URL}/points?card_key={card_key}&device_id={device_id}&school_id={SCHOOL_ID}',
                    timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    self.update_remaining_points(data.get('points'))
                    self.root.after(0, lambda: self.btn_run.configure(state='normal'))
                    self.root.after(0, lambda: self.btn_recharge_trigger.configure(state='normal'))
                    try:
                        with open(self.key_file_path, 'w', encoding='utf-8') as f:
                            f.write(card_key)
                    except Exception:
                        pass
                    success = True
                else:
                    err_detail = res.json().get('detail', '无对应账户')
                    self.root.after(0, lambda e=err_detail: self.lbl_points.configure(
                        text=f'查询失败: {e}', fg=self.COLOR_PRIMARY))
                    self.root.after(0, lambda: self.btn_run.configure(state='normal'))
                    self.root.after(0, lambda: self.btn_recharge_trigger.configure(state='normal'))
            except Exception as e:
                self.root.after(0, lambda e=e: self.lbl_points.configure(
                    text='网络异常', fg=self.COLOR_PRIMARY))
                self.root.after(0, lambda: self.btn_run.configure(state='normal'))
                self.root.after(0, lambda: self.btn_recharge_trigger.configure(state='normal'))

        import threading
        t_query = threading.Thread(target=run_query)
        t_query.daemon = True
        t_query.start()
        return None

    def deduct_checkout(self, solve_count):
        """按实际填涂题数从服务器一次性扣点，返回是否成功。"""
        card_key = self.ent_key.get().strip()
        device_id = get_device_id()
        url = f'{SERVER_URL}/solve/checkout'
        payload = {
            'card_key': card_key,
            'device_id': device_id,
            'school_id': SCHOOL_ID,
            'solve_count': solve_count,
        }
        try:
            res = requests.post(url, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                self.update_remaining_points(data.get('remaining_points'))
                self.log(f'      [合并结算] 本章实际填涂 {solve_count} 题，成功一次性扣除 '
                         f'{solve_count} 点。余额: {data.get("remaining_points")}')
                return True
            if res.status_code == 402:
                self.log(f'      [合并结算] 充值卡点数不足！结算 {solve_count} 题失败，'
                         f'正在自动强制终止刷课...')
                self.root.after(0, self.trigger_stop)
                messagebox.showwarning('点数不足',
                                       f'您的点数余额不足以结算本章已填涂的 {solve_count} 道题目，'
                                       f'进程已自动终止。请点击 [充点] 充值后继续使用！')
                return False
            err_msg = res.json().get('detail', '结算失败')
            self.log(f'      [警告] 章节结账请求被服务器拒绝: {err_msg}')
            return False
        except Exception as e:
            self.log(f'      [警告] 章节结账时网络连接异常: {e}')
            return False

    def open_shop_url(self):
        self.log('[系统] 正在为您打开官方发卡平台，请稍候...')

        def run():
            opened = False
            try:
                kill_and_launch_browser()
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                    context = browser.contexts[0]
                    page = context.new_page()
                    page.goto(SHOP_URL)
                    page.bring_to_front()
                    browser.close()
                opened = True
                self.log('[系统] 已成功在沙盒浏览器中打开官方发卡平台。')
            except Exception as e:
                print(f'[调试] 沙盒浏览器打开链接失败: {e}')
                opened = False
            if not opened:
                import webbrowser
                try:
                    webbrowser.open(SHOP_URL)
                    self.log('[系统] 已为您在系统浏览器中打开官方发卡平台。')
                except Exception as ex:
                    self.log(f'[错误] 无法打开浏览器: {ex}')
            return None

        import threading
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return None

    def open_recharge_dialog(self):
        card_key = self.ent_key.get().strip()
        if not card_key or card_key == 'TEST-KEY-123':
            messagebox.showwarning('提示',
                                   '当前使用的是系统公共试用卡密 [TEST-KEY-123]，无法直接充值。\n\n'
                                   '请先在主界面左侧输入框中填入您专属的【账户卡密】'
                                   '（即您长期绑定的固定账号）后再点击 [充点] 按钮！')
            return None

        recharge_win = tk.Toplevel(self.root)
        recharge_win.title('卡密自助充值核销舱')
        recharge_win.geometry('380x160')
        recharge_win.resizable(False, False)
        recharge_win.configure(bg=self.COLOR_BG)
        tk.Label(recharge_win, text=f'当前充值账户: {card_key}', font=('Microsoft YaHei', 9),
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MUTED).pack(anchor='w', padx=20, pady=(15, 5))
        input_frame = tk.Frame(recharge_win, bg=self.COLOR_BG)
        input_frame.pack(fill=tk.X, padx=20, pady=5)
        tk.Label(input_frame, text='激活码:', font=('Microsoft YaHei', 9, 'bold'),
                 bg=self.COLOR_BG, fg=self.COLOR_TEXT_MAIN).pack(side=tk.LEFT)
        ent_code = tk.Entry(input_frame, font=('Segoe UI', 10), bg='#FFFFFF', fg=self.COLOR_TEXT_MAIN,
                            bd=0, highlightthickness=1, highlightbackground=self.COLOR_CARD_BORDER,
                            insertbackground=self.COLOR_PRIMARY)
        ent_code.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0), ipady=2)

        # 原程序中 btn_confirm 定义在 do_recharge 之后（闭包互相引用），此处保持一致
        def do_recharge():
            code = ent_code.get().strip()
            if not code:
                messagebox.showwarning('提示', '请输入您购买到的充值激活码！')
                return None
            btn_confirm.configure(state='disabled', text='正在向云端安全核销...')

            def run():
                url = f'{SERVER_URL}/recharge'
                payload = {'card_key': card_key, 'recharge_key': code}
                try:
                    res = requests.post(url, json=payload, timeout=15)
                    if res.status_code == 200:
                        data = res.json()

                        def ok():
                            self.update_remaining_points(data.get('remaining_points'))
                            messagebox.showinfo('充值成功', data.get('msg'))
                            recharge_win.destroy()

                        self.root.after(0, ok)
                        return None
                    else:
                        err_msg = res.json().get('detail', '充值核销失败')

                        def err():
                            btn_confirm.configure(state='normal', text='立即核销充值')
                            messagebox.showerror('充值失败', f'核销失败: {err_msg}')

                        self.root.after(0, err)
                        return None
                except Exception as e:

                    def err_ex():
                        btn_confirm.configure(state='normal', text='立即核销充值')
                        messagebox.showerror('网络异常', f'无法连接到充值服务器: {e}')

                    self.root.after(0, err_ex)
                    return None

            import threading
            t = threading.Thread(target=run)
            t.daemon = True
            t.start()
            return None

        btn_confirm = tk.Button(recharge_win, text='立即核销充值', bg=self.COLOR_PRIMARY_DARK,
                                fg='white', font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT,
                                cursor='hand2', command=do_recharge)
        btn_confirm.pack(fill=tk.X, padx=20, pady=(15, 0), ipady=3)
        self.bind_hover(btn_confirm, '#0D47A1', self.COLOR_PRIMARY_DARK)
        return None

    def deduct_video_heartbeat(self):
        """看课心跳：累计刷满 20 分钟视频即向服务器核销 1 点。"""
        card_key = self.ent_key.get().strip()
        device_id = get_device_id()

        def run():
            url = f'{SERVER_URL}/video_heartbeat'
            payload = {'card_key': card_key, 'device_id': device_id, 'school_id': SCHOOL_ID}
            try:
                res = requests.post(url, json=payload, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    self.update_remaining_points(data.get('remaining_points'))
                    self.log('      [看课心跳] 已累计挂机看课10分钟（刷完20分钟视频），'
                             f'成功核销 1 点。最新余额: {data.get("remaining_points")}')
                elif res.status_code == 402:
                    self.log('      [看课心跳] 充值卡点数余额已耗尽！正在自动强制终止刷课进程...')
                    self.root.after(0, self.trigger_stop)
                    messagebox.showwarning('点数耗尽',
                                           '您的充值卡点数余额已耗尽，请点击 [充点] 按钮充值后继续使用！')
                else:
                    err_msg = res.json().get('detail', '核销失败')
                    self.log(f'      [警告] 看课心跳核销失败: {err_msg}')
            except Exception as e:
                self.log(f'      [警告] 看课心跳发送异常: {e}')
            return None

        import threading
        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        return None

    # ---------------- UI 状态更新 ----------------

    def update_remaining_points(self, points):
        def update():
            self.lbl_points.configure(text=f'点数余额: {points}')

        self.root.after(0, update)
        return None

    def update_task_perception(self, video_count, doc_count, quiz_count):
        def update():
            if video_count is not None:
                self.lbl_task_video_num.configure(text=str(video_count))
            if doc_count is not None:
                self.lbl_task_doc_num.configure(text=str(doc_count))
            if quiz_count is not None:
                self.lbl_task_quiz_num.configure(text=str(quiz_count))

        self.root.after(0, update)
        return None

    def update_progress_task(self, task_name):
        self.root.after(0, lambda: self.lbl_prog_task.configure(text=f'当前状态: {task_name}'))
        return None

    def update_progress_video(self, percent_text):
        self.root.after(0, lambda: self.lbl_prog_video.configure(text=f'音视频进度: {percent_text}'))
        return None

    def update_progress_quiz(self, progress_text):
        self.root.after(0, lambda: self.lbl_prog_quiz.configure(text=f'测验进度: {progress_text}'))
        return None

    def check_server_version(self):
        try:
            card_key = self.ent_key.get().strip() if hasattr(self, 'ent_key') else ''
            response = requests.get(
                f'{SERVER_URL}/check_version?ver={VERSION}&school_id={SCHOOL_ID}&card_key={card_key}',
                timeout=5)
            if response.status_code == 200:
                data = response.json()
                self.start_scrolling_notice(data.get('notice'))
                if data.get('force_update'):
                    messagebox.showerror('更新提示',
                                         '检测到有新版本客户端发布，旧版本已停止支持。\n'
                                         f'请前往下载最新版本：{data.get("download_url")}')
                    self.btn_run.configure(state='disabled', text='版本已过期，请更新后使用', bg='#BDC3C7')
                    return None
            else:
                self.start_scrolling_notice('[提示] 无法获取服务器版本公告。')
                return None
        except Exception:
            self.start_scrolling_notice('[提示] 连接服务器失败，请检查网络或重试。')
            self.btn_run.configure(state='disabled', text='连接服务器失败', bg='#BDC3C7')
            return None

    # ---------------- 安全沙盒浏览器 ----------------

    def auto_launch_browser_on_start(self):
        self.log('[系统] 正在为您自动唤醒安全沙盒浏览器...')

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
                    self.root.after(0, lambda: self.log(
                        '[系统] 安全沙盒浏览器已就绪！请登录网课平台并点开做题页，然后点击右侧刷新。'))
            else:
                self.root.after(0, lambda: self.log(
                    '[警告] 安全沙盒浏览器自动打开失败，请确保9222端口未被占用。'))
            return None

        import threading
        t_launch = threading.Thread(target=do_launch)
        t_launch.daemon = True
        t_launch.start()
        return None

    def update_pages_dropdown_silent(self, pages_list):
        clean_pages = [p.strip() for p in pages_list
                       if p and p.strip() and p != 'about:blank'
                       and p != 'New Tab' and p != '新建标签页']
        if not clean_pages:
            self.cb_pages.configure(values=['[安全浏览器当前无活动标签页]'])
            self.cb_pages.set('[安全浏览器当前无活动标签页]')
            self.log('[系统] 安全浏览器已就绪！请在页面中点开做题页。')
            return None
        self.cb_pages.configure(values=clean_pages)
        keywords = ['学习', '作业', '检测', '测试', '考试', '评估']
        default_idx = 0
        for idx, title in enumerate(clean_pages):
            if any(kw in title for kw in keywords):
                default_idx = idx
                break
        self.cb_pages.current(default_idx)
        self.log('[系统] 自动锁定并恢复历史页面成功，可以直接启动自动化流程。')
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
                self.log('[警告] 未获取到活动标签，请点击刷新重新检测')
                self.btn_refresh.configure(state='normal', text='检测/刷新网页')
            return None

        import threading
        t_refresh = threading.Thread(target=do_refresh)
        t_refresh.daemon = True
        t_refresh.start()
        return None

    def update_pages_dropdown(self, pages_list):
        self.btn_refresh.configure(state='normal', text='检测/刷新网页')
        clean_pages = [p.strip() for p in pages_list
                       if p and p.strip() and p != 'about:blank'
                       and p != 'New Tab' and p != '新建标签页']
        if not clean_pages:
            self.cb_pages.configure(values=['[安全浏览器当前无活动标签页]'])
            self.cb_pages.set('[安全浏览器当前无活动标签页]')
            self.log('[警告] 页面检测失败：安全浏览器内未找到任何有效标签页，请先在浏览器中点开做题页。')
            return None
        self.cb_pages.configure(values=clean_pages)
        keywords = ['学习', '作业', '检测', '测试', '考试', '评估']
        default_idx = 0
        for idx, title in enumerate(clean_pages):
            if any(kw in title for kw in keywords):
                default_idx = idx
                break
        self.cb_pages.current(default_idx)
        self.log('[系统] 安全沙盒网页锁定成功！请在下拉框中确认，然后启动自动化。')
        return None

    # ---------------- 运行控制 ----------------

    def check_pause_and_stop(self):
        if self.stop_requested:
            return True
        while self.pause_requested and not self.stop_requested:
            time.sleep(0.2)
        if self.stop_requested:
            return True
        return False

    def toggle_pause(self):
        if self.pause_requested:
            self.pause_requested = False
            self.btn_pause.configure(text='暂停进程', bg='#E67E22')
            self.log('[系统] 收到恢复指令，继续执行自动化进程...')
        else:
            self.pause_requested = True
            self.btn_pause.configure(text='恢复执行', bg='#2980B9')
            self.log('[系统] 自动化流程已挂起，您可以点击恢复或终止。')
        return None

    def trigger_stop(self):
        self.stop_requested = True
        self.log('[系统] 已向执行线程投递终止信号，请等待当前节点安全归档...')
        return None

    def set_running_ui_state(self):
        self.btn_run.configure(state='disabled', text='正在运行自动化...')
        self.btn_pause.configure(state='normal', text='暂停进程', bg='#E67E22')
        self.btn_stop.configure(state='normal')
        return None

    def reset_control_buttons(self):
        self.btn_run.configure(state='normal', text='启动自动化刷课与测验')
        self.btn_pause.configure(state='disabled', text='暂停进程', bg=self.COLOR_TEXT_MUTED)
        self.btn_stop.configure(state='disabled')
        self.update_task_perception(0, 0, 0)
        self.update_progress_task('闲置中')
        self.update_progress_video('--')
        self.update_progress_quiz('--')
        return None

    def start_solver_thread(self):
        import threading
        t = threading.Thread(target=self.run_solver_process)
        t.daemon = True
        t.start()
        return None

    def show_large_image(self, b64_img):
        img_win = tk.Toplevel(self.root)
        img_win.title('错题原图 100% 超高清放大视图 (支持鼠标滚轮/拖动滚动)')
        img_win.geometry('900x700')
        img_win.configure(bg='white')
        canvas = tk.Canvas(img_win, bg='white', highlightthickness=0)
        v_scroll = ttk.Scrollbar(img_win, orient='vertical', command=canvas.yview)
        h_scroll = ttk.Scrollbar(img_win, orient='horizontal', command=canvas.xview)
        canvas.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        try:
            img_bytes = base64.b64decode(b64_img)
            if HAS_PIL:
                pil_img = Image.open(io.BytesIO(img_bytes))
                photo = ImageTk.PhotoImage(pil_img)
                w, h = pil_img.size
            else:
                photo = tk.PhotoImage(data=img_bytes)
                w, h = photo.width(), photo.height()
            canvas.create_image(0, 0, anchor='nw', image=photo)
            photo.image = canvas  # keep reference
            canvas.configure(scrollregion=(0, 0, w, h))

            def _on_img_wheel(event):
                if event.state & 1:
                    canvas.xview_scroll(int(-1 * (event.delta / 120)), 'units')
                else:
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

            canvas.bind('<MouseWheel>', _on_img_wheel)
        except Exception as e:
            messagebox.showerror('渲染错误', f'无法加载超高精度原图: {e}')
        return None
# ---------------- 错题申诉 / 云端退点 ----------------

    def open_appeal_window(self):
        if not self.session_history:
            messagebox.showwarning('提示',
                                   '当前运行记录为空！请先完成一次做题，确认网页判分结果后再来申诉。')
            return None

        appeal_win = tk.Toplevel(self.root)
        appeal_win.title('错题在线自主退款申诉面板 (多章节选项卡舱)')
        appeal_win.geometry('830x650')
        appeal_win.resizable(False, False)
        appeal_win.configure(bg=self.COLOR_BG)
        self.appeal_win_ptr = appeal_win
        self.appeal_cards_map = {}

        def on_destroy_win(e=None):
            self.appeal_win_ptr = None
            self.appeal_cards_map = {}
            appeal_win.destroy()

        appeal_win.protocol('WM_DELETE_WINDOW', on_destroy_win)

        tk.Label(appeal_win, text='请在上方选择错题章节选项卡，勾选错题并填写您认为的正确答案发起退点：',
                 font=('Microsoft YaHei', 9, 'bold'), bg=self.COLOR_BG, fg='#D35400'
                 ).pack(anchor=tk.W, padx=18, pady=(12, 5))

        notebook = ttk.Notebook(appeal_win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=18, pady=(5, 10))

        frame_bottom = tk.Frame(appeal_win, bg=self.COLOR_BG)
        frame_bottom.pack(side='bottom', fill='x', padx=18, pady=(5, 15))
        self.lbl_appeal_progress = tk.Label(frame_bottom, text='就绪。请选择错题并点击提交。',
                                            font=('Microsoft YaHei', 9), bg=self.COLOR_BG,
                                            fg=self.COLOR_TEXT_MUTED)
        self.lbl_appeal_progress.pack(pady=(0, 8))
        lbl_progress = self.lbl_appeal_progress

        if hasattr(self, 'appealing_hashes') and self.appealing_hashes:
            self.lbl_appeal_progress.configure(
                text=f'云端正在并发审计中：当前还有 {len(self.appealing_hashes)} 道题处于处理队列，请耐心等待...',
                fg=self.COLOR_PRIMARY_DARK)

        all_vars_maps = {}

        def dim_card(hash_id, status_text, status_fg, bg_color='#F1F5F9'):
            entry = self.appeal_cards_map.get(hash_id)
            if not entry:
                return
            entry['status_lbl'].configure(text=status_text, fg=status_fg)
            entry['chk'].configure(state='disabled')
            entry['ent'].configure(state='disabled')

        def undim_card(hash_id, status_text, status_fg):
            entry = self.appeal_cards_map.get(hash_id)
            if entry:
                entry['status_lbl'].configure(text=status_text, fg=status_fg)
                entry['chk'].configure(state='normal')
                entry['ent'].configure(state='normal')

        def bind_mousewheel_recursive(widget, canvas_ctrl):
            widget.bind_all('<MouseWheel>',
                            lambda e: canvas_ctrl.yview_scroll(int(-1 * (e.delta / 120)), 'units'))
            for child in widget.winfo_children():
                bind_mousewheel_recursive(child, canvas_ctrl)

        def show_manual_button(task_item, text_label, trigger_cmd, canvas_ctrl, frame_ctrl):
            """在卡片上放一个『人工申诉』按钮，点击后隐藏并弹辅助输入框。"""
            btn = tk.Button(task_item, text=text_label, command=trigger_cmd, bg=self.COLOR_PRIMARY,
                            fg='white', font=('Microsoft YaHei', 8, 'bold'), relief=tk.FLAT)
            btn.pack(side=tk.RIGHT, padx=(4, 0), pady=4)

            def update_ui():
                frame_ctrl.pack_forget()

            return btn

        for chapter_name, items_list in self.session_history.items():
            if not items_list:
                continue
            tab_frame = tk.Frame(notebook, bg=self.COLOR_BG)
            notebook.add(tab_frame, text=chapter_name)

            canvas = tk.Canvas(tab_frame, bg=self.COLOR_BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(tab_frame, orient='vertical', command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)

            def make_scroll_updater(c_ctrl, f_ctrl):
                return lambda e: c_ctrl.configure(scrollregion=c_ctrl.bbox('all'))

            scrollable_frame.bind('<Configure>', make_scroll_updater(canvas, scrollable_frame))
            canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
            canvas.bind('<Configure>', lambda e, cw=canvas_window: canvas.itemconfigure(cw, width=e.width))
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side='left', fill='both', expand=True, padx=(10, 0), pady=5)
            scrollbar.pack(side='right', fill='y', padx=(0, 10), pady=5)
            bind_mousewheel_recursive(tab_frame, canvas)

            vars_map = {}
            all_vars_maps[chapter_name] = vars_map
            for item in items_list:
                card_frame = tk.Frame(scrollable_frame, bg=self.COLOR_CARD_BG, highlightthickness=1,
                                      highlightbackground=self.COLOR_CARD_BORDER)
                card_frame.pack(fill=tk.X, padx=8, pady=4)

                var = tk.BooleanVar(value=False)
                header_inner = tk.Frame(card_frame, bg=self.COLOR_CARD_BG)
                header_inner.pack(fill=tk.X, padx=8, pady=(6, 2))
                chk = tk.Checkbutton(header_inner, variable=var, text=f"第 {item['index'] + 1} 题",
                                     font=('Microsoft YaHei', 9, 'bold'), bg=self.COLOR_CARD_BG,
                                     fg=self.COLOR_TEXT_MAIN, activebackground=self.COLOR_CARD_BG)
                chk.pack(side=tk.LEFT)
                lbl_status = tk.Label(header_inner, text='待申诉', font=('Microsoft YaHei', 8),
                                      bg=self.COLOR_CARD_BG, fg=self.COLOR_PRIMARY)
                lbl_status.pack(side=tk.LEFT, padx=(12, 0))

                # 缩略图与纠错输入
                img_bytes = base64.b64decode(item.get('image', '')) if item.get('image') else b''
                lbl_img = tk.Label(card_frame, text='(无图)', bg=self.COLOR_CARD_BG)
                lbl_img.pack(side=tk.LEFT, padx=8, pady=4)
                if img_bytes:
                    try:
                        if HAS_PIL:
                            pil_img = Image.open(io.BytesIO(img_bytes))
                            w, h = pil_img.size
                            ratio = min(120 / max(w, 1), 80 / max(h, 1))
                            resample_filter = getattr(Image, 'LANCZOS', None) or getattr(Image, 'ANTIALIAS', None)
                            new_w, new_h = int(w * ratio), int(h * ratio)
                            photo = ImageTk.PhotoImage(pil_img.resize((new_w, new_h), resample_filter))
                        else:
                            photo = tk.PhotoImage(data=img_bytes)
                            photo = None
                        if photo is not None:
                            lbl_img.configure(image=photo)
                            lbl_img.image = photo
                    except Exception:
                        pass

                input_frame = tk.Frame(card_frame, bg=self.COLOR_CARD_BG)
                input_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=4)
                tk.Label(input_frame, text='我给出的正确答案:', font=('Microsoft YaHei', 8),
                         bg=self.COLOR_CARD_BG, fg=self.COLOR_TEXT_MUTED).pack(anchor=tk.W)
                ent_ans = tk.Entry(input_frame, font=('Segoe UI', 10), bg='#FFFFFF',
                                   fg=self.COLOR_TEXT_MAIN, insertbackground=self.COLOR_PRIMARY)
                ent_ans.pack(fill=tk.X, ipady=2)

                self.appeal_cards_map[item['hash_id']] = {
                    'chk': chk, 'ent': ent_ans, 'status_lbl': lbl_status, 'card': card_frame,
                    'task': item, 'var': var,
                }
                vars_map[item['hash_id']] = var

        # 底部提交按钮
        def submit_appeal():
            card_key = self.ent_key.get().strip()
            selected_tasks = []
            for h_id, task in self.appeal_cards_map.items():
                if task['var'].get() and h_id not in self.appealing_hashes:
                    user_ans_val = task['ent'].get().strip()
                    if not user_ans_val:
                        messagebox.showwarning(
                            '提示',
                            f"【{task['task']['chapter_name']}】第 {task['task']['index'] + 1} 题"
                            f"已勾选，请先在纠错框中填入您认为的答案！")
                        return None
                    u_clean = re.sub(r'\s+', '', user_ans_val).upper().replace('===', ',')
                    o_clean = re.sub(r'\s+', '', task['task'].get('original_answer', '')).upper()
                    if u_clean == o_clean:
                        messagebox.showwarning(
                            '提示',
                            f"【{task['task']['chapter_name']}】第 {task['task']['index'] + 1} 题"
                            f"填写的纠错答案与原答案完全相同！")
                        return None
                    selected_tasks.append((h_id, task['task'], user_ans_val))
            if not selected_tasks:
                messagebox.showwarning('提示', '您未勾选任何处于空闲状态的错题进行申诉！')
                return None

            lbl_progress.configure(text='正在安全交送审计单中...', fg=self.COLOR_TEXT_MAIN)
            for h_id, task, user_ans_val in selected_tasks:
                self.appealing_hashes.add(h_id)
                dim_card(h_id, '[云端审计中...]', status_fg='#D35400')
                task['var'].set(False)
            appeal_win.update_idletasks()

            def run_appeal_background(tasks_to_process):
                success_count = 0
                fail_count = 0
                for idx, (h_id, task_old, user_ans_val) in enumerate(tasks_to_process):
                    rem_cnt = len(self.appealing_hashes)

                    def update_prog_label(cnt):
                        lbl_progress.configure(fg=self.COLOR_PRIMARY_DARK)

                    appeal_win.after(0, lambda c=rem_cnt: update_prog_label(c))
                    task = self.appeal_cards_map.get(h_id) if hasattr(self, 'appeal_cards_map') else None

                    def on_auditing():
                        self.appeal_cards_map[h_id]['status_lbl'].configure(
                            text='[审计中]', fg=self.COLOR_PRIMARY_DARK)

                    if hasattr(self, 'appeal_win_ptr') and self.appeal_win_ptr and self.appeal_win_ptr.winfo_exists():
                        appeal_win.after(0, on_auditing)
                    appeal_win.update_idletasks()

                    url = f'{SERVER_URL}/appeal'
                    payload = {
                        'card_key': card_key,
                        'hash_id': h_id,
                        'image': task_old['image'],
                        'user_answer': user_ans_val,
                        'school_id': SCHOOL_ID,
                    }
                    try:
                        res = requests.post(url, json=payload, timeout=150)
                        if h_id in self.appealing_hashes:
                            self.appealing_hashes.remove(h_id)
                        if res.status_code == 200:
                            data = res.json()
                            status = data.get('status')
                            # 更新 session_history 中的申诉状态
                            for ch_name, items_list in self.session_history.items():
                                for it in items_list:
                                    if it['hash_id'] == h_id:
                                        it['appeal_status'] = status
                            pts = data.get('refund_points') or data.get('remaining_points')
                            if pts is not None:
                                self.update_remaining_points(pts)
                            if status in ('ok', 'accepted', 'auditing'):
                                success_count += 1
                                appeal_win.after(0, lambda c=success_count, t=len(tasks_to_process):
                                                 lbl_progress.configure(
                                                     text=f'审计交送完成：成功 {c}/{t}，正在等待云端裁定...'))
                            else:
                                fail_count += 1
                                appeal_win.after(0, lambda d=data: messagebox.showinfo(
                                    '审计结果', str(d)))
                        else:
                            fail_count += 1
                            detail = res.json().get('detail', '审计请求被拒绝')
                            self.root.after(0, lambda d=detail: messagebox.showerror('审计失败', str(d)))
                    except Exception as ex:
                        if h_id in self.appealing_hashes:
                            self.appealing_hashes.remove(h_id)
                        fail_count += 1
                        self.root.after(0, lambda e=ex: messagebox.showerror(
                            '网络异常', f'无法连接到审计服务器: {e}'))
                appeal_win.after(0, lambda: lbl_progress.configure(
                    text=f'交送完毕：成功 {success_count}，失败 {fail_count}。'))

            import threading
            t = threading.Thread(target=run_appeal_background, args=(selected_tasks,))
            t.daemon = True
            t.start()
            return None

        btn_submit = tk.Button(frame_bottom, text='提交申诉并开始云端审计退点', bg=self.COLOR_PRIMARY_DARK,
                               fg='white', font=('Microsoft YaHei', 9, 'bold'), relief=tk.FLAT,
                               cursor='hand2', command=submit_appeal)
        btn_submit.pack(fill='x', ipady=4)
        self.bind_hover(btn_submit, '#0D47A1', self.COLOR_PRIMARY_DARK)
        return None

    def show_manual_button(self, task_item, text_label, trigger_cmd, update_ui):
        btn = tk.Button(task_item, text=text_label, command=trigger_cmd, bg=self.COLOR_PRIMARY,
                        fg='white', font=('Microsoft YaHei', 8, 'bold'), relief=tk.FLAT)
        btn.pack(side=tk.RIGHT, padx=(4, 0), pady=4)
        return btn

    def submit_manual_appeal_background(self, task, hash_id, item_ref, user_answer_val):
        def run():
            card_key = self.ent_key.get().strip()
            url = f'{SERVER_URL}/appeal'
            payload = {
                'card_key': card_key,
                'hash_id': hash_id,
                'image': task.get('image'),
                'user_answer': user_answer_val,
                'school_id': SCHOOL_ID,
            }
            try:
                res = requests.post(url, json=payload, timeout=150)
                if res.status_code == 200:
                    data = res.json()

                    def ok():
                        self.update_remaining_points(data.get('remaining_points'))
                        messagebox.showinfo('申诉成功', str(data))
                        self.appealing_hashes.discard(hash_id)

                    self.root.after(0, ok)
                else:
                    detail = res.json().get('detail', '审计请求被拒绝')

                    def err():
                        messagebox.showerror('申诉失败', str(detail))
                        self.appealing_hashes.discard(hash_id)

                    self.root.after(0, err)
            except Exception as e:

                def err_ex():
                    messagebox.showerror('网络异常', f'无法连接到审计服务器: {e}')
                    self.appealing_hashes.discard(hash_id)

                self.root.after(0, err_ex)
            return None

        import threading
        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        return None

    def _on_mousewheel(self, event):
        self.appeal_canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        return None

    def bind_mousewheel_recursive(self, widget, canvas_ctrl):
        widget.bind_all('<MouseWheel>',
                        lambda e: canvas_ctrl.yview_scroll(int(-1 * (e.delta / 120)), 'units'))
        for child in widget.winfo_children():
            self.bind_mousewheel_recursive(child, canvas_ctrl)

    # ---------------- 核心自动化 ----------------

    def traverse_to_leaf_frame(self, container_locator):
        """穿透 iframe 嵌套，找到承载 video/文档的最内层 frame。"""
        iframe_loc = container_locator.locator('iframe').first
        if iframe_loc.count() == 0:
            return None
        try:
            handle = iframe_loc.element_handle()
            current_frame = handle.content_frame() if handle else None
            if not current_frame:
                return None
            # 等待 iframe 真实渲染（最多 25 次 * 0.2s）
            for _ in range(25):
                if current_frame.url and current_frame.url != 'about:blank':
                    break
                time.sleep(0.2)
            url_lower = (current_frame.url or '').lower()
            has_media_elements = current_frame.locator('video, audio').count() > 0
            has_doc_elements = current_frame.locator('#panView, #container, #scrollBox').count() > 0
            is_task_url = any(kw in url_lower for kw in ('/video/', '/audio/'))
            if has_media_elements or has_doc_elements or is_task_url:
                return current_frame
            # 继续向下穿透
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
        """为单个任务生成稳定签名（页/卡片/任务序号 + 清洗后的 iframe src），用于去重。"""
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
            cleaned_src = clean_task_url(src) if src else 'unloaded'
            return f'p{page_counter}_t{tab_idx}_i{task_idx}_{cleaned_src}'
        except Exception:
            return f'p{page_counter}_t{tab_idx}_i{task_idx}_fallback'

    def run_video_task(self, target_page, task_frame, target_container, task_sig, completed_video_urls):
        """倍速/静音播放当前 video，监控进度并处理换线路/随堂测验/心跳扣点。"""
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
                    '(container) => { return container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\'); }')
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
                status = task_frame.evaluate('() => {\n'
                                             '        try {\n'
                                             '            let v = document.querySelector(\'video, audio\');\n'
                                             '            let ended_by_event = window.__my_video_task_done === true;\n'
                                             '            if (!v) return {ended: true, percent: "100.0", paused: false};\n'
                                             '            let duration = v.duration || 0;\n'
                                             '            let currentTime = v.currentTime || 0;\n'
                                             '            let percent = "0.0";\n'
                                             '            if (duration > 0) { percent = (currentTime / duration * 100).toFixed(1); }\n'
                                             '            let ended = ended_by_event || v.ended || (duration > 0 && currentTime >= v.duration - 1.5);\n'
                                             '            return { ended: ended, percent: percent, paused: v.paused };\n'
                                             '        } catch(e) { return {error: e.toString()}; }\n'
                                             '    }')
                if 'error' in status:
                    raise Exception(status['error'])
                inner_err_count = 0
                if status and not status.get('paused', True) and not status.get('ended', False):
                    self.accumulated_video_seconds += 0.5
                    if self.accumulated_video_seconds >= 600.0:
                        self.accumulated_video_seconds -= 600.0
                        self.deduct_video_heartbeat()   # 刷够20分钟扣1点

                if status.get('paused') and not status.get('ended'):
                    is_line_error = task_frame.evaluate('window.hackLineSwitch()')
                    if is_line_error:
                        self.log('         [警告] 视频源异常，正在自动重试切换线路...')
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
                                self.log('         [自愈] 成功恢复音视频播放。')
                                recovered = True
                                break
                        if not recovered and line_switch_count >= 3:
                            self.log('         [警告] 线路更换频繁，正在尝试重载播放容器...')
                            try:
                                target_container.evaluate(
                                    '(container) => { let iframe = container.querySelector(\'iframe\'); '
                                    'if (iframe) { let src = iframe.src; iframe.src = ""; '
                                    'setTimeout(() => { iframe.src = src; }, 100); } }')
                                line_switch_count = 0
                                time.sleep(2.0)
                            except Exception:
                                pass
                        continue
                    is_quiz = task_frame.evaluate('window.hackQuiz()')
                    if is_quiz:
                        self.log('         [提示] 监测到随堂测验弹窗，正在执行自动作答...')
                        for _ in range(10):
                            if self.check_pause_and_stop():
                                break
                            time.sleep(0.2)
                            quiz_dismissed = task_frame.evaluate(
                                '() => { let inputs = document.querySelectorAll('
                                '\'input[type="radio"], input[type="checkbox"]\'); '
                                'return inputs.length === 0 || (window.__my_video_task_done === true); }')
                            if quiz_dismissed:
                                self.log('         [完成] 随堂测验已通过。')
                                break
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
                    self.log('         [完成] 音视频任务播放结束。')
                    self.update_progress_video('已完成')
                    completed_video_urls.add(task_sig)
                    try:
                        task_frame.evaluate('() => { let v = document.querySelector(\'video, audio\'); '
                                            'if (v) { v.pause(); v.src = ""; v.load(); v.remove(); } }')
                    except Exception:
                        pass
                    media_completed = True
                    return None
            except Exception as loop_ex:
                inner_err_count += 1
                self.log(f'         [警告] 音视频状态监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_video_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.5)
        return None

    def run_doc_task(self, target_page, task_frame, target_container, task_sig, completed_doc_urls):
        """步进滚动阅读文档（pdf/ppt/doc 预览）。"""
        doc_completed = False
        inner_err_count = 0
        self.update_progress_task('步进阅读文档')

        while not doc_completed:
            if self.check_pause_and_stop() or target_page.is_closed():
                return None
            try:
                is_finished = target_container.evaluate(
                    '(container) => { return container.classList.contains("ans-job-finished") || '
                    '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\'); }')
                if is_finished:
                    self.log('         [完成] 平台已标记文档阅读完成。')
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
                    self.log('         [完成] 文档阅读已触底，任务结束。')
                    self.update_progress_video('已完成')
                    completed_doc_urls.add(task_sig)
                    doc_completed = True
                    return None
            except Exception as loop_ex:
                inner_err_count += 1
                self.log(f'         [警告] 文档状态监控异常 ({inner_err_count}/5): {loop_ex}')
                if inner_err_count >= 5:
                    completed_doc_urls.add(task_sig)
                    return None
                time.sleep(1.5)
            time.sleep(0.4)
        return None

    def run_solver_process(self):
        """自动化主流程：锁定网页 → 逐章节/卡片 → 做题/刷视频/刷文档 → 提交/暂存 → 翻页。"""
        card_key = self.ent_key.get().strip()
        if not card_key:
            messagebox.showwarning('警告', '请输入您的充值卡密！')
            return None
        selected_title = self.cb_pages.get().strip()
        if (not selected_title) or '[请点击' in selected_title or '[安全浏览器' in selected_title:
            messagebox.showwarning('提示', '请确保已点开做题网页，并在下拉框中选择要刷题的网页！')
            return None

        self.stop_requested = False
        self.pause_requested = False
        self.root.after(0, self.set_running_ui_state)
        self.log('================================================')
        self.log('[启动] 自动化主流程已就绪，正在准备数据...')
        self.appealing_hashes.clear()
        completed_video_urls = set()
        completed_doc_urls = set()

        ok, proc = kill_and_launch_browser()
        if ok:
            self.browser_proc = proc
        else:
            self.log('[错误] 浏览器挂载失败。请手动双击打开一个 Edge 窗口。')
            self.root.after(0, self.reset_control_buttons)
            return None

        start_time = time.time()
        self.log('[系统] 正在连接浏览器 CDP 远程调试通道...')

        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
                context = browser.contexts[0]

                if not context.pages:
                    self.log('[警告] 检测到浏览器当前无任何活动标签页，正在自动拉起新窗口。')
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

                self.log(f'[系统] 锁定初始网页: 【{target_page.title()}】')
                target_page.on('dialog', lambda dialog: dialog.accept())
                page_counter = 1
                saved_url = None

                while True:
                    # 每次循环重新校验页面
                    context = browser.contexts[0]
                    if not context.pages:
                        self.log('[警告] 检测到浏览器当前无任何活动标签页，正在自动拉起新窗口。')
                        target_page = context.new_page()
                        time.sleep(0.5)
                    elif saved_url:
                        target_page = context.pages[-1]
                        target_page.goto(saved_url)
                        self.log('[系统] 🌟 浏览器重启完毕，已无痕空降返回目标页，继续自动挂机...')
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
                            target_page = context.pages[-1]
                    self.log(f'[系统] 锁定当前网页: 【{target_page.title()}】')
                    target_page.on('dialog', lambda dialog: dialog.accept())
                    inner_loop_broken_for_restart = False

                    while True:
                        if self.check_pause_and_stop():
                            self.log('[系统] 任务因用户请求退出。')
                            inner_loop_broken_for_restart = True
                            break
                        self.log(f'\n--- [ 正在准备第 {page_counter} 页数据 ] ---')
                        try:
                            target_page.wait_for_load_state('load', timeout=15000)
                        except Exception:
                            pass

                        current_chapter_name = '未知章节'
                        # 识别当前章节名
                        try:
                            selectors_current = [
                                '.posCatalog_active .posCatalog_name', 'li.currents .posCatalog_name',
                                '.currents .posCatalog_name', '.posCatalog_active', '.prev_title',
                                '.prev_title_pos', 'h4.currents', 'li.currents', 'span.currents',
                                '.currents', "span.pos[class*='currents']", 'a.currents',
                            ]
                            frames_to_scan = [target_page.main_frame] + target_page.frames
                            for frame in frames_to_scan:
                                try:
                                    for sel in selectors_current:
                                        loc = frame.locator(sel)
                                        if loc.count() > 0:
                                            txt_val = (loc.first.inner_text()
                                                       or loc.first.get_attribute('title') or '')
                                            clean_txt = re.sub(r'\s+', ' ', txt_val).strip()
                                            if clean_txt and any(
                                                    kw in clean_txt for kw in
                                                    ('学生学习页面', '做作业', '做测试', '章节目录')):
                                                current_chapter_name = clean_txt
                                                break
                                    if current_chapter_name != '未知章节':
                                        break
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        if current_chapter_name != '未知章节':
                            current_chapter_name = re.sub(r'\s+', ' ', current_chapter_name).strip()
                        if current_chapter_name == '未知章节':
                            try:
                                raw_title = target_page.title() or ''
                                clean_title = re.sub(
                                    r'[-\s]*(学习|课程学习|学生学习页面|做作业|做测试|测验|课程|New Tab|新建标签页)[-\s]*',
                                    '', raw_title, flags=re.I).strip()
                                if clean_title:
                                    current_chapter_name = clean_title
                            except Exception:
                                pass

                        self.log('[系统] 正在等待主章节框架加载...')
                        robust_wait_for_tasks_to_render(target_page, self.check_pause_and_stop)

                        # 定位 task 卡片 frame
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

                        tab_buttons = []
                        if cards_frame:
                            tab_buttons = find_tab_buttons(cards_frame)
                        total_tabs = max(1, len(tab_buttons))
                        if total_tabs > 1:
                            self.log('[系统] 探测到本节包含多个卡片，启动选项卡顺序轮流处理机制...')
                        else:
                            self.log('[系统] 本节包含单个卡片。')

                        for tab_idx in range(total_tabs):
                            if self.check_pause_and_stop():
                                inner_loop_broken_for_restart = True
                                break
                            self.log(f'\n   --- [ 正在处理任务卡片 {tab_idx + 1} / {total_tabs} ] ---')
                            if len(tab_buttons) > 1:
                                try:
                                    current_tabs = find_tab_buttons(cards_frame)
                                    if tab_idx < len(current_tabs):
                                        target_tab = current_tabs[tab_idx]
                                        target_tab.scroll_into_view_if_needed()
                                        time.sleep(0.3)
                                        target_tab.click(True, force=True)
                                        self.log(f'      [卡片] 已切换至卡片 {tab_idx + 1}，等待渲染...')
                                        time.sleep(1.5)
                                        robust_wait_for_tasks_to_render(target_page,
                                                                         self.check_pause_and_stop)
                                except Exception as tab_ex:
                                    self.log(f'      [警告] 切换任务卡片选项卡失败: {tab_ex}')

                            self.log('      [曝光] 正在滚动激活懒加载组件...')
                            try:
                                active_cards_frame = cards_frame if cards_frame else target_page.main_frame
                                targets = active_cards_frame.locator(
                                    "div.ans-attach-ct, .ans-attach-online, .ans-cc, iframe, div[class*='attach']").all()
                                for target_el in targets:
                                    try:
                                        if target_el.is_visible():
                                            target_el.scroll_into_view_if_needed()
                                    except Exception:
                                        pass
                            except Exception as scroll_ex:
                                print(f'曝光滚动异常: {scroll_ex}')
                            time.sleep(0.3)

                            questions, target_frame = scan_page_recursively(target_page)
                            if questions:
                                quiz_completed = False
                                try:
                                    if target_frame:
                                        quiz_completed = target_frame.evaluate(
                                            '() => { let bodyText = document.body ? document.body.innerText : ""; '
                                            'let is_done_by_text = /得分：|成绩：|已提交|已完成|我的答案|正确答案|查看作答|已批阅|本题得\\s*\\d+/.test(bodyText); '
                                            'let inputs = document.querySelectorAll(\'input[type="radio"], input[type="checkbox"], textarea\'); '
                                            'let all_disabled = inputs.length > 0 && Array.from(inputs).every(el => el.disabled); '
                                            'let has_marks = document.querySelectorAll(\'.answer-right, .score, .scoreNum, .dui, .cuo, [class*="score"]\').length > 0; '
                                            'return is_done_by_text || all_disabled || has_marks; }')
                                        if not quiz_completed:
                                            frame_element = target_frame.frame_element()
                                            if frame_element:
                                                quiz_completed = frame_element.evaluate(
                                                    '(iframe) => { let parent = iframe.closest(\'div.ans-attach-ct\') || iframe.closest(\'.ans-attach-online\'); '
                                                    'if (parent) { return parent.classList.contains("ans-job-finished") || /ans-job-finished|icon_Completed|jobFinish|job-finished/.test(parent.className || \'\'); } '
                                                    'return false; }')
                                except Exception as e:
                                    print(f'[调试] 复合检查测验完成状态异常: {e}')
                                    quiz_completed = False

                                if quiz_completed:
                                    self.log('      [跳过] 检测到当前测验任务点已被平台标记完成，无需重复解答。')
                                    self.update_task_perception(None, None, 0)
                                    self.update_progress_quiz('已完成')
                                else:
                                    total_q = len(questions)
                                    self.log(f'      [测验] 探测到文字题（共 {total_q} 道），正在截图判定...')
                                    self.update_task_perception(None, None, total_q)
                                    self.update_progress_task('自动做题中')
                                    tasks_map = {}
                                    snippets_map = {}
                                    for i, q in enumerate(questions):
                                        if self.stop_requested:
                                            break
                                        try:
                                            q.scroll_into_view_if_needed()
                                            time.sleep(0.05)
                                            img_bytes = q.screenshot()
                                            q_type, num_inputs = detect_question_type_and_inputs(q)
                                            text_source = extract_clean_text_with_latex(q)
                                            tasks_map[i] = {
                                                'image_bytes': img_bytes,
                                                'question_type': q_type,
                                                'num_blanks': num_inputs,
                                                'text_hash_source': text_source,
                                            }
                                            if len(text_source) > 28:
                                                snippets_map[i] = text_source[:25] + '...'
                                            else:
                                                snippets_map[i] = text_source if text_source \
                                                    else f'第 {i + 1} 题'
                                        except Exception as ex:
                                            self.log(f'         [错误] 第 {i + 1} 题扫描失败: {ex}')

                                    if self.check_pause_and_stop():
                                        inner_loop_broken_for_restart = True
                                        break
                                    self.log('      [请求] 正在上传云端并发推理...')
                                    results_map = {}
                                    tasks_list = list(tasks_map.items())
                                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                                        futures = [
                                            executor.submit(solve_single_question_client,
                                                            data, card_key, self.log, 300)
                                            for data in tasks_list]
                                        for future in futures:
                                            idx, res = future.result()
                                            if res:
                                                results_map[idx] = res
                                                pts = res.get('remaining_points')
                                                if pts is not None:
                                                    self.update_remaining_points(pts)
                                                if res.get('question_type') in ('choice', 'multi_choice'):
                                                    ans_key_disp = res.get('answer_key')
                                                else:
                                                    ans_key_disp = ', '.join(res.get('text_answers', []))
                                                if current_chapter_name not in self.session_history:
                                                    self.session_history[current_chapter_name] = []
                                                self.session_history[current_chapter_name].append({
                                                    'index': idx,
                                                    'hash_id': res.get('hash_id'),
                                                    'image': res.get('image'),
                                                    'answer_key': ans_key_disp,
                                                    'snippet': snippets_map.get(idx, f'第 {idx + 1} 题'),
                                                    'question_type': res.get('question_type', 'choice'),
                                                    'original_answer': ans_key_disp,
                                                })
                                    # 未成功的题目重试补刀
                                    failed_indices = [i for i in tasks_map.keys() if i not in results_map]
                                    if failed_indices and not self.stop_requested:
                                        self.log('      [系统] 正在静置缓冲 4 秒，释放残留通道连接...')
                                        for _ in range(20):
                                            if self.check_pause_and_stop():
                                                break
                                            time.sleep(0.2)
                                        if not self.stop_requested:
                                            self.log('      [系统] 正在对未成功解答的题目进行重试补刀...')
                                            for i in failed_indices:
                                                if self.stop_requested:
                                                    break
                                                _, res = solve_single_question_client(
                                                    (i, tasks_map[i]), card_key, self.log, 180)
                                                if res:
                                                    results_map[i] = res
                                                    pts = res.get('remaining_points')
                                                    if pts is not None:
                                                        self.update_remaining_points(pts)
                                                    if res.get('question_type') in ('choice', 'multi_choice'):
                                                        ans_key_disp = res.get('answer_key')
                                                    else:
                                                        ans_key_disp = ', '.join(res.get('text_answers', []))
                                                    if current_chapter_name not in self.session_history:
                                                        self.session_history[current_chapter_name] = []
                                                    self.session_history[current_chapter_name].append({
                                                        'index': i,
                                                        'hash_id': res.get('hash_id'),
                                                        'image': res.get('image'),
                                                        'answer_key': ans_key_disp,
                                                        'snippet': snippets_map.get(i, f'第 {i + 1} 题'),
                                                        'question_type': res.get('question_type', 'choice'),
                                                        'original_answer': ans_key_disp,
                                                    })
                                    if self.check_pause_and_stop():
                                        inner_loop_broken_for_restart = True
                                        break

                                    # 填涂
                                    self.log('      [填涂] 正在自动填入答案...')
                                    clicked_ok = 0
                                    for idx, result in results_map.items():
                                        if self.stop_requested:
                                            break
                                        try:
                                            q = questions[idx]
                                            if fill_and_click_smart(q, result):
                                                clicked_ok += 1
                                            self.update_progress_quiz(f'已填涂 {clicked_ok}/{total_q}')
                                            time.sleep(0.1)
                                        except Exception:
                                            pass
                                    self.log(f'      [完成] 本卡片填涂完毕，成功填入: {clicked_ok}/{total_q} 题！')
                                    self.update_progress_quiz(f'已填涂 {clicked_ok}/{total_q} 题')

                                    if self.check_pause_and_stop():
                                        inner_loop_broken_for_restart = True
                                        break

                                    if clicked_ok > 0:
                                        if clicked_ok == total_q:
                                            self.log(f'      [结算] 完美通刷本章！正在向服务器申请 '
                                                     f'{clicked_ok} 道题的合并核销...')
                                        else:
                                            missing_count = total_q - clicked_ok
                                            self.log(f'      [警告] ⚠️ 本页共有 {total_q} 题，因超时或网络故障漏做 '
                                                     f'{missing_count} 题！系统已自动按实际填涂的 {clicked_ok} 题'
                                                     f'提交结算（扣除 {clicked_ok} 点）。')
                                            self.log(f'      [结算] 请稍后手动返回本页补做漏掉的 {missing_count} '
                                                     f'道题目。已为您自动保存并翻页继续，实现无人值守挂机。')
                                        if not self.deduct_checkout(clicked_ok):
                                            self.log('      [拦截] 结算核销未完成（余额不足），已成功强行拦截后续暂存与自动翻页。')
                                            inner_loop_broken_for_restart = True
                                            break

                                    should_submit = (clicked_ok == total_q) and bool(self.var_auto_submit.get())
                                    if should_submit:
                                        self.log('      [提交] 本章题目 100% 完美解答，开始执行[自动提交]流程...')
                                        submit_btn, submit_frame = find_submit_button(target_page)
                                        if submit_btn:
                                            try:
                                                submit_btn.scroll_into_view_if_needed()
                                                time.sleep(0.3)
                                                submit_btn.click(True, force=True)
                                                self.log('         [提交] 已触发提交按钮，正在等待二次确认弹窗...')
                                                time.sleep(0.8)
                                                confirm_btn, confirm_frame = None, None
                                                selectors_confirm = ['#popok', 'a#popok', '.jb_btn_92',
                                                                     "a:has-text('确定')", "button:has-text('确定')"]
                                                frames_to_scan_conf = [target_page.main_frame] + target_page.frames
                                                for f_conf in frames_to_scan_conf:
                                                    try:
                                                        for sel_conf in selectors_confirm:
                                                            loc_conf = f_conf.locator(sel_conf)
                                                            if loc_conf.count() > 0:
                                                                el_conf = loc_conf.first
                                                                if el_conf.is_visible() and el_conf.is_enabled():
                                                                    confirm_btn = el_conf
                                                                    confirm_frame = f_conf
                                                                    break
                                                        if confirm_btn:
                                                            break
                                                    except Exception:
                                                        pass
                                                if confirm_btn:
                                                    confirm_btn.scroll_into_view_if_needed()
                                                    confirm_btn.click(True, force=True)
                                                    self.log('         [提交] 二次确认完成，任务点顺利点绿！')
                                                else:
                                                    self.log('         [提示] 未检测到 HTML 确认弹窗（可能已被浏览器 Alert 自动放行）。')
                                                for _ in range(15):
                                                    if self.stop_requested:
                                                        break
                                                    time.sleep(0.2)
                                            except Exception as ex:
                                                self.log(f'         [警告] 自动提交失败: {ex}，正在自动降级为暂存保底...')
                                                should_submit = False
                                        else:
                                            self.log('         [警告] 未检测到页面上的提交按钮，自动降级为暂存保底...')
                                            should_submit = False
                                    if not should_submit:
                                        if clicked_ok < total_q:
                                            self.log(f'      [暂存] 检测到漏题（漏做 {total_q - clicked_ok} 题），'
                                                     f'强制执行[自动暂存]以保持任务点为黄色...')
                                        else:
                                            self.log('      [暂存] 正在执行[自动暂存]流程...')
                                        save_btn, save_frame = find_save_button(target_page)
                                        if save_btn:
                                            try:
                                                save_btn.scroll_into_view_if_needed()
                                                time.sleep(0.3)
                                                save_btn.click(True, force=True)
                                                self.log('         [存档] 暂存成功！答案已留存，任务点将保持黄色状态。')
                                                for _ in range(15):
                                                    if self.stop_requested:
                                                        break
                                                    time.sleep(0.2)
                                            except Exception as ex:
                                                self.log(f'         [警告] 点击暂存按钮失败: {ex}')
                                        else:
                                            self.log('         [系统] 未检测到暂存按钮，跳过暂存。')
                            else:
                                self.log('      [系统] 当前卡片无待作答文字题。')
                                self.update_task_perception(None, None, 0)
                                self.update_progress_quiz('无题目')

                            if self.check_pause_and_stop():
                                inner_loop_broken_for_restart = True
                                break

                            # ---- 音视频 / 文档任务 ----
                            self.log('      [扫描] 正在检索当前视图下的音视频与文档任务...')
                            active_cards_frame = cards_frame if cards_frame else target_page.main_frame
                            all_placeholders = active_cards_frame.locator('div.ans-attach-ct').all()
                            valid_jobs = []
                            for p in all_placeholders:
                                try:
                                    if not p.is_visible():
                                        continue
                                    box = p.bounding_box()
                                    if not box or box['height'] < 10 or box['width'] < 10:
                                        continue
                                    inner_html = p.inner_html().lower()
                                    if any(kw in inner_html for kw in
                                           ('video', 'audio', 'fastforward', 'insertvideo',
                                            'pdf', 'ppt', 'doc', 'preview')):
                                        valid_jobs.append(p)
                                except Exception:
                                    pass
                            if not valid_jobs:
                                self.log('      [系统] 当前卡片无音视频 or 文档阅读任务。')
                                self.update_task_perception(0, 0, None)
                            else:
                                v_count, d_count = 0, 0
                                for p in valid_jobs:
                                    try:
                                        html = p.inner_html().lower()
                                        if any(kw in html for kw in
                                               ('video', 'audio', 'fastforward', 'insertvideo')):
                                            v_count += 1
                                        elif any(kw in html for kw in ('pdf', 'ppt', 'doc', 'preview')):
                                            d_count += 1
                                    except Exception:
                                        pass
                                self.update_task_perception(v_count, d_count, None)
                                self.log(f'      [系统] 检测到 {len(valid_jobs)} 个多媒体/阅读任务点，开始监控...')
                                for task_idx, target_container in enumerate(valid_jobs):
                                    if self.check_pause_and_stop():
                                        break
                                    self.log(f'\n         [对焦] 正在将目标任务拉入视口 {task_idx + 1}/'
                                             f'{len(valid_jobs)} ...')
                                    try:
                                        target_container.scroll_into_view_if_needed()
                                        time.sleep(0.5)
                                    except Exception:
                                        pass
                                    task_sig = self.generate_task_signature(
                                        page_counter, tab_idx, task_idx, target_container)
                                    is_already_done = (task_sig in completed_video_urls
                                                       or task_sig in completed_doc_urls)
                                    if is_already_done:
                                        self.log('         [跳过] 此任务在先前影子缓存验证中已被标记完成。')
                                    else:
                                        try:
                                            is_finished = target_container.evaluate(
                                                '(container) => { return container.classList.contains("ans-job-finished") || '
                                                '/ans-job-finished|icon_Completed|jobFinish|job-finished/.test(container.className || \'\'); }')
                                            if is_finished:
                                                is_already_done = True
                                                self.log('         [跳过] 平台官方已标记此任务完成。')
                                        except Exception:
                                            pass
                                    if is_already_done:
                                        completed_video_urls.add(task_sig)
                                        completed_doc_urls.add(task_sig)
                                        self.update_progress_video('已完成')
                                        continue

                                    task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        self.log('         [等待] 任务容器正在初始化，等待2秒...')
                                        time.sleep(2.0)
                                        task_frame = self.traverse_to_leaf_frame(target_container)
                                    if not task_frame:
                                        self.log('         [警告] 容器穿透检测失败，跳过该任务。')
                                        continue
                                    try:
                                        has_video = False
                                        has_doc = False
                                        for _ in range(25):
                                            has_video = (task_frame.locator('video, audio').count() > 0
                                                         or any(kw in (task_frame.url or '').lower()
                                                                for kw in ('/video/', '/audio/')))
                                            has_doc = (task_frame.locator('#panView, #container, #scrollBox').count() > 0
                                                       or any(kw in (task_frame.url or '').lower()
                                                              for kw in ('/pdf/', '/ppt/', '/doc/', '/pub/preview')))
                                            if has_video or has_doc:
                                                break
                                            time.sleep(0.2)
                                        if has_video:
                                            self.log('         [视频] 发现音视频任务，已自动开启倍速与静音播放...')
                                            self.run_video_task(target_page, task_frame, target_container,
                                                                task_sig, completed_video_urls)
                                        elif has_doc:
                                            self.log('         [文档] 发现文档任务，已自动开启步进滚动阅读...')
                                            self.run_doc_task(target_page, task_frame, target_container,
                                                              task_sig, completed_doc_urls)
                                        else:
                                            self.log('         [系统] 容器中未发现视频 or 文档，强制标记已读。')
                                            self.update_progress_video('已完成')
                                            try:
                                                target_container.evaluate(
                                                    "(container) => container.classList.add('ans-job-finished')")
                                            except Exception:
                                                pass
                                        self.log('         [等待] 执行完成，等待平台状态同步中...')
                                        time.sleep(1.5)
                                    except Exception as eval_ex:
                                        self.log(f'         [警告] 节点异常: {eval_ex}')
                                        try:
                                            target_container.evaluate(
                                                "(container) => container.classList.add('ans-job-finished')")
                                        except Exception:
                                            pass
                                        time.sleep(1.0)

                            if self.check_pause_and_stop():
                                inner_loop_broken_for_restart = True
                                break

                        if inner_loop_broken_for_restart:
                            break
                        if self.check_pause_and_stop():
                            self.log('[系统] 任务因用户请求退出。')
                            break

                        # ---- 翻页 ----
                        self.log('[导航] 正在检索下一页/下一章按钮...')
                        next_btn, next_frame = find_next_button(target_page)
                        if next_btn:
                            try:
                                next_btn.scroll_into_view_if_needed()
                                time.sleep(0.5)
                                next_btn.click(True, force=True)
                                self.log('[导航] 导航跳转成功，正在检测拦截强行确认提示...')
                                dialog_handled = False
                                for _ in range(5):
                                    if self.check_pause_and_stop():
                                        break
                                    time.sleep(0.2)
                                    bypass_btn, bypass_frame = find_confirmation_bypass_button(target_page)
                                    if bypass_btn:
                                        self.log('[系统] 拦截到未完成任务提示弹窗，已自动强制执行强行跳转授权。')
                                        bypass_btn.click(True, force=True)
                                        dialog_handled = True
                                        break
                                self.log('[导航] 翻页指令执行完成，正在等待页面载入...')
                                time.sleep(0.8)
                                if self.stop_requested:
                                    self.log('[系统] 自动化任务因用户请求而提前退出。')
                                    break
                                page_counter += 1
                                if page_counter % 10 == 0:
                                    self.log('[系统] 🌟 已连刷 10 个页面，正在执行标签页无感重建与内存释放...')
                                    saved_url = target_page.url
                                    new_page = context.new_page()
                                    new_page.on('dialog', lambda dialog: dialog.accept())
                                    target_page.close()
                                    new_page.goto(saved_url)
                                    target_page = new_page
                                    self.log('[系统] 🌟 标签页内存垃圾清理完毕，已恢复挂机...')
                                completed_video_urls.clear()
                                completed_doc_urls.clear()
                                self.log('[系统] 已自动清理上一页影子缓存。')
                            except Exception as ex:
                                self.log(f'   [警告] 导航跳转出现阻碍: {ex}')
                                break
                        else:
                            self.log('[系统] 未探测到导航页按钮，自动化作业流程结束。')
                            break

                # 收尾
                for ch_name in self.session_history:
                    self.session_history[ch_name].sort(key=lambda x: x['index'])
                self.log(f'\n[系统] 自动化主流程运行完毕，共处理页面数: {page_counter}')
                self.log(f'总计耗时: {time.time() - start_time:.2f}s')
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f'[错误] 流程因异常而意外中断: {e}')
        self.root.after(0, self.reset_control_buttons)
        return None
# ---------------- 入口 ----------------

if __name__ == '__main__':
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
