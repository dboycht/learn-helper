using System;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Windows.UI;

namespace LearnHelper.App;

/// <summary>
/// Owns the live appearance state: which theme mode is active and what the glass
/// parameters are. Persists through <see cref="SettingsService"/> and pushes changes
/// into the XAML tree and the system backdrop.
///
/// Two separate mechanisms, do not mix them up:
///  - COLOURS  -> resolved by XAML via ThemeDictionaries. Controls must reference them
///                with <c>ThemeResource</c>; a <c>StaticResource</c> reference is
///                resolved once and will NOT follow a runtime theme switch.
///  - GLASS    -> the system backdrop is imperative (DesktopAcrylicKind + opacities),
///                so it is pushed into the controller from here.
/// </summary>
public sealed class ThemeService
{
    private AcrylicGlassBackdrop? _backdrop;

    /// <summary>
    /// Live settings. Assignable so the settings dialog can swap in an editable working
    /// copy for live preview (see MainWindow.Appearance.cs) - on close it either keeps
    /// that copy (applied) or puts the original back (not applied).
    /// </summary>
    public ThemeSettings Settings { get; set; }

    /// <summary>True when the OS actually gave us a material to drive.</summary>
    public bool MaterialActive => _backdrop != null;

    /// <summary>Raised after any appearance change so the shell can refresh its self-report.</summary>
    public event Action? Changed;

    public ThemeService(AcrylicGlassBackdrop? backdrop, ThemeSettings settings)
    {
        _backdrop = backdrop;
        Settings = settings;
    }

    /// <summary>
    /// Bind the material after construction.
    ///
    /// Needed because the backdrop can only be created once the window exists, while the
    /// theme service has to exist BEFORE that (assigning Window.SystemBackdrop fires the
    /// backdrop's callbacks synchronously - see MainWindow's ordering note).
    /// </summary>
    public void AttachMaterial(AcrylicGlassBackdrop backdrop)
    {
        _backdrop = backdrop;
        Changed?.Invoke();
    }

    /// <summary>Apply theme mode + glass parameters to the live tree/window.</summary>
    public void Apply(FrameworkElement root)
    {
        ApplyThemeMode(root);
        ApplyGlass();
        Changed?.Invoke();
    }

    /// <summary>
    /// Same as <see cref="Apply"/> but reports failures instead of rethrowing.
    ///
    /// Appearance is cosmetic: a broken brush update must never take the app down
    /// during startup, and it must leave a readable trace when it does fail.
    /// </summary>
    public bool TryApply(FrameworkElement root, out string? error)
    {
        try
        {
            Apply(root);
            error = null;
            return true;
        }
        catch (Exception ex)
        {
            error = $"{ex.GetType().Name}: {ex.Message}";
            return false;
        }
    }

    /// <summary>HWND of the window whose system title bar must follow the theme.</summary>
    public IntPtr TitleBarHwnd { get; set; }

    public void ApplyThemeMode(FrameworkElement root)
    {
        // RequestedTheme must be set on a FrameworkElement; a Window is not one.
        root.RequestedTheme = IsLight ? ElementTheme.Light : ElementTheme.Dark;
        _backdrop?.SetTheme(IsLight ? SystemBackdropTheme.Light : SystemBackdropTheme.Dark);

        // The system title bar is drawn by DWM and does NOT follow RequestedTheme, so it
        // must be told explicitly on EVERY theme change - otherwise it keeps its dark
        // styling (white text) on a light window (user report 2026-09-12).
        if (TitleBarHwnd != IntPtr.Zero)
        {
            TitleBarTheming.SetDark(TitleBarHwnd, !IsLight);
        }
    }

    public bool IsLight => Settings.ThemeMode == "light";

    /// <summary>Plain mode: conventional opaque UI, no glass anywhere.</summary>
    public bool IsPlainMode => Settings.UiMode == "plain";

