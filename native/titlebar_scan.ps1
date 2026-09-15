# titlebar_scan.ps1 -- click every title-bar button slot of the RUNNING app and report
# which control the app actually invoked (read from native-diag.log "ui: invoke X").
#
# Why: after adding a new title-bar entry, "the button looks right but does nothing"
# is almost always a paint-vs-hit-test coordinate mismatch. This measures it.
#
# Requires: the app already running (D:\code\DeepSeekHarness\learn-helper\native\run ...).
# ASCII only (PS 5.1 misreads BOM-less UTF-8 as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe"
)

Add-Type -TypeDefinition @'
using System; using System.Text; using System.Runtime.InteropServices;
public class TB {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetDpiForWindow(IntPtr h);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public static readonly IntPtr DPI_AWARE_V2 = new IntPtr(-4);

  public static IntPtr Find(string cls) {
    IntPtr f = IntPtr.Zero;
    EnumWindows((h,p) => { var sb=new StringBuilder(256); GetClassNameW(h,sb,256);
      if (sb.ToString()==cls) { f=h; return false; } return true; }, IntPtr.Zero);
    return f;
  }
  public static void Click(IntPtr h, int x, int y) {
    IntPtr lp = (IntPtr)((y << 16) | (x & 0xFFFF));
    SendMessage(h, 0x0201, (IntPtr)1, lp);
    SendMessage(h, 0x0202, (IntPtr)0, lp);
  }

  public static string Run(int fromRight) {
    SetProcessDpiAwarenessContext(DPI_AWARE_V2);
    var log = new StringBuilder();
    IntPtr main = Find("LearnHelperNativeWnd");
    if (main == IntPtr.Zero) return "NO_MAIN_WINDOW\n";
    RECT cr; GetClientRect(main, out cr);
    int dpi = GetDpiForWindow(main); if (dpi <= 0) dpi = 96;
    int btn = (int)(48L * dpi * 100 / 96 / 100); if (btn < 40) btn = 40;
    int w = cr.Right;
    int y = 30;
    log.AppendLine("client=" + w + "x" + cr.Bottom + " dpi=" + dpi + " btn=" + btn);
    // slot i: 0..(fromRight-1) counting from the right edge.
    // Slot 0 IS the close button (rightmost) - skip it, because invoking it
    // destroys the window and every later click would hit a dead hwnd.
    for (int i = 0; i < fromRight; i++) {
      if (i == 0) { log.AppendLine("slot 0 = close button, skipped"); continue; }
      int x = w - (i * btn) - (btn / 2);
      Click(main, x, y);
      System.Threading.Thread.Sleep(350);
      // close any dialog that opened so the next click reaches the main window
      IntPtr a = Find("LearnHelperAboutWnd");    if (a != IntPtr.Zero) { SendMessage(a, 0x0010, (IntPtr)0, (IntPtr)0); }
      IntPtr s = Find("LearnHelperSettingsWnd"); if (s != IntPtr.Zero) { SendMessage(s, 0x0010, (IntPtr)0, (IntPtr)0); }
      System.Threading.Thread.Sleep(250);
      log.AppendLine("slot " + i + " x=" + x);
    }
    return log.ToString();
  }
}
'@

$log = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'
$before = 0
if (Test-Path $log) { $before = @(Get-Content $log -Encoding UTF8).Count }

Write-Output ([TB]::Run(6))

Start-Sleep -Milliseconds 400
if (Test-Path $log) {
    $all = @(Get-Content $log -Encoding UTF8)
    Write-Output "--- invoked controls (in order) ---"
    $all | Where-Object { $_ -match 'ui: invoke ' } | ForEach-Object { Write-Output $_ }
    Write-Output "--- dialog traces ---"
    $all | Where-Object { $_ -match 'window shown|Cancel clicked|settings: window' } | ForEach-Object { Write-Output $_ }
}
