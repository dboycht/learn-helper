using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace LearnHelper.App;

/// <summary>
/// Appearance settings UI: the settings drawer and the developer colour drawer.
///
/// BOTH ARE NON-MODAL DRAWERS, not dialogs. A ContentDialog dims the whole window with a
/// scrim, which is exactly wrong for a panel whose purpose is retuning the window you are
/// looking at (user report 2026-09-12: "你覆盖了我怎么调整"). The scrim here is nearly
/// transparent and only exists to dismiss on click.
///
/// DRAFT SEMANTIC: edits preview on the window immediately, but nothing is written to disk
/// until "应用并保存" / "应用到界面". Closing without applying reverts the preview, so what
/// you see and what is stored can never disagree.
/// </summary>
public sealed partial class MainWindow
{
    private ThemeSettings? _original;      // settings as of drawer open
    private ThemeSettings? _working;       // live preview copy while the settings drawer is open
    private ThemeSettings? _devDraft;      // developer drawer copy
    private bool _devEditingLight;
    private string? _devSelectedToken;     // token the swatch palette will paint
    private readonly Dictionary<string, TextBox> _devBoxes = new();
    private readonly Dictionary<string, Button> _devRows = new();

    /// <summary>Preset colours offered as one-click chips in the developer drawer.</summary>
    private static readonly string[][] PresetSwatches =
    {
        new[] { "#101418", "#171C22", "#1C222A", "#252C35", "#0B0E12", "#F2F4F7", "#FFFFFF", "#E7EBF0", "#C3CBD5" },
        new[] { "#4CC2FF", "#0D6396", "#1A3A4D", "#DCEEF9", "#3DDC97", "#FFC24B", "#FF6B6B", "#8B96A5", "#5A6674" },
    };

    // =====================================================================
    // settings drawer
    // =====================================================================

    private void OnOpenAppearanceClick(object sender, RoutedEventArgs e)
    {
        if (AppearancePanel.Visibility == Visibility.Visible)
        {
            CloseAppearanceDrawer(apply: false, logIt: false);
            return;
        }

        _original = _theme.Settings.DeepClone();
        _working = _theme.Settings.DeepClone();
        _theme.Settings = _working;

        LoadSettingsIntoUi();
        ApplyAppearanceChrome();
        UpdateDirtyState();
        RefreshStateText();
        ShowDrawer(AppearancePanel, AppearanceScrim);
        SettingsService.Trace("appearance drawer: opened");
    }

    private void OnAppearanceScrimTapped(object sender, TappedRoutedEventArgs e) =>
        CloseAppearanceDrawer(apply: false, logIt: true);

    private void OnAppearanceCloseClick(object sender, RoutedEventArgs e) =>
        CloseAppearanceDrawer(apply: false, logIt: true);

    private void OnAppearanceApplyClick(object sender, RoutedEventArgs e) =>
        CloseAppearanceDrawer(apply: true, logIt: true);

    /// <summary>
    /// One exit path for the settings drawer.
    ///
    /// apply=true  -> persist what the user previewed.
    /// apply=false -> put the stored settings back, so a cancelled session leaves no trace.
    /// </summary>
    private void CloseAppearanceDrawer(bool apply, bool logIt)
    {
        if (_working == null && AppearancePanel.Visibility != Visibility.Visible)
        {
            return;
        }

        if (apply)
        {
            _theme.SaveNow();
            SettingsService.Trace($"appearance: applied and saved ({DescribeSettings(_theme.Settings)})");
            if (logIt)
            {
                AppendLog("[外观] 已应用并保存外观设置");
            }
        }
        else
        {
            if (_original != null)
            {
                _theme.Settings = _original;
                _theme.ApplyThemeMode(RootGrid);
                _theme.ApplyGlass();
                ApplySurfaces();
                _theme.SaveNow();
            }

            SettingsService.Trace("appearance: closed without applying, preview reverted");
            if (logIt)
            {
                AppendLog("[外观] 已关闭（未应用的改动已撤销）");
            }
        }

        _working = null;
        _original = null;

        _loadingUi = true;
        LoadSettingsIntoUi();
        _loadingUi = false;

        UpdateThemeToggleGlyph();
        RefreshStateText();
        ApplyAppearanceChrome();
        HideDrawer(AppearancePanel, AppearanceScrim);
    }

    /// <summary>Any edit marks the draft dirty so "应用并保存" becomes available.</summary>
    private void UpdateDirtyState()
    {
        var dirty = _original != null && _working != null && !SettingsEqual(_original, _working);
        AppearanceApplyButton.IsEnabled = dirty;

        if (!dirty && AppearancePanel?.Visibility == Visibility.Visible)
        {
            // Nothing pending: keep the stored look so the preview is never a lie.
            _theme.SaveNow();
        }
    }

    private static bool SettingsEqual(ThemeSettings a, ThemeSettings b) =>
        a.UiMode == b.UiMode
        && a.ThemeMode == b.ThemeMode
        && a.GlassKind == b.GlassKind
        && Math.Abs(a.CardOpacity - b.CardOpacity) < 1e-6
        && Math.Abs(a.TintOpacity - b.TintOpacity) < 1e-6
        && Math.Abs(a.LuminosityOpacity - b.LuminosityOpacity) < 1e-6
        && a.TintColor == b.TintColor
        && OverridesEqual(a.Overrides, b.Overrides);

    private static bool OverridesEqual(
        Dictionary<string, Dictionary<string, string>> x,
        Dictionary<string, Dictionary<string, string>> y)
    {
        if (x.Count != y.Count)
        {
            return false;
        }

        foreach (var kv in x)
        {
            if (!y.TryGetValue(kv.Key, out var other) || other.Count != kv.Value.Count)
            {
                return false;
            }

            foreach (var entry in kv.Value)
            {
                if (!other.TryGetValue(entry.Key, out var v) || v != entry.Value)
                {
                    return false;
                }
            }
        }

        return true;
    }

