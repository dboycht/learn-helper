using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.UI.Xaml;

namespace LearnHelper.App;

public partial class App : Application
{
    // A crash inside a P/Invoke or the composition layer is a NATIVE failure: no managed
    // handler runs, the process just dies. That is exactly why "clicking the theme toggle
    // crashes" left nothing in the log but the normal close line.
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr SetUnhandledExceptionFilter(IntPtr filter);

    private delegate int UnhandledExceptionFilterDelegate(IntPtr info);

    // Held in a static field on purpose: a delegate that is only referenced by the native
    // side can be collected, and the callback would then jump into freed memory.
    private static UnhandledExceptionFilterDelegate? _filter;

    private Window? _window;

    public App()
    {
        InitializeComponent();

        // The agent cannot see this app (no screenshot, no input injection - ERROR.md E10)
        // and Windows may swallow a XAML parse failure, so every unhandled exception is
        // written to ui-diag.log. Without this a startup crash looks like "nothing happens".
        UnhandledException += (_, e) =>
        {
            SettingsService.Trace($"UNHANDLED: {e.Exception}");
            e.Handled = false;
        };

        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            SettingsService.Trace($"APPDOMAIN UNHANDLED: {e.ExceptionObject}");

        _filter = NativeFilter;
        SetUnhandledExceptionFilter(Marshal.GetFunctionPointerForDelegate(_filter));

        SettingsService.Trace("app: constructed");
    }

    /// <summary>
    /// Last-chance native filter: records a marker plus the exception code so a hard crash
    /// is distinguishable from a normal exit, then lets default handling continue.
    /// </summary>
    private static int NativeFilter(IntPtr exceptionInfo)
    {
        try
        {
            var code = exceptionInfo != IntPtr.Zero ? Marshal.ReadInt32(exceptionInfo) : 0;
            SettingsService.Trace($"NATIVE CRASH: code=0x{code:X8}");
            File.AppendAllText(
                Path.Combine(AppContext.BaseDirectory, "ui-crash.txt"),
                $"{DateTime.Now:HH:mm:ss.fff} native crash code=0x{code:X8}{Environment.NewLine}",
                Encoding.UTF8);
        }
        catch (Exception)
        {
            // nothing more we can do this late
        }

        return 0;   // EXCEPTION_CONTINUE_SEARCH: let the default handler run
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        SettingsService.Trace("app: OnLaunched begin");
        try
        {
            _window = new MainWindow();
            SettingsService.Trace("app: MainWindow constructed");
            _window.Activate();
            SettingsService.Trace("app: MainWindow activated");
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"app: OnLaunched FAILED {ex}");
            throw;
        }
    }
}
