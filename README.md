# learn-helper

学习助理 —— 网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。

## 版本

- **2.1.2**（开发中，未发版）：
  - **「当前网页」可下拉选择** —— 点网页框弹出标签页列表
    （当前页打 ✓、↑↓/Enter/Esc、滚轮翻页），选中即下发后端并**记住**（下次启动仍选中同一页）；
    选中前网页框会提示"已检测到 N 个网页 —— 点此处选择"。
  - **运行日志自带可视滚动条**：可拖拽滑块、点轨道翻页（上/下各一屏），
    拖到底自动恢复"跟随最新日志"；鼠标停在滚动条上时滚轮归滚动条（不抢），
    日志不满一屏时不占位。
  - **启动时自动打开沙盒浏览器**（恢复老版本行为）：界面起来几秒后自动拉起
    独立配置的 Edge/Chrome（`browser_profile/`，并恢复上次的学习页），
    不用再先点一次「检测/刷新网页」。可在「答题设置」里关掉（`run.auto_launch_browser`，默认开）。
- **2.1.1**（已发布，源码包）：**界面重构为原生 Win32（Rust）+ Python 后端服务**，主打「好传播」。
  - **前端**：`native/` —— Rust 手写 Win32/GDI/DWM，**单 exe 约 0.37 MB，零运行时依赖**，
    解压即用；**纯色自绘界面**（用系统标题栏 + Win11 圆角 + 深色标题栏，
    客户区不透明自绘 —— 不做玻璃背板），保留原生缩放 / 贴边。
  - **后端**：`backend/` —— Python 服务（Playwright 刷课 + 答题），
    经 **HTTP 请求-响应 + 命名管道推送** 与前端通信。
  - **界面内「答题设置」对话框**：答题方式 / 后端地址 / 并发 / 超时 / 重试 /
    大模型参数 / 自动提交，全部可视化编辑，保存走 `PUT /api/settings`（合并式写入）。
  - 前作 **1.0.3 的 WinUI 3 界面保留在 `winui/` 作为备选退路**（不再默认启动）。
- **1.0.2**：刷课（视频/文档）+ 答题。答题默认接入**内部答题 API**（自建后端 `POST /solve`，客户端零配置）；
  也可切换为**自配大模型**（OpenAI 兼容视觉模型）或**仅识别不答题**。
- **1.0.1**：纯刷课。
- 全程**不含任何计费 / 卡密 / 点数逻辑**：没有余额、充值、商城、扣点与卡密输入，
  客户端只发送题目截图与题干；后端地址、模型等由使用者自己配置。

## 2.1.1 更新内容（原生 Win32 界面 + 后端服务）