    private static string DescribeSettings(ThemeSettings s) =>
        $"{s.UiMode}/{s.ThemeMode}/{s.GlassKind} card={s.CardOpacity:0.00} " +
        $"tint={s.TintOpacity:0.00} lum={s.LuminosityOpacity:0.00} " +
        $"overrides(dark={s.OverridesFor(false).Count},light={s.OverridesFor(true).Count})";

    // =====================================================================
    // drawer show/hide (with slide-in animation)
    // =====================================================================

    /// <summary>
    /// Slide a drawer in.
    ///
    /// Only TranslateTransform.X and Opacity are animated - BOTH are composition properties
    /// that do not touch layout. An earlier version animated Width, which is a layout property
    /// ("dependent animation", needs a layout pass every frame) and did not land reliably: the
    /// panel reported Visibility=Visible / ActualWidth=440 / a plausible rect while the screen
    /// showed a ~20px sliver. The size is now set BEFORE the animation starts, so the worst
    /// case is "no slide", never "no panel".
    /// </summary>
    private void ShowDrawer(Grid panel, Grid scrim)
    {
        var full = ResolveDrawerWidth(panel);
        _drawerWidths[panel.Name] = full;

        // Size and visibility FIRST: the panel is fully usable even if the animation is
        // interrupted or never runs.
        panel.Width = full;
        panel.Visibility = Visibility.Visible;
        scrim.Visibility = Visibility.Visible;

        var slide = EnsureSlideTransform(panel);
        var offset = SlideOffset(panel, full);

        // NOTE: `++dict[key]` reads BEFORE it writes, so a missing key throws
        // KeyNotFoundException. Thrown from a dispatcher callback that becomes a stowed
        // exception (0xc000027b) and kills the process - which is exactly what happened here,
        // and it looked like "the animation crashes the app" purely because the animation code
        // sits after this line. Always seed the key via TryGetValue.
        _drawerAnimToken.TryGetValue(panel.Name, out var previous);
        var token = previous + 1;
        _drawerAnimToken[panel.Name] = token;

        // Optional part switch, so a future animation regression can be bisected without a
        // rebuild: LH_UI_ANIM = off | fade | slide | both (default both).
        var mode = Environment.GetEnvironmentVariable("LH_UI_ANIM")?.Trim().ToLowerInvariant() ?? "both";
        var wantSlide = mode is "slide" or "both";
        var wantFade = mode is "fade" or "both";

        var board = new Microsoft.UI.Xaml.Media.Animation.Storyboard();

        if (wantSlide)
        {
            var move = new Microsoft.UI.Xaml.Media.Animation.DoubleAnimation
            {
                From = offset,
                To = 0,
                Duration = new Duration(TimeSpan.FromMilliseconds(190)),
            };
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTarget(move, slide);
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTargetProperty(move, "X");
            board.Children.Add(move);
        }
        else
        {
            slide.X = 0;
        }

        if (wantFade)
        {
            var fade = new Microsoft.UI.Xaml.Media.Animation.DoubleAnimation
            {
                From = 0.0,
                To = 1.0,
                Duration = new Duration(TimeSpan.FromMilliseconds(150)),
            };
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTarget(fade, panel);
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTargetProperty(fade, "Opacity");
            board.Children.Add(fade);
        }
        else
        {
            panel.Opacity = 1.0;
        }

        if (board.Children.Count > 0)
        {
            board.Completed += (_, _) =>
            {
                // If another open/close started meanwhile, this board is stale - leave the
                // panel exactly where the newer one put it.
                if (_drawerAnimToken[panel.Name] != token)
                {
                    return;
                }

                slide.X = 0;
                panel.Opacity = 1.0;
            };
            board.Begin();
        }

        SettingsService.Trace($"drawer '{panel.Name}': shown, width {full:0}, mode={mode}, slide {offset:0}");

        ReportDrawerLayout(panel);
    }

    /// <summary>Which way a drawer slides in: from its own edge of the window.</summary>
    private static double SlideOffset(Grid panel, double width) =>
        panel.HorizontalAlignment == HorizontalAlignment.Right ? width * 0.30 : -width * 0.30;

    /// <summary>Give the panel a translate transform, reusing an existing one.</summary>
    private static Microsoft.UI.Xaml.Media.TranslateTransform EnsureSlideTransform(Grid panel)
    {
        if (panel.RenderTransform is Microsoft.UI.Xaml.Media.TranslateTransform t)
        {
            return t;
        }

        var fresh = new Microsoft.UI.Xaml.Media.TranslateTransform();
        panel.RenderTransform = fresh;
        return fresh;
    }

