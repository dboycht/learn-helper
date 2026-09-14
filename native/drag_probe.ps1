# drag_probe.ps1 -- simulate a real title-bar drag with SendInput and report the window
# rectangle before/after each step. Used to reproduce "the window disappears when dragged".
# ASCII only (rules/01 section 8.2).

param(
    [int]$Steps = 6,
    [int]$Dx = 120,
    [int]$Dy = 60
)

$code = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class DP {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  // 探针自身也必须是 DPI 感知的：否则 PowerShell 进程会被**坐标虚拟化**，
  // GetWindowRect 读到的是逻辑像素（1173/1.5=782），与被测进程的物理像素对不上，
  // 看起来就像"程序夹取算错了"（实测踩过，见 ERROR.md E37）。
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  public static readonly IntPtr DPI_PER_MONITOR_AWARE_V2 = new IntPtr(-4);

  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }

  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
    public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
  }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT {
    public uint type; public MOUSEINPUT mi;
  }
  [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint n, INPUT[] inputs, int size);

  const uint INPUT_MOUSE = 0;
  const uint LEFTDOWN = 0x0002;
  const uint LEFTUP   = 0x0004;

  public static void LeftDown() {
    var a = new INPUT[1];
    a[0].type = INPUT_MOUSE; a[0].mi.dwFlags = LEFTDOWN;
    SendInput(1, a, Marshal.SizeOf(typeof(INPUT)));
  }
  public static void LeftUp() {
    var a = new INPUT[1];
    a[0].type = INPUT_MOUSE; a[0].mi.dwFlags = LEFTUP;
    SendInput(1, a, Marshal.SizeOf(typeof(INPUT)));
  }

  public static IntPtr FindByClass(string cls) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      var sb = new StringBuilder(256);
      GetClassNameW(h, sb, 256);
      if (sb.ToString() == cls) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }

  public static string Rect(IntPtr h) {
    RECT r;
    if (!GetWindowRect(h, out r)) return "(GetWindowRect failed)";
    return string.Format("{0},{1} {2}x{3} visible={4} iconic={5}",
      r.Left, r.Top, r.Right - r.Left, r.Bottom - r.Top, IsWindowVisible(h), IsIconic(h));
  }
}
'@
Add-Type -TypeDefinition $code

# 先把自己变成 DPI 感知进程，再量窗口（否则量到的是虚拟化后的逻辑像素）
[void][DP]::SetProcessDpiAwarenessContext([DP]::DPI_PER_MONITOR_AWARE_V2)

$hwnd = [DP]::FindByClass("LearnHelperNativeWnd")
if ($hwnd -eq [IntPtr]::Zero) { Write-Output "NO_WINDOW"; exit 1 }

[void][DP]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 600
Write-Output ("START      : " + [DP]::Rect($hwnd))

# 起始点：标题栏中部（避开右侧按钮）
# NOTE: PowerShell 5.1 不能直接 New-Object 嵌套结构体，必须用 [Type]::new() / New-Object -TypeName
$r = [DP+RECT]::new()
[void][DP]::GetWindowRect($hwnd, [ref]$r)
$sx = [int](($r.Left + $r.Right) / 2)
$sy = $r.Top + 25
Write-Output ("RECT       : {0},{1} {2}x{3}" -f $r.Left, $r.Top, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
Write-Output ("GRAB POINT : $sx,$sy")

[void][DP]::SetCursorPos($sx, $sy)
Start-Sleep -Milliseconds 200
$pt = [DP+POINT]::new()
[void][DP]::GetCursorPos([ref]$pt)
$under = [DP]::WindowFromPoint($pt)
Write-Output ("UNDER CURSOR: hwnd=$under  (target=$hwnd)")

[DP]::LeftDown()
Start-Sleep -Milliseconds 250

$cx = $sx; $cy = $sy
for ($i = 1; $i -le $Steps; $i++) {
  $cx += $Dx; $cy += $Dy
  [void][DP]::SetCursorPos($cx, $cy)
  Start-Sleep -Milliseconds 220
  Write-Output ("STEP {0,-2}    : cursor={1},{2}  window={3}" -f $i, $cx, $cy, [DP]::Rect($hwnd))
}

[DP]::LeftUp()
Start-Sleep -Milliseconds 300
Write-Output ("AFTER UP   : " + [DP]::Rect($hwnd))