> ⚠️ **本版本只发源码**（Release 附件 = 源码 zip）。仓库里**不含**编译好的 exe；
> 界面用 `cargo build --release` 自己构建（见下方「构建与运行」），后端直接跑 Python 源码。
> 需要 PyInstaller 打整包（UI + 后端单 exe）时用 `native\build_release.ps1 -Zip`，产物在本地 `dist\`。

### 分发（本次重构的直接动机）

| | 体积 | 目标机需要装什么 |
| --- | --- | --- |
| 1.0.3（WinUI 3，框架依赖） | 26.1 MB | **.NET 9 Desktop Runtime + Windows App SDK Runtime** |
| 1.0.3（WinUI 3，自包含） | 85.7 MB | 无 |
| **2.1.1（原生 UI 单 exe）** | **0.37 MB** | **什么都不用装** |

> 后端仍然需要本机有 Python + playwright（或者用 `build_release.ps1` 把它打成 exe 一起分发）；
> 打整包时体积的大头（约 51 MB）几乎全是 Playwright 自带的浏览器运行时，UI 只占 0.7%。

### 新增

- **原生 Win32 前端**（`native/`，Rust）：自绘标题栏（含拖拽 / 八方向缩放 / 双击最大化）
  + 完全自绘的纯色界面，Win11 圆角、深色标题栏、PerMonitorV2 DPI 感知；
  界面空闲时**零重绘**（状态指纹门控 + 双缓冲）。
- **Python 后端服务**（`backend/learn_helper/`）：把原先 Tkinter 客户端里的
  刷课 / 答题逻辑迁出（**去掉全部 UI**），成为独立服务：
  - **HTTP**（`127.0.0.1:<随机端口>`）：`/api/health`、`/api/status`、`/api/pages`、
    `/api/settings`（GET/PUT）、`/api/control`（启动 / 暂停 / 停止 / 刷新网页 / 选择网页 /
    诊断 / 测试后端 / 自检 / 退出）、`/api/logs`；
  - **命名管道**：后端单向推送日志 / 进度 / 状态，界面实时显示；
  - 端口由系统随机分配，前端通过后端 stdout 的**握手行**获知，不猜端口。
- **「答题设置」对话框**（界面标题栏齿轮图标）：答题方式三选一、后端地址、并发线程、
  单题超时、失败重试、大模型 Base URL / API Key / 模型名、自动提交开关；
  「测试连接」按钮可当场验证后端地址。**API Key 留空 = 不修改**（不会把已存的 key 抹掉）。
- **`backend/verify_backend.py`**：31 项无头自动化验收（进程级端到端、引擎状态机、
  纯逻辑与 mock 后端自检），改动后端后一条命令即可回归。

### 保留

- **`learn_helper.py`（旧 Tkinter 客户端）原样保留**，逻辑仍可参考；
  它的自动化逻辑已迁入 `backend/learn_helper/core.py`。
- **`winui/`（WinUI 3 版）保留为备选退路**，启动器在 `winui/RunWinUI.bat`。

## 文件

| 路径 | 说明 |
| --- | --- |
| **`native/`** | **原生 Win32 前端（Rust）** —— 本版默认界面，单 exe 约 0.37 MB |
| `native/src/ui.rs` | 窗口与自绘界面（布局 / 绘制 / 鼠标 / 按钮状态） |
| `native/src/settings.rs` | 「答题设置」对话框（自绘控件 + 读写 `/api/settings`） |
| `native/src/backend.rs` | 后端客户端：拉起进程、stdout 握手、命名管道事件、HTTP 调用 |
| `native/src/dwm.rs` | DWM 圆角 / 深色标题栏（**不做玻璃**：客户区纯色自绘） |
| `native/src/winhttp.rs`、`json.rs` | 极简 WinHTTP 客户端与 JSON（不引第三方依赖） |
| `native/build_release.ps1` | 整包构建：Rust 构建 + PyInstaller 后端 + staging + zip |
| `native/settings_probe.ps1` | 「答题设置」端到端无头验收（真实点击链路 → PUT → 核对 `config.json`） |
| **`backend/`** | **Python 后端服务**（唯一业务大脑，无 UI） |
| `backend/learn_helper/core.py` | 刷课 / 答题核心（注入脚本、题型识别、求解、填涂） |
| `backend/learn_helper/engine.py` | `SolverEngine`：状态机 + 专用自动化线程 |
| `backend/learn_helper/ipc.py` | HTTP 接口 + 命名管道推送 |
| `backend/verify_backend.py` | **后端自动化验收（31 项）** |
| `winui/` | WinUI 3 界面（1.0.3 起；**现为备选退路**，`winui/RunWinUI.bat` 启动） |
| `learn_helper.py` | 旧 Tkinter 客户端（逻辑参考） |
| `client_app_reconstructed.py` | 原软件的反编译重建（研究参考，见 `docs/技术文档.md`） |
| **`docs/对接文档.md`** | **后端 / 答题模型对接文档**（接口契约、参考实现、内置自检与排错） |
| `docs/技术文档.md` | 反编译技术文档（**描述原始 exe**，含其原始接口契约） |
| `run.bat` / `运行界面.bat` | 启动器（前者启动旧 Python 客户端，后者启动**原生界面**） |

## 构建与运行

### 原生界面（默认，仅需 Rust 工具链）

```bat
cd native
cargo build --release
```

产物 `native\target\release\learn-helper-native.exe`（约 0.37 MB），双击 `运行界面.bat` 亦可。

> 界面会自动寻找并拉起后端：打包后是 `backend\learn-helper-core.exe`；
> 开发期则是沿目录树找到 `backend\main.py` 并用本机 Python 拉起
> （需要该解释器已装 `playwright` 与 `requests`）。

### 后端服务（开发期直接跑源码）

```bat
python backend\main.py --port 0
```

它会在 stdout 打一行 JSON 握手（含随机端口与管道名），并写 `logs/learn_helper.log`。

### 整包构建（UI + 后端 + 打包，可选）

```bat
powershell -ExecutionPolicy Bypass -File native\build_release.ps1 -Zip
```

产出 `dist\learn-helper-2.1.1\`（`LearnHelper.exe` + `backend\learn-helper-core.exe` + `Run.bat` + `README.txt`）
与同名 zip。目标机器**无需安装任何运行时**。（本版本 Release **不附**该整包，只发源码。）

## 工作原理

- 通过 `--remote-debugging-port=9222` 连接本机 Edge/Chrome，用 Playwright 操控。
- **浏览器什么时候被打开**：界面启动后自动拉起一个**独立配置**的沙盒浏览器
  （`browser_profile/`，不碰你日常在用的那个浏览器窗口；并 `--restore-last-session`
  恢复上次的学习页）；随后运行中掉线会自动重连。想手工控制就打「检测/刷新网页」，
  想关掉自动打开就去「答题设置」取消勾选 `run.auto_launch_browser`（默认勾选）。
- 控制逻辑（点击 / 翻页 / 刷视频 / 滚文档 / 防弹窗 / 每 10 页重建）**全部本地实现**；
  注入页面的脚本（倍速、换线路、滚动）为**本地常量**，不由服务器下发。
- 远端调用点只有两个，都指向你自己配置的后端：`/check_version`、`/solve`。
- ⚠️ **线程铁律**：Playwright 的同步 API 绑定创建它的线程 ——
  截图 / 题型识别 / 填涂必须留在**专用自动化线程**，只有纯网络求解可并发。

## 答题方式

| 方式 | 说明 |
| --- | --- |
| **内部答题 API**（默认） | 截图 + 题干发给自建后端 `POST /solve`，答案回填页面 |
| 自配大模型 | 直连你自己的 OpenAI 兼容视觉模型接口（Base URL / API Key / 模型名） |
| 仅识别不答题 | 只把题型/题干写进日志，便于排查平台改版 |

- 一批题目的处理顺序：**识别（截图/题型/题干）→ 并发求解 → 填涂 → 提交/暂存**。
- 配置写在 exe 同目录的 `config.json`（发布包内附 `config.example.json` 作为模板）。

## 开发者：回归校验

改完后端逻辑（`backend/`）后：

```bat
py -3.10 backend\verify_backend.py
```

改完原生界面（`native/`）后：

```bat
cd native && cargo build --release

