# -*- coding: utf-8 -*-
"""
learn_helper 后端服务包（1.0.4 起，无任何 Tk 依赖）。

模块职责：
- config.py   配置读写（config.json，兼容旧版 Tk 客户端 schema）+ 版本常量
- core.py     刷课/答题的 UI 无关核心（注入 JS、识别、求解、填涂、浏览器控制）
- engine.py   SolverEngine：状态机 + 专用自动化线程（Playwright sync API 线程铁律）
- ipc.py      HTTP 请求-响应服务 + 命名管道推送服务（事件广播）
- runtime.py  入口装配：随机端口、单实例锁、stdout 握手、stdin 父死子亡、优雅退出
"""

__all__ = ['APP_VERSION', 'setup_logger', 'LOGGER', 'LOG_DIR']

from .config import APP_VERSION, SCHOOL_ID, setup_logger, LOGGER, LOG_DIR  # noqa: E402,F401
