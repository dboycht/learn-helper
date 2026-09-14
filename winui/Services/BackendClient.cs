using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace LearnHelper.App;

/// <summary>
/// DTOs mirroring the Python backend's JSON (see backend/learn_helper/ipc.py).
///
/// The wire format is deliberately plain: every payload is ASCII-escaped JSON, so
/// Chinese text arrives as \uXXXX and neither side can be tripped by an encoding.
/// </summary>
public sealed class BackendEngineStatus
{
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("running")] public bool Running { get; set; }
    [JsonPropertyName("paused")] public bool Paused { get; set; }
    [JsonPropertyName("selected_page")] public string SelectedPage { get; set; } = "";
    [JsonPropertyName("page_count")] public int PageCount { get; set; }
    [JsonPropertyName("video_count")] public int VideoCount { get; set; }
    [JsonPropertyName("doc_count")] public int DocCount { get; set; }
    [JsonPropertyName("task_text")] public string TaskText { get; set; } = "";
    [JsonPropertyName("video_text")] public string VideoText { get; set; } = "";
    [JsonPropertyName("quiz_text")] public string QuizText { get; set; } = "";
    [JsonPropertyName("last_page")] public string LastPage { get; set; } = "";
    [JsonPropertyName("last_error")] public string LastError { get; set; } = "";
    [JsonPropertyName("uptime")] public double Uptime { get; set; }
}

public sealed class BackendStatus
{
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("protocol")] public int Protocol { get; set; }
    [JsonPropertyName("device_id")] public string DeviceId { get; set; } = "";
    [JsonPropertyName("pipe")] public string Pipe { get; set; } = "";
    [JsonPropertyName("selected_page")] public string SelectedPage { get; set; } = "";
    [JsonPropertyName("pages")] public List<string> Pages { get; set; } = new();
    [JsonPropertyName("engine")] public BackendEngineStatus? Engine { get; set; }
    [JsonPropertyName("action_ok")] public bool? ActionOk { get; set; }
    [JsonPropertyName("action_message")] public string ActionMessage { get; set; } = "";
}

/// <summary>One pushed event from the named pipe (log / progress / status / pages / hello).</summary>
public sealed class BackendEvent
{
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("seq")] public long Seq { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("task")] public string Task { get; set; } = "";
    [JsonPropertyName("video")] public string Video { get; set; } = "";
    [JsonPropertyName("quiz")] public string Quiz { get; set; } = "";
    [JsonPropertyName("pages")] public List<string> Pages { get; set; } = new();
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("device_id")] public string DeviceId { get; set; } = "";
    [JsonPropertyName("engine")] public BackendEngineStatus? Engine { get; set; }
}

/// <summary>Backend /api/settings payload.</summary>
public sealed class BackendSettings
{
    [JsonPropertyName("server_url")] public string ServerUrl { get; set; } = "";
    [JsonPropertyName("device_id")] public string DeviceId { get; set; } = "";
    [JsonPropertyName("answer")] public BackendAnswerSettings Answer { get; set; } = new();
    [JsonPropertyName("llm")] public BackendLlmSettings Llm { get; set; } = new();
    [JsonPropertyName("run")] public BackendRunSettings Run { get; set; } = new();
}

public sealed class BackendAnswerSettings
{
    [JsonPropertyName("mode")] public string Mode { get; set; } = "server";
    [JsonPropertyName("mode_label")] public string ModeLabel { get; set; } = "";
    [JsonPropertyName("workers")] public int Workers { get; set; } = 4;
    [JsonPropertyName("solver_timeout")] public int SolverTimeout { get; set; } = 240;
    [JsonPropertyName("retry")] public int Retry { get; set; } = 2;
}

public sealed class BackendLlmSettings
{
    [JsonPropertyName("base_url")] public string BaseUrl { get; set; } = "";
    [JsonPropertyName("model")] public string Model { get; set; } = "";
    [JsonPropertyName("has_api_key")] public bool HasApiKey { get; set; }
}

public sealed class BackendRunSettings
{
    [JsonPropertyName("video_speed")] public double VideoSpeed { get; set; } = 2.0;
    [JsonPropertyName("auto_submit")] public bool AutoSubmit { get; set; } = true;
}

/// <summary>Handshake line printed by the backend on stdout.</summary>
internal sealed class BackendHandshake
{
    [JsonPropertyName("token")] public string Token { get; set; } = "";
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("pid")] public int Pid { get; set; }
    [JsonPropertyName("port")] public int Port { get; set; }
    [JsonPropertyName("pipe")] public string Pipe { get; set; } = "";
}