rem 主界面：直接调自身绘制代码出图（唯一可信的界面截图，不依赖系统截图 API）
native\target\release\learn-helper-native.exe --render-probe 1686 960 native\render-probe.bmp

rem 「答题设置」对话框：不建窗口直接出图，并打印按钮/文字宽度的布局体检数据
native\target\release\learn-helper-native.exe --render-probe-settings 770 664 native\settings-probe.bmp

rem 「答题设置」端到端：真实点击 → PUT /api/settings → 核对 config.json（隔离在临时目录）
powershell -ExecutionPolicy Bypass -File native\settings_probe.ps1

rem 日志面板滚动条：端到端（滚轮/拖拽/翻页/悬停吞滚轮，15+ 项，不碰你的鼠标）
powershell -ExecutionPolicy Bypass -File native\logscroll_probe.ps1

rem 滚动条悬停高亮：像素级验证（两次渲染探针对比滑块颜色，不建窗口）
powershell -ExecutionPolicy Bypass -File native\logscroll_hover_check.ps1

rem 启动自动开浏览器：端到端（会真的开一次沙盒浏览器，跑完自己关掉）
powershell -ExecutionPolicy Bypass -File native\autolaunch_probe.ps1
```

> 界面类问题请**先取事实再改代码**：`--render-probe` 的产物 + 它落盘的
> `probe-layout:` 一行（控件矩形 / 文字实测宽 / 内边距）比肉眼判断可靠得多。
> `native\pixel_check_native.ps1` 依赖 `PrintWindow` 抓自己的窗口，在无边框窗口上
> **取不到客户区**，已不作为首选手段。

WinUI 备选线的校验（改 `winui/` 才需要）：

```bat
powershell -File winui\verify_theme.ps1
powershell -File winui\verify_panel.ps1
python winui\contrast_check.py
```

## 免责声明

仅供学习与技术研究使用。使用者需自行遵守所在平台的服务条款与相关法律法规，
因使用本工具产生的一切后果由使用者自负。
