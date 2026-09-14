using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace LearnHelper.App;

/// <summary>
/// The WinUI half of the Python-backend integration (1.0.4).
///
/// Split out of MainWindow.xaml.cs so the (already large) appearance code stays
/// readable. Everything here is presentation only: the backend owns all state.
///
/// Threading: <see cref="BackendClient.EventReceived"/> fires on the pipe-reader thread,
/// so every handler hops onto the dispatcher before touching XAML. Log lines are batched
/// through a timer - a busy solve run emits hundreds of lines a minute and appending each
/// one individually both costs layout time and fights the ScrollViewer.
/// </summary>
public sealed partial class MainWindow
{
    private BackendClient? _backend;
    private readonly StringBuilder _pendingLog = new();
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _logFlushTimer;
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _statusPollTimer;
    private bool _uiLoaded;

    /// <summary>Total characters kept in the on-screen log before the oldest half is dropped.</summary>
    private const int LogCharBudget = 120_000;

    // =====================================================================
    // startup
    // =====================================================================

    /// <summary>
    /// Connect to the Python backend, launching it when needed.
    ///
    /// Never blocks the UI thread: the handshake wait happens in the background and the
    /// status card reports progress. A missing backend leaves the window fully usable.
    /// </summary>
    private void InitializeBackend()
    {
        _logFlushTimer = DispatcherQueue.CreateTimer();
        _logFlushTimer.Interval = TimeSpan.FromMilliseconds(200);
        _logFlushTimer.IsRepeating = true;
        _logFlushTimer.Tick += (s, _) => FlushLog();
        _logFlushTimer.Start();

        _backend = new BackendClient();
        _backend.EventReceived += OnBackendEvent;
        _backend.StateChanged += OnBackendStateChanged;

        _ = ConnectBackendAsync();
    }

    private async Task ConnectBackendAsync()
    {
        var client = _backend;
        if (client == null)
        {
            return;
        }

        AppendLog("[后端] 正在启动 Python 后端服务…");
        var ok = await client.StartAsync().ConfigureAwait(true);
        if (!ok)
        {
            AppendLog($"[后端] {client.StatusText}");
            AppendLog("[后端] 界面仍可使用；后端恢复后点「检测/刷新网页」或重启程序即可重连。");
            return;
        }

        AppendLog($"[后端] 已连接：{client.StatusText}");
        await RefreshSettingsAsync().ConfigureAwait(true);

        // Poll as a safety net: the pipe is push-only and could drop out (it is
        // reconnected automatically) - a slow poll keeps the UI honest either way.
        _statusPollTimer = DispatcherQueue.CreateTimer();
        _statusPollTimer.Interval = TimeSpan.FromSeconds(2);
        _statusPollTimer.IsRepeating = true;
        _statusPollTimer.Tick += async (_, _) => await PollStatusAsync().ConfigureAwait(true);
        _statusPollTimer.Start();

        await PollStatusAsync().ConfigureAwait(true);
    }

    private async Task RefreshSettingsAsync()
    {
        if (_backend == null)
        {
            return;
        }

        var settings = await _backend.RefreshSettingsAsync().ConfigureAwait(true);
        if (settings == null)
        {
            return;
        }

        if (_uiLoaded)
        {
            AnswerButton.Content = $"答题设置（{settings.Answer.ModeLabel}）";
        }

        NoticeText.Text = $"后端 {settings.ServerUrl}　·　答题方式 {settings.Answer.ModeLabel}" +
                          $"　·　设备 {settings.DeviceId}";
        SettingsService.Trace($"backend: settings mode={settings.Answer.Mode} " +
                              $"speed={settings.Run.VideoSpeed:0.0} autoSubmit={settings.Run.AutoSubmit}");
    }

    private async Task PollStatusAsync()
    {
        if (_backend is null || !_backend.IsConnected)
        {
            return;
        }

        var status = await _backend.RefreshStatusAsync().ConfigureAwait(true);
        if (status != null)
        {
            ApplyStatus(status);
        }
    }

    // =====================================================================
    // event / status application
    // =====================================================================

