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
import threading
from logging.handlers import RotatingFileHandler

APP_VERSION = '2.1.4'
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
    # 「只刷视频」：章节列表里那些**只有题目、没有任何视频/文档任务点**的章节
    # （测验/作业类任务点）整个跳过，不答题也不提交。默认关。
    # ⚠️ 只跳"纯测验"章节：视频/文档任务点里的题目照做，不受影响。
    'skip_quiz_only': False,
}


def setup_logger():
    # ⚠️ 日志目录不可写（冻结后装在 Program Files、只读共享、ACL 拒绝）时，
    # 原来的 `makedirs` / `RotatingFileHandler` 会**在 import 期抛出**，
    # 于是 `main.py` 连握手行都打不出来、界面永远连不上。这里降级为"只不写文件"。
    lg = logging.getLogger('learn_helper')
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, 'learn_helper.log')
            fh = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5,
                                     encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                                              '%Y-%m-%d %H:%M:%S'))
            lg.addHandler(fh)
        except Exception as e:
            lg.addHandler(logging.NullHandler())
            lg.warning(f'日志文件不可用（{e}），本次运行不写日志文件。')
    return lg


LOGGER = setup_logger()


# ----------------------------------------------------------------------------
# config.json 读写（合并式写入；绝不整段覆盖，见 ERROR.md E5）
# ----------------------------------------------------------------------------
# ⚠️ 两个**已实测**的坑（2026-09-19，本轮修复，详见 ERROR.md E60）：
#   ① 旧写法是 `open(CONFIG_PATH,'w')` 直接截断后 dump —— 中途崩溃/被杀就留下
#      半截文件，而 `load_config()` 一律吞异常返回 `{}` ⇒ **用户的 api_key /
#      last_page_url 等全部静默丢失**；
#   ② `update_config` 是"读-改-写"且**没有锁**：界面线程（PUT /api/settings）与
#      引擎线程（set_video_speed / remember_page）会并发进来。实测 8 线程 × 40 次
#      写入后**所有键全部丢失、文件甚至不再是合法 JSON**（读到别人截断后的空文件
#      ⇒ 把 `{}` 当成现状写回去）。
# 修法：写盘一律"临时文件 + os.replace"（同目录内原子替换），读-改-写全程持锁。
_CONFIG_LOCK = threading.RLock()


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_config_state():
    """返回 `(cfg, unreadable)`。

    ⚠️ **不能把"文件不存在"和"文件读不出来"混为一谈**（2026-09-19）：
    两者原来都返回 `{}`，于是"带 BOM 的 / 上次崩溃留下的半截 config.json"
    会被下一次 `update_config` 当成"当前配置就是空的"**整段写回**，
    用户的 `llm.api_key`、`server_url`、`last_page_url` 就这么没了。
    这里把"文件确实存在、但解析失败"单独标出来，让写入方**拒绝覆盖**。
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else {}), False
    except FileNotFoundError:
        return {}, False
    except Exception as e:
        LOGGER.error(f'config.json 读取失败（保留原文件，不覆盖）: {e}')
        return {}, True


def _quarantine_broken_config():
    """把"存在但读不出来"的 config.json 挪成 `config.json.bad`。

    ⚠️ 为什么不是"拒绝写入"（2026-09-19 想清楚了）：拒绝写入会让**设置永远存不上**
    （磁盘上那份坏文件一直在，用户每次保存都失败），比"覆盖"更糟。
    正确做法是**保住现场**（改名而不是删除，用户可以自己把里面的键抄回来），
    然后用默认值 + 本次改动重建一份可用的配置。
    """
    bad_path = CONFIG_PATH + '.bad'
    try:
        os.replace(CONFIG_PATH, bad_path)
        LOGGER.error(f'config.json 无法解析，已备份为 {bad_path} 并重建。')
        return True
    except Exception as e:
        LOGGER.error(f'config.json 无法解析且备份失败（{e}），本次不写入以免造成更大破坏。')
        return False


def save_config(cfg):
    """原子写盘：先写同目录临时文件，再 `os.replace` 覆盖 —— 任何一步失败都不会
    弄坏已有的 config.json（要么是旧内容，要么是新内容，不会出现半截）。"""
    tmp_path = CONFIG_PATH + '.tmp'
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        return True
    except Exception as e:
        LOGGER.error(f'保存 config.json 失败: {e}')
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def update_config(patch):
    """**合并式**写入：只覆盖 patch 里给出的键，其余段落原样保留。

    1.0.2 的缺陷是 save_config 直接整段覆盖，会把用户填过的 server_url 抹掉；
    这里对 dict 值做一层合并，非 dict 值直接替换。

    ⚠️ 全程持 `_CONFIG_LOCK`：这样"读 → 合并 → 写"是一个整体，两个线程同时改
    **不同**的键也不会互相丢（各写各的键，最后一次写入包含前一次的合并结果）。

    ⚠️ 现有 config.json **存在但读不出来**时：先把它备份成 `config.json.bad`
    （保住现场，用户还能手动抄回 api_key 等键），再从默认值重建一份可用的配置。
    这样"保存"始终能成功，而不是永远失败。
    """
    with _CONFIG_LOCK:
        cfg, unreadable = _load_config_state()
        if unreadable and not _quarantine_broken_config():
            return False
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                merged = dict(cfg[k])
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v
        return save_config(cfg)


def _as_bool(value, default):
    """把配置里读到的值当**布尔**解释。

    ⚠️ 不能直接用 `bool(v)`：`bool("false")` 和 `bool("0")` 都是 **True**
    （2026-09-19 实测）—— 手改过的 config.json 或别的客户端发来的
    `{"auto_submit": "false"}` 会被理解成"开启自动提交"，与用户意图相反。
    这里只认真正的布尔与常见字面量，其余一律回落默认值。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('true', '1', 'yes', 'on', '开', '是'):
            return True
        if v in ('false', '0', 'no', 'off', '关', '否', ''):
            return False
    return default


