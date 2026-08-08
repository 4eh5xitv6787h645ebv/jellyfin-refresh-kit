using System.Collections.Generic;
using System.Linq;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.RefreshKit.Controllers
{
    /// <summary>
    /// The plugin's two public endpoints, plus one admin diagnostic.
    ///
    /// <para>
    /// <c>Generation</c> and <c>kit.js</c> are BOTH anonymous on purpose: the
    /// login screen is a fully-fledged page of the web client, it is where a
    /// user with a stale cache most often lands, and a tab can sit on it for
    /// days. An authenticated version endpoint would leave exactly that page
    /// unable to notice a plugin update. Neither endpoint discloses anything a
    /// logged-out visitor cannot already see: an opaque aggregate token, and a
    /// public MIT-licensed script.
    /// </para>
    /// </summary>
    [ApiController]
    [Route("RefreshKit")]
    public class RefreshKitController : RefreshKitVersionControllerBase
    {
        /// <summary>
        /// The aggregate generation of every installed plugin, in the shape
        /// jellyfin-refresh-kit.js polls: <c>{ Version, BuildId, CacheKey }</c>
        /// with <c>CacheKey</c> carrying the generation (the injected tag sets
        /// <c>data-version-json-field="CacheKey"</c> to match, and its
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
        /// What the generation is actually made of — one row per installed
        /// plugin. Admin-only: it enumerates installed plugins and their
        /// binaries' timestamps, which is inventory, not something the login
        /// screen needs.
        /// </summary>
        [HttpGet("Diagnostics")]
        [Authorize(Policy = "RequiresElevation")]
        public ActionResult<IReadOnlyList<object>> GetDiagnostics()
        {
            var provider = PluginGenerationProvider.Instance;
            RefreshKit.ApplyNoStore(Response);
            return Ok(new
            {
                Generation = provider.Generation,
                KitVersion = Plugin.KitVersion,
                PluginVersion = RefreshKit.Version,
                BuildId = RefreshKit.BuildId,
                Plugins = provider.Details
                    .Select(d => new
                    {
                        d.Folder,
                        d.Id,
                        d.Version,
                        d.Status,
                        d.NewestDllTicks,
                        d.NewestConfigTicks,
                    })
                    .OrderBy(d => d.Folder, System.StringComparer.Ordinal)
                    .ToList(),
            });
        }
    }
}
