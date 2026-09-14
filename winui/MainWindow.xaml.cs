using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.UI;
using WinRT.Interop;

namespace LearnHelper.App;

public sealed partial class MainWindow : Window
{
    private readonly AcrylicGlassBackdrop? _backdrop;
    private readonly ThemeService _theme;

    /// <summary>
    /// Guards the value-changed / checked handlers while we push persisted settings
    /// INTO the controls. Without it, populating the panel would immediately write
    /// back to disk and could re-enter the apply path.
    /// </summary>
    private bool _loadingUi = true;

    public MainWindow()
    {
        SettingsService.Trace("ctor: begin");
        InitializeComponent();
        SettingsService.Trace("ctor: InitializeComponent done");

        Title = "学习助理";
        AppVersionText.Text = "v" + GetVersion();

        // ---- self-drawn title bar, system window abilities kept ----
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);

        // ---- appearance state first ----
        //
        // ⚠️ ORDER MATTERS. Assigning Window.SystemBackdrop synchronously invokes
        // SystemBackdrop.OnTargetConnected on the backdrop, which reports back through
        // StateReported. That handler needs _theme to already exist - creating the
        // service AFTER attaching the backdrop made the callback throw
        // NullReferenceException, the attach failed, and glass silently never worked
        // (this is exactly what broke "the density slider does nothing" on 2026-09-12).
        _theme = new ThemeService(backdrop: null, SettingsService.Load());
        _theme.Changed += OnThemeChanged;

        // ---- material ----
        if (AcrylicGlassBackdrop.IsSupported)
        {
            var candidate = new AcrylicGlassBackdrop();
            candidate.StateReported += OnMaterialStateReported;
            try
            {
                SystemBackdrop = candidate;
                _backdrop = candidate;
                _theme.AttachMaterial(candidate);
                SettingsService.Trace("material: SystemBackdrop assigned and bound");
            }
            catch (Exception ex)
            {
                // Fallback by design: any of these can decline acrylic. Report to BOTH
                // the on-screen log and the diagnostic file - the on-screen one may not
                // be reachable this early, which previously hid the reason entirely.
                SettingsService.Trace($"material: attach FAILED {ex}");
                AppendLog($"[界面] 挂载玻璃材质失败，已回退一般形式：{ex.Message}");
            }
        }
        else
        {
            SettingsService.Trace("material: IsSupported() == false, using solid background");
            AppendLog("[界面] 本机不支持亚克力材质，已回退一般形式（功能不受影响）。");
        }

        // Record which materials this machine actually hands out. This is the line to
        // read first when "the glass does nothing": a virtual display adapter can kill
        // acrylic while leaving Mica available (memory/01 section 3).
        SettingsService.Trace($"material support: acrylic={AcrylicGlassBackdrop.IsSupported} " +
                              $"mica={AcrylicGlassBackdrop.IsMicaSupported} " +
                              $"attached={_theme.MaterialActive}");

        StyleCaptionButtons();

        // Scriptable overrides: let a verification run drive appearance without input
        // injection (memory/04 section 1 forbids synthetic input).
        if (ApplyEnvironmentOverrides())
        {
            _theme.SaveNow();
            AppendLog($"[外观] 已由环境变量覆盖并写盘：{_theme.Settings.UiMode} / " +
                      $"{_theme.Settings.ThemeMode} / {_theme.Settings.GlassKind} / " +
                      $"卡片={_theme.Settings.CardOpacity:0.00} / tint={_theme.Settings.TintOpacity:0.00}");
        }

        // Defer the first real paint until the tree is loaded.
        //
        // Painting during the constructor is wasted work - the elements created afterwards
        // never receive a colour, and a second paint then runs against a half-built tree.
        // That double-apply is what left the drawers mis-styled and occasionally invisible
        // in the 2026-09-12 captures. One paint, after Loaded, is both correct and cheaper.
        RootGrid.Loaded += (_, _) =>
        {
            try
            {
                ApplyThemeAndSurfaces();
                RefreshStateText();
                SettingsService.Trace("ctor: first paint after Loaded");
            }
            catch (Exception ex)
            {
                _surfaceError = $"{ex.GetType().Name}: {ex.Message}";
                SettingsService.Trace($"ctor: post-Loaded paint failed {ex}");
            }
        };

        LoadSettingsIntoUi();
        _loadingUi = false;
        IsReady = true;
        DumpPanelState("startup");
        SettingsService.Trace("ctor: ui loaded, saving settings");

        // Persist once on startup. Two reasons:
        //  1. "Open settings file" in the panel needs a file to open on a fresh install;
        //  2. it makes the effective defaults visible/inspectable without clicking anything.
        // Values already loaded from disk are written back unchanged.
        _theme.SaveNow();
        SettingsService.Trace("ctor: settings saved; ctor complete");

        Activated += OnActivated;
        Closed += OnClosed;

        AppendLog("[界面] WinUI 3 界面已启动。");

