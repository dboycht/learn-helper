# learn-helper

学习助理 —— 网课学习平台的自动挂机刷课工具（自研 / 技术研究用途）。

## 版本

- **1.0.3**（当前）：**界面迁移到 WinUI 3**。前端从 Tkinter 换成 C# / .NET 9 + Windows App SDK，
  界面重做为半透明玻璃（Mica / Acrylic）+ 圆角 + 原生深浅主题。
  ⚠️ **本版是「界面里程碑」**：刷课 / 答题的**后端逻辑尚未接入**（面板里显示为「待连接」），
  该部分在 **1.0.4** 实现（Python 后端服务 + HTTP / 命名管道通信）。
- **1.0.2**：刷课（视频/文档）+ 答题。答题默认接入**内部答题 API**（自建后端 `POST /solve`，客户端零配置）；
  也可在「答题设置」里切换为**自配大模型**（OpenAI 兼容视觉模型）或**仅识别不答题**。
- **1.0.1**：纯刷课；账户静态显示（admin / 9999）。
- 全程**不含任何计费 / 卡密 / 点数逻辑**：没有余额、充值、商城、扣点与卡密输入，
  客户端只发送题目截图与题干；后端地址、模型等由使用者自己配置。

## 1.0.3 更新内容（WinUI 3 界面）

### 新增

- **界面整体迁移到 WinUI 3**（`winui/`，C# / .NET 9 + Windows App SDK 2.4），不再是 Tkinter。
- **玻璃材质**：整窗 **Mica / Acrylic** 系统背景（A 层），卡片为半透明玻璃面（B 层）；
  材质不可用时自动回落到不透明样式并如实标注状态。
- **深浅主题**，且**跟随系统 / 手动锁定**皆可；标题栏用原生深色模式绘制，随主题切换。
- **「外观」设置抽屉**：界面样式（玻璃 / 一般）、主题模式、卡片不透明度、玻璃材质（Thin / Base）、
  玻璃浓度、亮度层，全部可调；**改动实时预览**，「应用并保存」才落盘。
- **「开发者模式 · 配色」抽屉**：16 个设计令牌逐个调色（点色块套用或直接填 `#RRGGBB`），
  右侧实时显示对比度数值；可只看某个主题、可整体恢复默认。
- **可读性自动兜底**：所有文字/表面组合按 WCAG 校验（正文 ≥ 4.5:1），
  明显不可读的组合会被自动纠正，避免调出「看不见的字」。
- **窗口几何记忆**：位置与大小落盘（去抖），多显示器下夹取到可用区域。

### 说明

- 界面已按「前端（C# WinUI 3）+ 后端（Python 服务）」拆分；**1.0.4 接入后端**。
- 旧的 Tkinter 客户端 `learn_helper.py` **仍保留在仓库中**：它的刷课 / 答题逻辑将迁移为后端服务，
  见 `docs/对接文档.md`。

## 文件

| 路径 | 说明 |
| --- | --- |
| **`winui/`** | **WinUI 3 前端（C# / .NET 9）** —— 本版新界面 |
| `winui/MainWindow.xaml(.cs)` | 主窗口：布局、主题应用、表面着色 |
| `winui/GlassPalette.cs` | **颜色令牌的单一来源**（17 个令牌 + 对比度校验/自动纠正） |
| `winui/AcrylicGlassBackdrop.cs` | 系统玻璃材质（Mica / Acrylic）与标题栏主题 |
| `winui/Services/` | 设置读写（`ui-settings.json`）与主题服务 |
| `winui/verify_theme.ps1`、`verify_panel.ps1`、`contrast_check.py` | **自动化校验**（设置持久化 / 面板状态 / 对比度） |
| `winui/pixel_check.ps1` | 两套主题的**像素级**验收（防止「代码对、画面错」） |
| `learn_helper.py` | 旧 Tkinter 客户端（**1.0.4 将迁移为后端服务**） |
| `client_app_reconstructed.py` | 原软件的反编译重建（研究参考，见 `docs/技术文档.md`） |
| **`docs/对接文档.md`** | **后端 / 答题模型对接文档**（接口契约、参考实现、内置自检与排错） |
| `docs/技术文档.md` | 反编译技术文档（**描述原始 exe**，含其原始接口契约） |
| `run.bat` / `运行界面.bat` | 启动器（前者启动 Python 客户端，后者启动 WinUI 界面） |

