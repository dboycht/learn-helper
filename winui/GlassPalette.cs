using System;
using System.Collections.Generic;
using Windows.UI;

namespace LearnHelper.App;

/// <summary>
/// The colour tokens, computed in code.
///
/// WHY THIS EXISTS (and why it is not just XAML ThemeDictionaries):
/// every token must repaint the moment the user changes a setting. Relying on
/// ThemeResource re-resolution fought with the brushes this app injects at runtime,
/// so theme switches only took full effect after a restart (user report 2026-09-12).
/// Making code the single source of truth removes the ambiguity: one place computes
/// colours, one place pushes them into the resource dictionary, and the UI repaints
/// immediately.
///
/// The hex values here MUST stay in step with winui/contrast_check.py, which audits
/// both themes against WCAG and cross-checks these exact literals for drift.
/// Re-run that script after touching anything below.
///
/// NOTE: keep this file written by a UTF-8 tool (the editor), never round-tripped
/// through PowerShell Set-Content - that adds a BOM and can mangle non-ASCII text
/// (rules/03 section 4).
/// </summary>
public static class GlassPalette
{
    /// <summary>
    /// Build the full token set.
    /// </summary>
    /// <param name="light">Light theme when true.</param>
    /// <param name="surfaceOpacity">
    /// 0.30 - 1.00 for the card / text-box surfaces. In glass mode this is what lets the
    /// system material show through; in plain mode the caller passes 1.0.
    /// </param>
    /// <param name="overrides">
    /// Optional per-theme hex overrides from the developer theme editor, keyed by the
    /// token's <see cref="Token.Short"/> id. Overriding a colour does not bypass the
    /// opacity rules - the caller still clamps the surface opacity first.
    /// </param>
    public static Dictionary<string, Color> Build(
        bool light, double surfaceOpacity, IReadOnlyDictionary<string, string>? overrides = null)
    {
        var a = ToAlpha(surfaceOpacity);
        var hi = ToAlpha(Math.Min(1.0, surfaceOpacity + 0.06));
        var map = new Dictionary<string, Color>();

        if (light)
        {
            // Accent is #0D6396, not the dark theme's #4CC2FF: measured against the light
            // surfaces #4CC2FF is ~1.9:1 and even #0A84C8 only reaches 4.08:1, both failing
            // WCAG AA for text. #0D6396 gives 6.48 / 5.88 / 6.48.
            Add(map, "GlassBaseBrush", "#F2F4F7", 0xFF);
            Add(map, "GlassDeepBrush", "#E7EBF0", 0xFF);
            Add(map, "GlassCardBrush", "#FFFFFF", a);
            // The log / text-box surface stays fully opaque: this is where text is READ,
            // and a translucent light surface over a dark wallpaper was exactly what made
            // the log read as "the colours are inverted" (user report 2026-09-12).
            Add(map, "GlassSurfaceBrush", "#FFFFFF", 0xFF);
            Add(map, "GlassCardHiBrush", "#FFFFFF", hi);
            Add(map, "GlassDividerBrush", "#C3CBD5", 0xFF);
            Add(map, "GlassAccentBrush", "#0D6396", 0xFF);
            // In the light theme the accent text colour is already dark enough to double as a
            // button fill, so both tokens hold the same value here.
            Add(map, "GlassAccentFillBrush", "#0D6396", 0xFF);
            Add(map, "GlassAccentSoftBrush", "#DCEEF9", 0xFF);
            Add(map, "GlassOnAccentBrush", "#FFFFFF", 0xFF);
            Add(map, "GlassTextMainBrush", "#15202B", 0xFF);
            Add(map, "GlassTextSubBrush", "#47535F", 0xFF);
            Add(map, "GlassTextMutedBrush", "#5A6674", 0xFF);
            Add(map, "GlassSuccessBrush", "#0F8A5F", 0xFF);
            Add(map, "GlassWarnBrush", "#9A6600", 0xFF);
            Add(map, "GlassDangerBrush", "#C0392B", 0xFF);
            Add(map, "GlassLogBrush", "#FFFFFF", 0xFF);
        }
        else
        {
            Add(map, "GlassBaseBrush", "#101418", 0xFF);
            Add(map, "GlassDeepBrush", "#0B0E12", 0xFF);
            Add(map, "GlassCardBrush", "#171C22", a);
            // Fully opaque for the same reason as the light theme (see above).
            Add(map, "GlassSurfaceBrush", "#171C22", 0xFF);
            Add(map, "GlassCardHiBrush", "#1C222A", hi);
            Add(map, "GlassDividerBrush", "#252C35", 0xFF);
            // Accent split into TWO tokens on purpose.
            //
            // It used to be one key doing two jobs, which breaks down here: as bright cyan
            // (#4CC2FF) it is excellent for accent TEXT/ICONS, but bad as a button FILL
            // (dark label on light blue reads as washed out - user report 2026-09-12, even
            // though it measured 9.37:1). As a deep blue (#0A6CA8) it is the conventional
            // button fill with white text (5.64:1), but useless as text (2.85:1).
            // So: accent = text/icons, accentFill = solid fills.
            Add(map, "GlassAccentBrush", "#4CC2FF", 0xFF);
            Add(map, "GlassAccentFillBrush", "#0A6CA8", 0xFF);
            Add(map, "GlassAccentSoftBrush", "#1A3A4D", 0xFF);
            Add(map, "GlassOnAccentBrush", "#FFFFFF", 0xFF);
            Add(map, "GlassTextMainBrush", "#E8EDF2", 0xFF);
            Add(map, "GlassTextSubBrush", "#B8C2CE", 0xFF);
            Add(map, "GlassTextMutedBrush", "#8B96A5", 0xFF);
            Add(map, "GlassSuccessBrush", "#3DDC97", 0xFF);
            Add(map, "GlassWarnBrush", "#FFC24B", 0xFF);
            Add(map, "GlassDangerBrush", "#FF6B6B", 0xFF);
            Add(map, "GlassLogBrush", "#171C22", 0xFF);
        }

        // Developer-mode overrides are applied ON TOP of the theme defaults.
        ApplyOverrides(map, overrides);

        // Then make sure no override (or hand-edited settings file) produced unreadable text.
        // This enforces the user's rule automatically: light background -> dark text.
        map["GlassTextMainBrush"] = EnsureReadable(map["GlassTextMainBrush"], map["GlassSurfaceBrush"]);
        map["GlassTextSubBrush"] = EnsureReadable(map["GlassTextSubBrush"], map["GlassSurfaceBrush"]);
        map["GlassTextMutedBrush"] = EnsureReadable(map["GlassTextMutedBrush"], map["GlassSurfaceBrush"], 3.0);
        // Text drawn on cards (headings) must also stay readable.
        map["GlassTextMainBrush"] = EnsureReadable(map["GlassTextMainBrush"], map["GlassCardBrush"]);
        // Button label sits on the accent fill.
        map["GlassOnAccentBrush"] = EnsureReadable(map["GlassOnAccentBrush"], map["GlassAccentBrush"]);

        return map;
    }

