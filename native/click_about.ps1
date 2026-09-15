# click_about.ps1 -- click the About button in the title bar and report whether the dialog opens.
#
# ⚠️ 本脚本必须先把**自己**设成 DPI 感知进程：否则 PowerShell 进程会被坐标虚拟化，
# 它发出的鼠标坐标会被 Windows 按系统缩放换算后才交给目标进程
# （实测：发 (849,30) → 目标收到 (1274,45)，全部打偏，见 ERROR.md E37/E39）。
# 所有循环都在 C# 里做 —— PowerShell 5.1 的嵌套数组算术很容易把自己绕死。

Add-Type -TypeDefinition @'
using System; using System.Text; using System.Runtime.InteropServices;
public class CA3 {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetDpiForWindow(IntPtr h);
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
  public static string Size(IntPtr h) { RECT r; GetWindowRect(h, out r);
    return (r.Right-r.Left) + "x" + (r.Bottom-r.Top); }

  public static string Run() {
    SetProcessDpiAwarenessContext(DPI_AWARE_V2);
    var log = new StringBuilder();
    IntPtr main = Find("LearnHelperNativeWnd");
    if (main == IntPtr.Zero) return "NO_MAIN_WINDOW\n";

    RECT cr; GetClientRect(main, out cr);
    int dpi = GetDpiForWindow(main);
    if (dpi <= 0) dpi = 96;
    int btn = (int)(48L * dpi * 100 / 96 / 100);   // px(48): 96dpi->48, 144dpi->72
    if (btn < 40) btn = 40;
    int w = cr.Right;

    // Title bar right side: x_start = w - 3*btn;
    // About = x_start - 3*btn, Settings = x_start - 2*btn.
    // (2026-09-15: the new Settings entry shifted About left by one slot --
    //  whenever you touch title-bar buttons, update these coordinates too.)
    int xAbout = w - btn * 3 - btn * 3 + btn / 2;
    int xSettings = w - btn * 3 - btn * 2 + btn / 2;
    int y = 30;
    log.AppendLine("probe: dpi=" + dpi + " client=" + cr.Right + "x" + cr.Bottom + " btn=" + btn + " xAbout=" + xAbout + " xSettings=" + xSettings);

    bool aboutOk = false;
    Click(main, xAbout, y);
    System.Threading.Thread.Sleep(500);
    IntPtr dlg = Find("LearnHelperAboutWnd");
    if (dlg != IntPtr.Zero) {
      log.AppendLine("CLICK " + xAbout + "," + y + " -> ABOUT DIALOG OPEN, size " + Size(dlg));
      aboutOk = true;
      SendMessage(dlg, 0x0010, (IntPtr)0, (IntPtr)0); // WM_CLOSE: close before the next test
      System.Threading.Thread.Sleep(400);
    } else {
      log.AppendLine("CLICK " + xAbout + "," + y + " -> no about dialog");
    }

    Click(main, xSettings, y);
    // the settings dialog may need a moment (it also kicks off a settings refresh)
    IntPtr sdlg = IntPtr.Zero;
    for (int t = 0; t < 12; t++) {
      System.Threading.Thread.Sleep(250);
      sdlg = Find("LearnHelperSettingsWnd");
      if (sdlg != IntPtr.Zero) break;
    }
    if (sdlg != IntPtr.Zero) {
      log.AppendLine("CLICK " + xSettings + "," + y + " -> SETTINGS DIALOG OPEN, size " + Size(sdlg));
      SendMessage(sdlg, 0x0010, (IntPtr)0, (IntPtr)0);
      System.Threading.Thread.Sleep(300);
    } else {
      log.AppendLine("CLICK " + xSettings + "," + y + " -> no settings dialog");
    }

    log.AppendLine(aboutOk ? "ABOUT OK" : "ABOUT DIALOG NOT OPENED");
    return log.ToString();
  }
}
'@

Write-Output ([CA3]::Run())