def _as_section(raw):
    """配置里的"段"必须是 dict；否则当空段处理（不抛异常）。"""
    return raw if isinstance(raw, dict) else {}


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
    # ⚠️ `or {}` 只兜得住"空值"，兜不住 `{"llm": "x"}` 这种段被写成字符串的情况
    # （config.json 与旧 Tk 客户端共用、用户也可能手改）⇒ 用 `_as_section` 统一收口。
    cfg = _as_section(load_config().get('llm'))
    return {
        'base_url': cfg.get('base_url') or DEFAULT_LLM['base_url'],
        'api_key': cfg.get('api_key') or '',
        'model': cfg.get('model') or DEFAULT_LLM['model'],
    }


def get_answer_cfg():
    """读取答题配置（缺项补默认 + 钳位）。"""
    cfg = _as_section(load_config().get('answer'))
    out = dict(DEFAULT_ANSWER)
    for k, default in DEFAULT_ANSWER.items():
        v = cfg.get(k, default)
        if isinstance(default, bool):
            out[k] = _as_bool(v, default)
        elif isinstance(default, int):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = default
        else:
            out[k] = v if isinstance(v, str) and v else default
    # `mode` 必须是可哈希的字符串：手改成 `["a"]` 会让 `in dict` 抛
    # `TypeError: unhashable type: 'list'`（实测），整个 /api/settings 变 500。
    if not isinstance(out['mode'], str) or out['mode'] not in ANSWER_MODE_LABELS:
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
    """运行期设置（倍速 / 自动提交 / 启动自动开浏览器 / 只刷视频），带钳位。"""
    cfg = _as_section(load_config().get('run'))
    out = dict(DEFAULT_RUN)
    try:
        out['video_speed'] = float(cfg.get('video_speed', DEFAULT_RUN['video_speed']))
    except (TypeError, ValueError):
        out['video_speed'] = DEFAULT_RUN['video_speed']
    out['video_speed'] = max(1.0, min(4.0, out['video_speed']))
    # 三个开关都走 `_as_bool`：`bool("false")` 是 True，不能直接用（实测）。
    out['auto_submit'] = _as_bool(cfg.get('auto_submit'), DEFAULT_RUN['auto_submit'])
    out['auto_launch_browser'] = _as_bool(cfg.get('auto_launch_browser'),
                                          DEFAULT_RUN['auto_launch_browser'])
    out['skip_quiz_only'] = _as_bool(cfg.get('skip_quiz_only'),
                                     DEFAULT_RUN['skip_quiz_only'])
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
