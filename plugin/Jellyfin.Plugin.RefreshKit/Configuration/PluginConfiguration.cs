using System;
using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.RefreshKit.Configuration
{
    /// <summary>
    /// Admin-facing configuration. Every switch here is a KILL SWITCH: the
    /// plugin's job is to make other plugins load fresh, so an admin must be
    /// able to turn any part of it off without uninstalling anything.
    /// </summary>
    public class PluginConfiguration : BasePluginConfiguration
    {
        /// <summary>
        /// Gets or sets a value indicating whether index.html is served through
        /// the injection middleware. When off, no runtime tag or asset stamp is
        /// added and the host shell passes through; configuration, diagnostics,
        /// generation and version endpoints remain available.
        /// </summary>
        public bool EnableInjection { get; set; } = true;

        /// <summary>
        /// Gets or sets a value indicating whether unversioned script/link tags
        /// belonging to OTHER plugins get a <c>?rkv=</c> cache-busting stamp.
        /// </summary>
        public bool EnableThirdPartyStamping { get; set; } = true;

        /// <summary>
        /// Gets or sets a value indicating whether the client runtime performs
        /// safe auto-reloads when the generation changes (mode=auto) or merely
        /// logs the update (mode=notify).
        /// </summary>
        public bool EnableAutoReload { get; set; } = true;

        /// <summary>
        /// Gets or sets how often (seconds) an open tab polls the generation
        /// endpoint. Clamped by the client runtime to 15..3600.
        /// </summary>
        public int PollSeconds { get; set; } = 60;

        /// <summary>
        /// Gets or sets the user-idle time (seconds) required before an
        /// auto-reload. Clamped by the client runtime to 0..300.
        /// </summary>
        public int IdleSeconds { get; set; } = 5;

        /// <summary>
        /// Gets or sets the maximum number of reloads per rolling 60s window.
        /// Clamped by the client runtime to 1..100.
        /// </summary>
        public int ReloadBudget { get; set; } = 3;

        /// <summary>
        /// Gets or sets a value indicating whether a plugin's SETTINGS being
        /// saved counts as a change worth reloading clients for.
        /// <para>
        /// On by default, because the common case is exactly what users expect:
        /// an admin enables a feature in a plugin's settings and the open tabs
        /// pick it up. The cost is that a plugin which persists per-user or
        /// runtime state into its plugin-configuration XML can make the whole
        /// server reload; the debounce, the per-plugin cooldown and
        /// <see cref="ConfigWatchExclusions"/> bound that. Turning this off still
        /// detects loaded host/plugin module identity and active loose browser
        /// asset content; it omits configuration content only.
        /// </para>
        /// </summary>
        public bool EnableConfigWatching { get; set; } = true;

        /// <summary>
        /// Gets or sets the plugins whose configuration changes are IGNORED.
        /// An entry matches a plugin folder (<c>Media Bar_2.4.12.0</c>), the
        /// folder's display-name part or the plugin's real display name
        /// (<c>Media Bar</c>), a plugin GUID in any common form, or an assembly
        /// name (<c>Jellyfin.Plugin.MediaBar</c>).
        /// <para>
        /// Empty by default: the plugins tested (Jellyfin Enhanced, Media Bar,
        /// File Transformation, InPlayerEpisodePreview) keep per-user
        /// preferences and runtime caches in their private data directory,
        /// NOT in the watched plugin configuration XML. A plugin that writes
        /// bookkeeping into its configuration XML is usually better served by
        /// <see cref="ConfigIgnoredElements"/>, which keeps its real settings
        /// watched. Add a plugin here only if it bumps the generation while
        /// nobody is changing settings and no single element explains it.
        /// </para>
        /// </summary>
        public string[] ConfigWatchExclusions { get; set; } = Array.Empty<string>();

        /// <summary>
        /// Gets or sets the top-level elements of a plugin's configuration XML
        /// that are left out of its configuration identity. A plugin that
        /// records a timestamp or telemetry receipt in its configuration on a
        /// timer, or at every start, would otherwise reload every open tab
        /// each time it does so.
        /// <para>
        /// One entry per line. A bare element name (<c>LastRunUtc</c>) applies
        /// to every plugin; <c>&lt;plugin&gt;:&lt;element&gt;</c> limits it to one
        /// plugin, where the plugin part accepts the same forms as
        /// <see cref="ConfigWatchExclusions"/>. Element names are compared
        /// case-insensitively. Only direct children of the document element
        /// are matched; a document that is not well-formed XML is hashed as
        /// exact bytes instead.
        /// </para>
        /// <para>
        /// This list is one of three layers. A plugin can declare its own
        /// bookkeeping in a top-level <c>RefreshKitIgnoredElements</c> element
        /// of its configuration XML, which is always honoured and needs no
        /// entry anywhere. The shipped default of this list comes from the
        /// built-in registry in <c>KnownPluginConfigurationHints.cs</c> (today:
        /// Jellyfin Enhanced 12.8, whose scheduled translation-cache task
        /// rewrites <c>ClearTranslationCacheTimestamp</c> at every server start
        /// and whose optional usage analytics rewrite the <c>Analytics*</c>
        /// receipts on a multi-day timer). This setting is the per-server
        /// override on top of both. Restarts and updates still move the
        /// generation through the loaded code; the one thing an ignored element
        /// gives up is a reload for a change to that element alone.
        /// </para>
        /// </summary>
        public string[] ConfigIgnoredElements { get; set; } = DefaultConfigIgnoredElements();

        /// <summary>The shipped <see cref="ConfigIgnoredElements"/> list, rendered from the built-in registry.</summary>
        public static string[] DefaultConfigIgnoredElements() => KnownPluginConfigurationHints.DefaultConfigIgnoredElements();

        /// <summary>
        /// Gets or sets the length, in minutes, of the burst window that follows a
        /// configuration-driven generation change FOR THE SAME PLUGIN. The gate is
        /// LEADING-EDGE: after a new configuration identity has been observed by
        /// two provider scans at least ten seconds apart, a change arriving while
        /// no window is open publishes and opens the window. With polling as the
        /// only traffic, a write just after a poll can therefore take roughly two
        /// poll intervals to appear. Further changes inside the window are held
        /// and coalesce into one publish when it expires. Nothing is dropped.
        /// Zero disables the cooldown (the debounce still applies). Loaded-module
        /// and loose-asset changes ignore it entirely.
        /// </summary>
        public int ConfigCooldownMinutes { get; set; } = 5;

        /// <summary>
        /// Gets or sets a value indicating whether the served client runtime is
        /// marked no-store instead of immutable (for debugging this plugin).
        /// </summary>
        public bool DevMode { get; set; }
    }
}