    private void OnBackendStateChanged(string text)
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            BackendText.Text = text;
            if (text.Contains("已连接", StringComparison.Ordinal))
            {
                DeviceText.Text = _backend?.Settings?.DeviceId ?? _backend?.Status?.DeviceId ?? "";
            }
        });
    }

    private void OnBackendEvent(BackendEvent evt)
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            switch (evt.Type)
            {
                case "hello":
                    BackendText.Text = $"已连接 v{evt.Version}";
                    DeviceText.Text = evt.DeviceId;
                    break;
                case "log":
                    AppendLog(evt.Text);
                    break;
                case "progress":
                    ProgressTaskText.Text = $"当前状态   {evt.Task}";
                    ProgressVideoText.Text = $"音视频进度 {evt.Video}";
                    ProgressQuizText.Text = $"答题进度   {evt.Quiz}";
                    break;
                case "pages":
                    ApplyPages(evt.Pages);
                    break;
                case "status" when evt.Engine != null:
                    // The push only carries the engine block; keep the pages we already have.
                    ApplyEngine(evt.Engine);
                    break;
            }
        });
    }

    private void ApplyStatus(BackendStatus status)
    {
        BackendText.Text = $"已连接 v{status.Version}";
        DeviceText.Text = string.IsNullOrEmpty(status.DeviceId)
            ? DeviceText.Text
            : $"设备 {status.DeviceId}";

        if (status.Pages.Count > 0)
        {
            ApplyPages(status.Pages, status.SelectedPage);
        }

        if (status.Engine != null)
        {
            ApplyEngine(status.Engine);
        }
    }

    private void ApplyEngine(BackendEngineStatus engine)
    {
        _uiLoaded = true;

        KpiVideoValue.Text = engine.VideoCount.ToString();
        KpiDocValue.Text = engine.DocCount.ToString();
        KpiQuizValue.Text = engine.Running || engine.PageCount > 0
            ? engine.PageCount.ToString()
            : "0";

        ProgressTaskText.Text = $"当前状态   {engine.TaskText}";
        ProgressVideoText.Text = $"音视频进度 {engine.VideoText}";
        ProgressQuizText.Text = $"答题进度   {engine.QuizText}";

        // Controls follow the backend's truth, not our own optimistic guess.
        StartButton.IsEnabled = !engine.Running;
        StartButton.Content = engine.Running ? "正在运行…" : "启动刷课";
        PauseButton.IsEnabled = engine.Running;
        PauseButton.Content = engine.Paused ? "继续执行" : "暂停进程";
        StopButton.IsEnabled = engine.Running;
        RefreshPagesButton.IsEnabled = !engine.Running;

        if (!string.IsNullOrEmpty(engine.LastError))
        {
            ProgressTaskText.Text = $"当前状态   ⚠ {engine.LastError}";
        }
    }

    private void ApplyPages(List<string> pages, string? selected = null)
    {
        if (pages.Count == 0)
        {
            return;
        }

        var wanted = selected ?? _backend?.Status?.SelectedPage ?? "";
        var current = PageCombo.SelectedItem as ComboBoxItem;
        var currentText = current?.Content?.ToString() ?? "";
        if (currentText == string.Join("", pages.Take(1)) && PageCombo.Items.Count == pages.Count)
        {
            return;   // nothing changed - avoid resetting the user's selection
        }

        PageCombo.SelectionChanged -= OnPageSelectionChanged;
        try
        {
            PageCombo.Items.Clear();
            ComboBoxItem? match = null;
            foreach (var label in pages)
            {
                var item = new ComboBoxItem { Content = label };
                PageCombo.Items.Add(item);
                if (!string.IsNullOrEmpty(wanted) && label == wanted)
                {
                    match = item;
                }
            }

            PageCombo.SelectedItem = match ?? PageCombo.Items.FirstOrDefault();
        }
        finally
        {
            PageCombo.SelectionChanged += OnPageSelectionChanged;
        }

        SettingsService.Trace($"backend: pages applied ({pages.Count}) selected=" +
                              $"{((PageCombo.SelectedItem as ComboBoxItem)?.Content ?? "(none)")}");
    }

    // =====================================================================
    // log handling
    // =====================================================================

    /// <summary>Queue a line; the 200ms timer appends the batch in one layout pass.</summary>
    private void AppendLog(string line)
    {
        lock (_pendingLog)
        {
            _pendingLog.Append(line).Append('\n');
        }
    }

    private void FlushLog()
    {
        string text;
        lock (_pendingLog)
        {
            if (_pendingLog.Length == 0)
            {
                return;
            }

            text = _pendingLog.ToString();
            _pendingLog.Clear();
        }

        var stamp = DateTime.Now.ToString("HH:mm:ss");
        var lines = text.TrimEnd('\n').Split('\n');
        var sb = new StringBuilder();
        foreach (var line in lines)
        {
            sb.Append('[').Append(stamp).Append("] ").Append(line).Append(Environment.NewLine);
        }

        LogText.Text += sb.ToString();

        // Keep the surface bounded: an all-night run must not grow a 100MB TextBlock.
        if (LogText.Text.Length > LogCharBudget)
        {
            LogText.Text = LogText.Text[^LogCharBudget..];
        }

        LogScroll.ChangeView(null, LogScroll.ScrollableHeight, null, true);
    }

    // =====================================================================
    // control handlers (bound from MainWindow.xaml)
    // =====================================================================

    private async void OnStartClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null || !_backend.IsConnected)
        {
            AppendLog("[后端] 尚未连接，正在重试启动…");
            if (!await ConnectRetryAsync().ConfigureAwait(true))
            {
                await ShowDialogAsync("无法启动后端", _backend?.StatusText ?? "后端不可用");
                return;
            }
        }

        var page = (PageCombo.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "";
        if (string.IsNullOrWhiteSpace(page))
        {
            await ShowDialogAsync("尚未选择网页", "请先在沙盒浏览器里打开学习页，再点「检测/刷新网页」。");
            return;
        }

        var status = await _backend!.ControlAsync("start", new { page }).ConfigureAwait(true);
        AppendLog($"[后端] {status?.ActionMessage ?? "(无响应)"}");
        if (status?.ActionOk == false)
        {
            await ShowDialogAsync("启动被拒绝", status.ActionMessage);
        }
    }

    private async void OnPauseClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null)
        {
            return;
        }

        var paused = _backend.Status?.Engine?.Paused ?? false;
        var status = await _backend.ControlAsync(paused ? "resume" : "pause").ConfigureAwait(true);
        AppendLog($"[后端] {status?.ActionMessage ?? "(无响应)"}");
    }

    private async void OnStopClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null)
        {
            return;
        }

        var status = await _backend.ControlAsync("stop").ConfigureAwait(true);
        AppendLog($"[后端] {status?.ActionMessage ?? "(无响应)"}（沙盒浏览器保留，便于查看页面）");
    }

    private async void OnRefreshPagesClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null || !_backend.IsConnected)
        {
            if (!await ConnectRetryAsync().ConfigureAwait(true))
            {
                AppendLog("[后端] 后端不可用，无法读取标签页。");
                return;
            }
        }

        RefreshPagesButton.IsEnabled = false;
        try
        {
            var status = await _backend!.ControlAsync("refresh_pages").ConfigureAwait(true);
            AppendLog($"[后端] {status?.ActionMessage ?? "(无响应)"}");
            if (status?.Pages is { Count: > 0 })
            {
                ApplyPages(status.Pages, status.SelectedPage);
            }
        }
        finally
        {
            RefreshPagesButton.IsEnabled = true;
        }
    }

    private async void OnPageSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_backend is null || !_backend.IsConnected)
        {
            return;
        }

        var page = (PageCombo.SelectedItem as ComboBoxItem)?.Content?.ToString();
        if (string.IsNullOrWhiteSpace(page))
        {
            return;
        }

        await _backend.ControlAsync("select_page", new { page }).ConfigureAwait(true);
    }

    private async void OnDiagnoseClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null || !_backend.IsConnected)
        {
            AppendLog("[后端] 后端未连接，无法诊断。");
            return;
        }

        AppendLog("[后端] 正在诊断当前页面…（结果同时写入后端日志）");
        var status = await _backend.ControlAsync("diagnose").ConfigureAwait(true);
        AppendLog($"[后端] {status?.ActionMessage ?? "(无响应)"}");
    }

    private async void OnAnswerSettingsClick(object sender, RoutedEventArgs e)
    {
        if (_backend is null || !_backend.IsConnected)
        {
            await ShowDialogAsync("后端未连接", "答题设置由 Python 后端持久化，请先连接后端。");
            return;
        }

        await RefreshSettingsAsync().ConfigureAwait(true);
        var settings = _backend.Settings;
        if (settings == null)
        {
            await ShowDialogAsync("读取失败", "未能读取后端设置。");
            return;
        }

        var body =
            $"后端地址：{settings.ServerUrl}\n" +
            $"答题方式：{settings.Answer.ModeLabel}（{settings.Answer.Mode}）\n" +
            $"并发 / 超时 / 重试：{settings.Answer.Workers} / {settings.Answer.SolverTimeout}s / {settings.Answer.Retry}\n" +
            $"自配大模型：{settings.Llm.Model}（密钥{(settings.Llm.HasApiKey ? "已配置" : "未配置")}）\n" +
            $"倍速：{settings.Run.VideoSpeed:0.0}x　提交：{(settings.Run.AutoSubmit ? "自动提交" : "仅暂存")}\n\n" +
            "编辑这些设置请修改项目根目录的 config.json（与旧版客户端共用同一份）。";
        await ShowDialogAsync("答题设置", body);
    }

    private async Task<bool> ConnectRetryAsync()
    {
        if (_backend is null)
        {
            return false;
        }

        if (_backend.IsConnected)
        {
            return true;
        }

        var ok = await _backend.StartAsync().ConfigureAwait(true);
        if (ok)
        {
            await RefreshSettingsAsync().ConfigureAwait(true);
            await PollStatusAsync().ConfigureAwait(true);
        }

        return ok;
    }

    private async Task ShowDialogAsync(string title, string body)
    {
        try
        {
            var dialog = new ContentDialog
            {
                Title = title,
                Content = new TextBlock { Text = body, TextWrapping = TextWrapping.Wrap },
                CloseButtonText = "知道了",
                XamlRoot = RootGrid.XamlRoot,
            };
            await dialog.ShowAsync();
        }
        catch (Exception ex)
        {
            // A dialog failure (e.g. another dialog already open) must never crash the app.
            AppendLog($"[界面] 提示框未能显示：{ex.Message}");
        }
    }

    /// <summary>Scriptable hook used by verify_panel.ps1 (see MainWindow ctor).</summary>
    private async Task RunBackendScriptedActionAsync(string action)
    {
        if (_backend is null)
        {
            return;
        }

        SettingsService.Trace($"verify: backend action '{action}'");
        switch (action.Trim().ToLowerInvariant())
        {
            case "status":
            case "refresh":
                await PollStatusAsync().ConfigureAwait(true);
                SettingsService.Trace($"verify: backend connected={_backend.IsConnected} " +
                                      $"status='{_backend.StatusText}' " +
                                      $"running={_backend.Status?.Engine?.Running} " +
                                      $"pages={_backend.Status?.Pages.Count}");
                break;
            case "pages":
                await _backend.ControlAsync("refresh_pages").ConfigureAwait(true);
                SettingsService.Trace($"verify: backend pages={_backend.Status?.Pages.Count}");
                break;
            case "start":
                await _backend.ControlAsync("start").ConfigureAwait(true);
                break;
            case "stop":
                await _backend.ControlAsync("stop").ConfigureAwait(true);
                break;
        }
    }

    private void ShutdownBackend()
    {
        _logFlushTimer?.Stop();
        _statusPollTimer?.Stop();
        FlushLog();
        try
        {
            _backend?.Dispose();
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"backend: dispose failed: {ex.Message}");
        }

        _backend = null;
    }
}
