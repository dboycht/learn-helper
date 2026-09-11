# learn-helper

学习助理 —— 网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。

## 版本
- **1.0.2**（当前）：刷课（视频/文档）+ 答题。答题默认接入**内部答题 API**
  （自建后端 `POST /solve`，客户端零配置）；也可在「答题设置」里切换为
  **自配大模型**（OpenAI 兼容视觉模型）或**仅识别不答题**。
- **不含任何计费 / 卡密 / 点数逻辑**：没有余额、充值、商城、扣点与卡密输入，
  客户端只发送题目截图与题干；后端地址、模型等由使用者自己配置。
- 1.0.1：纯刷课；账户静态显示（admin / 9999）。

## 文件
- `learn_helper.py` —— 刷课 + 答题客户端（Tkinter + Playwright/CDP 9222）
- `client_app_reconstructed.py` —— 原软件的反编译重建（研究参考，见 `docs/技术文档.md`）
- `docs/技术文档.md` —— 反编译技术文档（**描述原始 exe**，含其原始接口契约）

## 工作原理
- 通过 `--remote-debugging-port=9222` 连接本机 Edge/Chrome，用 Playwright 操控。
- 控制逻辑（点击 / 翻页 / 刷视频 / 滚文档 / 防弹窗 / 每 10 页重建）**全部本地实现**；
  注入页面的脚本（倍速、换线路、滚动）为**本地常量**，不由服务器下发。
- 远端调用点只有两个，都指向你自己配置的后端：`/check_version`、`/solve`。

## 答题方式（面板「答题设置」→「答题方式」）
| 方式 | 说明 |
| --- | --- |
| **内部答题 API**（默认） | 截图 + 题干发给自建后端 `POST /solve`，答案回填页面 |
| 自配大模型 | 直连你自己的 OpenAI 兼容视觉模型接口（Base URL / API Key / 模型名） |
| 仅识别不答题 | 只把题型/题干写进日志，便于排查平台改版 |

- 一批题目的处理顺序：**识别（截图/题型/题干）→ 并发求解 → 填涂 → 提交/暂存**。
- 并发数、单题超时、失败重试都在「答题设置 → 内部答题 API」里配；求解用守护线程并发，
  退出时会立刻放弃在途请求，不会因为一题卡住而拖住程序。

## 运行
1. Python 3.10；依赖：`pip install playwright requests`
   （只连接本机已安装的浏览器，无需 `playwright install` 下载内核）。
2. 配置后端地址（可选，默认 `http://127.0.0.1:8000`）——环境变量 `LH_SERVER_URL`，
   或同目录 `config.json`：
   ```json
   {
     "server_url": "http://127.0.0.1:8000",
     "answer": { "mode": "server", "solver_timeout": 240, "workers": 4, "retry": 2 },
     "llm": { "base_url": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-4o" }
   }
   ```
   （以上都可以在面板「答题设置」里填写并自动保存，无需手改 `config.json`。）
3. `python learn_helper.py`
4. 在弹出的沙盒浏览器里登录平台并打开学习页 → 面板点「检测/刷新网页」选择网页 → 「启动刷课」。
5. 首次做题前，先点「答题设置」→「内部答题 API」填好**后端地址**，点「测试连接」验证
   （会请求 `/check_version` 并回显后端公告）；提交方式可选「自动提交」/「仅暂存」。

## 退出行为
- 关闭窗口（X / Alt+F4）或点「**终止并退出**」走同一条出口。
- 只有在刷课流程运行中才会弹一次确认；空闲时直接退出，不打扰。
- 退出流程：停自动化 → 放弃在途答题请求 → 关闭本程序拉起的**沙盒浏览器**（独立 profile，
  不影响你正常的 Edge/Chrome）→ 刷盘日志 → 退出进程。

## 远端接口（供自建后端）
| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/check_version` | 公告 / 强制更新（「测试连接」也用它探活） |
| POST | `/solve` | 内部答题模型：题目截图 + 题干求解 |

`/solve` 请求体：

```json
{ "image": "<base64>", "question_type": "choice|multi_choice|blank|essay",
  "num_blanks": 4, "text_hash_source": "<题干文本>",
  "school_id": "nuaa", "device_id": "DEV-XXXXXXXXXXXX" }
```

响应（200）：

```json
{ "hash_id": "…", "answer_key": "AC", "text_answers": [], "cached": false }
```

- `answer_key` 用于选择/多选题（A–F 字母组合）；`text_answers` 用于填空/简答。
- 非 200 请返回 `{"detail": "错误说明"}`，客户端会记录并重试（网络抖动与 5xx 才重试）。
- 注：原始 exe 的接口还含 `card_key` / `remaining_points` / `/points` / `/solve/checkout` /
  `/video_heartbeat` 等计费字段，**本项目已全部移除**；原始契约见 `docs/技术文档.md` §5。

## 排查 / 日志
- 运行日志：`logs/learn_helper.log`（滚动，UTF-8），界面日志同步镜像。
- 诊断当前页面（列出所有 frame、关键元素计数、任务容器片段）：
  - 命令行：`python learn_helper.py --diagnose`
  - 界面：点「诊断页面」按钮
- 刷课时若识别不到任务容器，会自动追加一次页面诊断到日志，便于定位平台改版。
- 答题不生效时依次检查：答题方式是否为「内部答题 API」→「测试连接」是否通过 →
  日志里每题的类型/数量/答案是否正常。

## 合规说明
仅供自有 / 授权环境的技术研究与个人学习使用，请遵守所在平台的使用条款。