        // ---- Python backend (1.0.4): start it and subscribe to its push channel ----
        // Deferred to after the first paint so the connection log lands in a laid-out
        // log surface; the backend itself is launched off the UI thread.
        RootGrid.Loaded += (_, _) =>
        {
            InitializeBackend();

            // Verification hook (agent cannot click): drive one backend action by name.
            var backendAction = Environment.GetEnvironmentVariable("LH_UI_BACKEND");
            if (!string.IsNullOrWhiteSpace(backendAction))
            {
                DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low,
                    async () =>
                    {
                        await Task.Delay(1500);
                        await RunBackendScriptedActionAsync(backendAction);
                    });
            }
        };

        // Verification hook: open a drawer programmatically so a script can exercise it
        // (the agent cannot click). Harmless in normal runs - the variable is unset.
        var open = Environment.GetEnvironmentVariable("LH_UI_OPEN");
        if (!string.IsNullOrWhiteSpace(open))
        {
            // Deferred to just AFTER the first paint (see the Loaded handler): opening a
            // drawer before the tree is painted left it mis-styled or invisible.
            DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
            {
                SettingsService.Trace($"verify: opening drawer '{open}' programmatically");
                if (open.Trim().Equals("dev", StringComparison.OrdinalIgnoreCase))
                {
                    OnOpenDevModeClick(this, new RoutedEventArgs());
                }
                else
                {
                    OnOpenAppearanceClick(this, new RoutedEventArgs());
                }

                // Drawer CYCLE check: open, then close, and report the settled state.
                //
                // The close path is animation-driven and only collapses the panel from the
                // storyboard's Completed handler - so if that callback never ran, the drawer
                // would become impossible to close. This hook proves the whole round trip
                // without a human clicking (the agent cannot click).
                if (Environment.GetEnvironmentVariable("LH_UI_DRAWER_CYCLE") is { Length: > 0 })
                {
                    var timer = DispatcherQueue.CreateTimer();
                    timer.Interval = TimeSpan.FromMilliseconds(1200);
                    timer.Tick += (_, _) =>
                    {
                        timer.Stop();
                        SettingsService.Trace("cycle: closing drawer now");
                        var isDev = open.Trim().Equals("dev", StringComparison.OrdinalIgnoreCase);
                        if (isDev)
                        {
                            OnDevCloseClick(this, new RoutedEventArgs());
                        }
                        else
                        {
                            OnAppearanceCloseClick(this, new RoutedEventArgs());
                        }

                        // Report AFTER the close animation (150ms) has had time to finish -
                        // a value read immediately would just be an animation frame.
                        var after = DispatcherQueue.CreateTimer();
                        after.Interval = TimeSpan.FromMilliseconds(700);
                        after.Tick += (_, _) =>
                        {
                            after.Stop();
                            var p = isDev ? DevPanel : AppearancePanel;
                            var slide = p.RenderTransform as Microsoft.UI.Xaml.Media.TranslateTransform;
                            SettingsService.Trace(
                                $"cycle: settled visibility={p.Visibility} opacity={p.Opacity:0.00} " +
                                $"x={(slide?.X ?? 0):0}  (expect Collapsed / 1.00 / 0)");
                        };
                        after.Start();
                    };
                    timer.Start();
                }
            });
        }

        // Capture-probe mode: show the marker patch used to prove a screenshot belongs to
        // THIS window, and optionally drive the developer editor to one token + colour so
        // every option can be inspected one at a time.
        if (Environment.GetEnvironmentVariable("LH_UI_PROBE") is { Length: > 0 })
        {
            ProbeMarker.Visibility = Visibility.Visible;
            var token = Environment.GetEnvironmentVariable("LH_UI_PROBE_TOKEN");
            var hex = Environment.GetEnvironmentVariable("LH_UI_PROBE_HEX");
            if (!string.IsNullOrWhiteSpace(token) && !string.IsNullOrWhiteSpace(hex))
            {
                DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
                {
                    _devSelectedToken = token.Trim();
                    HighlightSelectedDevRow();
                    ApplySwatchToSelection(hex.Trim());
                    SettingsService.Trace($"probe: selected {token} and applied {hex}");
                });
            }
        }

        // Stress hook: repeatedly invoke the REAL quick-toggle handler. The user reports a
        // crash on that button; the agent cannot click, so the app clicks itself and the
        // native filter in App.xaml.cs records whether it died hard (native, not managed).
        if (int.TryParse(Environment.GetEnvironmentVariable("LH_UI_STRESS_TOGGLE"), out var toggles)
            && toggles > 0)
        {
            var done = 0;
            var timer = DispatcherQueue.CreateTimer();
            timer.Interval = TimeSpan.FromMilliseconds(200);
            timer.IsRepeating = true;
            timer.Tick += (s, _) =>
            {
                if (done >= toggles)
                {
                    s.Stop();
                    SettingsService.Trace($"stress: finished {done}/{toggles} toggles");
                    return;
                }

                done++;
                SettingsService.Trace($"stress: toggle #{done} begin");
                try
                {
                    OnToggleThemeClick(this, new RoutedEventArgs());
                    SettingsService.Trace($"stress: toggle #{done} ok");
                }
                catch (Exception ex)
                {
                    SettingsService.Trace($"stress: toggle #{done} THREW {ex}");
                    s.Stop();
                }
            };
            timer.Start();
        }
    }

    // =====================================================================
    // material state plumbing
    // =====================================================================

    private void OnMaterialStateReported(string message)
    {
        // Reachable during construction (assigning SystemBackdrop attaches synchronously),
        // so it must tolerate a partially built window.
        _materialStatus = message;
        SettingsService.Trace($"material state: {message}");
        if (IsReady)
        {
            RefreshStateText();
        }
    }

    /// <summary>Set once the constructor has finished wiring the visual tree.</summary>
    private bool IsReady { get; set; }

    /// <summary>Latest honest report from the material layer.</summary>
    private string _materialStatus = "（尚未报告）";

    // =====================================================================
    // appearance plumbing
    // =====================================================================

    private void OnThemeChanged()
    {
        try
        {
            ApplySurfaces();
            SettingsService.Trace($"surfaces: applied (plain={_theme.IsPlainMode})");
        }
        catch (Exception ex)
        {
            // Cosmetic only - never let a brush refresh kill the app.
            _surfaceError = $"{ex.GetType().Name}: {ex.Message}";
            SettingsService.Trace($"surfaces: FAILED {_surfaceError}");
        }

        RefreshStateText();
    }

    /// <summary>Last failure from the surface refresh, surfaced in the self-report.</summary>
    private string? _surfaceError;

    /// <summary>
    /// Coalesces rapid appearance updates.
    ///
    /// A slider drag fires ValueChanged on every tick; re-pushing to the composition
    /// controller each time produced dozens of redundant re-attaches per drag
    /// (observed in ui-diag.log on 2026-09-12). Values are still persisted immediately -
    /// only the expensive apply is deferred.
    /// </summary>
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _applyTimer;

    private void ScheduleApply()
    {
        _applyTimer ??= DispatcherQueue.CreateTimer();
        _applyTimer.Interval = TimeSpan.FromMilliseconds(80);
        _applyTimer.IsRepeating = false;
        _applyTimer.Tick -= OnApplyTimerTick;
        _applyTimer.Tick += OnApplyTimerTick;
        // Start() restarts the countdown, so a continuing drag keeps deferring the
        // apply until the user pauses - that is the behaviour we want.
        _applyTimer.Start();
    }

    private void OnApplyTimerTick(Microsoft.UI.Dispatching.DispatcherQueueTimer sender, object args)
    {
        sender.Stop();
        _theme.ApplyGlass();
        RefreshStateText();
    }

    /// <summary>Theme mode + glass + colours in one call, with failures reported not thrown.</summary>
    private void ApplyThemeAndSurfaces()
    {
        _theme.ApplyThemeMode(RootGrid);
        _theme.ApplyGlass();
        ApplySurfaces();
    }

    /// <summary>
    /// (Re)build every colour token and the window background.
    ///
    /// Called on EVERY appearance change, so the UI repaints immediately instead of
    /// only after a restart (user report 2026-09-12). Two things had to change for that:
    ///   1. colours are computed in code (GlassPalette) rather than left to ThemeResource
    ///      re-resolution, which fought with the brushes injected at runtime;
    ///   2. the material is re-pushed (see ThemeService.ApplyGlass) because the slider
    ///      only affects the composition controller, not the XAML tree.
    /// </summary>
    private void ApplySurfaces()
    {
        var plain = _theme.IsPlainMode || !_theme.MaterialActive;
        var light = _theme.IsLight;

        // In plain mode surfaces are fully opaque by definition: "一般形式" means no
        // translucency at all, so the user's glass slider must not half-apply here.
        // Otherwise clamp into the theme's safe range - light surfaces need a much
        // higher floor or a dark wallpaper darkens them until the text disappears.
        var surfaceOpacity = plain
            ? 1.0
            : GlassPalette.ClampSurfaceOpacity(light, _theme.Settings.CardOpacity);

        var overrides = _theme.Settings.OverridesFor(light);
        SettingsService.Trace(
            $"palette: using {overrides.Count} override(s) for {(light ? "light" : "dark")}: " +
            string.Join(",", overrides.Keys));

        var palette = GlassPalette.Build(light, surfaceOpacity, overrides);
        var missing = 0;
        foreach (var kv in palette)
        {
            if (!Application.Current.Resources.ContainsKey(kv.Key))
            {
                missing++;   // would mean the XAML no longer declares this token
            }

            SetBrush(kv.Key, kv.Value);
        }

        if (missing > 0)
        {
            SettingsService.Trace($"palette: {missing} key(s) were not declared in App.xaml");
        }

        StyleInputControls(palette);

        // The visual-tree repaint is COALESCED rather than done inline.
        //
        // Doing it synchronously here meant one gesture (theme toggle) mutated the resource
        // dictionary, the whole visual tree AND the composition controller in a single pass
        // on the render path. That is the classic shape of a 0xc000027b stowed exception in
        // Microsoft.UI.Xaml.dll - which is exactly what the event log recorded for the
        // "toggle the theme and it dies" report. Deferring the tree walk to the next
        // dispatcher turn keeps each step small and off the current render pass.
        ScheduleTextRepaint();

        // Bars that carry text and sit directly on the material get their own backing.
        // SEMI-transparent, not opaque: it keeps the frosted look (an unbacked title bar
        // was reported as "no glass at the top") while still giving the text enough contrast
        // on a bright wallpaper, which those bars did not have.
        AppTitleBar.Background =
            new SolidColorBrush(WithAlpha(palette["GlassDeepBrush"], 0xE6));
        NoticeBar.Background =
            new SolidColorBrush(WithAlpha(palette["GlassDeepBrush"], 0xE6));

        // The 1px outline has to follow the theme as well. It came from a StaticResource,
        // which resolves to the DARK default (#252C35) once and never updates - so the notice
        // bar kept a black edge in the light theme (user report 2026-09-12). Every other
        // bordered surface is repainted by PaintSurfacesFromPalette; this bar is painted here.
        NoticeBar.BorderBrush = new SolidColorBrush(palette["GlassDividerBrush"]);

        // Brand mark: same StaticResource problem - it stayed on the dark theme's bright cyan.
        if (RootGrid.FindName("BrandMark") is Border brandMark)
        {
            brandMark.Background = new SolidColorBrush(palette["GlassAccentBrush"]);
        }

        // Caption-button glyphs are compositor-drawn and cannot follow the XAML theme, but
        // touching AppWindow on every appearance change is a thread-discipline risk (it is a
        // compositor-side object), so only repaint them when the theme actually changed.
        if (_captionThemeApplied != _theme.IsLight)
        {
            StyleCaptionButtons();
            _captionThemeApplied = _theme.IsLight;
        }

        // Keep the settings drawer's own chrome in step. It follows the app theme, and the
        // quick toggle runs entirely inside the drawer's own handlers, so nothing else
        // refreshes it - leaving the drawer on the previous theme (white boxes and washed-out
        // text right after a toggle).
        if (AppearancePanel?.Visibility == Visibility.Visible)
        {
            ApplyAppearanceChrome();
        }

        // Evidence that a live repaint really happened - assertable from a script
        // without seeing the screen. The colour dump is what makes "the palette is
        // inverted" diagnosable from a log instead of from a guess.
        SettingsService.Trace(
            $"palette[{(_theme.IsLight ? "light" : "dark")}]: applied {palette.Count} tokens " +
            $"(plain={plain}, surfaceOpacity={surfaceOpacity:0.00})");
        SettingsService.Trace("  " + GlassPalette.Describe(palette));

        // Root: transparent in glass mode so the system material shows; a solid fill of
        // its own in plain mode (and as the code-level fallback when no material exists).
        var baseColor = palette["GlassBaseBrush"];
        RootGrid.Background = plain ? NewBrush(baseColor) : null;
    }

    /// <summary>Same colour with a different alpha - used to soften an opaque brush.</summary>
    private static Color WithAlpha(Color c, byte alpha) => Color.FromArgb(alpha, c.R, c.G, c.B);

    /// <summary>Tracks the theme the caption buttons were last painted for.</summary>
    private bool? _captionThemeApplied;

    /// <summary>
    /// Repaint text on the next dispatcher turn instead of inline.
    ///
    /// Keeps a single user gesture from mutating the resource dictionary, the visual tree and
    /// the composition controller all on the current render pass - the known shape of the
    /// 0xc000027b XAML stowed exception. Coalesced so a burst of changes causes one walk.
    /// </summary>
    private void ScheduleTextRepaint()
    {
        if (_textRepaintQueued)
        {
            return;
        }

        _textRepaintQueued = true;
        DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
        {
            _textRepaintQueued = false;
            try
            {
                var light = _theme.IsLight;
                var plain = _theme.IsPlainMode || !_theme.MaterialActive;
                var opacity = plain
                    ? 1.0
                    : GlassPalette.ClampSurfaceOpacity(light, _theme.Settings.CardOpacity);
                PaintTextFromPalette(
                    GlassPalette.Build(light, opacity, _theme.Settings.OverridesFor(light)));
            }
            catch (Exception ex)
            {
                SettingsService.Trace($"text repaint failed: {ex.Message}");
            }
        });
    }

    private bool _textRepaintQueued;

    /// <summary>
    /// Push colours into the CONTROL TEMPLATES that ignore Background/Foreground.
    ///
    /// A WinUI TextBox does not paint the Background you set on it - its template resolves
    /// TextControlBackground / TextControlForeground from the theme dictionaries, so a
    /// light theme produced a BLACK text box with WHITE text while the surrounding UI was
    /// light (user report 2026-09-12).
    ///
    /// THE READ-ONLY SET MATTERS TOO: the log box is IsReadOnly, and the template picks
    /// TextControl*ReadOnly* for that state. Overriding only the normal keys left the
    /// read-only box on the template default, which is why the colours came out exactly
    /// inverted on every hot toggle while a restart (which re-creates the template) looked
    /// correct. Both sets must be pushed.
    /// </summary>
    private void StyleInputControls(Dictionary<string, Color> palette)
    {
        var surface = palette["GlassSurfaceBrush"];
        var text = palette["GlassTextMainBrush"];
        var muted = palette["GlassTextMutedBrush"];

        // The log surface is a plain Border + TextBlock now (no template, no visual states),
        // so setting the colours here IS the rendered result - no theme-resource surprises.
        LogSurface.Background = new SolidColorBrush(surface);
        LogText.Foreground = new SolidColorBrush(text);

        SetControlBrush(PageCombo, "ComboBoxBackground", palette["GlassCardHiBrush"]);
        SetControlBrush(PageCombo, "ComboBoxBackgroundPointerOver", palette["GlassCardHiBrush"]);
        SetControlBrush(PageCombo, "ComboBoxBackgroundPressed", palette["GlassCardHiBrush"]);
        SetControlBrush(PageCombo, "ComboBoxForeground", text);
        SetControlBrush(PageCombo, "ComboBoxForegroundPointerOver", text);
        SetControlBrush(PageCombo, "ComboBoxForegroundPressed", text);
        SetControlBrush(PageCombo, "ComboBoxBorderBrush", palette["GlassDividerBrush"]);
        PageCombo.Foreground = new SolidColorBrush(text);

        SettingsService.Trace(
            $"inputs: want bg={GlassPalette.ToRgbHex(surface)} fg={GlassPalette.ToRgbHex(text)} | " +
            $"LogSurface bg={(LogSurface.Background is SolidColorBrush b2 ? GlassPalette.ToRgbHex(b2.Color) : "(null)")} " +
            $"fg={(LogText.Foreground is SolidColorBrush f2 ? GlassPalette.ToRgbHex(f2.Color) : "(null)")}");
    }

    private static void SetControlBrush(FrameworkElement control, string key, Color color)
    {
        control.Resources[key] = new SolidColorBrush(color);
    }

    /// <summary>
    /// Paint text colours from the palette, driven by the ROLE declared in XAML.
    ///
    /// Two earlier approaches failed, both worth remembering:
    ///   1. trusting the XAML ThemeResource/StaticResource - the injected brushes never
    ///      reached elements that had already resolved them;
    ///   2. "walk everything and snap each colour to the nearest palette value" - snapping is
    ///      LOSSY across theme changes. The first (dark) paint writes the light grey #B8C2CE;
    ///      the light repaint can then only snap that to the nearest LIGHT value, which is the
    ///      divider #C3CBD5, so body text rendered pale grey on a light card
    ///      (measured rgb(195,203,213), nearly invisible).
    ///
    /// Declaring the role in XAML (Tag="textMain" etc.) makes a repaint a plain assignment:
    /// deterministic, idempotent, independent of whatever the previous theme wrote.
    /// Elements without a Tag are left alone - buttons own their labels and the drawers paint
    /// their own chrome.
    /// </summary>
    private void PaintTextFromPalette(Dictionary<string, Color> palette)
    {
        // Role id -> colour, derived from the palette definition so the two cannot drift.
        var byRole = new Dictionary<string, Color>(StringComparer.Ordinal);
        foreach (var token in GlassPalette.Tokens)
        {
            if (palette.TryGetValue(token.Key, out var c))
            {
                byRole[token.Short] = c;
            }
        }

        var painted = 0;

        // The drawers paint their own chrome (Appearance chrome follows the app theme, the
        // developer drawer follows the theme BEING EDITED), so they are excluded here.
        _textPaintSkip.Clear();
        if (AppearancePanel != null)
        {
            _textPaintSkip.Add(AppearancePanel);
        }

        if (DevPanel != null)
        {
            _textPaintSkip.Add(DevPanel);
        }

        Walk(RootGrid);

        void Walk(DependencyObject node)
        {
            if (node == null || _textPaintSkip.Contains(node))
            {
                return;
            }

            // Buttons own their label colour (the accent buttons are white-on-blue).
            if (node is Microsoft.UI.Xaml.Controls.Button)
            {
                return;
            }

            var count = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                Walk(Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(node, i));
            }

            if (node is TextBlock tb && tb.Tag is string role
                && byRole.TryGetValue(role, out var colour))
            {
                tb.Foreground = new SolidColorBrush(colour);
                painted++;
            }
        }

        SettingsService.Trace($"text paint[{( _theme.IsLight ? "light" : "dark")}]: {painted} element(s) by role");

        PaintSurfacesFromPalette(palette);
    }

    /// <summary>Subtrees whose colours are owned elsewhere (the two drawers).</summary>
    private readonly HashSet<DependencyObject> _textPaintSkip = new();

    /// <summary>
    /// Colour every card / bar / button surface imperatively, by name.
    ///
    /// Why not let the XAML do it: the tokens are {StaticResource ...}, which resolves ONCE.
    /// Replacing the brush in Application.Resources afterwards does not reach elements that
    /// already resolved it, so a runtime theme change left every card on the parse-time
    /// default (measured: light theme still rendered rgb(32..48) surfaces). Assigning the
    /// brushes here removes XAML from the equation entirely.
    /// </summary>
    private void PaintSurfacesFromPalette(Dictionary<string, Color> palette)
    {
        var card = palette["GlassCardBrush"];
        var cardHi = palette["GlassCardHiBrush"];
        var deep = palette["GlassDeepBrush"];
        var divider = palette["GlassDividerBrush"];
        var accent = palette["GlassAccentBrush"];
        var accentSoft = palette["GlassAccentSoftBrush"];
        var onAccent = palette["GlassOnAccentBrush"];

        // Cards: (border name, fill key)
        var cards = new (string Name, string Fill)[]
        {
            ("StatusCard", "GlassCardBrush"),
            ("PageCard", "GlassCardBrush"),
            ("KpiCard", "GlassCardBrush"),
            ("ProgressCard", "GlassCardBrush"),
            ("LogCard", "GlassCardBrush"),
        };
        foreach (var (name, fillKey) in cards)
        {
            if (RootGrid.FindName(name) is Border border)
            {
                border.Background = new SolidColorBrush(palette[fillKey]);
                border.BorderBrush = new SolidColorBrush(divider);
            }
        }

        // KPI tiles and the notice pill.
        foreach (var name in new[] { "KpiTile1", "KpiTile2", "KpiTile3" })
        {
            if (RootGrid.FindName(name) is Border tile)
            {
                tile.Background = new SolidColorBrush(cardHi);
            }
        }

        if (RootGrid.FindName("NoticePill") is Border pill)
        {
            pill.Background = new SolidColorBrush(accentSoft);
        }

        // Version pill in the title bar: its soft accent fill must follow the theme too.
        if (RootGrid.FindName("VersionPill") is Border versionPill)
        {
            versionPill.Background = new SolidColorBrush(accentSoft);
        }

        // Solid accent buttons use the FILL token (deep blue + white), not the text accent -
        // one token could not serve both jobs (ERROR.md E24).
        var accentFill = palette["GlassAccentFillBrush"];

        foreach (var name in new[] { "StartButton", "AppearanceApplyButton", "DevApplyButton" })
        {
            if (RootGrid.FindName(name) is Microsoft.UI.Xaml.Controls.Button accentButton)
            {
                // FlatAccentButtonStyle binds straight to these two properties, and its
                // ContentTemplate paints the label white, so what renders here is exactly what
                // is set - no template visual state can swap the label colour per theme.
                //
                // NOTE: the Button's own Foreground property can read back a mid-animation
                // value (observed #464646), which is why the verification reads the LABEL
                // TextBlock's colour instead (see ReportButtonProbe).
                accentButton.Background = new SolidColorBrush(accentFill);
                accentButton.Foreground = new SolidColorBrush(onAccent);
            }
        }

        // The page picker: a ComboBox resolves its own template brushes, so push them all.
        // Same class of problem as the log box (ERROR.md E21/E22) - declarative styling
        // alone does not survive a runtime theme change.
        if (RootGrid.FindName("PageCombo") is Microsoft.UI.Xaml.Controls.ComboBox combo)
        {
            var comboBg = palette["GlassCardHiBrush"];
            var comboFg = palette["GlassTextMainBrush"];
            var comboBorder = palette["GlassDividerBrush"];

            foreach (var key in new[]
                     {
                         "ComboBoxBackground", "ComboBoxBackgroundPointerOver",
                         "ComboBoxBackgroundPressed", "ComboBoxBackgroundDisabled",
                         "ComboBoxBackgroundUnfocused", "ComboBoxItemBackgroundSelected",
                         "ComboBoxItemBackgroundPointerOver",
                         "ComboBoxItemBackgroundSelectedPointerOver",
                     })
            {
                SetControlBrush(combo, key, comboBg);
            }

            foreach (var key in new[]
                     {
                         "ComboBoxForeground", "ComboBoxForegroundPointerOver",
                         "ComboBoxForegroundPressed", "ComboBoxForegroundFocused",
                         "ComboBoxForegroundDisabled", "ComboBoxDropDownGlyphForeground",
                         "ComboBoxDropDownForeground", "ComboBoxItemForeground",
                         "ComboBoxItemForegroundPointerOver", "ComboBoxItemForegroundSelected",
                     })
            {
                SetControlBrush(combo, key, comboFg);
            }

            foreach (var key in new[]
                     {
                         "ComboBoxBorderBrush", "ComboBoxBorderBrushPointerOver",
                         "ComboBoxBorderBrushPressed", "ComboBoxBorderBrushFocused",
                     })
            {
                SetControlBrush(combo, key, comboBorder);
            }

            // No focus ring. Zeroing the thickness properties on the control is NOT enough -
            // the template reads these brushes, which is why startup still painted a white
            // box around it (user report 2026-09-12, item 3).
            var clear = new SolidColorBrush(Colors.Transparent);
            combo.Resources["FocusVisualPrimaryBrush"] = clear;
            combo.Resources["FocusVisualSecondaryBrush"] = clear;
            combo.Resources["FocusVisualPrimaryThickness"] = new Thickness(0);
            combo.Resources["FocusVisualSecondaryThickness"] = new Thickness(0);

            combo.Background = new SolidColorBrush(comboBg);
            combo.Foreground = new SolidColorBrush(comboFg);
            combo.BorderBrush = new SolidColorBrush(comboBorder);
        }

        // Title-bar chrome (sun/moon glyph, gear glyph, the "外观" label) uses the SECONDARY
        // text colour, which is dark in the light theme and light in the dark theme. Set it
        // explicitly rather than relying on the snapping walk.
        PaintTitleBarChrome(palette["GlassTextSubBrush"]);

        // The self-report strip at the bottom sits on the window itself.
        if (RootGrid.FindName("SelfCheckText") is TextBlock selfCheck)
        {
            selfCheck.Foreground = new SolidColorBrush(palette["GlassTextMutedBrush"]);
        }

        ReportSurfaceColors(palette);
        _ = deep;
    }

    /// <summary>Colour every text element inside the self-drawn title bar.</summary>
    private void PaintTitleBarChrome(Color colour)
    {
        var brush = new SolidColorBrush(colour);
        var painted = 0;
        Walk(AppTitleBar);

        void Walk(DependencyObject node)
        {
            var n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < n; i++)
            {
                var child = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(node, i);
                if (child is TextBlock tb)
                {
                    // Glyphs and labels alike; the brand name keeps the main text colour.
                    if (tb.Name != "AppNameText")
                    {
                        tb.Foreground = brush;
                        painted++;
                    }
                }

                Walk(child);
            }
        }

        SettingsService.Trace($"titlebar chrome: {painted} text element(s) -> {GlassPalette.ToRgbHex(colour)}");
    }
    /// <summary>
    /// Report the accent button's real state and the colour of the TextBlock that actually
    /// paints its label (found by walking the button's own visual tree).
    ///
    /// A property readback was not enough here: the button reported white while the light
    /// theme rendered grey rgb(90,102,116). Two things are worth distinguishing and this
    /// covers both - "the button is disabled (opacity/state)" and "some deeper element wins".
    /// </summary>
    private void ReportButtonProbe()
    {
        if (RootGrid.FindName("StartButton") is not Microsoft.UI.Xaml.Controls.Button btn)
        {
            SettingsService.Trace("btn probe: StartButton not found");
            return;
        }

        string Hex(Brush? b) => b is SolidColorBrush s ? GlassPalette.ToRgbHex(s.Color) : "(null)";

        var labelText = "(no TextBlock found)";
        var labelColour = "(unknown)";
        FindText(btn);
        void FindText(DependencyObject node)
        {
            var n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < n; i++)
            {
                var child = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(node, i);
                if (child is TextBlock tb)
                {
                    labelText = tb.Text;
                    labelColour = Hex(tb.Foreground);
                    return;
                }

                FindText(child);
            }
        }

        SettingsService.Trace(
            $"btn probe: enabled={btn.IsEnabled} template={btn.ContentTemplate != null} " +
            $"bg={Hex(btn.Background)} fg={Hex(btn.Foreground)} " +
            $"label='{labelText}' labelFg={labelColour} opacity={btn.Opacity:0.00}");
    }

    /// <summary>
    /// Log the colours actually held by the named surfaces.
    ///
    /// This is the check that survives an occluded window: reading element properties is
    /// independent of whether the window is visible on screen, unlike pixel sampling
    /// (which silently reads whatever window happens to be on top). Screen pixels remain
    /// the final arbiter of "what the user sees", but this narrows a failure to either
    /// "my code assigned the wrong colour" or "something overrode it at render time".
    /// </summary>
    private void ReportSurfaceColors(Dictionary<string, Color> palette)
    {
        string Hex(object? o) => o is SolidColorBrush b ? GlassPalette.ToRgbHex(b.Color) : "(null)";

        var parts = new List<string>();
        foreach (var name in new[]
                 {
                     "StatusCard", "KpiCard", "LogCard", "NoticePill", "VersionPill",
                     "NoticeBar", "BrandMark",
                     "StartButton", "PageCombo",
                 })
        {
            if (RootGrid.FindName(name) is not FrameworkElement el)
            {
                parts.Add($"{name}=MISSING");
                continue;
            }

            var bg = el switch
            {
                Border bd => Hex(bd.Background),
                Microsoft.UI.Xaml.Controls.ComboBox cb => Hex(cb.Background),
                Microsoft.UI.Xaml.Controls.Button bt => Hex(bt.Background),
                _ => "?",
            };
            var fg = el switch
            {
                Border bd2 => Hex(bd2.BorderBrush),
                Microsoft.UI.Xaml.Controls.ComboBox cb2 => Hex(cb2.Foreground),
                Microsoft.UI.Xaml.Controls.Button bt2 => Hex(bt2.Foreground),
                _ => "?",
            };
            parts.Add($"{name}[bg={bg},fg={fg}]");
        }

        SettingsService.Trace($"surfaces[{( _theme.IsLight ? "light" : "dark")}]: " + string.Join(" ", parts));
        ReportButtonProbe();
    }

    /// <summary>
    /// Snap a colour to whichever palette value it currently matches.
    ///
    /// The candidate list MUST be the whole palette, not just the three text tiers.
    /// With only main/sub/muted, a colour could be dragged onto the wrong tier: in the light
    /// theme the secondary text #47535F sits closer to the muted #5A6674 than to anything
    /// else, so every "次要文字" element (the title-bar icons, the "外观" label, the
    /// backdrop status) was rewritten to muted grey - which then rendered nearly invisible
    /// on the light background. Offering every token makes each colour snap to ITSELF.
    /// </summary>
    /// <summary>
    /// Legacy helper kept only for reference: colour snapping is gone from the paint path
    /// because it is lossy across theme changes (see PaintTextFromPalette). Do not reintroduce it.
    /// </summary>
    private static Color Nearest(Color current, IEnumerable<Color> palette)
    {
        var best = current;
        var bestDistance = int.MaxValue;
        foreach (var candidate in palette)
        {
            var d = Math.Abs(current.R - candidate.R)
                    + Math.Abs(current.G - candidate.G)
                    + Math.Abs(current.B - candidate.B);
            if (d < bestDistance)
            {
                bestDistance = d;
                best = candidate;
            }
        }

        return best;
    }

    private static void SetBrush(string key, Color color)
    {
        var brush = NewBrush(color);
        if (Application.Current.Resources.ContainsKey(key))
        {
            Application.Current.Resources[key] = brush;
        }
        else
        {
            Application.Current.Resources.Add(key, brush);
        }
    }

    private static SolidColorBrush NewBrush(Color c) => new(c);

    /// <summary>Push persisted settings into the panel controls without triggering writes.</summary>
    private void LoadSettingsIntoUi()
    {
        var s = _theme.Settings;

        ModeGlassRadio.IsChecked = !_theme.IsPlainMode;
        ModePlainRadio.IsChecked = _theme.IsPlainMode;

        ModeDarkRadio.IsChecked = !_theme.IsLight;
        ModeLightRadio.IsChecked = _theme.IsLight;

        KindThinRadio.IsChecked = s.GlassKind != "base";
        KindBaseRadio.IsChecked = s.GlassKind == "base";

        // Slider bounds follow the theme: the light theme's floor is much higher so the
        // user cannot drag the surfaces into an unreadable state.
        CardOpacitySlider.Minimum = GlassPalette.MinSurfaceOpacityFor(_theme.IsLight);
        CardOpacitySlider.Maximum = GlassPalette.MaxSurfaceOpacityFor(_theme.IsLight);
        // Assigning Minimum can itself clamp the current Value, so set it after the bounds.
        CardOpacitySlider.Value = GlassPalette.ClampSurfaceOpacity(_theme.IsLight, s.CardOpacity);
        TintOpacitySlider.Value = s.TintOpacity;
        LuminositySlider.Value = s.LuminosityOpacity;

        RefreshValueLabels();
        UpdateThemeToggleGlyph();
        UpdateControlAvailability();
        DumpPanelState("after-load");
    }

    /// <summary>
    /// Write the panel's enablement to the diagnostic log.
    ///
    /// The agent cannot click this UI, so "can the style be switched back?" has to be
    /// observable some other way. A control that is silently disabled is invisible in a
    /// crash-free run; this makes it assertable from a script.
    /// </summary>
    private void DumpPanelState(string when)
    {
        SettingsService.Trace(
            $"panel[{when}]: uiMode={_theme.Settings.UiMode} materialActive={_theme.MaterialActive} " +
            $"| styleGlass={ModeGlassRadio.IsEnabled} stylePlain={ModePlainRadio.IsEnabled} " +
            $"| dark={ModeDarkRadio.IsEnabled} light={ModeLightRadio.IsEnabled} " +
            $"| thin={KindThinRadio.IsEnabled} base={KindBaseRadio.IsEnabled} " +
            $"| card={CardOpacitySlider.IsEnabled} tint={TintOpacitySlider.IsEnabled} " +
            $"lum={LuminositySlider.IsEnabled}");
    }

    /// <summary>
    /// Decide which controls are usable.
    ///
    /// ⚠️ The STYLE selector (glass / plain) must ALWAYS stay enabled - disabling it
    /// made the style impossible to switch back, which is exactly the bug the user hit
    /// on 2026-09-12. Only the glass-specific knobs are gated, and they are gated on
    /// the current style (not on material availability), so the panel never presents
    /// a control that cannot do anything.
    /// </summary>
    private void UpdateControlAvailability()
    {
        var glassAvailable = _theme.MaterialActive;
        var glassMode = !_theme.IsPlainMode;

        // Always selectable.
        ModeGlassRadio.IsEnabled = true;
        ModePlainRadio.IsEnabled = true;
        // Glass parameters only matter in glass mode.
        var knobs = glassMode;
        KindThinRadio.IsEnabled = knobs;
        KindBaseRadio.IsEnabled = knobs;
        TintOpacitySlider.IsEnabled = knobs;
        LuminositySlider.IsEnabled = knobs;

        // Surface opacity is meaningful in both styles (in glass mode it is what
        // reveals the material, in plain mode it still controls the surface tone).
        CardOpacitySlider.IsEnabled = true;

        // Say plainly when glass will not actually render, instead of hiding the option.
        GlassUnavailableNote.Visibility = (!glassAvailable && glassMode)
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    private void RefreshValueLabels()
    {
        CardOpacityValue.Text = _theme.Settings.CardOpacity.ToString("0.00");
        TintOpacityValue.Text = _theme.Settings.TintOpacity.ToString("0.00");
        LuminosityValue.Text = _theme.Settings.LuminosityOpacity.ToString("0.00");
    }

    private void UpdateThemeToggleGlyph()
    {
        // E706 = SUN, E708 = MOON. Show the mode you would switch TO.
        // (This was inverted before, so dark mode displayed a sun and the user read it as
        // "it claims light mode but is not" - report 2026-09-12.)
        ThemeToggleGlyph.Text = _theme.IsLight ? "\uE706" : "\uE708";
        ToolTipService.SetToolTip(ThemeToggleButton,
            _theme.IsLight ? "当前浅色，点击切换到深色" : "当前深色，点击切换到浅色");
    }

    private void OnToggleThemeClick(object sender, RoutedEventArgs e)
    {
        _theme.ToggleThemeMode(RootGrid);

        _loadingUi = true;
        ModeDarkRadio.IsChecked = !_theme.IsLight;
        ModeLightRadio.IsChecked = _theme.IsLight;
        RefreshValueLabels();
        _loadingUi = false;

        UpdateThemeToggleGlyph();
        DumpPanelState("quick-theme-toggle");
        AppendLog($"[外观] 快速切换 → {(_theme.IsLight ? "浅色" : "深色")}");
    }

    /// <summary>
    /// Relaunch the app. Appearance changes apply live, so this is only a fallback for
    /// the rare case where a window-level effect needs the window to be recreated
    /// (the system material and the native title bar are the usual suspects).
    /// Settings are saved first so the restart cannot lose them.
    /// </summary>

    private void OnOpenSettingsFileClick(object sender, RoutedEventArgs e)
    {
        try
        {
            // Make sure the file exists so the shell has something to select.
            if (!File.Exists(SettingsService.SettingsPath))
            {
                SettingsService.Save(_theme.Settings);
            }

            Process.Start(new ProcessStartInfo(SettingsService.SettingsPath) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            AppendLog($"[外观] 打开设置文件失败：{ex.Message}");
        }
    }

    /// <summary>
    /// Read appearance overrides from the environment. Intended for verification runs:
    /// it exercises the theme/glass/persistence paths end to end without synthesising
    /// clicks (which memory/04 section 1 forbids).
    /// </summary>
    private bool ApplyEnvironmentOverrides()
    {
        // Read once, remember which variables were actually present, then apply.
        var mode = Environment.GetEnvironmentVariable("LH_UI_MODE");
        var theme = Environment.GetEnvironmentVariable("LH_UI_THEME");
        var kind = Environment.GetEnvironmentVariable("LH_UI_GLASS_KIND");
        var card = Environment.GetEnvironmentVariable("LH_UI_CARD_OPACITY");
        var tint = Environment.GetEnvironmentVariable("LH_UI_TINT_OPACITY");
        var lum = Environment.GetEnvironmentVariable("LH_UI_LUMINOSITY");

        var changed = mode != null || theme != null || kind != null
                      || card != null || tint != null || lum != null;
        if (!changed)
        {
            return false;
        }

        _theme.MutateInPlace(s =>
        {
            if (mode != null && mode.Trim().ToLowerInvariant() is "glass" or "plain")
            {
                s.UiMode = mode.Trim().ToLowerInvariant();
            }

            if (theme != null && theme.Trim().ToLowerInvariant() is "dark" or "light")
            {
                s.ThemeMode = theme.Trim().ToLowerInvariant();
                s.TintColor = s.ThemeMode == "light" ? "#F2F4F7" : "#101418";
                // Mirror the panel behaviour: each theme brings its own luminosity default.
                if (lum == null)
                {
                    s.LuminosityOpacity = GlassPalette.DefaultLuminosity(s.ThemeMode == "light");
                }
            }

            if (kind != null && kind.Trim().ToLowerInvariant() is "thin" or "base")
            {
                s.GlassKind = kind.Trim().ToLowerInvariant();
            }

            if (double.TryParse(card, out var cardValue))
            {
                s.CardOpacity = cardValue;
            }

            if (double.TryParse(tint, out var tintValue))
            {
                s.TintOpacity = tintValue;
            }

            if (double.TryParse(lum, out var lumValue))
            {
                s.LuminosityOpacity = lumValue;
            }
        });

        return true;
    }

    // =====================================================================
    // window plumbing
    // =====================================================================

    private static string GetVersion()
    {
        var asm = Assembly.GetExecutingAssembly();
        var info = asm.GetCustomAttribute<AssemblyInformationalVersionAttribute>();
        if (!string.IsNullOrWhiteSpace(info?.InformationalVersion))
        {
            var v = info!.InformationalVersion;
            var plus = v.IndexOf('+');
            return plus > 0 ? v[..plus] : v;
        }

        return asm.GetName().Version?.ToString(3) ?? "0.0.0";
    }

    /// <summary>
    /// Make the system caption buttons blend with the material instead of punching an
    /// opaque rectangle into the glass (memory/03 section 4).
    /// </summary>
    private void StyleCaptionButtons()
    {
        var hwnd = WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        // Hand the HWND to the theme service: the system title bar is DWM-drawn, so it has
        // to be switched explicitly whenever the theme changes.
        _theme.TitleBarHwnd = hwnd;
        TitleBarTheming.SetDark(hwnd, !_theme.IsLight);

        if (appWindow.TitleBar is { } bar)
        {
            // Follow the theme: dark glyphs on the light title bar, light glyphs on dark.
            var glyph = _theme.IsLight
                ? Color.FromArgb(0xFF, 0x15, 0x20, 0x2B)
                : Color.FromArgb(0xFF, 0xE8, 0xED, 0xF2);
            var glyphInactive = _theme.IsLight
                ? Color.FromArgb(0xFF, 0x6B, 0x76, 0x83)
                : Color.FromArgb(0xFF, 0x8B, 0x96, 0xA5);

            bar.ButtonBackgroundColor = Colors.Transparent;
            bar.ButtonInactiveBackgroundColor = Colors.Transparent;
            bar.ButtonHoverBackgroundColor = _theme.IsLight
                ? Color.FromArgb(0x22, 0x00, 0x00, 0x00)
                : Color.FromArgb(0x33, 0xFF, 0xFF, 0xFF);
            bar.ButtonPressedBackgroundColor = _theme.IsLight
                ? Color.FromArgb(0x33, 0x00, 0x00, 0x00)
                : Color.FromArgb(0x55, 0xFF, 0xFF, 0xFF);
            bar.ButtonForegroundColor = glyph;
            bar.ButtonInactiveForegroundColor = glyphInactive;
            bar.ButtonHoverForegroundColor = glyph;
            bar.ButtonPressedForegroundColor = glyph;
        }
    }

    private void OnActivated(object sender, WindowActivatedEventArgs args)
    {
        // An inactive window should not keep the live blur burning GPU.
        _backdrop?.SetInputActive(args.WindowActivationState != WindowActivationState.Deactivated);
    }

    private void OnClosed(object sender, WindowEventArgs args)
    {
        // Stop the backend (and the browser it owns) with the window: leaving either
        // behind would keep the sandbox Edge profile locked for the next run.
        ShutdownBackend();

        if (_backdrop != null)
        {
            _backdrop.Enabled = false;
            SystemBackdrop = null;
        }
    }

    /// <summary>
    /// State self-report, as required by memory/04 section 2: before trusting any visual
    /// conclusion the probe must state which mode it is actually in.
    ///
    /// IsSupported() only means "the API exists here" - the system can still fall back
    /// silently, so only the user's eyes can confirm the glass itself.
    /// </summary>
    private void RefreshStateText()
    {
        var active = _theme.DescribeActive();
        var surfaceNote = _surfaceError == null ? "正常" : $"⚠ {_surfaceError}";
        SelfCheckText.Text =
            $"外观：{active}　|　材质可用={_theme.MaterialActive}（仅表示 API 可用）" +
            $"　|　材质层回报：{_materialStatus}　|　表面画刷：{surfaceNote}";
        BackdropStatusText.Text = _theme.IsPlainMode
            ? "一般形式"
            : (_theme.MaterialActive ? "玻璃：已挂载" : "玻璃：纯色兜底");
        FlyoutStateText.Text =
            $"当前生效：{active}\n材质层回报：{_materialStatus}\n表面画刷：{surfaceNote}\n" +
            $"设置文件：{SettingsService.SettingsPath}";
    }
}