/// <summary>
/// Talks to the Python backend: owns the child process, the HTTP client and the
/// named-pipe event subscription.
///
/// Threading: <see cref="EventReceived"/> fires on the pipe-reader thread. The UI layer
/// must marshal to the dispatcher itself (MainWindow does this) - the client deliberately
/// knows nothing about WinUI so it stays testable.
/// </summary>
public sealed class BackendClient : IDisposable
{
    /// <summary>Longest we wait for /api/health after the handshake line arrives.</summary>
    private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(20);

    private readonly object _gate = new();
    private Process? _process;
    private HttpClient? _http;
    private NamedPipeClientStream? _pipe;
    private CancellationTokenSource? _pipeCts;
    private int _handshakePort;
    private string _handshakePipe = "";
    private string _version = "";

    /// <summary>Set once the backend answered /api/health.</summary>
    public bool IsConnected { get; private set; }

    /// <summary>Human-readable state for the status card / diagnostics.</summary>
    public string StatusText { get; private set; } = "待连接…";

    public string BaseUrl { get; private set; } = "";

    /// <summary>Backend version reported by the handshake (empty when not started).</summary>
    public string BackendVersion => _version;

    /// <summary>Last settings snapshot (null until fetched).</summary>
    public BackendSettings? Settings { get; private set; }

    /// <summary>Last full status snapshot (null until fetched).</summary>
    public BackendStatus? Status { get; private set; }

    /// <summary>Raised for every pipe event. Fires off the UI thread.</summary>
    public event Action<BackendEvent>? EventReceived;

    /// <summary>Raised when the backend connection state changes (connect / lost).</summary>
    public event Action<string>? StateChanged;

    /// <summary>
    /// Launch the backend and wait until it answers. Returns false with
    /// <see cref="StatusText"/> explaining why - never throws for the expected
    /// "python/deps missing" case, because the UI must stay usable.
    /// </summary>
    public async Task<bool> StartAsync(string? explicitPath = null)
    {
        if (IsConnected)
        {
            return true;
        }

        var command = explicitPath != null
            ? new BackendCommand(explicitPath, new[] { "--port", "0" })
            : LocateBackend();
        if (command == null)
        {
            SetState("未找到后端程序（backend\\learn-helper-core.exe 或 backend\\main.py + Python 3.10）");
            return false;
        }

        SetState("正在启动后端…");

        var psi = new ProcessStartInfo
        {
            FileName = command.FileName,
            WorkingDirectory = AppContext.BaseDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var arg in command.Arguments)
        {
            psi.ArgumentList.Add(arg);
        }

        try
        {
            _process = Process.Start(psi);
        }
        catch (Exception ex)
        {
            SetState($"后端启动失败：{ex.Message}");
            return false;
        }

        if (_process == null)
        {
            SetState("后端进程未能创建");
            return false;
        }

        SettingsService.Trace($"backend: launched pid={_process.Id} file={command.FileName}");

        // Drain stderr in the background; a full pipe buffer would deadlock the child.
        _ = Task.Run(() => DrainStderrAsync(_process));

        var handshake = await ReadHandshakeAsync(_process).ConfigureAwait(false);
        if (handshake == null || handshake.Port <= 0)
        {
            SetState("后端未返回握手行（详见 ui-diag.log 与 backend 日志）");
            SettingsService.Trace("backend: handshake failed");
            return false;
        }

        _handshakePort = handshake.Port;
        _handshakePipe = handshake.Pipe;
        _version = handshake.Version;
        BaseUrl = $"http://127.0.0.1:{handshake.Port}";
        _http = new HttpClient { BaseAddress = new Uri(BaseUrl), Timeout = TimeSpan.FromSeconds(30) };
        SettingsService.Trace($"backend: handshake port={handshake.Port} pipe={handshake.Pipe} " +
                              $"version={handshake.Version}");

        if (!await WaitHealthyAsync().ConfigureAwait(false))
        {
            SetState($"后端握手成功但 /api/health 无响应（{BaseUrl}）");
            return false;
        }

        IsConnected = true;
        SetState($"已连接 v{_version} · {BaseUrl}");
        StartPipeReader(_handshakePipe);
        _ = RefreshStatusAsync();
        return true;
    }

    /// <summary>Launcher + arguments for the backend process.</summary>
    public sealed record BackendCommand(string FileName, IReadOnlyList<string> Arguments);