    /// <summary>
    /// Log the STEADY layout state (not the moment of opening): a value read right after
    /// Begin() reflects an animation frame, which is how "440 wide" once hid the fact that
    /// nothing was on screen.
    /// </summary>
    private void ReportDrawerLayout(Grid panel)
    {
        DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
        {
            var bounds = panel.TransformToVisual(RootGrid)
                .TransformBounds(new Windows.Foundation.Rect(0, 0, panel.ActualWidth, panel.ActualHeight));
            SettingsService.Trace(
                $"drawer '{panel.Name}': visibility={panel.Visibility} " +
                $"actual={panel.ActualWidth:0}x{panel.ActualHeight:0} " +
                $"rect=({bounds.X:0},{bounds.Y:0},{bounds.Width:0}x{bounds.Height:0}) " +
                $"opacity={panel.Opacity:0.00} x={(panel.RenderTransform as Microsoft.UI.Xaml.Media.TranslateTransform)?.X ?? 0:0}");
        });
    }

    /// <summary>
    /// The width a drawer should occupy. Never returns NaN/0: at the moment the drawer is
    /// first opened the element may not have been laid out yet, so Width/ActualWidth can
    /// still be NaN.
    /// </summary>
    private static double ResolveDrawerWidth(Grid panel)
    {
        var candidates = new[] { panel.Width, panel.ActualWidth, 440.0 };
        foreach (var c in candidates)
        {
            if (!double.IsNaN(c) && !double.IsInfinity(c) && c >= 200)
            {
                return c;
            }
        }

        return 440.0;
    }

    /// <summary>Per-drawer token so a stale animation cannot undo a newer open/close.</summary>
    private readonly Dictionary<string, int> _drawerAnimToken = new();

    private readonly Dictionary<string, double> _drawerWidths = new();

    /// <summary>
    /// Slide a drawer out, then hide it.
    ///
    /// The panel is only collapsed AFTER the animation finishes, otherwise it would vanish
    /// instantly and there would be nothing to animate. The token guard matters: closing and
    /// re-opening quickly used to let the close's Completed handler collapse the panel that
    /// had just been re-opened.
    /// </summary>
    private void HideDrawer(Grid panel, Grid scrim)
    {
        var full = _drawerWidths.TryGetValue(panel.Name, out var w) ? w : ResolveDrawerWidth(panel);
        _drawerWidths[panel.Name] = full;
        panel.Width = full;

        var slide = EnsureSlideTransform(panel);

        // Seed the key first - see the note in ShowDrawer about `++dict[key]`.
        _drawerAnimToken.TryGetValue(panel.Name, out var previous);
        var token = previous + 1;
        _drawerAnimToken[panel.Name] = token;

        var board = new Microsoft.UI.Xaml.Media.Animation.Storyboard();
        var mode = Environment.GetEnvironmentVariable("LH_UI_ANIM")?.Trim().ToLowerInvariant() ?? "both";
        var wantSlide = mode is "slide" or "both";
        var wantFade = mode is "fade" or "both";

        if (wantSlide)
        {
            var move = new Microsoft.UI.Xaml.Media.Animation.DoubleAnimation
            {
                From = slide.X,
                To = SlideOffset(panel, full),
                Duration = new Duration(TimeSpan.FromMilliseconds(150)),
            };
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTarget(move, slide);
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTargetProperty(move, "X");
            board.Children.Add(move);
        }

        if (wantFade)
        {
            var fade = new Microsoft.UI.Xaml.Media.Animation.DoubleAnimation
            {
                From = panel.Opacity,
                To = 0.0,
                Duration = new Duration(TimeSpan.FromMilliseconds(150)),
            };
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTarget(fade, panel);
            Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTargetProperty(fade, "Opacity");
            board.Children.Add(fade);
        }

        board.Completed += (_, _) =>
        {
            if (_drawerAnimToken[panel.Name] != token)
            {
                return;   // a newer open took over; do not hide it
            }

            panel.Visibility = Visibility.Collapsed;
            scrim.Visibility = Visibility.Collapsed;
            slide.X = 0;
            panel.Opacity = 1.0;   // ready for the next open
        };

        if (board.Children.Count > 0)
        {
            board.Begin();
        }
        else
        {
            // Nothing to animate - close immediately.
            panel.Visibility = Visibility.Collapsed;
            scrim.Visibility = Visibility.Collapsed;
        }
    }

    // =====================================================================
    // panel handlers (settings drawer)
    // =====================================================================

    private void OnUiModeChanged(object sender, RoutedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        var wantPlain = ReferenceEquals(sender, ModePlainRadio) && ModePlainRadio.IsChecked == true;
        var want = wantPlain ? "plain" : "glass";
        if (_working.UiMode == want)
        {
            return;
        }

        _working.UiMode = want;
        _theme.ApplyGlass();
        ApplySurfaces();
        UpdateControlAvailability();
        UpdateDirtyState();
        AppendLog($"[外观] 界面样式 → {(wantPlain ? "一般（无玻璃）" : "玻璃")}");
    }

    private void OnThemeModeChanged(object sender, RoutedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        var wantLight = ReferenceEquals(sender, ModeLightRadio) && ModeLightRadio.IsChecked == true;
        var wantMode = wantLight ? "light" : "dark";
        if (_working.ThemeMode == wantMode)
        {
            return;
        }

        _working.ThemeMode = wantMode;
        _working.TintColor = GlassPalette.BaseHex(wantLight);
        // Each theme has its own sensible luminosity default (light 0.25 / dark 0.35).
        _working.LuminosityOpacity = GlassPalette.DefaultLuminosity(wantLight);
        _working.SanitizeInPlace();      // re-clamp the surface opacity for the new theme

        _theme.ApplyThemeMode(RootGrid);
        _theme.ApplyGlass();
        ApplySurfaces();

        // Surface-opacity bounds differ per theme, so refresh the slider bounds too.
        _loadingUi = true;
        LoadSettingsIntoUi();
        _loadingUi = false;

        UpdateThemeToggleGlyph();
        UpdateDirtyState();
        DumpPanelState($"theme-mode->{wantMode}");
        ApplyAppearanceChrome();      // the drawer follows the theme being previewed
        ApplyDevChrome();
        AppendLog($"[外观] 主题模式 → {(wantLight ? "浅色" : "深色")}");
    }

    private void OnGlassKindChanged(object sender, RoutedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        var wantBase = ReferenceEquals(sender, KindBaseRadio) && KindBaseRadio.IsChecked == true;
        var kind = wantBase ? "base" : "thin";
        if (_working.GlassKind == kind)
        {
            return;
        }

        _working.GlassKind = kind;
        _theme.ApplyGlass();
        UpdateDirtyState();
        AppendLog($"[外观] 玻璃材质 → {(wantBase ? "Base" : "Thin")}");
    }

    private void OnCardOpacityChanged(object sender,
        Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        _working.CardOpacity = e.NewValue;
        ApplySurfaces();
        RefreshValueLabels();
        UpdateDirtyState();
    }

    private void OnTintOpacityChanged(object sender,
        Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        _working.TintOpacity = e.NewValue;
        RefreshValueLabels();
        UpdateDirtyState();
        ScheduleApply();
    }

    private void OnLuminosityChanged(object sender,
        Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        if (_loadingUi || _working == null)
        {
            return;
        }

        _working.LuminosityOpacity = e.NewValue;
        RefreshValueLabels();
        UpdateDirtyState();
        ScheduleApply();
    }

    private void OnResetThemeClick(object sender, RoutedEventArgs e)
    {
        if (_working == null)
        {
            return;
        }

        // Reset the DRAFT: one single way for a change to become permanent (the apply button).
        var defaults = new ThemeSettings();
        _working.UiMode = defaults.UiMode;
        _working.ThemeMode = defaults.ThemeMode;
        _working.GlassKind = defaults.GlassKind;
        _working.CardOpacity = defaults.CardOpacity;
        _working.TintOpacity = defaults.TintOpacity;
        _working.LuminosityOpacity = defaults.LuminosityOpacity;
        _working.TintColor = defaults.TintColor;
        _working.Overrides.Clear();

        _theme.ApplyThemeMode(RootGrid);
        _theme.ApplyGlass();
        ApplySurfaces();

        _loadingUi = true;
        LoadSettingsIntoUi();
        _loadingUi = false;

        UpdateThemeToggleGlyph();
        UpdateDirtyState();
        AppendLog("[外观] 已把面板重置为默认值（点「应用并保存」生效）");
    }

    /// <summary>
    /// Relaunch the app. Appearance changes apply live, so this is only a fallback for the
    /// rare case where a window-level effect needs the window to be recreated (the system
    /// material and the native title bar are the usual suspects). Settings are saved first.
    /// </summary>
    private void OnRestartAppClick(object sender, RoutedEventArgs e)
    {
        _theme.SaveNow();
        SettingsService.Trace("restart: requested by user");

        try
        {
            Microsoft.Windows.AppLifecycle.AppInstance.Restart(string.Empty);
            return;
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"restart: AppInstance.Restart failed ({ex.Message}), falling back");
        }

        try
        {
            var exe = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(exe))
            {
                System.Diagnostics.Process.Start(
                    new System.Diagnostics.ProcessStartInfo(exe) { UseShellExecute = true });
                Application.Current.Exit();
                return;
            }
        }
        catch (Exception ex)
        {
            SettingsService.Trace($"restart: fallback failed {ex.Message}");
        }

        AppendLog("[外观] 重启失败，请手动关闭并重新打开应用。");
    }

    // =====================================================================
    // developer drawer
    // =====================================================================

    private void OnOpenDevModeClick(object sender, RoutedEventArgs e)
    {
        _devDraft = _theme.Settings.DeepClone();
        _devEditingLight = _theme.IsLight;
        _devSelectedToken = "textMain";

        DevDarkRadio.IsChecked = !_devEditingLight;
        DevLightRadio.IsChecked = _devEditingLight;

        BuildSwatchPalette();
        RebuildDevTokenList();
        RefreshDevPreview();
        ShowDrawer(DevPanel, DevScrim);
        SettingsService.Trace(
            $"dev drawer: opened (editing {(_devEditingLight ? "light" : "dark")}, " +
            $"chrome bg={(_devEditingLight ? "#F4F6F9/#12181F" : "#161B22/#F2F6FA")})");

        // Verification: exercise the exact path a swatch click takes, then report the
        // resulting palette. Without this the chip handler could be dead and nothing in a
        // headless run would show it (that is precisely how "点色块没用" slipped through).
        if (Environment.GetEnvironmentVariable("LH_UI_DEV_SELFTEST") is { Length: > 0 })
        {
            DispatcherQueue.TryEnqueue(() =>
            {
                _devSelectedToken = "textMain";
                ApplySwatchToSelection("#FF00AA");
                var after = CurrentDevPalette()["GlassTextMainBrush"];
                SettingsService.Trace(
                    $"dev selftest: swatch click -> textMain={GlassPalette.ToRgbHex(after)} " +
                    $"(override stored={_devDraft?.OverridesFor(_devEditingLight).GetValueOrDefault("textMain") ?? "(none)"})");
            });
        }
    }

    private void OnDevScrimTapped(object sender, TappedRoutedEventArgs e) =>
        CloseDevDrawer(apply: false, logIt: true);

    private void OnDevCloseClick(object sender, RoutedEventArgs e) =>
        CloseDevDrawer(apply: false, logIt: true);

    private void OnDevApplyClick(object sender, RoutedEventArgs e) =>
        CloseDevDrawer(apply: true, logIt: true);

    /// <summary>Same single-exit rule as the settings drawer.</summary>
    private void CloseDevDrawer(bool apply, bool logIt)
    {
        if (apply && _devDraft != null)
        {
            var target = _theme.Settings;
            target.Overrides = _devDraft.Overrides;
            target.SanitizeInPlace();
            _theme.ApplyGlass();
            ApplySurfaces();
            _theme.SaveNow();

            var counts = $"dark={target.OverridesFor(false).Count},light={target.OverridesFor(true).Count}";
            SettingsService.Trace($"dev: palette applied ({counts})");
            if (logIt)
            {
                AppendLog($"[外观·开发者] 已应用配色（{counts}）");
            }
        }
        else
        {
            SettingsService.Trace("dev: closed without applying");
            if (logIt)
            {
                AppendLog("[外观·开发者] 已关闭（未应用的改动已撤销）");
            }
        }

        _devDraft = null;
        RefreshStateText();
        HideDrawer(DevPanel, DevScrim);
    }

    private void OnDevRevertClick(object sender, RoutedEventArgs e)
    {
        if (_devDraft == null)
        {
            return;
        }

        _devDraft = _theme.Settings.DeepClone();
        RebuildDevTokenList();
        RefreshDevPreview();
        AppendLog("[外观·开发者] 已撤销未应用的改动");
    }

    private void OnDevThemeChanged(object sender, RoutedEventArgs e)
    {
        if (_devDraft == null)
        {
            return;
        }

        var wantLight = ReferenceEquals(sender, DevLightRadio) && DevLightRadio.IsChecked == true;
        if (wantLight == _devEditingLight)
        {
            return;
        }

        _devEditingLight = wantLight;
        RebuildDevTokenList();
        RefreshDevPreview();
    }

    private void OnDevResetThemeClick(object sender, RoutedEventArgs e)
    {
        if (_devDraft == null)
        {
            return;
        }

        _devDraft.OverridesFor(_devEditingLight).Clear();
        RebuildDevTokenList();
        RefreshDevPreview();
        AppendLog($"[外观·开发者] 已清空{(_devEditingLight ? "浅色" : "深色")}主题的自定义色（点「应用到界面」生效）");
    }

    /// <summary>
    /// Build the one-click colour chips.
    ///
    /// A hex box alone turned out to be useless in practice for the user, so the primary
    /// way to change a colour is: tap a token row, then tap a chip.
    /// </summary>
    private void BuildSwatchPalette()
    {
        DevSwatchRow1.Items.Clear();
        DevSwatchRow2.Items.Clear();

        var rows = new[] { DevSwatchRow1, DevSwatchRow2 };
        for (var r = 0; r < PresetSwatches.Length && r < rows.Length; r++)
        {
            foreach (var hex in PresetSwatches[r])
            {
                var color = GlassPalette.TryParseHex(hex) ?? Colors.Gray;

                // A Button, NOT a bare Border: a Border only receives taps where it has a
                // background, and Tapped inside an ItemsControl is unreliable - that is why
                // clicking the chips did nothing (user report 2026-09-12). A Button is
                // hit-testable by construction and gives a hover state for free.
                var chip = new Button
                {
                    Width = 32,
                    Height = 30,
                    Padding = new Thickness(0),
                    BorderThickness = new Thickness(0),
                    Background = new SolidColorBrush(Colors.Transparent),
                    IsTabStop = false,
                    FocusVisualPrimaryThickness = new Thickness(0),
                    FocusVisualSecondaryThickness = new Thickness(0),
                    Content = new Border
                    {
                        Width = 26,
                        Height = 26,
                        CornerRadius = new CornerRadius(6),
                        Background = new SolidColorBrush(color),
                        BorderThickness = new Thickness(1),
                        BorderBrush = new SolidColorBrush(Color.FromArgb(0x66, 0xFF, 0xFF, 0xFF)),
                    },
                };
                ToolTipService.SetToolTip(chip, $"点击应用到选中的颜色项（{hex}）");

                var captured = hex;
                chip.Click += (_, _) => ApplySwatchToSelection(captured);
                rows[r].Items.Add(chip);
            }
        }
    }

    /// <summary>
    /// Apply a preset colour to whichever token row is selected.
    ///
    /// Mutates the DRAFT directly and refreshes explicitly. It previously just assigned
    /// TextBox.Text, so picking a colour that was already in the box fired no TextChanged
    /// and nothing happened - the other half of "点色块没用".
    /// </summary>
    private void ApplySwatchToSelection(string hex)
    {
        if (_devDraft == null || string.IsNullOrEmpty(_devSelectedToken))
        {
            SettingsService.Trace("dev swatch: ignored (no draft or no selected token)");
            return;
        }

        var token = GlassPalette.Tokens.FirstOrDefault(t => t.Short == _devSelectedToken);
        if (token == null)
        {
            return;
        }

        var parsed = GlassPalette.TryParseHex(hex);
        if (parsed == null)
        {
            return;
        }

        _devDraft.OverridesFor(_devEditingLight)[token.Short] = GlassPalette.ToRgbHex(parsed.Value);
        _devDraft.SanitizeInPlace();

        if (_devBoxes.TryGetValue(token.Short, out var box) && box.Text != hex)
        {
            box.Text = hex;   // keeps the row's own handler in sync
        }

        SettingsService.Trace($"dev swatch: {token.Short} <- {hex}");
        RebuildDevTokenList();
        RefreshDevPreview();
    }

    /// <summary>One row per token: label + hex box + contrast note + swatch.</summary>
    private void RebuildDevTokenList()
    {
        if (_devDraft == null)
        {
            return;
        }

        _devBoxes.Clear();
        _devRows.Clear();
        DevTokenList.Children.Clear();

        var overrides = _devDraft.OverridesFor(_devEditingLight);
        var palette = CurrentDevPalette();
        ApplyDevChrome();
        var ui = ThemeChrome(_devEditingLight);

        foreach (var token in GlassPalette.Tokens)
        {
            palette.TryGetValue(token.Key, out var effective);
            var hex = GlassPalette.ToRgbHex(effective);

            var row = new Grid
            {
                Padding = new Thickness(6, 3, 6, 3),
                Background = new SolidColorBrush(Colors.Transparent),
            };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(118) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(94) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(30) });

            var label = new TextBlock
            {
                Text = token.Label,
                FontSize = 12,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = new SolidColorBrush(ui.Text),
            };
            Grid.SetColumn(label, 0);

            var box = new TextBox
            {
                Text = hex,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 12,
                Width = 86,
                Padding = new Thickness(6, 3, 6, 3),
                Background = new SolidColorBrush(ui.Surface),
                Foreground = new SolidColorBrush(ui.Text),
                BorderBrush = new SolidColorBrush(ui.Divider),
            };
            // A TextBox template resolves its own theme brushes, so the explicit values above
            // are not enough - push them into the control's resources too.
            box.Resources["TextControlBackground"] = new SolidColorBrush(ui.Surface);
            box.Resources["TextControlBackgroundPointerOver"] = new SolidColorBrush(ui.Surface);
            box.Resources["TextControlBackgroundFocused"] = new SolidColorBrush(ui.Surface);
            box.Resources["TextControlForeground"] = new SolidColorBrush(ui.Text);
            box.Resources["TextControlForegroundPointerOver"] = new SolidColorBrush(ui.Text);
            box.Resources["TextControlForegroundFocused"] = new SolidColorBrush(ui.Text);
            Grid.SetColumn(box, 1);

            var contrast = new TextBlock
            {
                FontSize = 11,
                Margin = new Thickness(8, 0, 0, 0),
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = new SolidColorBrush(ui.Muted),
            };
            Grid.SetColumn(contrast, 2);

            var swatch = new Border
            {
                Width = 24,
                Height = 24,
                CornerRadius = new CornerRadius(5),
                BorderThickness = new Thickness(1),
                BorderBrush = new SolidColorBrush(ui.Divider),
                Background = new SolidColorBrush(effective),
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetColumn(swatch, 3);

            var captured = token;
            box.TextChanged += (_, _) => OnDevHexChanged(captured, box, swatch, contrast);
            box.LostFocus += (_, _) => OnDevHexChanged(captured, box, swatch, contrast);
            box.GotFocus += (_, _) => SelectDevToken(captured);
            swatch.Tapped += (_, _) => SelectDevToken(captured);

            row.Children.Add(label);
            row.Children.Add(box);
            row.Children.Add(contrast);
            row.Children.Add(swatch);

            // Wrap in a Button: it is hit-testable across the whole row (a Grid with a
            // transparent background is not), so "select the token I clicked" actually works.
            var rowButton = new Button
            {
                Padding = new Thickness(0),
                BorderThickness = new Thickness(0),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Background = new SolidColorBrush(Colors.Transparent),
                IsTabStop = false,
                FocusVisualPrimaryThickness = new Thickness(0),
                FocusVisualSecondaryThickness = new Thickness(0),
                Content = row,
            };
            rowButton.Click += (_, _) => SelectDevToken(captured);

            DevTokenList.Children.Add(rowButton);

            _devBoxes[token.Short] = box;
            _devRows[token.Short] = rowButton;

            UpdateDevRow(captured, effective, box, swatch, contrast, overrides.ContainsKey(token.Short));
        }

        HighlightSelectedDevRow();
    }

    private void SelectDevToken(GlassPalette.Token token)
    {
        _devSelectedToken = token.Short;
        HighlightSelectedDevRow();
    }

    private void HighlightSelectedDevRow()
    {
        var ui = ThemeChrome(_devEditingLight);
        foreach (var kv in _devRows)
        {
            var selected = kv.Key == _devSelectedToken;
            kv.Value.Background = new SolidColorBrush(selected
                ? Color.FromArgb(0x55, ui.Accent.R, ui.Accent.G, ui.Accent.B)
                : Colors.Transparent);
        }

        var label = GlassPalette.Tokens.FirstOrDefault(t => t.Short == _devSelectedToken)?.Label;
        DevSelectedText.Text = string.IsNullOrEmpty(label)
            ? "① 点一行选中颜色项　② 点下面的色块应用（或直接改色值）"
            : $"已选中：【{label}】—— 点下面的色块即可套用，也可直接改色值";
    }

    /// <summary>Validate one hex box, update swatch/preview and mirror into the draft.</summary>
    private void OnDevHexChanged(GlassPalette.Token token, TextBox box, Border swatch,
                                 TextBlock contrast)
    {
        if (_devDraft == null)
        {
            return;
        }

        var parsed = GlassPalette.TryParseHex(box.Text);
        if (parsed == null)
        {
            box.BorderBrush = new SolidColorBrush(Color.FromArgb(0xFF, 0xC0, 0x39, 0x2B));
            contrast.Text = "格式应为 #RRGGBB";
            contrast.Foreground = new SolidColorBrush(Color.FromArgb(0xFF, 0xC0, 0x39, 0x2B));
            return;
        }

        box.BorderBrush = null;
        _devDraft.OverridesFor(_devEditingLight)[token.Short] = GlassPalette.ToRgbHex(parsed.Value);
        _devDraft.SanitizeInPlace();

        CurrentDevPalette().TryGetValue(token.Key, out var effective);
        UpdateDevRow(token, effective, box, swatch, contrast, true);
        RefreshDevPreview();
    }

    private void UpdateDevRow(GlassPalette.Token token, Color effective, TextBox box,
                              Border swatch, TextBlock contrast, bool isOverridden)
    {
        swatch.Background = new SolidColorBrush(effective);

        var (text, ok) = DescribeContrast(token, effective);
        var ui = ThemeChrome(_devEditingLight);
        contrast.Text = text;
        contrast.Foreground = ok
            ? new SolidColorBrush(ui.Muted)
            : new SolidColorBrush(ui.Warn);

        if (isOverridden)
        {
            swatch.BorderBrush = new SolidColorBrush(ui.Accent);   // marks a customized token
        }
    }

    /// <summary>
    /// Contrast note for a token, measured against the surface it actually sits on - so a
    /// bad edit is obvious immediately instead of after a user complaint.
    /// </summary>
    private (string Text, bool Ok) DescribeContrast(GlassPalette.Token token, Color color)
    {
        var palette = CurrentDevPalette();

        switch (token.Short)
        {
            case "textMain":
            case "textSub":
            case "textMuted":
            {
                var bg = palette.TryGetValue("GlassSurfaceBrush", out var s)
                    ? s
                    : Color.FromArgb(0xFF, 0x17, 0x1C, 0x22);
                var r = GlassPalette.ContrastRatio(color, bg);
                return ($"对文本框底 {r:0.00}:1 {(r >= 4.5 ? "✓" : "⚠ 偏低")}", r >= 4.5);
            }

            case "onAccent":
            {
                var bg = palette.TryGetValue("GlassAccentBrush", out var a)
                    ? a
                    : Color.FromArgb(0xFF, 0x4C, 0xC2, 0xFF);
                var r = GlassPalette.ContrastRatio(color, bg);
                return ($"对强调色底 {r:0.00}:1 {(r >= 4.5 ? "✓" : "⚠ 偏低")}", r >= 4.5);
            }

            case "accent":
            {
                var onCard = palette.TryGetValue("GlassCardBrush", out var c)
                    ? c
                    : Color.FromArgb(0xFF, 0x17, 0x1C, 0x22);
                var r = GlassPalette.ContrastRatio(color, onCard);
                return ($"对卡片 {r:0.00}:1 {(r >= 4.5 ? "✓" : "⚠ 偏低")}", r >= 4.5);
            }

            default:
                return ("—", true);
        }
    }

    /// <summary>Palette for the theme being edited, with the draft overrides applied.</summary>
    private Dictionary<string, Color> CurrentDevPalette() =>
        GlassPalette.Build(
            _devEditingLight,
            GlassPalette.ClampSurfaceOpacity(_devEditingLight, _devDraft?.CardOpacity ?? 0.85),
            _devDraft?.OverridesFor(_devEditingLight));

    /// <summary>Recompute both preview boxes and the summary contrast text.</summary>
    private void RefreshDevPreview()
    {
        if (_devDraft == null)
        {
            return;
        }

        var draft = CurrentDevPalette();
        var live = GlassPalette.Build(
            _devEditingLight,
            GlassPalette.ClampSurfaceOpacity(_devEditingLight, _theme.Settings.CardOpacity),
            _theme.Settings.OverridesFor(_devEditingLight));

        PaintPreview(draft, DevPreviewDraft, DevDraftPill, DevDraftPillText,
                     DevDraftMain, DevDraftSub, DevDraftLog);
        PaintPreview(live, DevPreviewLive, DevLivePill, DevLivePillText,
                     DevLiveMain, DevLiveSub, DevLiveLog);

        var mainRatio = GlassPalette.ContrastRatio(
            draft["GlassTextMainBrush"], draft["GlassSurfaceBrush"]);
        var subRatio = GlassPalette.ContrastRatio(
            draft["GlassTextSubBrush"], draft["GlassSurfaceBrush"]);
        var onAccent = GlassPalette.ContrastRatio(
            draft["GlassOnAccentBrush"], draft["GlassAccentBrush"]);

        var warn = mainRatio < 4.5 || subRatio < 4.5 || onAccent < 4.5;
        var ui = ThemeChrome(_devEditingLight);
        DevContrastText.Text =
            $"正文/文本框底 {mainRatio:0.00}:1　次要/文本框底 {subRatio:0.00}:1　" +
            $"按钮文字/强调色 {onAccent:0.00}:1　" + (warn ? "⚠ 有项目低于 4.5:1" : "全部达标 ✓");
        DevContrastText.Foreground = new SolidColorBrush(warn ? ui.Warn : ui.Muted);
    }

    private void PaintPreview(Dictionary<string, Color> p, Border panel, Border pill,
                              TextBlock pillText, TextBlock main, TextBlock sub, Border logBox)
    {
        panel.Background = new SolidColorBrush(p["GlassCardBrush"]);
        panel.BorderBrush = new SolidColorBrush(p["GlassDividerBrush"]);
        pill.Background = new SolidColorBrush(p["GlassAccentSoftBrush"]);
        pillText.Foreground = new SolidColorBrush(p["GlassAccentBrush"]);
        main.Foreground = new SolidColorBrush(p["GlassTextMainBrush"]);
        sub.Foreground = new SolidColorBrush(p["GlassTextSubBrush"]);
        logBox.Background = new SolidColorBrush(p["GlassSurfaceBrush"]);
    }

    private static Brush Brush(string key) =>
        Application.Current.Resources.TryGetValue(key, out var value) && value is Brush brush
            ? brush
            : new SolidColorBrush(Colors.Transparent);

    /// <summary>
    /// Paint the settings drawer with the SAME rule as the developer drawer:
    /// dark theme -> dark surface + light text; light theme -> light surface + dark text.
    ///
    /// The drawer previously took its background from GlassCardBrush (a translucent card)
    /// while its text came from the theme's text brushes. Those two are independent, so a
    /// light card surface could end up carrying light text - the panel looked blank (user
    /// report 2026-09-12). Binding both to one chrome removes that whole class of bug.
    /// </summary>
    private void ApplyAppearanceChrome()
    {
        var c = ThemeChrome(_theme.IsLight);
        var surface = new SolidColorBrush(c.Background);
        var text = new SolidColorBrush(c.Text);
        var muted = new SolidColorBrush(c.Muted);

        AppearancePanel.Background = surface;

        foreach (var tb in new[]
                 {
                     AppTitleText, AppUiModeLabel, AppThemeModeLabel, AppCardOpacityLabel,
                     AppGlassKindLabel, AppTintOpacityLabel, AppLuminosityLabel,
                 })
        {
            tb.Foreground = text;
        }

        foreach (var tb in new[]
                 {
                     AppUiModeHint, AppCardOpacityHint, AppTintOpacityHint, AppLuminosityHint,
                     AppApplyHint, FlyoutStateText,
                 })
        {
            tb.Foreground = muted;
        }

        GlassUnavailableNote.Foreground = new SolidColorBrush(c.Warn);
        AppearancePanelFooter.BorderBrush = new SolidColorBrush(c.Divider);

        // RadioButton does not honour Foreground through its template, and its little circle is
        // drawn with its own theme brushes - on a light drawer both came out white (the "white
        // box" report). Lightweight styling: override the template keys on the control itself.
        var stroke = new SolidColorBrush(c.Muted);
        var fill = new SolidColorBrush(c.Surface);
        var check = new SolidColorBrush(c.Accent);
        foreach (var rb in new[]
                 {
                     ModeGlassRadio, ModePlainRadio, ModeDarkRadio, ModeLightRadio,
                     KindThinRadio, KindBaseRadio,
                 })
        {
            rb.Foreground = text;
            rb.Resources["RadioButtonForeground"] = text;
            rb.Resources["RadioButtonForegroundPointerOver"] = text;
            rb.Resources["RadioButtonForegroundPressed"] = text;
            rb.Resources["RadioButtonForegroundDisabled"] = muted;

            rb.Resources["RadioButtonOuterEllipseStroke"] = stroke;
            rb.Resources["RadioButtonOuterEllipseStrokePointerOver"] = check;
            rb.Resources["RadioButtonOuterEllipseStrokePressed"] = check;
            rb.Resources["RadioButtonOuterEllipseFill"] = fill;
            rb.Resources["RadioButtonOuterEllipseFillPointerOver"] = fill;
            rb.Resources["RadioButtonOuterEllipseFillPressed"] = fill;
            rb.Resources["RadioButtonOuterEllipseCheckedStroke"] = check;
            rb.Resources["RadioButtonOuterEllipseCheckedFill"] = check;
            rb.Resources["RadioButtonCheckGlyphFill"] = fill;
            rb.Resources["RadioButtonCheckGlyphStroke"] = check;
        }

        SettingsService.Trace(
            $"appearance chrome: {(_theme.IsLight ? "light" : "dark")} " +
            $"bg={GlassPalette.ToRgbHex(c.Background)} text={GlassPalette.ToRgbHex(c.Text)}");
    }

    /// <summary>
    /// The drawer chrome for a theme, by the simplest possible rule:
    ///
    ///   dark  -> dark background,  white-ish text
    ///   light -> light background, black-ish text
    ///
    /// It follows the theme BEING EDITED (not the app's live theme) and is applied to the
    /// drawer background, every label, the hex boxes and the preview panels, so nothing in
    /// the editor can end up unreadable. These colours are deliberately NOT part of the
    /// editable token list.
    /// </summary>
    private sealed record Chrome(
        Color Background, Color Surface, Color Text, Color Muted, Color Accent, Color Divider,
        Color Warn);

    private static Chrome ThemeChrome(bool light) => light
        ? new Chrome(
            Background: GlassPalette.FromHex("#F4F6F9"),
            Surface: GlassPalette.FromHex("#FFFFFF"),
            Text: GlassPalette.FromHex("#12181F"),
            Muted: GlassPalette.FromHex("#516071"),
            Accent: GlassPalette.FromHex("#0D6396"),
            Divider: GlassPalette.FromHex("#C3CBD5"),
            Warn: GlassPalette.FromHex("#B45309"))
        : new Chrome(
            Background: GlassPalette.FromHex("#161B22"),
            Surface: GlassPalette.FromHex("#0E1216"),
            Text: GlassPalette.FromHex("#F2F6FA"),
            Muted: GlassPalette.FromHex("#AEBAC8"),
            Accent: GlassPalette.FromHex("#4CC2FF"),
            Divider: GlassPalette.FromHex("#2C333C"),
            Warn: GlassPalette.FromHex("#E8B44B"));

    /// <summary>Repaint every piece of the developer drawer for the theme being edited.</summary>
    private void ApplyDevChrome()
    {
        var c = ThemeChrome(_devEditingLight);

        DevPanel.Background = new SolidColorBrush(c.Background);

        // Fixed labels in the drawer header.
        foreach (var tb in new[] { DevTitleText, DevEditLabel, DevPreviewDraftCaption, DevPreviewLiveCaption })
        {
            tb.Foreground = new SolidColorBrush(c.Text);
        }

        DevSelectedText.Foreground = new SolidColorBrush(c.Muted);
        DevContrastText.Foreground = new SolidColorBrush(c.Muted);

        // Theme selector: RadioButton has no plain Foreground path through the template on
        // all WinUI versions, so set the control-level resource too.
        foreach (var rb in new[] { DevDarkRadio, DevLightRadio })
        {
            rb.Foreground = new SolidColorBrush(c.Text);
            rb.Resources["RadioButtonForeground"] = new SolidColorBrush(c.Text);
            rb.Resources["RadioButtonForegroundPointerOver"] = new SolidColorBrush(c.Text);
            rb.Resources["RadioButtonForegroundPressed"] = new SolidColorBrush(c.Text);
        }

        // The swatch strip sits on a tinted band so its own boundary is visible.
        DevSwatchBand.Background = new SolidColorBrush(
            Color.FromArgb(0x22, c.Accent.R, c.Accent.G, c.Accent.B));

        // Preview captions are handled above; the preview boxes get painted per palette in
        // RefreshDevPreview, which is always called right after this.
    }
}