    /// <summary>
    /// Default luminosity-layer opacity per theme.
    ///
    /// Light needs a lower value or the material reads as a flat white sheet; the user
    /// settled on 0.25 for light and 0.35 for dark (2026-09-12).
    /// </summary>
    public const double DefaultLuminosityLight = 0.25;
    public const double DefaultLuminosityDark = 0.35;

    public static double DefaultLuminosity(bool light) =>
        light ? DefaultLuminosityLight : DefaultLuminosityDark;

    /// <summary>
    /// Display metadata for one token: resource key, a stable short id used by the
    /// developer theme editor, and the Chinese label shown in the UI.
    /// </summary>
    public sealed record Token(string Key, string Short, string Label);

    /// <summary>Every token, in the order the developer editor should list them.</summary>
    public static readonly Token[] Tokens =
    {
        new("GlassBaseBrush", "base", "窗口底色"),
        new("GlassDeepBrush", "deep", "深色底/公告栏"),
        new("GlassCardBrush", "card", "卡片"),
        new("GlassCardHiBrush", "cardHi", "强调块"),
        new("GlassSurfaceBrush", "surface", "文本框/日志底"),
        new("GlassDividerBrush", "divider", "分隔线/描边"),
        new("GlassAccentBrush", "accent", "强调色（文字/图标）"),
        new("GlassAccentFillBrush", "accentFill", "强调填充（按钮底）"),
        new("GlassAccentSoftBrush", "accentSoft", "强调淡底/胶囊"),
        new("GlassOnAccentBrush", "onAccent", "强调底上的文字"),
        new("GlassTextMainBrush", "textMain", "正文文字"),
        new("GlassTextSubBrush", "textSub", "次要文字"),
        new("GlassTextMutedBrush", "textMuted", "弱化文字"),
        new("GlassSuccessBrush", "success", "成功/绿"),
        new("GlassWarnBrush", "warn", "警告/黄"),
        new("GlassDangerBrush", "danger", "危险/红"),
        new("GlassLogBrush", "log", "日志区底色"),
    };

