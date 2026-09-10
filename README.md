# learn-helper

学习助理 —— 网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。

## 版本
- **1.0.1**（当前）：纯刷课 —— 只做「视频 + 文档」自动化。
- 1.0.2（计划）：接入自研 AI API 的答题功能。

## 文件
- `learn_helper.py` —— 纯刷课客户端（Tkinter + Playwright/CDP 9222）
- `client_app_reconstructed.py` —— 原软件的反编译重建（研究参考，见 `docs/技术文档.md`）
- `docs/技术文档.md` —— 反编译技术文档

## 工作原理
- 通过 `--remote-debugging-port=9222` 连接本机 Edge/Chrome，用 Playwright 操控。
- 控制逻辑（点击 / 翻页 / 刷视频 / 滚文档 / 防弹窗 / 每 10 页重建）**全部本地实现**；
  注入页面的脚本（倍速、换线路、滚动）为**本地常量**，不由服务器下发。
- 仅 3 个远端调用点，可指向自建后端。

## 运行
1. Python 3.10；依赖：`pip install playwright requests`
   （只连接本机已安装的浏览器，无需 `playwright install` 下载内核）。
2. 配置后端地址（可选，默认 `http://127.0.0.1:8000`）——环境变量 `LH_SERVER_URL`，
   或同目录 `config.json`：
   ```json
   { "server_url": "http://127.0.0.1:8000" }
   ```
3. `python learn_helper.py`
4. 在弹出的沙盒浏览器里登录平台并打开学习页 → 面板点「检测/刷新网页」选择网页 → 「启动刷课」。

## 远端接口（供自建后端）
| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/points` | 查点数余额 |
| GET | `/check_version` | 公告 / 强制更新 |
| POST | `/video_heartbeat` | 看课心跳（累计播放满 600 秒触发一次） |

## 排查 / 日志
- 运行日志：`logs/learn_helper.log`（滚动，UTF-8），界面日志同步镜像。
- 诊断当前页面（列出所有 frame、关键元素计数、任务容器片段）：
  - 命令行：`python learn_helper.py --diagnose`
  - 界面：点「诊断页面」按钮
- 刷课时若识别不到任务容器，会自动追加一次页面诊断到日志，便于定位平台改版。

## 合规说明
仅供自有 / 授权环境的技术研究与个人学习使用，请遵守所在平台的使用条款。
