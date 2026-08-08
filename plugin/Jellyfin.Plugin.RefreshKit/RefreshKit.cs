// -----------------------------------------------------------------------------
//  RefreshKit.cs — server-side half of the Jellyfin refresh kit.
//
//  MIT License
//
//  Copyright (c) 2026 <YOUR NAME HERE>
//
//  Permission is hereby granted, free of charge, to any person obtaining a copy
//  of this software and associated documentation files (the "Software"), to deal
//  in the Software without restriction, including without limitation the rights
//  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//  copies of the Software, and to permit persons to whom the Software is
//  furnished to do so, subject to the following conditions:
//
//  The above copyright notice and this permission notice shall be included in
//  all copies or substantial portions of the Software.
//
//  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//  SOFTWARE.
// -----------------------------------------------------------------------------
//
//  VENDORED COPY — DO NOT EDIT CASUALLY
//  ===================================
//  This is the repository-root RefreshKit.cs, vendored into the standalone
//  plugin. It differs from the root file in exactly three places, all marked
//  "STANDALONE-PLUGIN ADAPTATION":
//    1. the namespace is Jellyfin.Plugin.RefreshKit (root: JellyfinRefreshKit);
//    2. RefreshKitOptions gains HtmlPostProcess + ApplyHtmlPostProcess;
//    3. the middleware calls ApplyHtmlPostProcess right after
//       ReplaceOwnedScriptTags.
//  To re-sync after the root file changes:
//    sed 's/^namespace JellyfinRefreshKit$/namespace Jellyfin.Plugin.RefreshKit/' \
//        RefreshKit.cs > plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs
//  and re-apply (2) and (3). The file is vendored rather than <Compile Link>ed
//  because those two hooks do not exist upstream and the root file is owned by
//  the single-file adoption path.
//
//  WHAT THIS FILE IS
//  =================
//  Drop this ONE file into any Jellyfin plugin (10.11 or 12) and you get the
//  three server-side layers of the "stale client" fix:
//
//    1. Request-time <script> injection into jellyfin-web's index.html, with
//       full HTTP semantics — a revalidating representation cache, strong
//       ETags, 304/412, gzip/brotli preservation, Range/HEAD handling, and
//       fail-open behaviour on every error path. No writing to the web folder
//       (which needs a writable root, is wiped on every jellyfin-web update,
//       and does not exist as a supported hook on Jellyfin 12 at all).
//
//    2. A stable, content-derived build identity (`RefreshKit.BuildId`) plus a
//       per-build cache key (`RefreshKit.CacheKey`) that is stamped onto every
//       injected tag and onto the `?v=` of every script URL, so a new build is
//       always a NEW URL and can never be served from a browser's HTTP cache.
//
//    3. Cache-correct script serving — one call in your script endpoint
//       (`RefreshKit.ApplyScriptCacheHeaders`) gives you
//       `public, max-age=31536000, immutable` in production and `no-store`
//       in dev mode.
//
//  It is the C# companion to jellyfin-refresh-kit.js: this file makes sure a
//  reloaded tab actually GETS new bytes; the JS kit makes sure the tab reloads.
//  If you serve the JS kit through ScriptPaths, it must be the FIRST entry —
//  ScriptPaths order is execution order (every tag is emitted `defer`), and any
//  script that runs before the kit creates its sub-assets unversioned. See the
//  ScriptPaths doc comment.
//
//  ADOPTION — the "few lines"
//  ==========================
//    (a) Drop RefreshKit.cs anywhere in your plugin project.
//
//    (b) In your IPluginServiceRegistrator:
//
//          using JellyfinRefreshKit;
//          ...
//          serviceCollection.AddRefreshKit(new RefreshKitOptions
//          {
//              PluginName  = "My Plugin",     // tag identity + scrub key
//              BasePath    = "MyPlugin",      // your controller's [Route]
//              ScriptPaths = new[] { "script" },
//              DevMode     = () => Plugin.Instance?.Configuration.DevMode == true,
//          });
//
//        That is the whole injection layer. Every index.html now carries
//        exactly one:
//
//          <script plugin="My Plugin" version="1.2.3-638…" data-boot-version="1.2.3-638…"
//                  dev="false" build="ab12…" src="../MyPlugin/script?v=1.2.3-638…" defer></script>
//
//        `data-boot-version` is the load-coupled build identity of the server
//        that produced THIS document. jellyfin-refresh-kit.js 2.1+ reads it off
//        its own tag and seeds its baseline with it, so a plugin update landing
//        between page-serve and the client's first poll is DETECTED rather than
//        absorbed into the baseline. Nothing to configure — but if you also
//        serve the JS kit, point its version endpoint at the same identity
//        (data-version-json-field="CacheKey", see (d)).
//
//    (c) In your script endpoint (the one serving `../MyPlugin/script`):
//
//          [HttpGet("script")]
//          [AllowAnonymous]
//          public ActionResult GetScript()
//          {
//              // Reads the LIVE DevMode delegate registered in (b), so the
//              // endpoint's headers can never disagree with the tag's dev="…".
//              // (The explicit overload — ApplyScriptCacheHeaders(Response,
//              // devMode) — is there when you resolve the flag yourself.)
//              RefreshKit.ApplyScriptCacheHeaders(Response);
//              return Content(js, "application/javascript");
//          }
//
//    (d) OPTIONAL — a version endpoint for the JS kit to poll. Controllers
//        cannot be shipped generically (two plugins embedding this file would
//        both claim the same route), so the endpoint is OPT-IN: subclass
//        RefreshKitVersionControllerBase with YOUR OWN route.
//
//          [ApiController]
//          [Route("MyPlugin")]
//          public class MyVersionController : RefreshKitVersionControllerBase
//          {
//              [HttpGet("RefreshVersion")]
//              [AllowAnonymous]
//              public ActionResult Version() => VersionJson();
//          }
//
//        Then point the JS kit at it, e.g. via ExtraAttributes:
//
//          ExtraAttributes = _ => "data-version-url=\"../MyPlugin/RefreshVersion\" "
//                               + "data-version-json-field=\"CacheKey\"",
//
//        `CacheKey` is the right field precisely because it is what
//        BuildScriptTags already stamps into data-boot-version: the seed and
//        the polled value then name the same identity, and the client can tell
//        "the server moved" from "these two values were never comparable".
//
//  DESIGN NOTES
//  ============
//  * Self-contained: nothing here references plugin-specific types. Logging
//    comes from ILoggerFactory in DI; version/build identity come from the
//    assembly this file is compiled into.
//  * Thread-safe: the middleware is a singleton; all mutable state is behind a
//    lock or an Interlocked, and the LRU cache is bounded.
//  * Fail-open: any unexpected condition serves the host's original bytes. This
//    middleware sits in front of the web app shell — it must never be the reason
//    Jellyfin fails to load.
//  * Two plugins can each embed this file. Both compile a `JellyfinRefreshKit`
//    namespace into their OWN assembly; identical namespaces in different
//    assemblies are perfectly legal at runtime (they are distinct types with
//    distinct statics). Each plugin gets its own middleware instance, its own
//    cache, and its own `plugin="…"` scrub identity, so their tags coexist and
//    neither scrubs the other's. The only true collision risk is route names,
//    which is exactly why the version controller is opt-in.
//
// -----------------------------------------------------------------------------

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Primitives;
using Microsoft.Net.Http.Headers;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// Everything the kit needs to know about your plugin. Populate it once and
    /// hand it to <see cref="RefreshKit.AddRefreshKit"/>; treat it as immutable
    /// afterwards (it is read from request threads without locking).
    /// </summary>
    public sealed class RefreshKitOptions
    {
        /// <summary>
        /// REQUIRED. Human-readable plugin name. This is the tag's identity:
        /// every injected tag carries <c>plugin="&lt;PluginName&gt;"</c>, and the
        /// scrub regex removes exactly the tags carrying that marker. Two plugins
        /// therefore never scrub each other. Changing this value between releases
        /// orphans the old tag (it will no longer be scrubbed), so pick it once.
        /// </summary>
        public string PluginName { get; set; } = string.Empty;

        /// <summary>
        /// REQUIRED. The route segment your controller serves scripts under —
        /// i.e. the value in your controller's <c>[Route("...")]</c>. Injected
        /// srcs are built as <c>../{BasePath}/{scriptPath}?v={cacheKey}</c>.
        /// The leading <c>../</c> is deliberate: index.html lives at <c>/web/</c>,
        /// so a relative src resolves against the server root and keeps working
        /// behind a base-url prefix (e.g. <c>/jellyfin/web/</c>).
        /// </summary>
        public string BasePath { get; set; } = string.Empty;

        /// <summary>
        /// REQUIRED, at least one entry. Ordered list of script paths relative to
        /// <see cref="BasePath"/>. Each becomes one injected tag, in this order.
        /// <para>
        /// THIS LIST IS THE EXECUTION ORDER. Every tag is emitted with
        /// <c>defer</c>, and deferred scripts run in document order — so an entry
        /// that appears earlier here runs earlier in the browser.
        /// </para>
        /// <para>
        /// If you also serve the JS kit (<c>jellyfin-refresh-kit.js</c>, the
        /// layer-3 companion), it MUST BE THE FIRST ENTRY. The kit installs its
        /// <c>document.createElement</c> hook at the moment it runs, and any
        /// sub-asset a script creates before that point goes out unversioned —
        /// silently, on every load, with the kit still reporting a healthy
        /// resolved version. <c>{ "script", "jellyfin-refresh-kit.js" }</c> is the
        /// natural edit and the wrong one; write
        /// <c>{ "jellyfin-refresh-kit.js", "script" }</c>. Your own
        /// early-capture/bootstrap script goes immediately after it.
        /// </para>
        /// </summary>
        public IReadOnlyList<string> ScriptPaths { get; set; } = Array.Empty<string>();

        /// <summary>
        /// OPTIONAL. Live dev-mode flag, stamped into the tag as
        /// <c>dev="true|false"</c> so client code can branch on it. This does NOT
        /// change server cache headers on its own — call
        /// <see cref="RefreshKit.ApplyScriptCacheHeaders(HttpResponse)"/> in your
        /// endpoint, which reads THIS delegate, so the headers and the tag cannot
        /// disagree. (The two-argument overload is there for when you resolve the
        /// flag yourself.)
        /// Defaults to false. Exceptions from the delegate are swallowed.
        /// </summary>
        public Func<bool>? DevMode { get; set; }

        /// <summary>
        /// OPTIONAL. Overrides the cache key stamped into tags and <c>?v=</c>.
        /// Default is <see cref="RefreshKit.CacheKey"/>:
        /// <c>{assembly version}-{DLL LastWriteTimeUtc ticks}</c>, which changes
        /// on every build even when the version number does not (the local-dev
        /// case that silently breaks naive version-only cache busting).
        /// Must be URL- and attribute-safe. Exceptions fall back to the default.
        /// </summary>
        public Func<string>? VersionProvider { get; set; }

        /// <summary>
        /// OPTIONAL. Extra raw attributes appended to a tag. The argument is the
        /// script path being emitted, so you can decorate one tag only. The
        /// returned string is spliced into the tag VERBATIM — you own its
        /// escaping and quoting. Typical use is handing config to the JS kit:
        /// <c>data-version-url="../MyPlugin/RefreshVersion"</c>.
        /// </summary>
        public Func<string, string?>? ExtraAttributes { get; set; }

        /// <summary>
        /// OPTIONAL kill switch, evaluated per request. Return false and the
        /// middleware passes index.html straight through untouched — the escape
        /// hatch to expose as an admin config toggle. Default: enabled.
        /// Exceptions from the delegate are treated as "enabled".
        /// </summary>
        public Func<bool>? Enabled { get; set; }

        /// <summary>
        /// OPTIONAL — STANDALONE-PLUGIN ADAPTATION (not in the upstream
        /// RefreshKit.cs at the repository root). A last-pass rewrite of the
        /// injected HTML, applied AFTER this plugin's own tags have been
        /// scrubbed-and-reinserted and BEFORE the representation is encoded,
        /// cached and ETagged. The standalone plugin uses it to stamp
        /// <c>?rkv=</c> onto OTHER plugins' unversioned script/link tags.
        /// <para>
        /// The delegate must be PURE with respect to the current generation:
        /// the representation cache is keyed on the injected tag block (which
        /// carries the generation), so the same input HTML plus the same
        /// generation must always produce the same output bytes.
        /// </para>
        /// Exceptions are swallowed and the un-post-processed HTML is used.
        /// </summary>
        public Func<string, string>? HtmlPostProcess { get; set; }

        /// <summary>Applies <see cref="HtmlPostProcess"/>; never throws, never returns null/empty.</summary>
        internal string ApplyHtmlPostProcess(string html)
        {
            if (HtmlPostProcess == null || string.IsNullOrEmpty(html))
            {
                return html;
            }

            try
            {
                var processed = HtmlPostProcess(html);
                return string.IsNullOrEmpty(processed) ? html : processed;
            }
            catch
            {
                return html;
            }
        }

        /// <summary>Resolved dev flag; never throws.</summary>
        internal bool ResolveDevMode()
        {
            try
            {
                return DevMode != null && DevMode();
            }
            catch
            {
                return false;
            }
        }

        /// <summary>Resolved cache key; never throws, never empty.</summary>
        internal string ResolveVersion()
        {
            try
            {
                if (VersionProvider != null)
                {
                    var custom = VersionProvider();
                    if (!string.IsNullOrWhiteSpace(custom))
                    {
                        return custom;
                    }
                }
            }
            catch
            {
                // Fall through to the built-in key.
            }

            return RefreshKit.CacheKey;
        }

        /// <summary>Resolved kill switch; never throws.</summary>
        internal bool ResolveEnabled()
        {
            try
            {
                return Enabled == null || Enabled();
            }
            catch
            {
                return true;
            }
        }
    }

    /// <summary>
    /// The kit's public surface: one registration extension, the build identity
    /// accessors, the response-header helper, and the tag builder/scrubber (also
    /// usable for a legacy on-disk index.html rewrite).
    /// </summary>
    public static class RefreshKit
    {
        // Regexes are cheap to build but not free; one per plugin name is plenty
        // and the dictionary is bounded by the number of names a process uses
        // (realistically one).
        private static readonly ConcurrentDictionary<string, Regex> _scrubRegexes =
            new ConcurrentDictionary<string, Regex>(StringComparer.Ordinal);

        // Content-derived identity of this exact binary: SHA-256 of the DLL on
        // disk, falling back to a hash of the assembly's ModuleVersionId when the
        // file can't be read (single-file hosting). Unlike CacheKey this is stable
        // across server restarts for the same bytes, so a client can compare it
        // against the build that served its page and detect a plugin update —
        // including a same-version binary replacement. Computed once per process.
        private static readonly Lazy<string> _buildId = new Lazy<string>(() =>
        {
            var assembly = OwnAssembly;
            try
            {
                var location = assembly.Location;
                if (!string.IsNullOrEmpty(location) && File.Exists(location))
                {
                    using var stream = File.OpenRead(location);
                    return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
                }
            }
            catch
            {
                // Fall through to the ModuleVersionId fallback below.
            }

            var mvid = assembly.ManifestModule.ModuleVersionId.ToString("N");
            return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(mvid))).ToLowerInvariant();
        });

        /// <summary>
        /// The assembly this file was compiled into — i.e. YOUR plugin. Using
        /// typeof(RefreshKit) rather than Assembly.GetCallingAssembly() keeps the
        /// answer identical no matter who asks (a controller, the middleware, or
        /// an inlined caller).
        /// </summary>
        private static Assembly OwnAssembly => typeof(RefreshKit).Assembly;

        /// <summary>
        /// Per-build cache key: <c>{assembly version}-{DLL LastWriteTimeUtc ticks}</c>,
        /// falling back to the bare version when the assembly file cannot be
        /// stat'ed. Stamped into every tag and every <c>?v=</c>.
        /// <para>
        /// Recomputed on each read (one file stat) on purpose: a plugin DLL can be
        /// replaced in place while the server runs, and a cached value would keep
        /// serving the old key until restart.
        /// </para>
        /// </summary>
        public static string CacheKey
        {
            get
            {
                var assembly = OwnAssembly;
                var version = assembly.GetName().Version?.ToString() ?? "unknown";
                try
                {
                    var location = assembly.Location;
                    if (!string.IsNullOrEmpty(location) && File.Exists(location))
                    {
                        var ticks = new FileInfo(location).LastWriteTimeUtc.Ticks;
                        return version + "-" + ticks.ToString(System.Globalization.CultureInfo.InvariantCulture);
                    }
                }
                catch
                {
                    // Fall through to the bare version.
                }

                return version;
            }
        }

        /// <summary>Assembly version of the plugin, without the build timestamp.</summary>
        public static string Version =>
            OwnAssembly.GetName().Version?.ToString() ?? "unknown";

        /// <summary>
        /// SHA-256 of the plugin DLL (lowercase hex). Stable for identical bytes
        /// across restarts; changes the instant the binary changes.
        /// </summary>
        public static string BuildId => _buildId.Value;

        /// <summary>
        /// The options handed to the most recent <see cref="AddRefreshKit"/> call
        /// in this assembly. Exposed so <see cref="RefreshKitVersionControllerBase"/>
        /// can report the SAME cache key the tags carry, including a custom
        /// <see cref="RefreshKitOptions.VersionProvider"/>. Null before registration.
        /// </summary>
        public static RefreshKitOptions? Options { get; private set; }

        /// <summary>
        /// Registers the request-time index.html injection middleware as a
        /// singleton <see cref="IStartupFilter"/>. Call from your
        /// <c>IPluginServiceRegistrator.RegisterServices</c>.
        /// </summary>
        /// <exception cref="ArgumentNullException">options is null.</exception>
        /// <exception cref="ArgumentException">
        /// PluginName / BasePath / ScriptPaths are missing. These are author
        /// errors that surface immediately on first run, never at request time.
        /// </exception>
        public static IServiceCollection AddRefreshKit(this IServiceCollection services, RefreshKitOptions options)
        {
            if (services == null)
            {
                throw new ArgumentNullException(nameof(services));
            }

            if (options == null)
            {
                throw new ArgumentNullException(nameof(options));
            }

            if (string.IsNullOrWhiteSpace(options.PluginName))
            {
                throw new ArgumentException("RefreshKitOptions.PluginName is required.", nameof(options));
            }

            if (string.IsNullOrWhiteSpace(options.BasePath))
            {
                throw new ArgumentException("RefreshKitOptions.BasePath is required.", nameof(options));
            }

            if (options.ScriptPaths == null || options.ScriptPaths.Count == 0)
            {
                throw new ArgumentException("RefreshKitOptions.ScriptPaths needs at least one entry.", nameof(options));
            }

            Options = options;

            // Resolved through a factory so the filter gets the options instance
            // directly; ILoggerFactory is optional so the kit still works in a
            // host that has not registered one.
            services.AddSingleton<IStartupFilter>(sp => new RefreshKitScriptInjectionFilter(
                options,
                sp.GetService<ILoggerFactory>()?.CreateLogger("JellyfinRefreshKit") ?? (ILogger)NullLogger.Instance));

            return services;
        }

        /// <summary>
        /// One-liner for a script endpoint. Production gets
        /// <c>public, max-age=31536000, immutable</c> — safe precisely because the
        /// injected src carries <c>?v={cacheKey}</c>, so a new build is a new URL.
        /// Dev mode gets <c>no-store</c> plus the legacy anti-cache headers, so an
        /// intermediary proxy can't hand back yesterday's script either.
        /// </summary>
        public static void ApplyScriptCacheHeaders(HttpResponse response, bool devMode)
        {
            if (response == null)
            {
                return;
            }

            if (devMode)
            {
                ApplyNoStore(response);
                return;
            }

            response.Headers["Cache-Control"] = "public, max-age=31536000, immutable";
            response.Headers.Remove("Pragma");
            response.Headers.Remove("Expires");
        }

        /// <summary>
        /// Same as <see cref="ApplyScriptCacheHeaders(HttpResponse, bool)"/>, but
        /// reads the LIVE <see cref="RefreshKitOptions.DevMode"/> delegate you
        /// registered with <see cref="AddRefreshKit"/>
        /// — the same delegate that decides the <c>dev="true|false"</c> attribute on
        /// the injected tag. Prefer this overload: it is the only form in which the
        /// endpoint's cache headers cannot drift out of step with the tag, which is
        /// exactly the drift that makes a plugin author conclude "dev mode is
        /// broken" while the browser holds a year-long immutable script.
        /// Falls back to production headers when the kit has not been registered
        /// (<see cref="RefreshKit.Options"/> is null) or the delegate throws.
        /// </summary>
        public static void ApplyScriptCacheHeaders(HttpResponse response)
        {
            ApplyScriptCacheHeaders(response, Options?.ResolveDevMode() ?? false);
        }

        /// <summary>
        /// Marks a response as never-cacheable. Use on version/identity endpoints:
        /// a cached "current version" is worse than no version endpoint at all.
        /// </summary>
        public static void ApplyNoStore(HttpResponse response)
        {
            if (response == null)
            {
                return;
            }

            response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0";
            response.Headers["Pragma"] = "no-cache";
            response.Headers["Expires"] = "0";
        }

        /// <summary>
        /// Builds the exact tag block the middleware injects, from the registered
        /// options. Public so an on-disk index.html fallback (or a test) can emit
        /// byte-identical markup and stay convergent with the middleware.
        /// </summary>
        public static string BuildScriptTags(RefreshKitOptions options)
        {
            if (options == null)
            {
                throw new ArgumentNullException(nameof(options));
            }

            return BuildScriptTags(
                options.PluginName,
                options.BasePath,
                options.ScriptPaths,
                options.ResolveVersion(),
                options.ResolveDevMode(),
                BuildId,
                options.ExtraAttributes);
        }

        /// <summary>
        /// Pure tag construction — no plugin state, so the emitted markup can be
        /// asserted in a unit test. Emits one tag per script path, newline
        /// separated, in the order given.
        /// <para>
        /// Every tag carries the cache key twice, deliberately: as
        /// <c>version="{cacheKey}"</c> (human/diagnostic, unchanged since 1.0)
        /// and as <c>data-boot-version="{cacheKey}"</c>, which
        /// <c>jellyfin-refresh-kit.js</c> 2.1+ reads off its own
        /// <c>document.currentScript</c> and uses to SEED its baseline.
        /// </para>
        /// <para>
        /// That seed is what closes the page-serve → first-poll blind spot. The
        /// cache key is load-coupled — it describes the exact build that
        /// produced THIS document — so a plugin update landing between the
        /// moment this HTML was served and the moment the client's first poll
        /// resolves is DETECTED by that first poll instead of being silently
        /// absorbed into the baseline. Without it the client's own first poll
        /// defines "the build this tab is running", which is an assumption the
        /// server can cheaply prove instead.
        /// </para>
        /// <para>
        /// The seed only works if it names the same identity the client's
        /// version endpoint reports, so pair it with
        /// <c>data-version-json-field="CacheKey"</c> (what
        /// <see cref="RefreshKitVersionControllerBase"/> serialises) or the
        /// plain-text variant. The JS side self-heals a mismatch — after one
        /// reload that does not change the served identity it discards the seed
        /// with a warning — but configuring it correctly costs nothing.
        /// </para>
        /// </summary>
        public static string BuildScriptTags(
            string pluginName,
            string basePath,
            IReadOnlyList<string> scriptPaths,
            string cacheKey,
            bool devMode,
            string buildId,
            Func<string, string?>? extraAttributes = null)
        {
            var safeName = AttributeEscape(pluginName);
            var safeKey = AttributeEscape(cacheKey);
            var safeBuild = AttributeEscape(buildId);
            var trimmedBase = (basePath ?? string.Empty).Trim('/');

            var builder = new StringBuilder();
            for (var index = 0; index < (scriptPaths?.Count ?? 0); index++)
            {
                var scriptPath = (scriptPaths![index] ?? string.Empty).TrimStart('/');
                if (builder.Length > 0)
                {
                    builder.Append('\n');
                }

                builder.Append("<script plugin=\"").Append(safeName)
                    .Append("\" version=\"").Append(safeKey)
                    .Append("\" data-boot-version=\"").Append(safeKey)
                    .Append("\" dev=\"").Append(devMode ? "true" : "false")
                    .Append("\" build=\"").Append(safeBuild)
                    .Append('"');

                string? extra = null;
                try
                {
                    extra = extraAttributes?.Invoke(scriptPath);
                }
                catch
                {
                    // An author callback must never break injection.
                }

                if (!string.IsNullOrWhiteSpace(extra))
                {
                    builder.Append(' ').Append(extra!.Trim());
                }

                builder.Append(" src=\"../").Append(trimmedBase).Append('/').Append(scriptPath)
                    .Append("?v=").Append(UrlEscape(cacheKey))
                    .Append("\" defer></script>");
            }

            return builder.ToString();
        }

        /// <summary>
        /// Matches every script tag this plugin owns, whichever quoting style and
        /// trailing newline it was written with. All of the plugin's tags carry the
        /// same <c>plugin="&lt;name&gt;"</c> marker, so one scrub removes the whole
        /// set — that is what makes injection idempotent and lets a stale
        /// <c>?v=</c> tag be REPLACED rather than left behind as a duplicate load.
        /// </summary>
        public static Regex OwnScriptTagRegex(string pluginName)
        {
            var safeName = AttributeEscape(pluginName ?? string.Empty);
            return _scrubRegexes.GetOrAdd(
                safeName,
                name => new Regex(
                    "<script[^>]*plugin=[\"']" + Regex.Escape(name) + "[\"'][^>]*>\\s*</script>\\n?",
                    RegexOptions.IgnoreCase | RegexOptions.CultureInvariant));
        }

        /// <summary>
        /// Scrub-then-insert, so injection CONVERGES instead of accumulating: every
        /// tag this plugin owns (including one written by a legacy on-disk rewrite,
        /// or one carrying a stale <c>?v=</c>) is removed first, then the current
        /// tags are spliced in before the last closing body tag. A document with no
        /// closing body is returned untouched — that shape is also the signature of
        /// a truncated SendFileAsync on client disconnect, and must never be cached.
        /// </summary>
        public static string ReplaceOwnedScriptTags(string html, string pluginName, string currentTags)
        {
            if (string.IsNullOrEmpty(html))
            {
                return html;
            }

            var bodyClose = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
            if (bodyClose < 0)
            {
                return html;
            }

            var scrubbed = OwnScriptTagRegex(pluginName).Replace(html, string.Empty);
            bodyClose = scrubbed.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
            if (bodyClose < 0)
            {
                return html;
            }

            return scrubbed.Substring(0, bodyClose)
                + currentTags
                + "\n"
                + scrubbed.Substring(bodyClose);
        }

        // Minimal HTML attribute escaping. Realistic plugin names contain nothing
        // that needs it, but a name is author-supplied data landing inside a
        // double-quoted attribute, so it gets escaped anyway — and the SAME escaped
        // form feeds OwnScriptTagRegex, so scrub and emit can never disagree.
        internal static string AttributeEscape(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }

            return value
                .Replace("&", "&amp;", StringComparison.Ordinal)
                .Replace("<", "&lt;", StringComparison.Ordinal)
                .Replace(">", "&gt;", StringComparison.Ordinal)
                .Replace("\"", "&quot;", StringComparison.Ordinal)
                .Replace("'", "&#39;", StringComparison.Ordinal);
        }

        // The cache key goes into a query string. Uri.EscapeDataString keeps a
        // version like "1.2.3-638..." untouched while making anything exotic from a
        // custom VersionProvider safe.
        internal static string UrlEscape(string value) =>
            string.IsNullOrEmpty(value) ? string.Empty : Uri.EscapeDataString(value);
    }

    /// <summary>
    /// Payload of the optional version endpoint. Deliberately three flat strings:
    /// <list type="bullet">
    /// <item><description><c>Version</c> — the plugin's assembly version.</description></item>
    /// <item><description><c>BuildId</c> — SHA-256 of the DLL; changes on any rebuild.</description></item>
    /// <item><description><c>CacheKey</c> — exactly what the injected tags carry.</description></item>
    /// </list>
    /// Point the JS kit's <c>versionJsonField</c> at <c>CacheKey</c> (or use the
    /// plain-text variant and skip JSON parsing entirely).
    /// </summary>
    public sealed class RefreshKitVersionInfo
    {
        public RefreshKitVersionInfo(string version, string buildId, string cacheKey)
        {
            Version = version;
            BuildId = buildId;
            CacheKey = cacheKey;
        }

        public string Version { get; }

        public string BuildId { get; }

        public string CacheKey { get; }
    }

    /// <summary>
    /// Opt-in base for a version endpoint.
    /// <para>
    /// Jellyfin scans plugin assemblies for controllers, and this file compiles
    /// INTO your plugin — so a concrete <c>[ApiController]</c> here would be
    /// auto-registered in EVERY plugin that embeds the kit, and two such plugins
    /// would fight over the same route. This class is therefore <c>abstract</c>
    /// (MVC skips abstract controllers) and carries NO route attributes. You
    /// subclass it with your own <c>[Route]</c>, which is unique by construction.
    /// </para>
    /// <example>
    /// <code>
    /// [ApiController]
    /// [Route("MyPlugin")]
    /// public class MyVersionController : RefreshKitVersionControllerBase
    /// {
    ///     [HttpGet("RefreshVersion")]
    ///     [AllowAnonymous]                       // pollable before login
    ///     public ActionResult Version() =&gt; VersionJson();
    /// }
    /// </code>
    /// </example>
    /// </summary>
    public abstract class RefreshKitVersionControllerBase : ControllerBase
    {
        /// <summary>
        /// The current identity triple, with <c>no-store</c> applied. Uses the
        /// registered options' cache key when available so the reported value is
        /// byte-identical to what the injected tags carry.
        /// </summary>
        protected RefreshKitVersionInfo GetVersionCore()
        {
            RefreshKit.ApplyNoStore(Response);
            var options = RefreshKit.Options;
            return new RefreshKitVersionInfo(
                RefreshKit.Version,
                RefreshKit.BuildId,
                options != null ? options.ResolveVersion() : RefreshKit.CacheKey);
        }

        /// <summary>JSON form of <see cref="GetVersionCore"/>. Field casing follows the host's JSON settings.</summary>
        protected ActionResult VersionJson() => Ok(GetVersionCore());

        /// <summary>
        /// Plain-text form: the bare cache key, nothing else. The JS kit reads this
        /// with no <c>versionJsonField</c> configured, which sidesteps any
        /// host-specific JSON naming policy.
        /// </summary>
        protected ActionResult VersionPlainText() => Content(GetVersionCore().CacheKey, "text/plain; charset=utf-8");
    }

    /// <summary>
    /// Injects the plugin's client &lt;script&gt; tags into jellyfin-web's
    /// index.html at request time, via ASP.NET middleware registered through
    /// <see cref="IStartupFilter"/>.
    ///
    /// <para>
    /// Jellyfin 12 serves index.html as a plain static file with no native
    /// script-injection hook, so this runs middleware ahead of the static-file
    /// handler. It works identically on 10.11 and 12 and never writes to the web
    /// folder.
    /// </para>
    ///
    /// <para>The filter is deliberately defensive and convergent:</para>
    /// <list type="bullet">
    /// <item><description>only ever touches the web index.html response;</description></item>
    /// <item><description>removes stale plugin-owned tags and inserts exactly one current set;</description></item>
    /// <item><description>repeated rewrites are idempotent (a stale ?v= tag is replaced, not kept);</description></item>
    /// <item><description>on any error it serves the original response unchanged, never throwing into the pipeline.</description></item>
    /// </list>
    /// </summary>
    internal sealed class RefreshKitScriptInjectionFilter : IStartupFilter
    {
        // PERF: index.html is a hot host path. A warm request performs one
        // validator-only static-file revalidation plus O(1) bounded cache work; it
        // does not buffer or rewrite the shell again. Four fixed admission stripes
        // single-flight each representation key and bound aggregate cold/source-change
        // work. Encoded buffering and decoded/re-encoded transforms stop at 2 MiB;
        // larger or untransformable responses switch to the original host bytes in the
        // same downstream pass. The singleton retains at most 12 representations /
        // 8 MiB, covering the three route spellings and finite identity/gzip/br
        // negotiation without scaling with users or requests.
        internal const int MaxCachedRepresentations = 12;
        internal const int MaxCachedBodyBytes = 8 * 1024 * 1024;
        internal const int MaxCacheableRepresentationBytes = 2 * 1024 * 1024;
        internal const int MaxTransformBodyBytes = MaxCacheableRepresentationBytes;
        internal const int RepresentationGateCount = 4;

        private readonly RefreshKitOptions _options;
        private readonly ILogger _logger;
        private readonly Func<string> _scriptTagProvider;
        private readonly object _cacheLock = new object();
        private readonly Dictionary<string, CachedRepresentation> _cache =
            new Dictionary<string, CachedRepresentation>(StringComparer.Ordinal);
        private readonly LinkedList<string> _leastRecentlyUsed = new LinkedList<string>();
        private readonly SemaphoreSlim[] _representationGates = Enumerable
            .Range(0, RepresentationGateCount)
            .Select(_ => new SemaphoreSlim(1, 1))
            .ToArray();
        private int _cachedBodyBytes;
        private int _loggedOnce;

        internal RefreshKitScriptInjectionFilter(RefreshKitOptions options, ILogger logger)
            : this(options, logger, () => RefreshKit.BuildScriptTags(options))
        {
        }

        /// <summary>
        /// Test-friendly overload: the tag source is injectable so the caching,
        /// precondition and rewrite behaviour can be exercised without a live
        /// plugin instance.
        /// </summary>
        internal RefreshKitScriptInjectionFilter(
            RefreshKitOptions options,
            ILogger logger,
            Func<string> scriptTagProvider)
        {
            _options = options;
            _logger = logger;
            _scriptTagProvider = scriptTagProvider;
        }

        public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next)
        {
            return app =>
            {
                // Registered before the rest of the pipeline (next(app)) so this runs
                // outermost and captures the representation selected by Jellyfin's
                // compression/static-file middleware.
                app.Use(InvokeAsync);
                next(app);
            };
        }

        private async Task InvokeAsync(HttpContext context, Func<Task> nextMw)
        {
            if (!IsIndexRequest(context.Request.Path.Value))
            {
                await nextMw().ConfigureAwait(false);
                return;
            }

            var isHead = HttpMethods.IsHead(context.Request.Method);
            if (!HttpMethods.IsGet(context.Request.Method) && !isHead)
            {
                await nextMw().ConfigureAwait(false);
                return;
            }

            // Kill switch: "do nothing", never "fail the request".
            if (!_options.ResolveEnabled())
            {
                await nextMw().ConfigureAwait(false);
                return;
            }

            string scriptTags;
            try
            {
                scriptTags = _scriptTagProvider();
            }
            catch (Exception ex)
            {
                // The source response has not been claimed yet, so the host can still
                // serve its untouched shell if plugin state is unexpectedly unavailable.
                LogWarning(ex);
                await nextMw().ConfigureAwait(false);
                return;
            }

            if (string.IsNullOrEmpty(scriptTags))
            {
                await nextMw().ConfigureAwait(false);
                return;
            }

            var cacheKey = CreateCacheKey(
                context.Request.Path.Value ?? string.Empty,
                context.Request.Headers["Accept-Encoding"],
                scriptTags);
            var representationGate = _representationGates[cacheKey[0] % RepresentationGateCount];
            await representationGate.WaitAsync(context.RequestAborted).ConfigureAwait(false);
            var ownsRepresentationGate = true;
            var cached = GetCached(cacheKey);

            // A cold HEAD passes straight through: the transform needs the body bytes
            // and HEAD never carries them, so there is no way to produce transformed
            // metadata without issuing our own internal GET. Serving the host's native
            // metadata (source ETag/Last-Modified/Content-Length) keeps uptime monitors
            // and proxy health checks working; a warm HEAD below still answers with the
            // transformed representation's metadata.
            if (isHead && cached == null)
            {
                ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                await nextMw().ConfigureAwait(false);
                return;
            }

            try
            {
                var requestHeaders = context.Request.Headers;
                var originalRange = requestHeaders["Range"];
                var originalIfRange = requestHeaders["If-Range"];
                var clientIfMatch = requestHeaders["If-Match"];
                var clientIfNoneMatch = requestHeaders["If-None-Match"];
                var originalIfUnmodifiedSince = requestHeaders["If-Unmodified-Since"];
                var originalIfModifiedSince = requestHeaders["If-Modified-Since"];
                var hadRange = requestHeaders.ContainsKey("Range");
                var hadIfRange = requestHeaders.ContainsKey("If-Range");
                var hadIfMatch = requestHeaders.ContainsKey("If-Match");
                var hadIfNoneMatch = requestHeaders.ContainsKey("If-None-Match");
                var hadIfUnmodifiedSince = requestHeaders.ContainsKey("If-Unmodified-Since");
                var hadIfModifiedSince = requestHeaders.ContainsKey("If-Modified-Since");

                // Accept-Encoding deliberately stays untouched. Jellyfin's own response
                // compression middleware therefore selects the representation that lands
                // in our bounded response stream; after injection we restore that same
                // encoding and cache the rewritten representation.
                // A byte range of the source index is not a byte range of the rewritten
                // representation. Ignore Range/If-Range for this shell and return the
                // complete injected representation, preventing even a full-file 206 from
                // exposing an uninjected page. The host's validators likewise describe
                // the source index whereas browsers know only the rewritten representation.
                // Translate a warm entry back to source validators for a bodyless host
                // revalidation; a cold request suppresses client validators because they
                // cannot validate the source.
                requestHeaders.Remove("Range");
                requestHeaders.Remove("If-Range");
                requestHeaders.Remove("If-Match");
                requestHeaders.Remove("If-Unmodified-Since");
                if (cached != null)
                {
                    SetOrRemove(requestHeaders, "If-None-Match", cached.SourceETag);
                    SetOrRemove(requestHeaders, "If-Modified-Since", cached.SourceLastModified);
                }
                else
                {
                    requestHeaders.Remove("If-None-Match");
                    requestHeaders.Remove("If-Modified-Since");
                }

                var originalBody = context.Response.Body;
                using var buffer = new BoundedPassthroughStream(
                    originalBody,
                    MaxTransformBodyBytes,
                    () => PrepareSourceFallback(
                        context,
                        clientIfMatch,
                        clientIfNoneMatch,
                        originalIfUnmodifiedSince,
                        originalIfModifiedSince));
                context.Response.Body = buffer;
                try
                {
                    await nextMw().ConfigureAwait(false);
                }
                finally
                {
                    // Everything this middleware borrowed from the request is handed
                    // back exactly as found, whether the downstream pass succeeded or
                    // threw: later middleware/logging must never observe our edits.
                    // The request method is deliberately never rewritten (a cold HEAD
                    // passes through above instead), so there is nothing to restore.
                    context.Response.Body = originalBody;
                    RestoreHeader(requestHeaders, "Range", originalRange, hadRange);
                    RestoreHeader(requestHeaders, "If-Range", originalIfRange, hadIfRange);
                    RestoreHeader(requestHeaders, "If-Match", clientIfMatch, hadIfMatch);
                    RestoreHeader(requestHeaders, "If-None-Match", clientIfNoneMatch, hadIfNoneMatch);
                    RestoreHeader(
                        requestHeaders,
                        "If-Unmodified-Since",
                        originalIfUnmodifiedSince,
                        hadIfUnmodifiedSince);
                    RestoreHeader(
                        requestHeaders,
                        "If-Modified-Since",
                        originalIfModifiedSince,
                        hadIfModifiedSince);
                }

                // The bounded stream already committed or discarded the original source
                // response. In either case the downstream pipeline ran exactly once.
                if (buffer.IsPassthrough)
                {
                    ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                    return;
                }

                // A canceled static-file send can be swallowed by ASP.NET Core.
                // Cancellation must terminate cache admission and must never trigger a
                // second response producer.
                if (context.RequestAborted.IsCancellationRequested)
                {
                    return;
                }

                var encodedSource = buffer.ToArray();

                if (context.Response.StatusCode == StatusCodes.Status304NotModified && cached != null)
                {
                    // The host confirmed the source index is unchanged, so the cached
                    // injected representation is still current. The client's own
                    // validators are answered against the transformed ETag it knows.
                    ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                    await WriteRepresentationAsync(
                        context,
                        cached,
                        EvaluateClientPreconditions(clientIfMatch, clientIfNoneMatch, cached),
                        writeBody: !isHead,
                        originalBody).ConfigureAwait(false);
                    return;
                }

                if (isHead)
                {
                    if (context.Response.StatusCode != StatusCodes.Status200OK)
                    {
                        ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                        await WriteSourceFallbackAsync(
                            context,
                            buffer,
                            clientIfMatch,
                            clientIfNoneMatch,
                            originalIfUnmodifiedSince,
                            originalIfModifiedSince,
                            originalBody).ConfigureAwait(false);
                        return;
                    }

                    // HEAD never downloads the shell just to derive a transformed ETag.
                    // A cold HEAD already passed through untouched, and a warm unchanged
                    // source used the cached 304 path above — so reaching here means the
                    // source CHANGED under a warm entry. The stale transformed metadata
                    // is unsafe: evict the old entry and omit source validators/length
                    // rather than letting caches merge source metadata into a later
                    // injected GET.
                    RemoveCached(cacheKey);
                    context.Response.Headers.Remove("ETag");
                    context.Response.Headers.Remove("Last-Modified");
                    context.Response.Headers.Remove("Accept-Ranges");
                    context.Response.Headers.Remove("Content-Range");
                    // Content-Encoding is source metadata too. The host stamps it on
                    // its bodyless HEAD (Content-Encoding: br), and this branch
                    // deliberately publishes NO entity metadata for the changed
                    // source — leaving the coding behind would let a shared cache
                    // merge "br" onto a stored entry it holds under a different
                    // negotiated coding (RFC 9111 §4.3.4 updates stored headers from
                    // a 304). Same invariant WriteRepresentationAsync states and
                    // enforces for its own bodyless responses. `Vary` deliberately
                    // STAYS: RFC 9110 §15.4.5 requires a 304 to carry the Vary a 200
                    // would have carried, and dropping it is how a negotiating cache
                    // starts serving one coding's bytes for another's request.
                    context.Response.Headers.Remove("Content-Encoding");
                    context.Response.ContentLength = null;
                    context.Response.StatusCode = EvaluateMetadataOnlyHeadPrecondition(
                        clientIfMatch,
                        clientIfNoneMatch);
                    if (context.Response.StatusCode == StatusCodes.Status412PreconditionFailed)
                    {
                        // Static Files applies entity metadata only below 400, so its
                        // native 412 carries no Content-Type either (the same reshape
                        // PrepareSourceFallback performs).
                        context.Response.ContentType = null;
                    }

                    return;
                }

                var isHtml = context.Response.StatusCode == StatusCodes.Status200OK
                    && (context.Response.ContentType?.Contains("text/html", StringComparison.OrdinalIgnoreCase) ?? false);

                if (!isHtml)
                {
                    ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                    await WriteSourceFallbackAsync(
                        context,
                        buffer,
                        clientIfMatch,
                        clientIfNoneMatch,
                        originalIfUnmodifiedSince,
                        originalIfModifiedSince,
                        originalBody).ConfigureAwait(false);
                    return;
                }

                CachedRepresentation? representation = null;
                try
                {
                    if (!TryDecode(encodedSource, context.Response.Headers["Content-Encoding"], out var decodedSource))
                    {
                        // A future host compression provider or a representation whose
                        // decoded size exceeds the cap cannot be safely rewritten.
                    }
                    else
                    {
                        string html;
                        using (var decoded = new MemoryStream(decodedSource, writable: false))
                        using (var reader = new StreamReader(decoded, Encoding.UTF8, true, 1024, leaveOpen: false))
                        {
                            html = await reader.ReadToEndAsync().ConfigureAwait(false);
                        }

                        // A missing closing body is also the signature of the partial
                        // SendFileAsync result seen on disconnect. Never cache it.
                        if (html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            html = RefreshKit.ReplaceOwnedScriptTags(html, _options.PluginName, scriptTags);

                            // STANDALONE-PLUGIN ADAPTATION: third-party tag
                            // stamping runs here, inside the same transform,
                            // so its output is what gets encoded, ETagged and
                            // cached — one representation, one validator.
                            html = _options.ApplyHtmlPostProcess(html);

                            if (Interlocked.Exchange(ref _loggedOnce, 1) == 0)
                            {
                                _logger.LogInformation(
                                    "{Plugin}: injected the client script via request-time middleware (IStartupFilter).",
                                    _options.PluginName);
                            }

                            if (Encoding.UTF8.GetByteCount(html) <= MaxTransformBodyBytes)
                            {
                                var rewrittenUtf8 = Encoding.UTF8.GetBytes(html);
                                if (TryEncode(
                                    rewrittenUtf8,
                                    context.Response.Headers["Content-Encoding"],
                                    out var rewrittenBody))
                                {
                                    // Cache-Control and Vary are inherited from the source
                                    // verbatim: the host, not this plugin, owns the shell's
                                    // freshness and negotiation policy.
                                    representation = new CachedRepresentation(
                                        cacheKey,
                                        rewrittenBody,
                                        CreateETag(rewrittenBody),
                                        context.Response.Headers["ETag"].ToString(),
                                        context.Response.Headers["Last-Modified"].ToString(),
                                        "text/html;charset=utf-8",
                                        context.Response.Headers["Content-Encoding"].ToString(),
                                        context.Response.Headers["Cache-Control"].ToString(),
                                        context.Response.Headers["Vary"].ToString());
                                }
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    // Never break index.html — the original representation and all of
                    // its host-selected headers are still intact.
                    LogWarning(ex);
                }

                if (representation == null)
                {
                    // Commit the buffered source representation from this same downstream
                    // pass. Source preconditions are evaluated before any bytes reach the
                    // transport because the host saw transformed-representation validators.
                    ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                    await WriteSourceFallbackAsync(
                        context,
                        buffer,
                        clientIfMatch,
                        clientIfNoneMatch,
                        originalIfUnmodifiedSince,
                        originalIfModifiedSince,
                        originalBody).ConfigureAwait(false);
                    return;
                }

                if (context.RequestAborted.IsCancellationRequested)
                {
                    return;
                }

                PutCached(representation);
                ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
                await WriteRepresentationAsync(
                    context,
                    representation,
                    EvaluateClientPreconditions(clientIfMatch, clientIfNoneMatch, representation),
                    writeBody: !isHead,
                    originalBody).ConfigureAwait(false);
            }
            finally
            {
                ReleaseRepresentationGate(ref ownsRepresentationGate, representationGate);
            }
        }

        private void LogWarning(Exception ex) =>
            _logger.LogWarning(
                "{Plugin}: refresh-kit injection error (serving original HTML): {Message}",
                _options.PluginName,
                ex.Message);

        // The cache key covers everything that can change the produced bytes: the
        // requested spelling of the shell path, the encoding the host will pick for
        // this Accept-Encoding, and the exact script tags (which carry the plugin
        // version / build id, so a plugin upgrade never reuses an old entry).
        // The path is lower-cased first: IsIndexRequest admits case-insensitive
        // spellings (/web, /Web, /WEB/index.html ...) that all serve the identical
        // document, and keying them verbatim would let a client mint dozens of
        // distinct entries for one representation and churn the 12-entry LRU.
        private static string CreateCacheKey(string path, StringValues acceptEncoding, string scriptTags)
        {
            var material = Encoding.UTF8.GetBytes(
                path.ToLowerInvariant() + "\0" + SelectResponseEncoding(acceptEncoding) + "\0" + scriptTags);
            return Convert.ToHexString(SHA256.HashData(material));
        }

        // Strong validator for the transformed representation, derived from the exact
        // encoded bytes we are about to send. The "rk-" prefix makes it obvious in a
        // trace that the response came from the refresh kit, not the static-file
        // handler, and guarantees it can never collide with the source's ETag.
        private static string CreateETag(byte[] body) =>
            "\"rk-" + Convert.ToHexString(SHA256.HashData(body)).ToLowerInvariant() + "\"";

        private CachedRepresentation? GetCached(string key)
        {
            lock (_cacheLock)
            {
                if (!_cache.TryGetValue(key, out var cached))
                {
                    return null;
                }

                _leastRecentlyUsed.Remove(key);
                _leastRecentlyUsed.AddLast(key);
                return cached;
            }
        }

        private void RemoveCached(string key)
        {
            lock (_cacheLock)
            {
                if (_cache.Remove(key, out var cached))
                {
                    _cachedBodyBytes -= cached.Body.Length;
                    _leastRecentlyUsed.Remove(key);
                }
            }
        }

        private void PutCached(CachedRepresentation representation)
        {
            // Without a source validator, a later request cannot prove that the host
            // file is unchanged. Serve this request correctly but do not retain bytes
            // that could become stale.
            if (representation.Body.Length > MaxCacheableRepresentationBytes
                || (string.IsNullOrEmpty(representation.SourceETag)
                    && string.IsNullOrEmpty(representation.SourceLastModified)))
            {
                return;
            }

            lock (_cacheLock)
            {
                if (_cache.Remove(representation.Key, out var replaced))
                {
                    _cachedBodyBytes -= replaced.Body.Length;
                    _leastRecentlyUsed.Remove(representation.Key);
                }

                while (_cache.Count >= MaxCachedRepresentations
                    || (_cachedBodyBytes + representation.Body.Length > MaxCachedBodyBytes
                        && _leastRecentlyUsed.First != null))
                {
                    var oldestKey = _leastRecentlyUsed.First!.Value;
                    _leastRecentlyUsed.RemoveFirst();
                    if (_cache.Remove(oldestKey, out var oldest))
                    {
                        _cachedBodyBytes -= oldest.Body.Length;
                    }
                }

                _cache[representation.Key] = representation;
                _leastRecentlyUsed.AddLast(representation.Key);
                _cachedBodyBytes += representation.Body.Length;
            }
        }

        private static async Task WriteRepresentationAsync(
            HttpContext context,
            CachedRepresentation representation,
            ClientPrecondition precondition,
            bool writeBody,
            Stream responseBody)
        {
            context.Response.StatusCode = precondition switch
            {
                ClientPrecondition.NotModified => StatusCodes.Status304NotModified,
                ClientPrecondition.Failed => StatusCodes.Status412PreconditionFailed,
                _ => StatusCodes.Status200OK,
            };
            SetOrRemove(context.Response.Headers, "Cache-Control", representation.CacheControl);
            SetOrRemove(context.Response.Headers, "Vary", representation.Vary);
            // The source timestamp cannot describe the injected representation:
            // script generation can change while index.html does not. Publishing it
            // would let an IMS-only client validate a stale generation. The strong,
            // body-derived ETag is the transformed representation's validator.
            context.Response.Headers.Remove("Last-Modified");
            context.Response.Headers["ETag"] = representation.ETag;
            context.Response.Headers.Remove("Accept-Ranges");
            context.Response.Headers.Remove("Content-Range");

            if (precondition != ClientPrecondition.ShouldProcess)
            {
                // Bodyless responses must not advertise a content coding for bytes
                // that are never written: a strict client that pipes anything
                // labelled `Content-Encoding: br` through a decoder would see a
                // truncated stream. Mirror the host's native shape — a 304 keeps
                // Content-Type/ETag but no Content-Encoding; a 412 carries no
                // entity metadata at all (same rule PrepareSourceFallback applies
                // when reproducing the host's native 412).
                context.Response.Headers.Remove("Content-Encoding");
                if (precondition == ClientPrecondition.Failed)
                {
                    context.Response.ContentType = null;
                    context.Response.Headers.Remove("ETag");
                }
                else
                {
                    context.Response.ContentType = representation.ContentType;
                }

                context.Response.ContentLength = null;
                return;
            }

            context.Response.ContentType = representation.ContentType;
            SetOrRemove(context.Response.Headers, "Content-Encoding", representation.ContentEncoding);

            context.Response.ContentLength = representation.Body.Length;
            if (writeBody)
            {
                await responseBody.WriteAsync(
                    representation.Body.AsMemory(),
                    context.RequestAborted).ConfigureAwait(false);
            }
        }

        private static async Task WriteSourceFallbackAsync(
            HttpContext context,
            BoundedPassthroughStream bufferedSource,
            StringValues ifMatch,
            StringValues ifNoneMatch,
            StringValues ifUnmodifiedSince,
            StringValues ifModifiedSince,
            Stream responseBody)
        {
            if (!PrepareSourceFallback(
                context,
                ifMatch,
                ifNoneMatch,
                ifUnmodifiedSince,
                ifModifiedSince))
            {
                return;
            }

            await bufferedSource.CopyBufferedToAsync(
                responseBody,
                context.RequestAborted).ConfigureAwait(false);
        }

        // Decides whether the buffered source bytes should still be written, and when
        // they should not, reshapes the response into the status the host itself would
        // have produced for the client's original (suppressed) preconditions.
        // Returns true when the caller must copy the buffered body out.
        private static bool PrepareSourceFallback(
            HttpContext context,
            StringValues ifMatch,
            StringValues ifNoneMatch,
            StringValues ifUnmodifiedSince,
            StringValues ifModifiedSince)
        {
            if (context.RequestAborted.IsCancellationRequested)
            {
                return false;
            }

            if (context.Response.StatusCode != StatusCodes.Status200OK)
            {
                return !HttpMethods.IsHead(context.Request.Method);
            }

            var sourceETag = context.Response.Headers["ETag"].ToString();
            var sourceLastModified = context.Response.Headers["Last-Modified"].ToString();
            var statusCode = EvaluateSourcePreconditions(
                ifMatch,
                ifNoneMatch,
                ifUnmodifiedSince,
                ifModifiedSince,
                sourceETag,
                sourceLastModified);
            if (statusCode == StatusCodes.Status200OK)
            {
                return !HttpMethods.IsHead(context.Request.Method);
            }

            context.Response.StatusCode = statusCode;
            context.Response.ContentLength = null;
            context.Response.Headers.Remove("Content-Encoding");
            context.Response.Headers.Remove("Content-Range");
            if (statusCode == StatusCodes.Status412PreconditionFailed)
            {
                // Static Files applies entity metadata only to responses below 400.
                // The downstream 200 collected for same-pass fallback already added
                // these fields, so remove them when reproducing its native 412.
                // `Vary` is removed HERE and not above on purpose: a 304 must keep
                // the Vary its 200 would have carried (RFC 9110 §15.4.5) — dropping
                // it leaves a negotiating cache with no constraint to apply when it
                // freshens the stored entry, which is how one coding's bytes start
                // being served for another's request. Same invariant the warm-HEAD
                // branch and WriteRepresentationAsync already enforce for their own
                // synthesized 304s; a 412 publishes no entity metadata at all.
                context.Response.ContentType = null;
                context.Response.Headers.Remove("ETag");
                context.Response.Headers.Remove("Last-Modified");
                context.Response.Headers.Remove("Accept-Ranges");
                context.Response.Headers.Remove("Vary");
            }

            return false;
        }

        private static int EvaluateSourcePreconditions(
            StringValues ifMatch,
            StringValues ifNoneMatch,
            StringValues ifUnmodifiedSince,
            StringValues ifModifiedSince,
            string sourceETag,
            string sourceLastModified)
        {
            var sourceTag = EntityTagHeaderValue.TryParse(
                new StringSegment(sourceETag),
                out var parsedSourceTag)
                ? parsedSourceTag
                : null;
            var ifMatchState = SourcePrecondition.Unspecified;
            var parsedIfMatch = ParseEntityTags(ifMatch);
            if (parsedIfMatch.Count > 0)
            {
                ifMatchState = parsedIfMatch.Any(candidate =>
                    candidate.Equals(EntityTagHeaderValue.Any)
                    || candidate.Compare(sourceTag, useStrongComparison: true))
                    ? SourcePrecondition.ShouldProcess
                    : SourcePrecondition.Failed;
            }

            var ifNoneMatchState = SourcePrecondition.Unspecified;
            var parsedIfNoneMatch = ParseEntityTags(ifNoneMatch);
            if (parsedIfNoneMatch.Count > 0)
            {
                // RFC 9110 §13.1.2: If-None-Match uses the WEAK comparison function
                // (If-Match above correctly stays strong). StaticFileContext's
                // ComputeIfNoneMatch does the same, so a validator weakened by an
                // intermediary (nginx gzip marks ETags W/) still revalidates to a
                // 304 on the source-fallback path instead of a full-body 200.
                ifNoneMatchState = parsedIfNoneMatch.Any(candidate =>
                    candidate.Equals(EntityTagHeaderValue.Any)
                    || candidate.Compare(sourceTag, useStrongComparison: false))
                    ? SourcePrecondition.NotModified
                    : SourcePrecondition.ShouldProcess;
            }

            var now = DateTimeOffset.UtcNow;
            var ifModifiedSinceState = SourcePrecondition.Unspecified;
            var ifUnmodifiedSinceState = SourcePrecondition.Unspecified;
            if (TryParseHttpDate(sourceLastModified, out var lastModified))
            {
                if (TryParseHttpDate(ifModifiedSince, out var modifiedSince)
                    && modifiedSince <= now)
                {
                    ifModifiedSinceState = modifiedSince < lastModified
                        ? SourcePrecondition.ShouldProcess
                        : SourcePrecondition.NotModified;
                }

                if (TryParseHttpDate(ifUnmodifiedSince, out var unmodifiedSince)
                    && unmodifiedSince <= now)
                {
                    ifUnmodifiedSinceState = unmodifiedSince >= lastModified
                        ? SourcePrecondition.ShouldProcess
                        : SourcePrecondition.Failed;
                }
            }

            var state = new[]
            {
                ifMatchState,
                ifNoneMatchState,
                ifModifiedSinceState,
                ifUnmodifiedSinceState,
            }.Max();
            return state switch
            {
                SourcePrecondition.NotModified => StatusCodes.Status304NotModified,
                SourcePrecondition.Failed => StatusCodes.Status412PreconditionFailed,
                _ => StatusCodes.Status200OK,
            };
        }

        private static bool TryParseHttpDate(StringValues value, out DateTimeOffset parsed) =>
            HeaderUtilities.TryParseDate(new StringSegment(value.ToString()), out parsed);

        private static bool TryParseHttpDate(string value, out DateTimeOffset parsed) =>
            HeaderUtilities.TryParseDate(new StringSegment(value), out parsed);

        // The client only ever saw the transformed representation, so its conditional
        // headers are evaluated against the transformed ETag — never the source's.
        private static ClientPrecondition EvaluateClientPreconditions(
            StringValues ifMatch,
            StringValues ifNoneMatch,
            CachedRepresentation representation)
        {
            var parsedIfMatch = ParseEntityTags(ifMatch);
            if (parsedIfMatch.Count > 0
                && !EntityTagSatisfied(
                    parsedIfMatch,
                    representation.ETag,
                    useStrongComparison: true))
            {
                return ClientPrecondition.Failed;
            }

            var parsedIfNoneMatch = ParseEntityTags(ifNoneMatch);
            if (parsedIfNoneMatch.Count > 0)
            {
                return EntityTagSatisfied(
                    parsedIfNoneMatch,
                    representation.ETag,
                    useStrongComparison: false)
                    ? ClientPrecondition.NotModified
                    : ClientPrecondition.ShouldProcess;
            }

            return ClientPrecondition.ShouldProcess;
        }

        // HEAD without a known transformed ETag can only honour wildcard conditions:
        // any concrete entity tag would have to be compared against a validator we
        // deliberately refused to compute (that would mean buffering the whole body).
        private static int EvaluateMetadataOnlyHeadPrecondition(
            StringValues ifMatch,
            StringValues ifNoneMatch)
        {
            var parsedIfMatch = ParseEntityTags(ifMatch);
            if (parsedIfMatch.Count > 0
                && !parsedIfMatch.Any(candidate => candidate.Equals(EntityTagHeaderValue.Any)))
            {
                return StatusCodes.Status412PreconditionFailed;
            }

            var parsedIfNoneMatch = ParseEntityTags(ifNoneMatch);
            return parsedIfNoneMatch.Any(candidate => candidate.Equals(EntityTagHeaderValue.Any))
                ? StatusCodes.Status304NotModified
                : StatusCodes.Status200OK;
        }

        private static IList<EntityTagHeaderValue> ParseEntityTags(StringValues header)
        {
            var values = header
                .Where(value => value != null)
                .Select(value => value!)
                .ToArray();
            return EntityTagHeaderValue.TryParseList(values, out var parsed)
                ? parsed
                : Array.Empty<EntityTagHeaderValue>();
        }

        private static bool EntityTagSatisfied(
            IList<EntityTagHeaderValue> candidates,
            string etag,
            bool useStrongComparison)
        {
            if (!EntityTagHeaderValue.TryParse(new StringSegment(etag), out var parsedETag))
            {
                return candidates.Any(candidate => candidate.Equals(EntityTagHeaderValue.Any));
            }

            return candidates.Any(candidate =>
                candidate.Equals(EntityTagHeaderValue.Any)
                || candidate.Compare(parsedETag, useStrongComparison));
        }

        // Content-Encoding is applied left-to-right by the sender, so decoding walks
        // the list in reverse. An unknown coding fails the whole attempt and the
        // caller falls back to the untouched source bytes.
        private static bool TryDecode(byte[] body, StringValues contentEncoding, out byte[] decoded)
        {
            decoded = body;
            var encodings = ParseEncodings(contentEncoding);
            for (var index = encodings.Count - 1; index >= 0; index--)
            {
                if (!TryTransform(decoded, encodings[index], decompress: true, out decoded))
                {
                    return false;
                }
            }

            return true;
        }

        // Re-applies exactly the codings the host chose, so the rewritten body still
        // matches the Content-Encoding header already on the response.
        private static bool TryEncode(byte[] body, StringValues contentEncoding, out byte[] encoded)
        {
            encoded = body;
            foreach (var encoding in ParseEncodings(contentEncoding))
            {
                if (!TryTransform(encoded, encoding, decompress: false, out encoded))
                {
                    return false;
                }
            }

            return true;
        }

        private static List<string> ParseEncodings(StringValues contentEncoding) =>
            contentEncoding
                .SelectMany(value => (value ?? string.Empty).Split(','))
                .Select(value => value.Trim())
                .Where(value => value.Length > 0 && !value.Equals("identity", StringComparison.OrdinalIgnoreCase))
                .ToList();

        private static bool TryTransform(byte[] source, string encoding, bool decompress, out byte[] transformed)
        {
            transformed = source;
            if (!encoding.Equals("gzip", StringComparison.OrdinalIgnoreCase)
                && !encoding.Equals("br", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            using var input = new MemoryStream(source, writable: false);
            using var output = new BoundedMemoryStream(MaxTransformBodyBytes);
            try
            {
                if (decompress)
                {
                    using Stream decoder = encoding.Equals("gzip", StringComparison.OrdinalIgnoreCase)
                        ? new GZipStream(input, CompressionMode.Decompress)
                        : new BrotliStream(input, CompressionMode.Decompress);
                    decoder.CopyTo(output);
                }
                else
                {
                    using (Stream encoder = encoding.Equals("gzip", StringComparison.OrdinalIgnoreCase)
                        ? new GZipStream(output, CompressionLevel.Fastest, leaveOpen: true)
                        : new BrotliStream(output, CompressionLevel.Fastest, leaveOpen: true))
                    {
                        input.CopyTo(encoder);
                    }
                }
            }
            catch (BodyLimitExceededException)
            {
                // A decompression bomb or an oversized shell: refuse the transform
                // rather than allocating without bound.
                return false;
            }

            transformed = output.ToArray();
            return true;
        }

        private static string SelectResponseEncoding(StringValues acceptEncoding)
        {
            var input = acceptEncoding
                .Where(value => value != null)
                .Select(value => value!)
                .ToArray();
            if (!StringWithQualityHeaderValue.TryParseList(input, out var parsedValues))
            {
                return "identity";
            }

            // Mirror ResponseCompressionProvider's finite candidate selection for
            // Jellyfin's default Brotli/Gzip providers: the first duplicate wins,
            // zero-quality candidates are skipped, and a wildcard adds missing
            // providers then terminates parsing. Keying the selected representation
            // prevents both cross-encoding cache reuse and arbitrary quality-value churn.
            var candidates = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
            foreach (var parsedValue in parsedValues)
            {
                var coding = parsedValue.Value.ToString();
                var quality = parsedValue.Quality ?? 1.0;
                if (quality < double.Epsilon)
                {
                    continue;
                }

                if (coding.Equals("br", StringComparison.OrdinalIgnoreCase)
                    || coding.Equals("gzip", StringComparison.OrdinalIgnoreCase)
                    || coding.Equals("identity", StringComparison.OrdinalIgnoreCase))
                {
                    candidates.TryAdd(coding, quality);
                    continue;
                }

                if (coding.Equals("*", StringComparison.Ordinal))
                {
                    candidates.TryAdd("br", quality);
                    candidates.TryAdd("gzip", quality);
                    break;
                }
            }

            var selected = "identity";
            var selectedQuality = 0.0;
            foreach (var coding in new[] { "br", "gzip", "identity" })
            {
                if (candidates.TryGetValue(coding, out var quality) && quality > selectedQuality)
                {
                    selected = coding;
                    selectedQuality = quality;
                }
            }

            return selected;
        }

        // Every exit path funnels through this so the stripe is released exactly once,
        // as early as the shared work is done, and never twice from the outer finally.
        private static void ReleaseRepresentationGate(
            ref bool ownsRepresentationGate,
            SemaphoreSlim representationGate)
        {
            if (ownsRepresentationGate)
            {
                ownsRepresentationGate = false;
                representationGate.Release();
            }
        }

        private static void SetOrRemove(IHeaderDictionary headers, string name, string? value)
        {
            if (string.IsNullOrEmpty(value))
            {
                headers.Remove(name);
            }
            else
            {
                headers[name] = value;
            }
        }

        private static void RestoreHeader(
            IHeaderDictionary headers,
            string name,
            StringValues originalValue,
            bool existed)
        {
            if (existed)
            {
                headers[name] = originalValue;
            }
            else
            {
                headers.Remove(name);
            }
        }

        // Matches the web app shell however it is requested: bare "/web", "/web/"
        // (SPA serve), and explicit "/web/index.html". EndsWith keeps this correct
        // when Jellyfin is hosted under a base-url prefix (e.g. /jellyfin/web/).
        private static bool IsIndexRequest(string? path)
        {
            if (string.IsNullOrEmpty(path))
            {
                return false;
            }

            return path.EndsWith("/web/index.html", StringComparison.OrdinalIgnoreCase)
                || path.EndsWith("/web/", StringComparison.OrdinalIgnoreCase)
                || path.Equals("/web", StringComparison.OrdinalIgnoreCase);
        }

        private enum ClientPrecondition
        {
            ShouldProcess,
            NotModified,
            Failed,
        }

        // Numeric ordering intentionally matches StaticFileContext.PreconditionState:
        // its native implementation resolves simultaneous fields by taking the
        // highest state.
        private enum SourcePrecondition
        {
            Unspecified,
            NotModified,
            ShouldProcess,
            Failed,
        }

        /// <summary>
        /// MemoryStream that refuses to grow past a fixed limit, so a hostile or
        /// unexpectedly huge compressed payload can't be expanded without bound.
        /// </summary>
        private sealed class BoundedMemoryStream : MemoryStream
        {
            private readonly int _limit;

            public BoundedMemoryStream(int limit)
            {
                _limit = limit;
            }

            public override void Write(byte[] buffer, int offset, int count)
            {
                EnsureCapacityFor(count);
                base.Write(buffer, offset, count);
            }

            public override void Write(ReadOnlySpan<byte> buffer)
            {
                EnsureCapacityFor(buffer.Length);
                base.Write(buffer);
            }

            public override void WriteByte(byte value)
            {
                EnsureCapacityFor(1);
                base.WriteByte(value);
            }

            public override Task WriteAsync(
                byte[] buffer,
                int offset,
                int count,
                CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Write(buffer, offset, count);
                return Task.CompletedTask;
            }

            public override ValueTask WriteAsync(
                ReadOnlyMemory<byte> buffer,
                CancellationToken cancellationToken = default)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Write(buffer.Span);
                return ValueTask.CompletedTask;
            }

            private void EnsureCapacityFor(int count)
            {
                if (count > _limit - Position)
                {
                    throw new BodyLimitExceededException();
                }
            }
        }

        private sealed class BodyLimitExceededException : Exception
        {
        }

        /// <summary>
        /// Buffers the downstream response while it stays under the cap so it can be
        /// rewritten, and transparently switches to writing straight to the real body
        /// once it doesn't. The switch happens inside the single downstream pass, so
        /// an oversized or non-rewritable response is still served correctly and the
        /// pipeline is never run twice.
        /// </summary>
        private sealed class BoundedPassthroughStream : MemoryStream
        {
            public delegate bool PreparePassthroughCallback();

            private readonly Stream _destination;
            private readonly int _limit;
            private readonly PreparePassthroughCallback _preparePassthrough;
            private bool _writeToDestination;

            public BoundedPassthroughStream(
                Stream destination,
                int limit,
                PreparePassthroughCallback preparePassthrough)
            {
                _destination = destination;
                _limit = limit;
                _preparePassthrough = preparePassthrough;
            }

            public bool IsPassthrough { get; private set; }

            public async Task CopyBufferedToAsync(Stream destination, CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Position = 0;
                await CopyToAsync(destination, cancellationToken).ConfigureAwait(false);
            }

            public override void Flush()
            {
                // Deliberately hold flushes while the response remains under the cap.
                // The static app shell is a finite response, and committing a flush
                // would make a later safe rewrite impossible.
                if (IsPassthrough && _writeToDestination)
                {
                    _destination.Flush();
                }
            }

            public override Task FlushAsync(CancellationToken cancellationToken)
            {
                if (IsPassthrough && _writeToDestination)
                {
                    return _destination.FlushAsync(cancellationToken);
                }

                cancellationToken.ThrowIfCancellationRequested();
                return Task.CompletedTask;
            }

            public override void Write(byte[] buffer, int offset, int count)
            {
                if (!IsPassthrough && count <= _limit - Length)
                {
                    base.Write(buffer, offset, count);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.Write(buffer, offset, count);
                }
            }

            public override void Write(ReadOnlySpan<byte> buffer)
            {
                if (!IsPassthrough && buffer.Length <= _limit - Length)
                {
                    base.Write(buffer);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.Write(buffer);
                }
            }

            public override void WriteByte(byte value)
            {
                if (!IsPassthrough && Length < _limit)
                {
                    base.WriteByte(value);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.WriteByte(value);
                }
            }

            public override async Task WriteAsync(
                byte[] buffer,
                int offset,
                int count,
                CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!IsPassthrough && count <= _limit - Length)
                {
                    await base.WriteAsync(
                        buffer.AsMemory(offset, count),
                        cancellationToken).ConfigureAwait(false);
                    return;
                }

                await EnsurePassthroughAsync(cancellationToken).ConfigureAwait(false);
                if (_writeToDestination)
                {
                    await _destination.WriteAsync(
                        buffer.AsMemory(offset, count),
                        cancellationToken).ConfigureAwait(false);
                }
            }

            public override async ValueTask WriteAsync(
                ReadOnlyMemory<byte> buffer,
                CancellationToken cancellationToken = default)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!IsPassthrough && buffer.Length <= _limit - Length)
                {
                    await base.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
                    return;
                }

                await EnsurePassthroughAsync(cancellationToken).ConfigureAwait(false);
                if (_writeToDestination)
                {
                    await _destination.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
                }
            }

            private void EnsurePassthrough()
            {
                if (IsPassthrough)
                {
                    return;
                }

                // The callback decides whether the source response should still be
                // sent at all (it may turn the response into a 304/412 instead).
                _writeToDestination = _preparePassthrough();
                IsPassthrough = true;
                if (_writeToDestination)
                {
                    Position = 0;
                    CopyTo(_destination);
                }

                SetLength(0);
            }

            private async Task EnsurePassthroughAsync(CancellationToken cancellationToken)
            {
                if (IsPassthrough)
                {
                    return;
                }

                _writeToDestination = _preparePassthrough();
                IsPassthrough = true;
                if (_writeToDestination)
                {
                    Position = 0;
                    await CopyToAsync(_destination, cancellationToken).ConfigureAwait(false);
                }

                SetLength(0);
            }
        }

        /// <summary>
        /// One fully-formed, ready-to-send injected representation plus the source
        /// validators needed to revalidate it against the host on the next request.
        /// </summary>
        private sealed class CachedRepresentation
        {
            public CachedRepresentation(
                string key,
                byte[] body,
                string etag,
                string sourceETag,
                string sourceLastModified,
                string contentType,
                string contentEncoding,
                string cacheControl,
                string vary)
            {
                Key = key;
                Body = body;
                ETag = etag;
                SourceETag = sourceETag;
                SourceLastModified = sourceLastModified;
                ContentType = contentType;
                ContentEncoding = contentEncoding;
                CacheControl = cacheControl;
                Vary = vary;
            }

            public string Key { get; }

            public byte[] Body { get; }

            public string ETag { get; }

            public string SourceETag { get; }

            public string SourceLastModified { get; }

            public string ContentType { get; }

            public string ContentEncoding { get; }

            public string CacheControl { get; }

            public string Vary { get; }
        }
    }
}
