# -*- coding: utf-8 -*-
"""配置与日志：单一来源 = 项目根的 config.json（与旧 Tk 客户端共用同一份，schema 兼容）。

路径规则（两种运行形态都要能跑）：
- 源码运行：``backend/learn_helper/config.py`` → 上溯两级 = 项目根 ``learn-helper/``
- PyInstaller 冻结：``sys.frozen`` 为真 → 取 ``sys.executable`` 所在目录
  （发布形态里 exe 与 config.json / logs / browser_profile 同目录）
"""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

APP_VERSION = '2.1.2'
SCHOOL_ID = 'nuaa'

# 本模块不依赖 requests / playwright，保证「装没装浏览器依赖」都能起服务：
# 缺失时由 core.py 在真正要自动化时报出可读错误（见 core.require_playwright）。


def _base_dir():
    # 测试/排障用覆盖（自测脚本要在临时目录里跑，绝不碰用户真实 config.json）
    override = os.environ.get('LH_BASE_DIR')
    if override:
        return os.path.abspath(override)
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


BASE_DIR = _base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

DEFAULT_LLM = {
    'base_url': 'https://api.openai.com/v1',
    'api_key': '',
    'model': 'gpt-4o',
}

DEFAULT_ANSWER = {
    'mode': 'server',
    'solver_timeout': 240,   # /solve 单题超时（原程序 150~300s）
    'workers': 4,            # 并发求解线程数
    'retry': 2,              # 单题失败重试次数
}

DEFAULT_SERVER_URL = 'http://127.0.0.1:8000'

# 运行期设置（与旧 Tk 客户端的界面控件一一对应）
DEFAULT_RUN = {
    'video_speed': 2.0,      # 倍速
    'auto_submit': True,     # True=自动提交 / False=仅暂存
    # 启动时自动拉起沙盒浏览器（老 Tk 版 `auto_launch_browser_on_start` 的行为）。
    # 默认开：用户打开界面就能用上次的学习页，不必先点「检测/刷新网页」。
    'auto_launch_browser': True,
}


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger('learn_helper')
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        path = os.path.join(LOG_DIR, 'learn_helper.log')
        fh = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                                          '%Y-%m-%d %H:%M:%S'))
        lg.addHandler(fh)
    return lg


LOGGER = setup_logger()


# ----------------------------------------------------------------------------
# config.json 读写（合并式写入；绝不整段覆盖，见 ERROR.md E5）
# ----------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        LOGGER.error(f'保存 config.json 失败: {e}')
        return False


def update_config(patch):
    """**合并式**写入：只覆盖 patch 里给出的键，其余段落原样保留。

    1.0.2 的缺陷是 save_config 直接整段覆盖，会把用户填过的 server_url 抹掉；
    这里对 dict 值做一层合并，非 dict 值直接替换。
    """
    cfg = load_config()
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(cfg[k])
            merged.update(v)
            cfg[k] = merged
        else:
            cfg[k] = v
    return save_config(cfg)


def get_raw_server_url():
    """优先级：config.json 的 server_url > 环境变量 LH_SERVER_URL > 默认。"""
    url = load_config().get('server_url')
    if url:
        return str(url).rstrip('/')
    return os.environ.get('LH_SERVER_URL', DEFAULT_SERVER_URL).rstrip('/')


def effective_server_url():
    """运行期统一入口：每次调用都重读配置，改完地址即时生效，无需重启。"""
    return get_raw_server_url()


def get_llm_cfg():
    cfg = load_config().get('llm', {}) or {}
    return {
        'base_url': cfg.get('base_url') or DEFAULT_LLM['base_url'],
        'api_key': cfg.get('api_key') or '',
        'model': cfg.get('model') or DEFAULT_LLM['model'],
    }


def get_answer_cfg():
    """读取答题配置（缺项补默认 + 钳位）。"""
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


def get_run_cfg():
    """运行期设置（倍速 / 自动提交 / 启动自动开浏览器），带钳位。"""
    cfg = load_config().get('run', {}) or {}
    out = dict(DEFAULT_RUN)
    try:
        out['video_speed'] = float(cfg.get('video_speed', DEFAULT_RUN['video_speed']))
    except (TypeError, ValueError):
        out['video_speed'] = DEFAULT_RUN['video_speed']
    out['video_speed'] = max(1.0, min(4.0, out['video_speed']))
    out['auto_submit'] = bool(cfg.get('auto_submit', DEFAULT_RUN['auto_submit']))
    out['auto_launch_browser'] = bool(
        cfg.get('auto_launch_browser', DEFAULT_RUN['auto_launch_browser']))
    return out


# ----------------------------------------------------------------------------
# 设备指纹（供后端限设备）
# ----------------------------------------------------------------------------
def get_device_id():
    try:
        import hashlib
        import uuid as machine_uuid
        return 'DEV-' + hashlib.md5(str(machine_uuid.getnode()).encode()).hexdigest()[:12].upper()
    except Exception:
        return 'DEV-UNKNOWN'


DEVICE_ID = get_device_id()