    /// <summary>
    /// Replace the RGB of each overridden token, KEEPING the computed alpha.
    ///
    /// Keeping alpha is deliberate: the developer editor changes hues, not the opacity
    /// policy - otherwise a stray edit could make the text unreadable again.
    /// </summary>
    private static void ApplyOverrides(
        Dictionary<string, Color> map, IReadOnlyDictionary<string, string>? overrides)
    {
        if (overrides == null || overrides.Count == 0)
        {
            return;
        }

        foreach (var token in Tokens)
        {
            if (!overrides.TryGetValue(token.Short, out var hex) || string.IsNullOrWhiteSpace(hex))
            {
                continue;
            }

            var rgb = TryParseHex(hex);
            if (rgb == null || !map.TryGetValue(token.Key, out var current))
            {
                continue;
            }

            map[token.Key] = Color.FromArgb(current.A, rgb.Value.R, rgb.Value.G, rgb.Value.B);
        }
    }

    /// <summary>Parse "#RRGGBB" / "RRGGBB" / "#AARRGGBB"; null when unparsable.</summary>
    public static Color? TryParseHex(string? hex)
    {
        if (string.IsNullOrWhiteSpace(hex))
        {
            return null;
        }

        var h = hex.Trim().TrimStart('#');
        try
        {
            if (h.Length == 6)
            {
                return Color.FromArgb(
                    0xFF,
                    Convert.ToByte(h.Substring(0, 2), 16),
                    Convert.ToByte(h.Substring(2, 2), 16),
                    Convert.ToByte(h.Substring(4, 2), 16));
            }

            if (h.Length == 8)
            {
                return Color.FromArgb(
                    Convert.ToByte(h.Substring(0, 2), 16),
                    Convert.ToByte(h.Substring(2, 2), 16),
                    Convert.ToByte(h.Substring(4, 2), 16),
                    Convert.ToByte(h.Substring(6, 2), 16));
            }
        }
        catch (Exception)
        {
            // fall through
        }

        return null;
    }

    /// <summary>"#RRGGBB" for a colour (alpha dropped - the editor edits hues).</summary>
    public static string ToRgbHex(Color c) => $"#{c.R:X2}{c.G:X2}{c.B:X2}";

    /// <summary>
    /// WCAG 2.1 contrast ratio between two colours, alpha-compositing the foreground over
    /// the background when it is translucent. Used by the developer editor so a bad choice
    /// is visible immediately instead of after a user complaint.
    /// </summary>
    public static double ContrastRatio(Color fg, Color bg)
    {
        var f = CompositeOver(fg, bg);
        var l1 = RelativeLuminance(f);
        var l2 = RelativeLuminance(bg);
        var hi = Math.Max(l1, l2);
        var lo = Math.Min(l1, l2);
        return (hi + 0.05) / (lo + 0.05);
    }

    private static Color CompositeOver(Color fg, Color bg)
    {
        var a = fg.A / 255.0;
        return Color.FromArgb(
            0xFF,
            (byte)Math.Round(fg.R * a + bg.R * (1 - a)),
            (byte)Math.Round(fg.G * a + bg.G * (1 - a)),
            (byte)Math.Round(fg.B * a + bg.B * (1 - a)));
    }

    private static double RelativeLuminance(Color c) =>
        0.2126 * Channel(c.R) + 0.7152 * Channel(c.G) + 0.0722 * Channel(c.B);

