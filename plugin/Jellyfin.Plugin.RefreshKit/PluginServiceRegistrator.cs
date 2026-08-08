using System;
using System.Globalization;
using System.Net;
using System.Security.Cryptography;
using Jellyfin.Plugin.RefreshKit.Configuration;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// Wires up all three mechanisms at server start.
    ///
    /// <list type="number">
    /// <item><description><b>Fresh index.html</b> — <c>AddRefreshKit</c>
    /// registers the <c>IStartupFilter</c> that serves the web shell through the
    /// refresh kit's middleware. Ordinary final-byte ownership uses strong
    /// <c>rk-</c> ETags, 304/412 and encoding-preserving revalidation; nested
    /// outer buffers receive the complete transformed shell as no-store without
    /// validators while their final framing is preserved. This is the same
    /// registration path Jellyfin Canopy uses on 10.11, and it works from a
    /// plugin.</description></item>
    /// <item><description><b>Third-party tag stamping</b> — the
    /// <c>HtmlPostProcess</c> hook, running inside that same transform so visible
    /// eligible tags share the shell representation's selected cache policy.
    /// Tags appended later by an outer middleware remain outside the stamping
    /// boundary.</description></item>
    /// <item><description><b>Generation + client runtime</b> — the injected tag
    /// points jellyfin-refresh-kit.js at the generation endpoint, in standalone
    /// mode: no bootstrap entries, no asset patterns, just "poll the aggregate
    /// identity of the loaded Jellyfin host and plugins and, when it changes, reload this tab
    /// safely".</description></item>
    /// </list>
    /// </summary>
    public class PluginServiceRegistrator : IPluginServiceRegistrator
    {
        /// <summary>
        /// The tag identity. Also the scrub key: the middleware removes exactly
        /// the tags carrying <c>plugin="Jellyfin Refresh Kit"</c>, so injection
        /// converges and a plugin shipping its OWN copy of the kit is untouched.
        /// </summary>
        internal const string PluginName = "Jellyfin Refresh Kit";

        /// <summary>The controller route, and the base of every injected src.</summary>
        internal const string BasePath = "RefreshKit";

        /// <summary>
        /// The kit instance name. Deliberately explicit and distinctive: on a
        /// page where an adopting plugin also ships its own kit copy, both
        /// instances register with the one manager, and this name is how an
        /// admin (or a support log) tells the server-wide instance from the
        /// plugin's own.
        /// </summary>
        internal const string InstanceName = "RefreshKitPlugin";

        /// <summary>
        /// Opaque 128-bit incarnation sidecar, generated once for this loaded
        /// process. It is returned only by the JSON generation endpoint and is
        /// never stamped as an HTML value or folded into generation/cache keys.
        /// </summary>
        internal static readonly string ProcessEpoch = Convert.ToHexString(
                RandomNumberGenerator.GetBytes(16))
            .ToLowerInvariant();

        /// <summary>Substring used to recognise (and never stamp) our own tags.</summary>
        internal static readonly string OwnTagMarker = "plugin=\"" + PluginName + "\"";

        public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
        {
            serviceCollection.AddSingleton<PluginGenerationProvider>();

            static string LoadedFallbackGeneration() =>
                "fallback-" + typeof(PluginServiceRegistrator)
                    .Assembly
                    .ManifestModule
                    .ModuleVersionId
                    .ToString("N");

            string ResolveGeneration()
            {
                try
                {
                    return applicationHost.ServiceProvider?
                        .GetService<PluginGenerationProvider>()?
                        .Generation
                        ?? LoadedFallbackGeneration();
                }
                catch
                {
                    return LoadedFallbackGeneration();
                }
            }

            serviceCollection.AddRefreshKit(new RefreshKitOptions
            {
                PluginName = PluginName,
                BasePath = BasePath,
                ScriptPaths = new[] { "kit.js" },

                // THE GENERATION IS THE VERSION. Overriding VersionProvider makes
                // one value do four jobs: the injected tag's ?v=, its
                // data-boot-version seed, and the value the version endpoint
                // reports (RefreshKitVersionControllerBase reads the same
                // delegate). The postprocessor derives ?rkv= from this exact tag
                // block rather than reading mutable provider state a second time.
                // The middleware cache is keyed on the tag block, so a generation
                // change also invalidates the cached shell.
                VersionProvider = ResolveGeneration,
                EpochProvider = () => ProcessEpoch,

                DevMode = () => Config?.DevMode == true,
                Enabled = () => Config?.EnableInjection != false,
                ExtraAttributes = _ => BuildKitAttributes(),
                HtmlPostProcessWithTags = StampThirdPartyTags,
            });
        }

        private static PluginConfiguration? Config
        {
            get
            {
                try
                {
                    return Plugin.Instance?.Configuration;
                }
                catch
                {
                    return null;
                }
            }
        }

        /// <summary>
        /// The standalone-mode configuration of jellyfin-refresh-kit.js, declared
        /// the way the kit documents for a shipping plugin: <c>data-*</c>
        /// attributes on its own tag, read synchronously at tag position.
        /// <para>
        /// Deliberately NO <c>data-asset-patterns</c> and NO
        /// <c>data-entry-scripts</c>. This instance must not claim other
        /// plugins' runtime-created sub-assets: on a page where an adopting
        /// plugin ships its own kit copy, the first-registered matching instance
        /// wins, and a greedy pattern here would silently take over that
        /// plugin's own versioning. Layer 2 stays the adopter's business; this
        /// instance owns layer 3 (detect + safe reload) for the whole server,
        /// and the server-side stamping covers the static tags.
        /// </para>
        /// </summary>
        internal static string BuildKitAttributes()
        {
            var config = Config;
            var mode = config?.EnableAutoReload == false ? "notify" : "auto";
            return string.Format(
                CultureInfo.InvariantCulture,
                "data-name=\"{0}\" data-version-url=\"../{1}/Generation\" "
                + "data-version-json-field=\"CacheKey\" "
                + "data-version-epoch-json-field=\"Epoch\" data-mode=\"{2}\" "
                + "data-poll-seconds=\"{3}\" data-idle-seconds=\"{4}\" data-reload-budget=\"{5}\" "
                + "data-third-party-stamping=\"{6}\"",
                InstanceName,
                BasePath,
                mode,
                config?.PollSeconds ?? 60,
                config?.IdleSeconds ?? 5,
                config?.ReloadBudget ?? 3,
                config?.EnableThirdPartyStamping == false ? "false" : "true");
        }

        /// <summary>
        /// Stamps other plugins' unversioned tags with the exact generation in
        /// the tag block already selected for this response.
        /// </summary>
        internal static string StampThirdPartyTags(string html, string currentTags)
        {
            if (!string.Equals(
                    ReadTagAttribute(currentTags, "data-third-party-stamping"),
                    "true",
                    StringComparison.OrdinalIgnoreCase))
            {
                return html;
            }

            var generation = ReadTagAttribute(currentTags, "data-boot-version");
            if (string.IsNullOrEmpty(generation))
            {
                return html;
            }

            return ThirdPartyTagStamper.Stamp(
                html,
                generation,
                OwnTagMarker);
        }

        private static string? ReadTagAttribute(string currentTags, string attributeName)
        {
            if (string.IsNullOrEmpty(currentTags))
            {
                return null;
            }

            var prefix = attributeName + "=\"";
            var valueStart = currentTags.IndexOf(prefix, StringComparison.Ordinal);
            if (valueStart < 0)
            {
                return null;
            }

            valueStart += prefix.Length;
            var valueEnd = currentTags.IndexOf('"', valueStart);
            if (valueEnd <= valueStart)
            {
                return null;
            }

            return WebUtility.HtmlDecode(
                currentTags.Substring(valueStart, valueEnd - valueStart));
        }
    }
}
