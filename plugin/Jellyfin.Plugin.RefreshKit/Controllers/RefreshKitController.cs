using System;
using System.Linq;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.RefreshKit.Controllers
{
    /// <summary>
    /// The plugin's three public routes, plus one admin diagnostic.
    ///
    /// <para>
    /// <c>Generation</c>, <c>Generation.txt</c> and <c>kit.js</c> routes are
    /// anonymous on purpose: the
    /// login screen is a fully-fledged page of the web client, it is where a
    /// user with a stale cache most often lands, and a tab can sit on it for
    /// days. An authenticated version endpoint would leave exactly that page
    /// unable to notice a plugin update. They expose only opaque generation and
    /// process-incarnation tokens plus a public MIT-licensed script, never the
    /// detailed server inventory reserved for the authenticated diagnostic.
    /// </para>
    ///
    /// <para>
    /// What "opaque" does and does not buy, stated honestly. The generation
    /// cannot be reversed or enumerated: no name, version, path or count can be
    /// read out of it. But it is derived from public material with no
    /// per-install secret (see <see cref="PluginGenerationProvider.Generation"/>),
    /// so an unauthenticated observer who can already guess an exact host
    /// version plus plugin inventory can compute candidate folds offline and
    /// confirm the guess. The same observer can also watch the token move and
    /// learn WHEN an admin saved plugin settings, or when a server was updated
    /// and restarted — a timing oracle inherent to any anonymous change signal,
    /// which is what these routes exist to be. Both are documented in
    /// plugin/README.md, and neither can be designed away while the login
    /// screen must still notice an update: a change signal that page can read
    /// is a change signal an unauthenticated observer can read.
    /// </para>
    /// </summary>
    [ApiController]
    [Route("RefreshKit")]
    public class RefreshKitController : RefreshKitVersionControllerBase
    {
        private readonly PluginGenerationProvider _generationProvider;

        public RefreshKitController(PluginGenerationProvider generationProvider)
        {
            _generationProvider = generationProvider
                ?? throw new ArgumentNullException(nameof(generationProvider));
        }

        /// <summary>
        /// The aggregate generation of the loaded host and plugin state, in the shape
        /// jellyfin-refresh-kit.js polls: <c>{ Version, BuildId, CacheKey, Epoch }</c>
        /// with <c>CacheKey</c> carrying the generation (the injected tag sets
        /// <c>data-version-json-field="CacheKey"</c> to match, and its
        /// <c>data-version-epoch-json-field="Epoch"</c> sidecar distinguishes
        /// this loaded process without changing the generation, URLs or ETags.
        /// Its
        /// <c>data-boot-version</c> seed is the same value).
        /// Served <c>no-store</c> — a cached "current version" is worse than no
        /// version endpoint at all.
        /// </summary>
        [HttpGet("Generation")]
        [AllowAnonymous]
        public ActionResult GetGeneration() => VersionJson();

        /// <summary>
        /// The plain-text generation, for scripts and health checks that would
        /// rather not parse JSON.
        /// </summary>
        [HttpGet("Generation.txt")]
        [AllowAnonymous]
        public ActionResult GetGenerationText() => VersionPlainText();

        /// <summary>
        /// The embedded jellyfin-refresh-kit.js client runtime, with the cache
        /// headers the kit's own helper produces: <c>immutable</c> in production
        /// (safe — the injected src carries <c>?v={generation}</c>, so a new
        /// generation is a new URL) and <c>no-store</c> in dev mode.
        /// </summary>
        [HttpGet("kit.js")]
        [AllowAnonymous]
        public ActionResult GetKitScript()
        {
            RefreshKit.ApplyScriptCacheHeaders(Response);
            return Content(Plugin.KitJavaScript, "application/javascript; charset=utf-8");
        }

        /// <summary>
        /// What the generation is actually made of — loaded host identity and
        /// one row per loaded plugin. Admin-only: this is server inventory, not
        /// something the login screen needs.
        /// <para>
        /// The whole payload comes from ONE
        /// <see cref="PluginGenerationProvider.Snapshot"/>. Reading the
        /// generation, the host and the plugin rows as three separate provider
        /// properties would be three acquisitions, which can straddle the
        /// five-second scan-cache boundary and report a generation that the
        /// listed rows do not explain — precisely the confusion this endpoint is
        /// consulted to resolve.
        /// </para>
        /// </summary>
        [HttpGet("Diagnostics")]
        [Authorize(Policy = "RequiresElevation")]
        public ActionResult<object> GetDiagnostics()
        {
            var snapshot = _generationProvider.Snapshot;
            var host = snapshot.Host;
            RefreshKit.ApplyNoStore(Response);
            return Ok(new
            {
                Generation = snapshot.Generation,
                KitVersion = Plugin.KitVersion,
                PluginVersion = RefreshKit.Version,
                BuildId = RefreshKit.BuildId,
                Stamping = ThirdPartyTagStamper.Diagnostics,
                Host = new
                {
                    host.Identity,
                    host.Modules,
                },
                Plugins = snapshot.Details
                    .Select(d => new
                    {
                        d.Folder,
                        d.Id,
                        d.Version,
                        d.Status,
                        d.NewestDllTicks,
                        d.NewestAssetTicks,
                        d.NewestConfigTicks,
                        d.IsLoaded,
                        d.LoadedModuleIdentity,
                        d.AssetIdentity,
                        d.AssetFileCount,
                        d.AssetDirectoriesScanned,
                        d.AssetBytesHashed,
                        d.AssetScanTruncated,
                        d.AssetScanUnavailable,
                        d.UsingLastGoodAssets,
                        d.ConfigurationIdentity,
                        d.ConfigurationFileCount,
                        d.ConfigurationBytesHashed,
                        d.ConfigurationReparsePointsSkipped,
                        d.ConfigurationScanTruncated,
                        d.ConfigurationScanUnavailable,
                        d.UsingLastGoodConfiguration,
                        d.UsingLastKnownPluginRecord,
                    })
                    .OrderBy(d => d.Folder, System.StringComparer.Ordinal)
                    .ToList(),
            });
        }
    }
}