## 构建与运行（WinUI 3 界面）

需要 **.NET SDK 9**（Windows App SDK 会随 NuGet 还原，无需 Visual Studio）。

```bat
dotnet build winui\LearnHelper.App.csproj -p:Platform=x64
```

然后双击 `运行界面.bat`，或直接运行构建产物：

```bat
winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe
```

自包含发布（目标机器无需安装 .NET 运行时）：

```bat
dotnet publish winui\LearnHelper.App.csproj -c Release -p:Platform=x64 -r win-x64 --self-contained true
```

> ⚠️ **发布坑（已实测）**：`dotnet publish` 不会把应用自己的编译后 XAML 资源
> （`App.xbf`、`MainWindow.xbf`、`LearnHelper.App.pri`）复制进 `publish/`，
> 直接运行会抛 `XamlParseException: XAML parsing failed`（Debug 与 Release 构建本身都正常）。
> 发布后需从 `winui\bin\x64\Release\net9.0-windows10.0.19041.0\win-x64\` 手工补齐这 3 个文件。

运行后设置写入 exe 同目录的 `ui-settings.json`；排障日志为 `ui-diag.log`。

## 运行（旧 Python 客户端，逻辑参考）

1. Python 3.10；依赖：`pip install playwright requests pillow`
   （只连接本机已安装的浏览器，无需 `playwright install` 下载内核）。
2. `python learn_helper.py`
3. 在弹出的浏览器里登录平台并打开学习页 → 面板点「检测/刷新网页」选择网页 → 「启动刷课」。

## 工作原理

- 通过 `--remote-debugging-port=9222` 连接本机 Edge/Chrome，用 Playwright 操控。
- 控制逻辑（点击 / 翻页 / 刷视频 / 滚文档 / 防弹窗 / 每 10 页重建）**全部本地实现**；
  注入页面的脚本（倍速、换线路、滚动）为**本地常量**，不由服务器下发。
- 远端调用点只有两个，都指向你自己配置的后端：`/check_version`、`/solve`。

## 答题方式（1.0.2 起）

| 方式 | 说明 |
| --- | --- |
| **内部答题 API**（默认） | 截图 + 题干发给自建后端 `POST /solve`，答案回填页面 |
| 自配大模型 | 直连你自己的 OpenAI 兼容视觉模型接口（Base URL / API Key / 模型名） |
| 仅识别不答题 | 只把题型/题干写进日志，便于排查平台改版 |

- 一批题目的处理顺序：**识别（截图/题型/题干）→ 并发求解 → 填涂 → 提交/暂存**。
- **自检**：「答题设置」里两个页签各有一对测试按钮：「测试连接」请求 `/check_version` 确认地址可达；
  「测试图片」把合成图发给所涉接口，逐题报告返回内容、耗时与是否识别正确。
  ⚠️ **「测试连接」只发纯文本，不能证明模型能读图片** —— 要验证识图必须点「测试图片」。

## 开发者：界面回归校验

改完界面颜色/主题相关代码后，这三条应当全绿：

```bat
powershell -File winui\verify_theme.ps1
powershell -File winui\verify_panel.ps1
python winui\contrast_check.py
```

颜色令牌的**单一来源**是 `winui/GlassPalette.cs`，改色后需同步 `winui/contrast_check.py` 的基准值再重跑。

## 免责声明

仅供学习与技术研究使用。使用者需自行遵守所在平台的服务条款与相关法律法规，
因使用本工具产生的一切后果由使用者自负。
