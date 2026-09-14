# learn-helper

学习助理 —— 网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。

## 版本

- **2.1.1**（当前）：**界面重构为原生 Win32（Rust）+ Python 后端服务**，主打「好传播」。
  - **前端**：`native/` —— Rust 手写 Win32/GDI/DWM，**单 exe 约 0.30 MB，零运行时依赖**，
    解压即用；玻璃（Mica / Acrylic）由 **DWM** 提供（不是自绘假玻璃），保留原生缩放 / 贴边 / 圆角。
  - **后端**：`backend/` —— Python 服务（Playwright 刷课 + 答题），
    经 **HTTP 请求-响应 + 命名管道推送** 与前端通信；已用 PyInstaller 打成单 exe，随包分发。
  - 前作 **1.0.3 的 WinUI 3 界面保留在 `winui/` 作为备选退路**（不再默认启动）。
- **1.0.2**：刷课（视频/文档）+ 答题。答题默认接入**内部答题 API**（自建后端 `POST /solve`，客户端零配置）；
  也可切换为**自配大模型**（OpenAI 兼容视觉模型）或**仅识别不答题**。
- **1.0.1**：纯刷课。
- 全程**不含任何计费 / 卡密 / 点数逻辑**：没有余额、充值、商城、扣点与卡密输入，
  客户端只发送题目截图与题干；后端地址、模型等由使用者自己配置。

## 2.1.1 更新内容（原生 Win32 界面 + 后端服务）

### 分发（本次重构的直接动机）

| | 体积 | 目标机需要装什么 |
| --- | --- | --- |
| 1.0.3（WinUI 3，框架依赖） | 26.1 MB | **.NET 9 Desktop Runtime + Windows App SDK Runtime** |
| 1.0.3（WinUI 3，自包含） | 85.7 MB | 无 |
| **2.1.1（原生 + 后端整包）** | **51.4 MB**（UI 仅 **0.30 MB**） | **什么都不用装** |

> 剩下的 51 MB 几乎全是后端里 Playwright 自带的浏览器运行时，UI 只占 0.6%。

### 新增

- **原生 Win32 前端**（`native/`，Rust）：自绘标题栏 + 完全自绘界面，
  玻璃材质用 DWM 系统背板（Mica → Acrylic → 纯色三级回退并如实标注），
  Win11 圆角、深色标题栏、PerMonitorV2 DPI 感知。
- **Python 后端服务**（`backend/learn_helper/`）：把原先 Tkinter 客户端里的
  刷课 / 答题逻辑迁出（**去掉全部 UI**），成为独立服务：
  - **HTTP**（`127.0.0.1:<随机端口>`）：`/api/health`、`/api/status`、`/api/settings`、
    `/api/control`（启动 / 暂停 / 停止 / 刷新网页 / 选择网页 / 诊断 / 自检）、`/api/logs`；
  - **命名管道**：后端单向推送日志 / 进度 / 状态，界面实时显示；
  - 端口由系统随机分配，前端通过后端 stdout 的**握手行**获知，不猜端口。
- **`backend/verify_backend.py`**：29 项无头自动化验收（进程级端到端、引擎状态机、
  纯逻辑与 mock 后端自检），改动后端后一条命令即可回归。

### 保留

- **`learn_helper.py`（旧 Tkinter 客户端）原样保留**，逻辑仍可参考；
  它的自动化逻辑已迁入 `backend/learn_helper/core.py`。
- **`winui/`（WinUI 3 版）保留为备选退路**，启动器在 `winui/RunWinUI.bat`。

## 文件

| 路径 | 说明 |
| --- | --- |
| **`native/`** | **原生 Win32 前端（Rust）** —— 本版默认界面，约 0.30 MB |
| `native/src/ui.rs` | 窗口与自绘界面（布局 / 绘制 / 鼠标 / 按钮状态） |
| `native/src/backend.rs` | 后端客户端：拉起进程、stdout 握手、命名管道事件、HTTP 调用 |
| `native/src/dwm.rs` | DWM 玻璃 / 圆角 / 深色标题栏（三级回退） |
| `native/src/winhttp.rs`、`json.rs` | 极简 WinHTTP 客户端与 JSON（不引第三方依赖） |
| `native/build_release.ps1` | 整包构建：Rust 构建 + PyInstaller 后端 + staging + zip |
| `native/pixel_check_native.ps1` | 界面渲染验证（抓自己的窗口做像素采样） |
| **`backend/`** | **Python 后端服务**（唯一业务大脑，无 UI） |
| `backend/learn_helper/core.py` | 刷课 / 答题核心（注入脚本、题型识别、求解、填涂） |
| `backend/learn_helper/engine.py` | `SolverEngine`：状态机 + 专用自动化线程 |
| `backend/learn_helper/ipc.py` | HTTP 接口 + 命名管道推送 |
| `backend/verify_backend.py` | **后端自动化验收（29 项）** |
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

产物 `native\target\release\learn-helper-native.exe`（约 0.30 MB），双击 `运行界面.bat` 亦可。

> 界面会自动寻找并拉起后端：发布包里是 `backend\learn-helper-core.exe`；
> 开发期则是 `py -3.10 backend\main.py`。

### 后端服务（开发期直接跑源码）

```bat
py -3.10 backend\main.py --port 0
```

它会在 stdout 打一行 JSON 握手（含随机端口与管道名），并写 `logs/learn_helper.log`。

### 整包构建（UI + 后端 + 打包）

```bat
powershell -ExecutionPolicy Bypass -File native\build_release.ps1 -Zip
```

产出 `dist\learn-helper-2.1.1\`（`LearnHelper.exe` + `backend\learn-helper-core.exe` + `Run.bat` + `README.txt`）
与同名 zip。目标机器**无需安装任何运行时**。

## 工作原理

- 通过 `--remote-debugging-port=9222` 连接本机 Edge/Chrome，用 Playwright 操控。
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
powershell -ExecutionPolicy Bypass -File native\pixel_check_native.ps1
```

（后者会抓取自己的窗口做像素采样并输出 `native-capture.png`，用于确认"确实画出来了"。）

WinUI 备选线的校验（改 `winui/` 才需要）：

```bat
powershell -File winui\verify_theme.ps1
powershell -File winui\verify_panel.ps1
python winui\contrast_check.py
```

## 免责声明

仅供学习与技术研究使用。使用者需自行遵守所在平台的服务条款与相关法律法规，
因使用本工具产生的一切后果由使用者自负。
