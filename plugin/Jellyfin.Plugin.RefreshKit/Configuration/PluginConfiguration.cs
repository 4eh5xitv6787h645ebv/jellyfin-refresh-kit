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
        /// An entry matches a plugin folder (<c>Media Bar_2.4.12.0</c>), a
        /// display name (<c>Media Bar</c>), a plugin GUID, or an assembly name
        /// (<c>Jellyfin.Plugin.MediaBar</c>).
        /// <para>
        /// Empty by default: on a live 10.11.11 server, the plugins tested
        /// (Jellyfin Enhanced, Media Bar, File Transformation,
        /// InPlayerEpisodePreview) keep per-user preferences and runtime caches
        /// in their private data directory, NOT in the watched plugin
        /// configuration XML — so none of them needed excluding. Add a plugin
        /// here if you observe it bumping the generation while nobody is
        /// changing settings.
        /// </para>
        /// </summary>
        public string[] ConfigWatchExclusions { get; set; } = Array.Empty<string>();

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