    /// <summary>Glass mode is only meaningful when the OS actually gave us a material.</summary>
    public bool GlassActive => MaterialActive && !IsPlainMode;

    public DesktopAcrylicKind Kind =>
        Settings.GlassKind == "base" ? DesktopAcrylicKind.Base : DesktopAcrylicKind.Thin;

    public Color TintColor => ParseHex(Settings.TintColor, Color.FromArgb(0xFF, 0x10, 0x14, 0x18));

    /// <summary>
    /// Push glass parameters into the controller.
    ///
    /// Plain mode disables the controller entirely; otherwise we (re)attach and re-push
    /// every value. Safe to call when no material is available - it then only records
    /// the values for the self-report.
    /// </summary>
    public void ApplyGlass()
    {
        if (_backdrop == null)
        {
            return;
        }

        _backdrop.Enabled = !IsPlainMode;
        if (IsPlainMode)
        {
            return;
        }

        _backdrop.Kind = Kind;
        _backdrop.TintColor = TintColor;
        _backdrop.TintOpacity = Settings.TintOpacity;
        _backdrop.LuminosityOpacity = Settings.LuminosityOpacity;
        _backdrop.Reapply();
    }

    /// <summary>Toggle theme mode only (used by the quick switch on the title bar).</summary>
    public void ToggleThemeMode(FrameworkElement root)
    {
        Settings.ThemeMode = IsLight ? "dark" : "light";
        // Tint and luminosity follow the target theme's own defaults, so the quick switch
        // gives exactly the same result as choosing the mode in the panel. Without the
        // luminosity line the layer kept the other theme's value (seen as 0.00 in testing).
        Settings.TintColor = GlassPalette.BaseHex(IsLight);
        Settings.LuminosityOpacity = GlassPalette.DefaultLuminosity(IsLight);
        Settings.SanitizeInPlace();
        Apply(root);
        SettingsService.Save(Settings);
    }

    public void Update(Action<ThemeSettings> mutate, FrameworkElement root)
    {
        mutate(Settings);
        Settings = SettingsService.Sanitize(Settings);
        Apply(root);
        SettingsService.Save(Settings);
    }

    /// <summary>
    /// Mutate settings in place WITHOUT persisting or applying. Used by scriptable
    /// overrides so a verification run can stage several values and then save once.
    /// </summary>
    public void MutateInPlace(Action<ThemeSettings> mutate)
    {
        mutate(Settings);
        Settings = SettingsService.Sanitize(Settings);
    }

    public void SaveNow() => SettingsService.Save(Settings);

    public void ResetToDefaults(FrameworkElement root)
    {
        Settings = new ThemeSettings();
        Apply(root);
        SettingsService.Save(Settings);
    }

    /// <summary>Human-readable state, surfaced in the self-report line (memory/04 section 2).</summary>
    public string DescribeActive()
    {
        if (IsPlainMode)
        {
            return "一般形式（无玻璃，不透明界面）";
        }

        if (!MaterialActive)
        {
            return "纯色兜底（本机不支持亚克力或挂载失败）";
        }

        return $"{(IsLight ? "浅色" : "深色")} · {Kind} · 卡片不透明度={Settings.CardOpacity:0.00}" +
               $" · TintOpacity={Settings.TintOpacity:0.00}" +
               $" · LuminosityOpacity={Settings.LuminosityOpacity:0.00} · tint={Settings.TintColor}";
    }

    private static Color ParseHex(string hex, Color fallback)
    {
        try
        {
            var h = hex.TrimStart('#');
            if (h.Length == 6)
            {
                return Color.FromArgb(
                    0xFF,
                    Convert.ToByte(h.Substring(0, 2), 16),
                    Convert.ToByte(h.Substring(2, 2), 16),
                    Convert.ToByte(h.Substring(4, 2), 16));
            }
        }
        catch (Exception)
        {
            // fall through to the caller's default
        }

        return fallback;
    }
}
