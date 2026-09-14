using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace LearnHelper.App;

/// <summary>
/// Appearance settings for the glass UI, persisted next to the executable.
///
/// Kept deliberately small and flat so it can be hand-edited when diagnosing a
/// "the glass looks wrong" report.
/// </summary>
public sealed class ThemeSettings
{
    /// <summary>
    /// "glass" shows the WinUI glass style; "plain" is the conventional look with no
    /// translucency at all (user-requested 2026-09-12).
    /// </summary>
    public string UiMode { get; set; } = "glass";

    /// <summary>"dark" or "light".</summary>
    public string ThemeMode { get; set; } = "dark";

    /// <summary>"thin" or "base" (maps to DesktopAcrylicKind).</summary>
    public string GlassKind { get; set; } = "thin";

    /// <summary>
    /// Opacity of the card surfaces, 0.80 - 1.00.
    ///
    /// The floor is not arbitrary: below ~0.77 the weakest text tier drops under WCAG
    /// 4.5:1 once a wallpaper shows through the (also translucent) window tint, which
    /// reads to users as "the colours are inverted". See GlassPalette.MinSurfaceOpacity
    /// for the derivation, and contrast_check.py for the check that enforces it.
    /// </summary>
    public double CardOpacity { get; set; } = 0.85;

    /// <summary>0.0 - 0.95. Higher = the window's own tint covers more of the desktop.</summary>
    public double TintOpacity { get; set; } = 0.55;

    /// <summary>
    /// Luminosity layer opacity. Seeded per theme (light 0.25 / dark 0.35 - see
    /// GlassPalette.DefaultLuminosity), so the material reads as glass from the very first
    /// run. The Windows default of 0.8 washes the blur into a flat fill.
    /// </summary>
    public double LuminosityOpacity { get; set; } = GlassPalette.DefaultLuminosityDark;

    /// <summary>Tint colour as #RRGGBB (per theme mode this gets overridden by tokens).</summary>
    public string TintColor { get; set; } = "#101418";

    /// <summary>Extra dimming layer drawn by us when the system material is unavailable.</summary>
    public double FallbackDim { get; set; } = 0.0;

    /// <summary>
    /// Developer-mode colour overrides: theme name ("dark"/"light") -> token short id
    /// ("accent", "textMain", ...) -> "#RRGGBB".
    ///
    /// Only the RGB is taken from here; the palette keeps its computed alpha, so a
    /// hand-tuned colour can never accidentally reintroduce the unreadable-surface bug.
    /// Cleared by "恢复默认".
    /// </summary>
    public Dictionary<string, Dictionary<string, string>> Overrides { get; set; } = new();

    /// <summary>Overrides for one theme; never null, always editable in place.</summary>
    public Dictionary<string, string> OverridesFor(bool light)
    {
        var key = light ? "light" : "dark";
        if (!Overrides.TryGetValue(key, out var set) || set == null)
        {
            set = new Dictionary<string, string>();
            Overrides[key] = set;
        }

        return set;
    }

    /// <summary>Validate/clamp this instance in place (used by the developer editor).</summary>
    public void SanitizeInPlace()
    {
        var clean = SettingsService.Sanitize(this);
        UiMode = clean.UiMode;
        ThemeMode = clean.ThemeMode;
        GlassKind = clean.GlassKind;
        CardOpacity = clean.CardOpacity;
        TintOpacity = clean.TintOpacity;
        LuminosityOpacity = clean.LuminosityOpacity;
        FallbackDim = clean.FallbackDim;
        TintColor = clean.TintColor;
        Overrides = clean.Overrides;
    }

    public ThemeSettings Clone() => (ThemeSettings)MemberwiseClone();

