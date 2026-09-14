using System;
using Microsoft.UI.Composition;              // ICompositionSupportsSystemBackdrop
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace LearnHelper.App;

/// <summary>
/// Tunable acrylic backdrop.
///
/// The XAML convenience class <c>DesktopAcrylicBackdrop</c> exposes NO parameters
/// (memory/03 section 3), so to control the glass density we must host a
/// <c>DesktopAcrylicController</c> ourselves and feed it a backdrop configuration.
///
/// Verdict rules learned the hard way:
/// - The root container MUST be <c>Background="Transparent"</c>; any high-opacity
///   dark fill covers the material completely (memory/03 section 4).
/// - A returning S_OK from any setup call only means "accepted", NOT "rendered".
///   The system silently falls back to a solid color on virtual display adapters,
///   battery saver, disabled transparency or high contrast. Always keep a code-level
///   solid fallback and have the user confirm the effect visually.
/// </summary>
public static class TitleBarTheming
{
    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, uint attr, ref int value, int size);

    /// <summary>
    /// Switch the SYSTEM title bar between dark and light.
    ///
    /// This is a DWM attribute, so the OS draws it - it does NOT follow XAML's
    /// RequestedTheme and must be re-applied on every theme change. Missing that re-apply
    /// is why the title bar kept its dark styling (white text) while the rest of the
    /// window had gone light (user report 2026-09-12).
    /// </summary>
    public static void SetDark(IntPtr hwnd, bool dark)
    {
        var value = dark ? 1 : 0;
        foreach (var attr in new uint[] { 20, 19 })   // 20: Win11, 19: older builds
        {
            try
            {
                if (DwmSetWindowAttribute(hwnd, attr, ref value, sizeof(int)) == 0)
                {
                    return;
                }
            }
            catch (Exception)
            {
                // try the next attribute id
            }
        }
    }
}

public sealed class AcrylicGlassBackdrop : SystemBackdrop
{
    private DesktopAcrylicController? _controller;
    private SystemBackdropConfiguration? _configuration;
    private ICompositionSupportsSystemBackdrop? _target;

    /// <summary>
    /// Raised when the material is attached to (or detached from) a composition target.
    /// Without this the shell has no way to tell the difference between "damage
    /// happened" and "OnTargetConnected never fired" - and that difference is exactly
    /// what makes a slider feel dead.
    /// </summary>
    public event Action<string>? StateReported;

    /// <summary>
    /// Set false to run in "plain" mode: the material is torn down and the shell paints
    /// its own opaque background instead. Also used as the fallback path.
    /// </summary>
    public bool Enabled
    {
        get => _enabled;
        set
        {
            if (_enabled == value)
            {
                return;
            }

            _enabled = value;
            if (!value)
            {
                Teardown();
                StateReported?.Invoke("材质已关闭（一般形式）");
            }
            else if (_target != null)
            {
                Attach();
            }
        }
    }

    private bool _enabled = true;


    /// <summary>Thin is the most see-through; Base is denser.</summary>
    public DesktopAcrylicKind Kind { get; set; } = DesktopAcrylicKind.Thin;

    public Windows.UI.Color TintColor { get; set; } = Windows.UI.Color.FromArgb(0xFF, 0x10, 0x14, 0x18);

    /// <summary>0..1. Higher = more of the window's own tint, less desktop showing.</summary>
    public double TintOpacity { get; set; } = 0.55;

    /// <summary>
    /// Luminosity layer opacity. Keep at 0: the default 0.8 washes the blur into a
    /// flat fill so it no longer reads as glass (memory/03 section 4).
    /// </summary>
    public double LuminosityOpacity { get; set; }

    public static bool IsSupported => DesktopAcrylicController.IsSupported();

    /// <summary>
    /// Mica samples the DESKTOP WALLPAPER rather than the windows behind us, so it is a
    /// different capability from acrylic and can survive conditions that disable it
    /// (observed on 2026-09-12: a virtual display adapter killed acrylic while Mica
    /// stayed available). Useful as a second-choice material.
    /// </summary>
    public static bool IsMicaSupported => MicaController.IsSupported();

    /// <summary>
    /// Push the current property values into the live controller.
    ///
    /// Needed because the properties on this class are plain CLR properties: assigning
    /// them does NOT notify the composition controller. Without this the sliders in the
    /// theme panel would appear to do nothing.
    /// </summary>
    public void Reapply()
    {
        if (_controller == null)
        {
            // Not attached yet (or material disabled): try to attach if we now should.
            if (_enabled && _target != null)
            {
                Attach();
            }

            return;
        }

        _controller.Kind = Kind;
        _controller.TintColor = TintColor;
        _controller.TintOpacity = (float)TintOpacity;
        _controller.LuminosityOpacity = (float)LuminosityOpacity;
        _controller.FallbackColor = TintColor;
        StateReported?.Invoke(
            $"参数已下发 {Kind} · TintOpacity={TintOpacity:0.00} · Luminosity={LuminosityOpacity:0.00}");
    }

    protected override void OnTargetConnected(ICompositionSupportsSystemBackdrop connectedTarget, XamlRoot xamlRoot)
    {
        base.OnTargetConnected(connectedTarget, xamlRoot);

        _target = connectedTarget;
        Attach();
    }

    /// <summary>Create the controller and bind it to the current target.</summary>
    private void Attach()
    {
        if (!_enabled || _controller != null || _target == null)
        {
            return;
        }

        try
        {
            _controller = new DesktopAcrylicController
            {
                Kind = Kind,
                TintColor = TintColor,
                // DesktopAcrylicController takes float, not double (CS0266 otherwise).
                TintOpacity = (float)TintOpacity,
                LuminosityOpacity = (float)LuminosityOpacity,
                FallbackColor = TintColor,
            };

            _configuration = new SystemBackdropConfiguration
            {
                IsInputActive = true,
                Theme = SystemBackdropTheme.Dark,
            };

            _controller.AddSystemBackdropTarget(_target);
            _controller.SetSystemBackdropConfiguration(_configuration);

            // An honest report: this only means the plumbing succeeded, NOT that the
            // system actually rendered the material (it can still fall back silently).
            StateReported?.Invoke(
                $"已挂载 {Kind} · TintOpacity={TintOpacity:0.00} · Luminosity={LuminosityOpacity:0.00}");
        }
        catch (Exception ex)
        {
            _controller?.Dispose();
            _controller = null;
            _configuration = null;
            StateReported?.Invoke($"挂载失败，已回退纯色：{ex.Message}");
        }
    }

    protected override void OnTargetDisconnected(ICompositionSupportsSystemBackdrop disconnectedTarget)
    {
        base.OnTargetDisconnected(disconnectedTarget);
        _target = null;
        Teardown();
    }

    /// <summary>
    /// Keep the material in sync with window activation: an inactive window should
    /// render the "inactive" variant instead of burning GPU on the live blur.
    /// </summary>
    public void SetInputActive(bool active)
    {
        if (_configuration != null)
        {
            _configuration.IsInputActive = active;
        }
    }

    public void SetTheme(SystemBackdropTheme theme)
    {
        if (_configuration != null)
        {
            _configuration.Theme = theme;
        }
    }

    private void Teardown()
    {
        if (_controller != null)
        {
            if (_target != null)
            {
                _controller.RemoveSystemBackdropTarget(_target);
                _target = null;
            }

            _controller.Dispose();
            _controller = null;
        }

        _configuration = null;
    }
}