    /// <summary>
    /// Force a text colour to be readable on a given surface, keeping its hue.
    ///
    /// This implements the user's rule directly: "只要文字背景为浅色，文字就用深色"
    /// (and the mirror case). Rather than only warning about a bad choice, the colour is
    /// pushed toward black or white - whichever direction the surface allows - until the
    /// contrast target is met. Because the light and dark palettes are essentially
    /// inverted, the correct direction is simply "away from the surface's luminance".
    ///
    /// Returns the original colour when it already passes, so user choices survive.
    /// </summary>
    public static Color EnsureReadable(Color text, Color surface, double target = 4.5)
    {
        if (ContrastRatio(text, surface) >= target)
        {
            return text;
        }

        var surfaceIsLight = RelativeLuminance(surface) > 0.35;
        var result = text;
        // ~40 steps of 6% is far more than enough to reach either end of the range.
        for (var i = 0; i < 40 && ContrastRatio(result, surface) < target; i++)
        {
            result = surfaceIsLight
                ? Scale(result, 0.94)     // darken
                : Scale(result, 1.06);    // lighten
        }

        return result;
    }

    /// <summary>Multiply RGB by a factor, clamping to 0..255 (alpha untouched).</summary>
    private static Color Scale(Color c, double factor) => Color.FromArgb(
        c.A,
        (byte)Math.Clamp(Math.Round(c.R * factor), 0, 255),
        (byte)Math.Clamp(Math.Round(c.G * factor), 0, 255),
        (byte)Math.Clamp(Math.Round(c.B * factor), 0, 255));

    private static double Channel(byte v)
    {
        var s = v / 255.0;
        return s <= 0.04045 ? s / 12.92 : Math.Pow((s + 0.055) / 1.055, 2.4);
    }

    /// <summary>Base colour hex a theme uses for its window tint.</summary>
    public static string BaseHex(bool light) => light ? "#F2F4F7" : "#101418";

    /// <summary>
    /// How translucent the card / text-box surfaces may get.
    ///
    /// Derived, not guessed. The surfaces are translucent and the window tint behind them
    /// is translucent too, so a wallpaper can shine through: compositing the surface at
    /// opacity a over the theme's base colour is what the text actually sits on.
    /// Solving for WCAG 4.5:1 on the WEAKEST text tier (GlassTextSub) against both a
    /// black and a white wallpaper gives a required floor of 0.77 for BOTH themes;
    /// 0.80 keeps a margin. Below that, the text washes out and the palette looks
    /// "inverted" (user report 2026-09-12) - re-derive with the same method if the
    /// text/background colours ever change, and re-run contrast_check.py.
    /// </summary>
    public const double MinSurfaceOpacity = 0.80;
    public const double MaxSurfaceOpacity = 1.00;

    public static double MinSurfaceOpacityFor(bool light) => MinSurfaceOpacity;

    public static double MaxSurfaceOpacityFor(bool light) => MaxSurfaceOpacity;

    /// <summary>Clamp a surface opacity into the safe range.</summary>
    public static double ClampSurfaceOpacity(bool light, double value) =>
        Math.Clamp(value, MinSurfaceOpacityFor(light), MaxSurfaceOpacityFor(light));

    /// <summary>Compact "<key>=#AARRGGBB" dump, for the diagnostic log.</summary>
    public static string Describe(IReadOnlyDictionary<string, Color> palette)
    {
        var keys = new[]
        {
            "GlassBaseBrush", "GlassCardBrush", "GlassSurfaceBrush",
            "GlassTextMainBrush", "GlassTextSubBrush",
        };
        var parts = new List<string>();
        foreach (var k in keys)
        {
            if (palette.TryGetValue(k, out var c))
            {
                parts.Add($"{k.Replace("Glass", "").Replace("Brush", "")}={c.A:X2}{c.R:X2}{c.G:X2}{c.B:X2}");
            }
        }

        return string.Join(" ", parts);
    }

    private static void Add(Dictionary<string, Color> map, string key, string hex, byte alpha) =>
        map[key] = FromHex(hex, alpha);
    private static byte ToAlpha(double opacity) =>
        (byte)Math.Clamp(Math.Round(opacity * 255), 0, 255);

    /// <summary>Parse "#RRGGBB" and apply the given alpha.</summary>
    public static Color FromHex(string hex, byte alpha = 0xFF)    {
        try
        {
            var h = hex.TrimStart('#');
            if (h.Length == 6)
            {
                return Color.FromArgb(
                    alpha,
                    Convert.ToByte(h.Substring(0, 2), 16),
                    Convert.ToByte(h.Substring(2, 2), 16),
                    Convert.ToByte(h.Substring(4, 2), 16));
            }
        }
        catch (Exception)
        {
            // fall through
        }

        return Color.FromArgb(alpha, 0, 0, 0);
    }
}
