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
        /// the revalidating injection middleware at all. Off = the plugin is
        /// inert: no injected tag, no stamping, host bytes pass through.
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
        /// Gets or sets a value indicating whether the served client runtime is
        /// marked no-store instead of immutable (for debugging this plugin).
        /// </summary>
        public bool DevMode { get; set; }
    }
}