    /// <summary>
    /// Where the backend lives. Publish output ships <c>backend\learn-helper-core.exe</c>;
    /// during development we run the source with the same interpreter layout as the
    /// project's 运行界面.bat (py -3.10), if a packaged exe is not there yet.
    /// </summary>
    public static BackendCommand? LocateBackend()
    {
        var packaged = Path.Combine(AppContext.BaseDirectory, "backend", "learn-helper-core.exe");
        if (File.Exists(packaged))
        {
            return new BackendCommand(packaged, new[] { "--port", "0" });
        }

        // Development fallback: the repo layout (winui\bin\...\ -> ..\..\..\..\backend).
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 6 && dir != null; i++, dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, "backend", "main.py");
            if (File.Exists(candidate))
            {
                var python = FindPython310();
                if (python == null)
                {
                    return null;
                }

                return new BackendCommand(python, new[] { candidate, "--port", "0" });
            }
        }

        return null;
    }

    /// <summary>Find an interpreter that answers "py -3.10", then plain "python".</summary>
    private static string? FindPython310()
    {
        foreach (var exe in new[] { "py", "python" })
        {
            try
            {
                var psi = new ProcessStartInfo(exe, "-3.10 -c \"print(1)\"")
                {
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                };
                using var p = Process.Start(psi);
                if (p == null)
                {
                    continue;
                }

                p.WaitForExit(5000);
                if (p.ExitCode == 0)
                {
                    return exe;
                }
            }
            catch (Exception)
            {
                // try the next candidate
            }
        }

        return null;
    }

    private async Task DrainStderrAsync(Process process)
    {
        try
        {
            while (true)
            {
                var line = await process.StandardError.ReadLineAsync().ConfigureAwait(false);
                if (line == null)
                {
                    break;
                }

                SettingsService.Trace($"backend[stderr]: {line}");
            }
        }
        catch (Exception)
        {
            // process gone
        }
    }

    /// <summary>Read stdout until the handshake JSON line shows up.</summary>
    private static async Task<BackendHandshake?> ReadHandshakeAsync(Process process)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        try
        {
            while (!cts.IsCancellationRequested)
            {
                var line = await process.StandardOutput.ReadLineAsync().ConfigureAwait(false);
                if (line == null)
                {
                    return null;
                }

                if (!line.Contains("learn-helper-backend-ready", StringComparison.Ordinal))
                {
                    SettingsService.Trace($"backend[stdout]: {line}");
                    continue;
                }

                return JsonSerializer.Deserialize<BackendHandshake>(line);
            }
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"backend: handshake read failed: {ex.Message}");
        }

        return null;
    }

    private async Task<bool> WaitHealthyAsync()
    {
        var deadline = DateTime.UtcNow + HealthTimeout;
        while (DateTime.UtcNow < deadline)
        {
            try
            {
                var text = await _http!.GetStringAsync("/api/health").ConfigureAwait(false);
                if (text.Contains("\"ok\":true", StringComparison.Ordinal))
                {
                    return true;
                }
            }
            catch (Exception)
            {
                // not up yet
            }

            await Task.Delay(200).ConfigureAwait(false);
        }

        return false;
    }

    // ------------------------------------------------------------------
    // Named-pipe events
    // ------------------------------------------------------------------
    private void StartPipeReader(string pipeName)
    {
        if (string.IsNullOrEmpty(pipeName))
        {
            SettingsService.Trace("backend: no pipe name -> relying on HTTP polling");
            return;
        }

        _pipeCts = new CancellationTokenSource();
        var token = _pipeCts.Token;
        _ = Task.Run(async () =>
        {
            var delay = 250;
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.In);
                    await pipe.ConnectAsync(2000, token).ConfigureAwait(false);
                    SettingsService.Trace($"backend: pipe connected ({pipeName})");
                    delay = 250;
                    using var reader = new StreamReader(pipe, Encoding.UTF8);
                    while (!token.IsCancellationRequested)
                    {
                        var line = await reader.ReadLineAsync().ConfigureAwait(false);
                        if (line == null)
                        {
                            break;
                        }

                        if (line.Length == 0)
                        {
                            continue;
                        }

                        BackendEvent? evt = null;
                        try
                        {
                            evt = JsonSerializer.Deserialize<BackendEvent>(line);
                        }
                        catch (Exception ex)
                        {
                            SettingsService.Trace($"backend: bad event line: {ex.Message}");
                        }

                        if (evt != null)
                        {
                            try
                            {
                                EventReceived?.Invoke(evt);
                            }
                            catch (Exception ex)
                            {
                                SettingsService.Trace($"backend: event handler threw: {ex.Message}");
                            }
                        }
                    }
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    SettingsService.Trace($"backend: pipe error: {ex.Message}");
                }

                if (token.IsCancellationRequested)
                {
                    break;
                }

                // Reconnect with a gentle backoff: the backend recreates the pipe
                // instance after every client disconnect.
                try
                {
                    await Task.Delay(delay, token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }

                delay = Math.Min(delay * 2, 5000);
            }
        }, token);
    }

    // ------------------------------------------------------------------
    // HTTP API
    // ------------------------------------------------------------------
    public async Task<BackendStatus?> RefreshStatusAsync()
    {
        var status = await GetJsonAsync<BackendStatus>("/api/status").ConfigureAwait(false);
        if (status != null)
        {
            Status = status;
        }

        return status;
    }

    public async Task<BackendSettings?> RefreshSettingsAsync()
    {
        var payload = await GetJsonAsync<ApiEnvelope<BackendSettings>>("/api/settings")
            .ConfigureAwait(false);
        if (payload?.Data != null)
        {
            Settings = payload.Data;
        }

        return Settings;
    }

    public async Task<BackendStatus?> ControlAsync(
        string action, object? parameters = null)
    {
        var body = JsonSerializer.Serialize(new
        {
            action,
            @params = parameters ?? new { },
        });
        var status = await PostJsonAsync<BackendStatus>("/api/control", body)
            .ConfigureAwait(false);
        if (status != null)
        {
            Status = status;
        }

        return status;
    }

    public async Task<BackendSettings?> UpdateSettingsAsync(string json)
    {
        var payload = await SendJsonAsync<ApiEnvelope<BackendSettings>>(
            HttpMethod.Put, "/api/settings", json).ConfigureAwait(false);
        if (payload?.Data != null)
        {
            Settings = payload.Data;
        }

        return Settings;
    }

    /// <summary>Catch up on log lines missed while the pipe was reconnecting.</summary>
    public async Task<List<string>> FetchLogsSinceAsync(long since)
    {
        var payload = await GetJsonAsync<ApiEnvelope<LogEnvelope>>($"/api/logs?since={since}")
            .ConfigureAwait(false);
        var lines = new List<string>();
        if (payload?.Data?.Entries != null)
        {
            foreach (var entry in payload.Data.Entries)
            {
                lines.Add(entry.Text);
            }
        }

        return lines;
    }

    private async Task<T?> GetJsonAsync<T>(string path) where T : class
    {
        if (_http == null)
        {
            return null;
        }

        try
        {
            var text = await _http.GetStringAsync(path).ConfigureAwait(false);
            return JsonSerializer.Deserialize<T>(text);
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"backend: GET {path} failed: {ex.Message}");
            return null;
        }
    }

    private async Task<T?> PostJsonAsync<T>(string path, string json) where T : class =>
        await SendJsonAsync<T>(HttpMethod.Post, path, json).ConfigureAwait(false);

    private async Task<T?> SendJsonAsync<T>(HttpMethod method, string path, string json)
        where T : class
    {
        if (_http == null)
        {
            return null;
        }

        try
        {
            using var request = new HttpRequestMessage(method, path)
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json"),
            };
            using var response = await _http.SendAsync(request).ConfigureAwait(false);
            var text = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return JsonSerializer.Deserialize<T>(text);
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"backend: {method} {path} failed: {ex.Message}");
            return null;
        }
    }

    /// <summary>Ask the backend to stop (it closes the browser and exits).</summary>
    public void RequestShutdown()
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/api/shutdown")
            {
                Content = new StringContent("{}", Encoding.UTF8, "application/json"),
            };
            _http?.SendAsync(request, CancellationToken.None).Wait(2000);
        }
        catch (Exception)
        {
            // the process may already be gone - that is the desired outcome
        }
    }

    private void SetState(string text)
    {
        StatusText = text;
        try
        {
            StateChanged?.Invoke(text);
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"backend: StateChanged threw: {ex.Message}");
        }
    }

    // ------------------------------------------------------------------
    // Teardown
    // ------------------------------------------------------------------
    public void Dispose()
    {
        try
        {
            _pipeCts?.Cancel();
        }
        catch (Exception)
        {
            // ignore
        }

        try
        {
            _pipe?.Dispose();
        }
        catch (Exception)
        {
            // ignore
        }

        if (IsConnected)
        {
            RequestShutdown();
        }

        try
        {
            if (_process is { HasExited: false })
            {
                if (!_process.WaitForExit(3000))
                {
                    _process.Kill(entireProcessTree: true);
                }
            }
        }
        catch (Exception)
        {
            // ignore
        }

        _http?.Dispose();
        _process?.Dispose();
        _pipeCts?.Dispose();
        IsConnected = false;
    }

    private sealed class ApiEnvelope<T>
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("message")] public string Message { get; set; } = "";
        [JsonPropertyName("data")] public T? Data { get; set; }
    }

    private sealed class LogEnvelope
    {
        [JsonPropertyName("entries")] public List<LogEntry> Entries { get; set; } = new();
        [JsonPropertyName("last")] public long Last { get; set; }
    }

    private sealed class LogEntry
    {
        [JsonPropertyName("seq")] public long Seq { get; set; }
        [JsonPropertyName("text")] public string Text { get; set; } = "";
    }
}