    /// <summary>Deep copy (including the override maps) so the editor never aliases live state.</summary>
    public ThemeSettings DeepClone()
    {
        var copy = (ThemeSettings)MemberwiseClone();
        copy.Overrides = new Dictionary<string, Dictionary<string, string>>();
        foreach (var kv in Overrides)
        {
            copy.Overrides[kv.Key] = new Dictionary<string, string>(kv.Value);
        }

        return copy;
    }
}

public static class SettingsService
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };

    /// <summary>
    /// Append a line to ui-diag.log beside the exe.
    ///
    /// Needed because the agent cannot see this window (no screenshot, no input
    /// injection - see ERROR.md E10): a startup crash would otherwise be invisible.
    /// Never throws.
    /// </summary>
    public static void Trace(string message)
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "ui-diag.log");
            // Keep it bounded: this file is a diagnostic aid, not a runtime log.
            if (File.Exists(path) && new FileInfo(path).Length > 1_000_000)
            {
                File.Delete(path);
            }

            File.AppendAllText(path, $"{DateTime.Now:HH:mm:ss.fff} {message}{Environment.NewLine}");
        }
        catch (Exception)
        {
            // diagnostics must never be the reason something fails
        }
    }

    /// <summary>
    /// Settings live beside the exe so an unpackaged self-contained build stays portable.
    /// (ApplicationData.Current would tie us to a package identity we deliberately do not have.)
    /// </summary>
    public static string SettingsPath => Path.Combine(
        AppContext.BaseDirectory, "ui-settings.json");

    public static ThemeSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                var loaded = JsonSerializer.Deserialize<ThemeSettings>(json, Options);
                if (loaded != null)
                {
                    return Sanitize(loaded);
                }
            }
        }
        catch (Exception)
        {
            // A corrupt settings file must never block startup: fall back to defaults.
        }

        return new ThemeSettings();
    }

    public static void Save(ThemeSettings settings)
    {
        try
        {
            var json = JsonSerializer.Serialize(Sanitize(settings), Options);
            File.WriteAllText(SettingsPath, json);
        }
        catch (Exception)
        {
            // Persisting is best-effort; the live UI already reflects the change.
        }
    }

    /// <summary>Clamp everything into the range the controllers actually accept.</summary>
    public static ThemeSettings Sanitize(ThemeSettings s)
    {
        s.UiMode = string.Equals(s.UiMode, "plain", StringComparison.OrdinalIgnoreCase)
            ? "plain" : "glass";
        s.ThemeMode = string.Equals(s.ThemeMode, "light", StringComparison.OrdinalIgnoreCase)
            ? "light" : "dark";
        s.GlassKind = string.Equals(s.GlassKind, "base", StringComparison.OrdinalIgnoreCase)
            ? "base" : "thin";
        // Theme-aware: a translucent light surface over a dark wallpaper swallows the
        // text, so the light theme's floor is deliberately much higher.
        s.CardOpacity = GlassPalette.ClampSurfaceOpacity(s.ThemeMode == "light", s.CardOpacity);
        s.TintOpacity = Math.Clamp(s.TintOpacity, 0.0, 0.95);
        s.LuminosityOpacity = Math.Clamp(s.LuminosityOpacity, 0.0, 1.0);
        s.FallbackDim = Math.Clamp(s.FallbackDim, 0.0, 0.9);
        if (string.IsNullOrWhiteSpace(s.TintColor) || s.TintColor[0] != '#')
        {
            s.TintColor = "#101418";
        }

        // Drop unparsable / empty override entries rather than letting them reach the
        // palette (a bad hex there would silently fall back to the default colour).
        s.Overrides ??= new Dictionary<string, Dictionary<string, string>>();
        foreach (var themeKey in s.Overrides.Keys.ToList())
        {
            var set = s.Overrides[themeKey] ?? new Dictionary<string, string>();
            s.Overrides[themeKey] = set
                .Where(kv => GlassPalette.TryParseHex(kv.Value) != null)
                .ToDictionary(kv => kv.Key, kv => kv.Value);
        }

        return s;
    }
}
