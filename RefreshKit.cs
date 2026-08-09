// -----------------------------------------------------------------------------
//  RefreshKit.cs — server-side half of the Jellyfin refresh kit.
//
//  MIT License
//
//  Copyright (c) 2026 4eh5xitv6787h645ebv
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
//  WHAT THIS FILE IS
//  =================
//  Drop this ONE file into any Jellyfin plugin (10.11 or 12) and you get the
//  three server-side layers of the "stale client" fix:
//
//    1. Request-time <script> injection into jellyfin-web's index.html. When
//       this middleware owns the final bytes it provides a revalidating
//       representation cache, strong ETags, 304/412, gzip/brotli preservation,
//       and Range/HEAD handling. If an outer middleware owns a response buffer,
//       it instead preserves that owner's complete framing and serves the
//       transformed shell no-store without validators. Unsupported/error paths
//       preserve a usable host response. No writing to the web folder (which
//       needs a writable root, is wiped on every jellyfin-web update, and does
//       not exist as a supported hook on Jellyfin 12 at all).
//
//    2. A stable opaque loaded-build identity (`RefreshKit.BuildId`) plus an
//       opaque per-build cache key (`RefreshKit.CacheKey`) that is stamped onto
//       every injected tag and onto the `?v=` of every script URL, so a new
//       build is always a NEW URL and can never be served from a browser's HTTP
//       cache. Treat both strings as indivisible tokens; their format is not a
//       parsing contract.
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
//          <script plugin="My Plugin" version="{cache-key}" data-boot-version="{cache-key}"
//                  dev="false" build="{build-id}" src="../MyPlugin/script?v={cache-key}" defer></script>
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
//          EpochProvider = () => ProcessEpoch, // stable opaque token for this process
//          ExtraAttributes = _ => "data-version-url=\"../MyPlugin/RefreshVersion\" "
//                               + "data-version-json-field=\"CacheKey\" "
//                               + "data-version-epoch-json-field=\"Epoch\"",
//
//        `CacheKey` is the right field precisely because it is what
//        BuildScriptTags already stamps into data-boot-version: the seed and
//        the polled value then name the same identity, and the client can tell
//        "the server moved" from "these two values were never comparable".
//        Epoch is a JSON-only process-incarnation sidecar: never put its value
//        in HTML, CacheKey, asset URLs or ETags.
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
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.ResponseCompression;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Primitives;
using Microsoft.Net.Http.Headers;

namespace JellyfinRefreshKit
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
        /// HTML-aware scrubber removes exactly the tags carrying that marker. Two plugins
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
        /// <c>dev="true|false"</c> so client code can branch on it. Dev mode also
        /// adds <c>dev=1</c> to the script URL, preventing an immutable production
        /// response from masking a live toggle. This does NOT change server cache
        /// headers on its own — call
        /// <see cref="RefreshKit.ApplyScriptCacheHeaders(HttpResponse)"/> in your
        /// endpoint, which reads THIS delegate, so the headers and the tag cannot
        /// disagree. (The two-argument overload is there for when you resolve the
        /// flag yourself.)
        /// Defaults to false. Exceptions from the delegate are swallowed.
        /// </summary>
        public Func<bool>? DevMode { get; set; }

        /// <summary>
        /// OPTIONAL. Overrides the cache key stamped into tags and <c>?v=</c>.
        /// Default is the opaque <see cref="RefreshKit.CacheKey"/>. It changes
        /// when a rebuilt plugin is actually loaded, even when the version
        /// number does not, but remains stable while replacement bytes are
        /// merely staged on disk awaiting a restart. Consumers must not parse
        /// the default key's format, length, or segments.
        /// Must be URL- and attribute-safe. Exceptions fall back to the default.
        /// </summary>
        public Func<string>? VersionProvider { get; set; }

        /// <summary>
        /// OPTIONAL. Opaque process-incarnation sidecar for the JSON version
        /// endpoint. It must remain stable for the loaded server process and
        /// change on a genuine restart. It is deliberately excluded from cache
        /// keys, injected HTML values, script URLs and ETags; clients may use it
        /// only to distinguish a fresh process serving a historical generation.
        /// Exceptions or blank values resolve to an absent epoch.
        /// </summary>
        public Func<string>? EpochProvider { get; set; }

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

        /// <summary>Resolved process epoch sidecar; never throws.</summary>
        internal string ResolveEpoch()
        {
            try
            {
                var raw = EpochProvider?.Invoke();
                if (string.IsNullOrEmpty(raw) || raw.Length > 200)
                {
                    return string.Empty;
                }

                var epoch = raw.Trim();
                return string.IsNullOrEmpty(epoch) || epoch[0] == '<'
                    ? string.Empty
                    : epoch;
            }
            catch
            {
                return string.Empty;
            }
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
        // Retained for the public OwnScriptTagRegex compatibility helper. The
        // middleware itself uses the HTML-aware scrubber below; a regex is safe
        // only when a caller already holds an isolated tag string.
        private static readonly ConcurrentDictionary<string, Regex> _scrubRegexes =
            new ConcurrentDictionary<string, Regex>(StringComparer.Ordinal);

        // Identity of the exact module loaded in this process. Never inspect the
        // mutable file at Assembly.Location: Jellyfin can stage/replace that file
        // while the old assembly is still running. The assembly identity plus its
        // MVID comes from loaded metadata, is stable across restarts for the same
        // deterministic build, and changes for a same-version rebuild once that
        // build is actually activated. Computed once per loaded process.
        private static readonly Lazy<string> _buildId = new Lazy<string>(() =>
        {
            var assembly = OwnAssembly;
            var material = string.Concat(
                assembly.FullName ?? assembly.GetName().Name ?? string.Empty,
                "\n",
                assembly.ManifestModule.ModuleVersionId.ToString("N"));
            return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material)))
                .ToLowerInvariant();
        });

        private static readonly Lazy<string> _cacheKey = new Lazy<string>(() =>
            Version + "-" + BuildId.Substring(0, 16));

        /// <summary>
        /// The assembly this file was compiled into — i.e. YOUR plugin. Using
        /// typeof(RefreshKit) rather than Assembly.GetCallingAssembly() keeps the
        /// answer identical no matter who asks (a controller, the middleware, or
        /// an inlined caller).
        /// </summary>
        private static Assembly OwnAssembly => typeof(RefreshKit).Assembly;

        /// <summary>
        /// Opaque per-loaded-build cache key, stamped into every tag and every
        /// <c>?v=</c>. Compare or propagate the whole string only; its format,
        /// length, segments, and relationship to <see cref="BuildId"/> are
        /// implementation details and may change between kit versions.
        /// <para>
        /// The key deliberately does not read the file at
        /// <see cref="Assembly.Location"/>. A replacement can be staged there while
        /// the old module remains loaded; clients must switch only after Jellyfin
        /// activates the replacement (normally at restart).
        /// </para>
        /// </summary>
        public static string CacheKey => _cacheKey.Value;

        /// <summary>Assembly version of the plugin, without the build timestamp.</summary>
        public static string Version =>
            OwnAssembly.GetName().Version?.ToString() ?? "unknown";

        /// <summary>
        /// Opaque identity of the loaded build. Stable for the same deterministic
        /// build across restarts; changes when a rebuilt module is loaded, not
        /// when files are merely staged. Compare or propagate the whole string
        /// only; its digest algorithm, encoding, and length are not a contract.
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
                sp.GetService<ILoggerFactory>()?.CreateLogger("JellyfinRefreshKit") ?? (ILogger)NullLogger.Instance,
                sp.GetService<IResponseCompressionProvider>()));

            return services;
        }

        /// <summary>
        /// One-liner for a script endpoint. Production gets
        /// <c>public, max-age=31536000, immutable</c> — safe precisely because the
        /// injected src carries <c>?v={cacheKey}</c>, so a new build is a new URL.
        /// Dev mode gets <c>no-store</c> plus the legacy anti-cache headers, and
        /// <see cref="BuildScriptTags(RefreshKitOptions)"/> adds <c>dev=1</c> to
        /// the script URL. That separate URL prevents a previously cached
        /// immutable production response from masking a live dev-mode toggle.
        /// The URL marker itself also forces <c>no-store</c>, closing the race in
        /// which the live flag changes between serving the shell and fetching
        /// its script.
        /// </summary>
        public static void ApplyScriptCacheHeaders(HttpResponse response, bool devMode)
        {
            if (response == null)
            {
                return;
            }

            if (devMode || IsDevelopmentScriptRequest(response.HttpContext.Request))
            {
                ApplyNoStore(response);
                return;
            }

            response.Headers["Cache-Control"] = "public, max-age=31536000, immutable";
            response.Headers.Remove("Pragma");
            response.Headers.Remove("Expires");
        }

        private static bool IsDevelopmentScriptRequest(HttpRequest request)
        {
            try
            {
                return request.Query.TryGetValue("dev", out var markers)
                    && markers.Any(marker => string.Equals(marker, "1", StringComparison.Ordinal));
            }
            catch
            {
                // A malformed/unavailable query must never turn a development
                // URL into a year-long immutable response.
                return true;
            }
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
                    .Append(devMode ? "&amp;dev=1" : string.Empty)
                    .Append("\" defer></script>");
            }

            return builder.ToString();
        }

        /// <summary>
        /// Legacy convenience matcher for an isolated script-tag string. Do not
        /// apply it to a complete HTML document: regex cannot distinguish a real
        /// element from tag-like text inside script, style, comments or textarea.
        /// <see cref="ReplaceOwnedScriptTags"/> uses an HTML-aware scrubber.
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

            var scrubbed = RemoveOwnedScriptTags(html, pluginName ?? string.Empty);
            var bodyClose = FindLastHtmlBodyClose(scrubbed);
            if (bodyClose < 0)
            {
                return html;
            }

            return scrubbed.Substring(0, bodyClose)
                + currentTags
                + "\n"
                + scrubbed.Substring(bodyClose);
        }

        /// <summary>
        /// Finds the last actual body end-tag token, rather than the last matching
        /// byte sequence. A raw search can land inside a comment, attribute, script,
        /// style, textarea or template and splice executable markup into inert text.
        /// This deliberately small tokenizer shares the same bounded raw-text rules
        /// as the owned-tag scrubber, accepts legal ASCII whitespace (for example
        /// <c>&lt;/body &gt;</c>), and safely tolerates parser-recovered attributes.
        /// </summary>
        private static int FindLastHtmlBodyClose(string html)
        {
            var lastBodyClose = -1;
            var templateDepth = 0;
            var framesetDepth = 0;
            var sawFrameset = false;
            var elements = new List<HtmlElementContext>();
            var index = 0;
            while (index < html.Length)
            {
                var open = html.IndexOf('<', index);
                if (open < 0)
                {
                    break;
                }

                if (HtmlContainsNonSpace(html, index, open))
                {
                    HtmlEnsureImplicitBody(elements);
                }

                if (HtmlStartsWith(html, open, "<!--"))
                {
                    index = FindHtmlCommentEnd(html, open);
                    continue;
                }

                if (open + 1 >= html.Length)
                {
                    break;
                }

                var closing = html[open + 1] == '/';
                var nameStart = open + (closing ? 2 : 1);
                if (html[open + 1] == '!' || html[open + 1] == '?')
                {
                    var cdataAllowed = HtmlIsCdataAllowedAtCurrentNode(elements);
                    var specialEnd = FindHtmlSpecialMarkupEnd(html, open, cdataAllowed);
                    if (specialEnd < 0)
                    {
                        return -1;
                    }

                    index = specialEnd;
                    continue;
                }

                if (nameStart >= html.Length || !HtmlIsAsciiLetter(html[nameStart]))
                {
                    if (closing)
                    {
                        // An invalid end-tag opener enters bogus-comment state;
                        // quotes do not shield a later '>'.
                        var bogusEnd = html.IndexOf('>', nameStart);
                        index = bogusEnd < 0 ? html.Length : bogusEnd + 1;
                    }
                    else
                    {
                        index = open + 1;
                    }

                    continue;
                }

                var nameEnd = nameStart;
                while (nameEnd < html.Length && HtmlIsNameCharacter(html[nameEnd]))
                {
                    nameEnd++;
                }

                if (!TryFindHtmlTagEnd(html, nameEnd, out var tagEnd))
                {
                    break;
                }

                if (nameEnd >= html.Length
                    || (html[nameEnd] != '>'
                        && html[nameEnd] != '/'
                        && !HtmlIsSpace(html[nameEnd])))
                {
                    // HTML tag names end only at ASCII whitespace, '/' or '>'.
                    // NBSP and punctuation remain part of the name, so a sequence
                    // such as </body > or </body!> is not a body end tag.
                    index = tagEnd + 1;
                    continue;
                }

                var name = html.Substring(nameStart, nameEnd - nameStart);
                if (closing)
                {
                    var isBodyEnd = name.Equals("body", StringComparison.OrdinalIgnoreCase);
                    if (isBodyEnd
                        && !HtmlEndTagTargetsNamespace(
                            elements,
                            "body",
                            HtmlMarkupNamespace.Html))
                    {
                        HtmlEnsureImplicitBody(elements);
                    }

                    HtmlPopForForeignContentEndTagBreakout(elements, name);
                    if (HtmlForeignEndTagRequiresFailClosed(elements, name))
                    {
                        return -1;
                    }

                    if (HtmlEndTagCrossesScopeBoundary(elements, name))
                    {
                        return -1;
                    }

                    var hasForeignAncestor = HtmlHasForeignElement(elements);
                    var closesHtmlElement = HtmlEndTagTargetsNamespace(
                        elements,
                        name,
                        HtmlMarkupNamespace.Html);
                    var acceptsBodyEnd = isBodyEnd
                        && templateDepth == 0
                        && framesetDepth == 0
                        && !sawFrameset
                        && !hasForeignAncestor
                        && !HtmlBodyEndTagBlockedByInsertionMode(elements)
                        && closesHtmlElement;
                    if (closesHtmlElement
                        && name.Equals("frameset", StringComparison.OrdinalIgnoreCase))
                    {
                        if (framesetDepth > 0)
                        {
                            framesetDepth--;
                        }
                    }
                    else if (closesHtmlElement
                        && name.Equals("template", StringComparison.OrdinalIgnoreCase))
                    {
                        if (templateDepth > 0)
                        {
                            templateDepth--;
                        }
                    }
                    else if (acceptsBodyEnd)
                    {
                        lastBodyClose = open;
                    }

                    // The tree builder changes insertion mode for accepted body
                    // and html end tags without popping their stack frames. Keep
                    // them so a later reprocessed body token can still be the
                    // document's last accepted close.
                    if (!isBodyEnd
                        && !name.Equals("html", StringComparison.OrdinalIgnoreCase))
                    {
                        HtmlPopElement(elements, name);
                    }

                    index = tagEnd + 1;
                    continue;
                }

                HtmlPopForForeignContentStartTagBreakout(
                    elements,
                    name,
                    name.Equals("font", StringComparison.OrdinalIgnoreCase)
                        && (HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "color",
                                out _,
                                out _)
                            || HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "face",
                                out _,
                                out _)
                            || HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "size",
                                out _,
                                out _)));
                if (HtmlStartTagImpliesBody(name))
                {
                    HtmlEnsureImplicitBody(elements);
                }

                var elementNamespace = HtmlResolveStartTagNamespace(elements, name);
                var annotationXmlHtmlIntegrationPoint =
                    HtmlIsAnnotationXmlHtmlIntegrationPoint(
                        html,
                        name,
                        elementNamespace,
                        nameEnd,
                        tagEnd);
                if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("base", StringComparison.OrdinalIgnoreCase)
                    && templateDepth == 0
                    && HtmlTryGetAttributeValue(
                        html,
                        nameEnd,
                        tagEnd,
                        "href",
                        out _,
                        out _))
                {
                    // The injected runtime URL is intentionally relative so it
                    // follows Jellyfin's configured PathBase. Any effective base
                    // can redirect that URL to a different path or origin. A small
                    // server-side walker cannot prove DOM base ordering under every
                    // recovery mode, so preserve the source shell byte-for-byte.
                    return -1;
                }

                var selfClosing = HtmlStartTagIsSelfClosing(html, nameEnd, tagEnd);
                if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("frameset", StringComparison.OrdinalIgnoreCase))
                {
                    // Like template, frameset is a non-void HTML element: its
                    // self-closing flag is ignored in text/html.
                    framesetDepth++;
                    sawFrameset = true;
                }
                else if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("template", StringComparison.OrdinalIgnoreCase))
                {
                    // text/html ignores the self-closing flag on non-void HTML
                    // elements, so <template/> remains open until </template>.
                    templateDepth++;
                }

                HtmlPushElement(
                    elements,
                    name,
                    elementNamespace,
                    selfClosing,
                    annotationXmlHtmlIntegrationPoint);
                index = tagEnd + 1;
                if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("plaintext", StringComparison.OrdinalIgnoreCase))
                {
                    break;
                }

                if (elementNamespace == HtmlMarkupNamespace.Html
                    && HtmlIsRawTextElement(name))
                {
                    var rawClose = name.Equals("script", StringComparison.OrdinalIgnoreCase)
                        ? IndexOfHtmlScriptClose(html, index)
                        : IndexOfHtmlClosingTag(html, index, name);
                    index = rawClose < 0 ? html.Length : rawClose;
                }
            }

            return lastBodyClose;
        }

        private static bool HtmlStartTagIsSelfClosing(string html, int nameEnd, int tagEnd)
        {
            // The solidus must be immediately before '>' and must be in the
            // tokenizer's self-closing-start-tag path. In `x=value/>` it belongs
            // to the unquoted value, so an SVG/MathML root remains open.
            if (tagEnd <= nameEnd || html[tagEnd - 1] != '/')
            {
                return false;
            }

            var state = HtmlAttributeScanState.BeforeName;
            var quote = '\0';
            for (var index = nameEnd; index < tagEnd - 1; index++)
            {
                var character = html[index];
                switch (state)
                {
                    case HtmlAttributeScanState.BeforeName:
                        if (HtmlIsSpace(character) || character == '/')
                        {
                            continue;
                        }

                        state = HtmlAttributeScanState.Name;
                        break;
                    case HtmlAttributeScanState.Name:
                        if (HtmlIsSpace(character))
                        {
                            state = HtmlAttributeScanState.AfterName;
                        }
                        else if (character == '=')
                        {
                            state = HtmlAttributeScanState.BeforeValue;
                        }
                        else if (character == '/')
                        {
                            state = HtmlAttributeScanState.BeforeName;
                        }

                        break;
                    case HtmlAttributeScanState.AfterName:
                        if (HtmlIsSpace(character))
                        {
                            continue;
                        }

                        if (character == '=')
                        {
                            state = HtmlAttributeScanState.BeforeValue;
                        }
                        else if (character == '/')
                        {
                            state = HtmlAttributeScanState.BeforeName;
                        }
                        else
                        {
                            state = HtmlAttributeScanState.Name;
                        }

                        break;
                    case HtmlAttributeScanState.BeforeValue:
                        if (HtmlIsSpace(character))
                        {
                            continue;
                        }

                        if (character == '"' || character == '\'')
                        {
                            quote = character;
                            state = HtmlAttributeScanState.QuotedValue;
                        }
                        else
                        {
                            state = HtmlAttributeScanState.UnquotedValue;
                        }

                        break;
                    case HtmlAttributeScanState.QuotedValue:
                        if (character == quote)
                        {
                            quote = '\0';
                            state = HtmlAttributeScanState.AfterQuotedValue;
                        }

                        break;
                    case HtmlAttributeScanState.UnquotedValue:
                        if (HtmlIsSpace(character))
                        {
                            state = HtmlAttributeScanState.BeforeName;
                        }

                        break;
                    case HtmlAttributeScanState.AfterQuotedValue:
                        if (HtmlIsSpace(character) || character == '/')
                        {
                            state = HtmlAttributeScanState.BeforeName;
                        }
                        else
                        {
                            state = HtmlAttributeScanState.Name;
                        }

                        break;
                }
            }

            return state == HtmlAttributeScanState.BeforeName
                || state == HtmlAttributeScanState.Name
                || state == HtmlAttributeScanState.AfterName
                || state == HtmlAttributeScanState.AfterQuotedValue;
        }

        private static HtmlMarkupNamespace HtmlResolveStartTagNamespace(
            List<HtmlElementContext> elements,
            string name)
        {
            var parent = elements.Count > 0
                ? elements[elements.Count - 1]
                : new HtmlElementContext(string.Empty, HtmlMarkupNamespace.Html, false);
            var inHtml = parent.Namespace == HtmlMarkupNamespace.Html
                || parent.IsHtmlIntegrationPoint
                || (HtmlIsMathMlTextIntegrationPoint(parent)
                    && !name.Equals("mglyph", StringComparison.OrdinalIgnoreCase)
                    && !name.Equals("malignmark", StringComparison.OrdinalIgnoreCase))
                || (parent.Namespace == HtmlMarkupNamespace.MathMl
                    && parent.Name.Equals("annotation-xml", StringComparison.OrdinalIgnoreCase)
                    && name.Equals("svg", StringComparison.OrdinalIgnoreCase));
            if (inHtml)
            {
                if (name.Equals("svg", StringComparison.OrdinalIgnoreCase))
                {
                    return HtmlMarkupNamespace.Svg;
                }

                if (name.Equals("math", StringComparison.OrdinalIgnoreCase))
                {
                    return HtmlMarkupNamespace.MathMl;
                }

                return HtmlMarkupNamespace.Html;
            }

            return parent.Namespace;
        }

        private static bool HtmlIsMathMlTextIntegrationPoint(HtmlElementContext element) =>
            element.Namespace == HtmlMarkupNamespace.MathMl
            && (element.Name.Equals("mi", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mo", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mn", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("ms", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mtext", StringComparison.OrdinalIgnoreCase));

        private static bool HtmlIsCdataAllowedAtCurrentNode(
            List<HtmlElementContext> elements)
        {
            if (elements.Count == 0)
            {
                return false;
            }

            var current = elements[elements.Count - 1];
            return current.Namespace != HtmlMarkupNamespace.Html
                && !current.IsHtmlIntegrationPoint
                && !HtmlIsMathMlTextIntegrationPoint(current);
        }

        private static void HtmlPopForForeignContentStartTagBreakout(
            List<HtmlElementContext> elements,
            string name,
            bool isFontBreakout)
        {
            if (!isFontBreakout && !HtmlIsForeignContentBreakoutStartTag(name))
            {
                return;
            }

            HtmlPopOrdinaryForeignAncestors(elements);
        }

        private static void HtmlPopForForeignContentEndTagBreakout(
            List<HtmlElementContext> elements,
            string name)
        {
            if (name.Equals("br", StringComparison.OrdinalIgnoreCase)
                || name.Equals("p", StringComparison.OrdinalIgnoreCase))
            {
                HtmlPopOrdinaryForeignAncestors(elements);
            }
        }

        private static bool HtmlForeignEndTagRequiresFailClosed(
            List<HtmlElementContext> elements,
            string name)
        {
            if (elements.Count == 0
                || elements[elements.Count - 1].Namespace == HtmlMarkupNamespace.Html
                || name.Equals("body", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            var activeNamespace = elements[elements.Count - 1].Namespace;
            for (var index = elements.Count - 1;
                index >= 0 && elements[index].Namespace == activeNamespace;
                index--)
            {
                if (elements[index].Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    return false;
                }
            }

            return true;
        }

        private static bool HtmlEndTagCrossesScopeBoundary(
            List<HtmlElementContext> elements,
            string name)
        {
            if (name.Equals("body", StringComparison.OrdinalIgnoreCase)
                || name.Equals("html", StringComparison.OrdinalIgnoreCase))
            {
                // These tokens are governed by insertion mode/scope checks and
                // are often ignored while a blocker is active. Do not turn a
                // harmless ignored decoy into a whole-document failure.
                return false;
            }

            var crossedBoundary = false;
            for (var index = elements.Count - 1; index >= 0; index--)
            {
                var element = elements[index];
                if (element.Namespace != HtmlMarkupNamespace.Html)
                {
                    continue;
                }

                if (element.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    return crossedBoundary;
                }

                if (HtmlIsScopeBoundary(element.Name))
                {
                    crossedBoundary = true;
                }
            }

            return false;
        }

        private static bool HtmlIsScopeBoundary(string name) =>
            name.Equals("select", StringComparison.OrdinalIgnoreCase)
            || name.Equals("applet", StringComparison.OrdinalIgnoreCase)
            || name.Equals("caption", StringComparison.OrdinalIgnoreCase)
            || name.Equals("marquee", StringComparison.OrdinalIgnoreCase)
            || name.Equals("object", StringComparison.OrdinalIgnoreCase)
            || name.Equals("table", StringComparison.OrdinalIgnoreCase)
            || name.Equals("td", StringComparison.OrdinalIgnoreCase)
            || name.Equals("th", StringComparison.OrdinalIgnoreCase)
            || name.Equals("template", StringComparison.OrdinalIgnoreCase);

        private static bool HtmlEnsureImplicitBody(
            List<HtmlElementContext> elements)
        {
            var hasHtml = false;
            foreach (var element in elements)
            {
                if (element.Namespace != HtmlMarkupNamespace.Html)
                {
                    return false;
                }

                if (element.Name.Equals("html", StringComparison.OrdinalIgnoreCase))
                {
                    hasHtml = true;
                }
                else if (element.Name.Equals("body", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
                else if (element.Name.Equals("head", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                else if (element.Name.Equals("frameset", StringComparison.OrdinalIgnoreCase)
                    || element.Name.Equals("template", StringComparison.OrdinalIgnoreCase))
                {
                    return false;
                }
                else
                {
                    return false;
                }
            }

            elements.RemoveAll(element =>
                element.Namespace == HtmlMarkupNamespace.Html
                && element.Name.Equals("head", StringComparison.OrdinalIgnoreCase));
            if (!hasHtml)
            {
                elements.Insert(
                    0,
                    new HtmlElementContext(
                        "html",
                        HtmlMarkupNamespace.Html,
                        isHtmlIntegrationPoint: false));
            }

            elements.Add(new HtmlElementContext(
                "body",
                HtmlMarkupNamespace.Html,
                isHtmlIntegrationPoint: false));
            return true;
        }

        private static bool HtmlStartTagImpliesBody(string name) =>
            !name.Equals("html", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("head", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("body", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("frameset", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("base", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("basefont", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("bgsound", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("link", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("meta", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("noframes", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("noscript", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("script", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("style", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("template", StringComparison.OrdinalIgnoreCase)
            && !name.Equals("title", StringComparison.OrdinalIgnoreCase);

        private static bool HtmlContainsNonSpace(
            string html,
            int start,
            int end)
        {
            for (var index = start; index < end; index++)
            {
                if (!HtmlIsSpace(html[index]))
                {
                    return true;
                }
            }

            return false;
        }

        private static void HtmlPopOrdinaryForeignAncestors(
            List<HtmlElementContext> elements)
        {
            while (elements.Count > 0)
            {
                var current = elements[elements.Count - 1];
                if (current.Namespace == HtmlMarkupNamespace.Html
                    || current.IsHtmlIntegrationPoint
                    || HtmlIsMathMlTextIntegrationPoint(current))
                {
                    return;
                }

                elements.RemoveAt(elements.Count - 1);
            }
        }

        private static bool HtmlIsForeignContentBreakoutStartTag(string name) =>
            name.Equals("b", StringComparison.OrdinalIgnoreCase)
            || name.Equals("big", StringComparison.OrdinalIgnoreCase)
            || name.Equals("blockquote", StringComparison.OrdinalIgnoreCase)
            || name.Equals("body", StringComparison.OrdinalIgnoreCase)
            || name.Equals("br", StringComparison.OrdinalIgnoreCase)
            || name.Equals("center", StringComparison.OrdinalIgnoreCase)
            || name.Equals("code", StringComparison.OrdinalIgnoreCase)
            || name.Equals("dd", StringComparison.OrdinalIgnoreCase)
            || name.Equals("div", StringComparison.OrdinalIgnoreCase)
            || name.Equals("dl", StringComparison.OrdinalIgnoreCase)
            || name.Equals("dt", StringComparison.OrdinalIgnoreCase)
            || name.Equals("em", StringComparison.OrdinalIgnoreCase)
            || name.Equals("embed", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h1", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h2", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h3", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h4", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h5", StringComparison.OrdinalIgnoreCase)
            || name.Equals("h6", StringComparison.OrdinalIgnoreCase)
            || name.Equals("head", StringComparison.OrdinalIgnoreCase)
            || name.Equals("hr", StringComparison.OrdinalIgnoreCase)
            || name.Equals("i", StringComparison.OrdinalIgnoreCase)
            || name.Equals("img", StringComparison.OrdinalIgnoreCase)
            || name.Equals("li", StringComparison.OrdinalIgnoreCase)
            || name.Equals("listing", StringComparison.OrdinalIgnoreCase)
            || name.Equals("menu", StringComparison.OrdinalIgnoreCase)
            || name.Equals("meta", StringComparison.OrdinalIgnoreCase)
            || name.Equals("nobr", StringComparison.OrdinalIgnoreCase)
            || name.Equals("ol", StringComparison.OrdinalIgnoreCase)
            || name.Equals("p", StringComparison.OrdinalIgnoreCase)
            || name.Equals("pre", StringComparison.OrdinalIgnoreCase)
            || name.Equals("ruby", StringComparison.OrdinalIgnoreCase)
            || name.Equals("s", StringComparison.OrdinalIgnoreCase)
            || name.Equals("small", StringComparison.OrdinalIgnoreCase)
            || name.Equals("span", StringComparison.OrdinalIgnoreCase)
            || name.Equals("strong", StringComparison.OrdinalIgnoreCase)
            || name.Equals("strike", StringComparison.OrdinalIgnoreCase)
            || name.Equals("sub", StringComparison.OrdinalIgnoreCase)
            || name.Equals("sup", StringComparison.OrdinalIgnoreCase)
            || name.Equals("table", StringComparison.OrdinalIgnoreCase)
            || name.Equals("tt", StringComparison.OrdinalIgnoreCase)
            || name.Equals("u", StringComparison.OrdinalIgnoreCase)
            || name.Equals("ul", StringComparison.OrdinalIgnoreCase)
            || name.Equals("var", StringComparison.OrdinalIgnoreCase);

        private static bool HtmlIsAnnotationXmlHtmlIntegrationPoint(
            string html,
            string name,
            HtmlMarkupNamespace elementNamespace,
            int attributesStart,
            int tagEnd)
        {
            if (elementNamespace != HtmlMarkupNamespace.MathMl
                || !name.Equals("annotation-xml", StringComparison.OrdinalIgnoreCase)
                || !HtmlTryGetAttributeValue(
                    html,
                    attributesStart,
                    tagEnd,
                    "encoding",
                    out var valueStart,
                    out var valueLength))
            {
                return false;
            }

            var value = HtmlDecodeAnnotationXmlEncoding(
                html.Substring(valueStart, valueLength));
            return value.Equals("text/html", StringComparison.OrdinalIgnoreCase)
                || value.Equals("application/xhtml+xml", StringComparison.OrdinalIgnoreCase);
        }

        private static string HtmlDecodeAnnotationXmlEncoding(string rawValue)
        {
            var decoded = new StringBuilder(rawValue.Length);
            for (var index = 0; index < rawValue.Length; index++)
            {
                if (rawValue[index] != '&')
                {
                    decoded.Append(rawValue[index]);
                    continue;
                }

                if (HtmlTryDecodeNumericCharacterReference(
                        rawValue,
                        index,
                        out var numericValue,
                        out var numericEnd))
                {
                    decoded.Append(numericValue);
                    index = numericEnd - 1;
                    continue;
                }

                var semicolon = rawValue.IndexOf(';', index + 1);
                if (semicolon >= 0)
                {
                    var reference = rawValue.Substring(index, semicolon + 1 - index);
                    string value;
                    if (reference.Equals("&sol;", StringComparison.Ordinal))
                    {
                        value = "/";
                    }
                    else if (reference.Equals("&plus;", StringComparison.Ordinal))
                    {
                        value = "+";
                    }
                    else
                    {
                        value = WebUtility.HtmlDecode(reference);
                    }

                    if (!value.Equals(reference, StringComparison.Ordinal))
                    {
                        decoded.Append(value);
                        index = semicolon;
                        continue;
                    }
                }

                decoded.Append('&');
            }

            return decoded.ToString();
        }

        private static bool HtmlTryDecodeNumericCharacterReference(
            string value,
            int ampersand,
            out string decoded,
            out int end)
        {
            decoded = string.Empty;
            end = ampersand;
            var index = ampersand + 1;
            if (index >= value.Length || value[index] != '#')
            {
                return false;
            }

            index++;
            var hexadecimal = index < value.Length
                && (value[index] == 'x' || value[index] == 'X');
            if (hexadecimal)
            {
                index++;
            }

            var digitsStart = index;
            var codePoint = 0L;
            while (index < value.Length)
            {
                var digit = hexadecimal
                    ? HtmlHexDigitValue(value[index])
                    : value[index] >= '0' && value[index] <= '9'
                        ? value[index] - '0'
                        : -1;
                if (digit < 0)
                {
                    break;
                }

                codePoint = codePoint > 0x110000
                    ? 0x110000
                    : (codePoint * (hexadecimal ? 16 : 10)) + digit;
                index++;
            }

            if (index == digitsStart)
            {
                return false;
            }

            if (index < value.Length && value[index] == ';')
            {
                index++;
            }

            if (codePoint == 0 || codePoint > 0x10FFFF
                || (codePoint >= 0xD800 && codePoint <= 0xDFFF))
            {
                codePoint = 0xFFFD;
            }

            decoded = char.ConvertFromUtf32((int)codePoint);
            end = index;
            return true;
        }

        private static int HtmlHexDigitValue(char value) =>
            value >= '0' && value <= '9'
                ? value - '0'
                : value >= 'a' && value <= 'f'
                    ? value - 'a' + 10
                    : value >= 'A' && value <= 'F'
                        ? value - 'A' + 10
                        : -1;

        private static bool HtmlTryGetAttributeValue(
            string html,
            int from,
            int tagEnd,
            string targetName,
            out int valueStart,
            out int valueLength)
        {
            valueStart = -1;
            valueLength = 0;
            var index = from;
            while (index < tagEnd)
            {
                while (index < tagEnd && (HtmlIsSpace(html[index]) || html[index] == '/'))
                {
                    index++;
                }

                if (index >= tagEnd)
                {
                    break;
                }

                var nameStart = index;
                while (index < tagEnd
                    && html[index] != '='
                    && html[index] != '/'
                    && !HtmlIsSpace(html[index]))
                {
                    index++;
                }

                var nameLength = index - nameStart;
                var isTarget = nameLength == targetName.Length
                    && string.Compare(
                        html,
                        nameStart,
                        targetName,
                        0,
                        targetName.Length,
                        StringComparison.OrdinalIgnoreCase) == 0;
                while (index < tagEnd && HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (index >= tagEnd || html[index] != '=')
                {
                    if (isTarget)
                    {
                        valueStart = index;
                        return true;
                    }

                    continue;
                }

                index++;
                while (index < tagEnd && HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (index >= tagEnd)
                {
                    return isTarget;
                }

                var quote = html[index];
                if (quote == '"' || quote == '\'')
                {
                    var start = ++index;
                    while (index < tagEnd && html[index] != quote)
                    {
                        index++;
                    }

                    if (index >= tagEnd)
                    {
                        return false;
                    }

                    if (isTarget)
                    {
                        valueStart = start;
                        valueLength = index - start;
                        return true;
                    }

                    index++;
                    continue;
                }

                var unquotedStart = index;
                while (index < tagEnd && !HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (isTarget)
                {
                    valueStart = unquotedStart;
                    valueLength = index - unquotedStart;
                    return true;
                }
            }

            return false;
        }

        private static void HtmlPushElement(
            List<HtmlElementContext> elements,
            string name,
            HtmlMarkupNamespace elementNamespace,
            bool selfClosing,
            bool annotationXmlHtmlIntegrationPoint)
        {
            if ((elementNamespace != HtmlMarkupNamespace.Html && selfClosing)
                || (elementNamespace == HtmlMarkupNamespace.Html && HtmlIsVoidElement(name)))
            {
                return;
            }

            // text/html ignores the self-closing flag on non-void HTML tags.
            var isHtmlIntegrationPoint = annotationXmlHtmlIntegrationPoint
                || (elementNamespace == HtmlMarkupNamespace.Svg
                    && (name.Equals("foreignobject", StringComparison.OrdinalIgnoreCase)
                        || name.Equals("desc", StringComparison.OrdinalIgnoreCase)
                        || name.Equals("title", StringComparison.OrdinalIgnoreCase)));
            elements.Add(new HtmlElementContext(
                name,
                elementNamespace,
                isHtmlIntegrationPoint));
        }

        private static void HtmlPopElement(List<HtmlElementContext> elements, string name)
        {
            if (elements.Count == 0)
            {
                return;
            }

            // Never pop through a namespace boundary on malformed markup. That
            // keeps an unmatched body-shaped token inside SVG/MathML from making
            // a later source position look safely HTML.
            var activeNamespace = elements[elements.Count - 1].Namespace;
            for (var index = elements.Count - 1;
                index >= 0 && elements[index].Namespace == activeNamespace;
                index--)
            {
                if (elements[index].Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    elements.RemoveRange(index, elements.Count - index);
                    return;
                }
            }
        }

        private static bool HtmlEndTagTargetsNamespace(
            List<HtmlElementContext> elements,
            string name,
            HtmlMarkupNamespace elementNamespace)
        {
            if (elements.Count == 0
                || elements[elements.Count - 1].Namespace != elementNamespace)
            {
                return false;
            }

            for (var index = elements.Count - 1;
                index >= 0 && elements[index].Namespace == elementNamespace;
                index--)
            {
                if (elements[index].Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool HtmlHasForeignElement(List<HtmlElementContext> elements)
        {
            foreach (var element in elements)
            {
                if (element.Namespace != HtmlMarkupNamespace.Html)
                {
                    return true;
                }
            }

            return false;
        }

        private static bool HtmlBodyEndTagBlockedByInsertionMode(
            List<HtmlElementContext> elements)
        {
            for (var index = elements.Count - 1; index >= 0; index--)
            {
                var element = elements[index];
                if (element.Namespace != HtmlMarkupNamespace.Html)
                {
                    return true;
                }

                var name = element.Name;
                if (name.Equals("body", StringComparison.OrdinalIgnoreCase))
                {
                    return false;
                }

                // `</body>` requires body in standard scope. Select additionally
                // ignores it in the select insertion mode even though body is in
                // standard scope.
                if (name.Equals("select", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("applet", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("caption", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("html", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("marquee", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("object", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("table", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("td", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("th", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("template", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return true;
        }

        private static bool HtmlIsVoidElement(string name) =>
            name.Equals("area", StringComparison.OrdinalIgnoreCase)
            || name.Equals("base", StringComparison.OrdinalIgnoreCase)
            || name.Equals("br", StringComparison.OrdinalIgnoreCase)
            || name.Equals("col", StringComparison.OrdinalIgnoreCase)
            || name.Equals("embed", StringComparison.OrdinalIgnoreCase)
            || name.Equals("hr", StringComparison.OrdinalIgnoreCase)
            || name.Equals("img", StringComparison.OrdinalIgnoreCase)
            || name.Equals("input", StringComparison.OrdinalIgnoreCase)
            || name.Equals("link", StringComparison.OrdinalIgnoreCase)
            || name.Equals("meta", StringComparison.OrdinalIgnoreCase)
            || name.Equals("param", StringComparison.OrdinalIgnoreCase)
            || name.Equals("source", StringComparison.OrdinalIgnoreCase)
            || name.Equals("track", StringComparison.OrdinalIgnoreCase)
            || name.Equals("wbr", StringComparison.OrdinalIgnoreCase);

        /// <summary>
        /// Removes only real, empty script elements carrying this plugin's
        /// marker. A whole-document regex cannot distinguish markup from a
        /// JavaScript string, comment, textarea, or legacy script double-escaped
        /// state; applying it there corrupts source code. This bounded tokenizer
        /// keeps every byte other than a confirmed kit-owned element.
        /// </summary>
        private static string RemoveOwnedScriptTags(string html, string pluginName)
        {
            StringBuilder? output = null;
            var elements = new List<HtmlElementContext>();
            var copyFrom = 0;
            var index = 0;
            while (index < html.Length)
            {
                var open = html.IndexOf('<', index);
                if (open < 0)
                {
                    break;
                }

                if (HtmlStartsWith(html, open, "<!--"))
                {
                    index = FindHtmlCommentEnd(html, open);
                    continue;
                }

                if (open + 1 >= html.Length)
                {
                    break;
                }

                if (html[open + 1] == '!' || html[open + 1] == '?')
                {
                    var cdataAllowed = HtmlIsCdataAllowedAtCurrentNode(elements);
                    var specialEnd = FindHtmlSpecialMarkupEnd(html, open, cdataAllowed);
                    if (specialEnd < 0)
                    {
                        return html;
                    }

                    index = specialEnd;
                    continue;
                }

                if (html[open + 1] == '/')
                {
                    var closeNameStart = open + 2;
                    if (closeNameStart >= html.Length
                        || !HtmlIsAsciiLetter(html[closeNameStart]))
                    {
                        var bogusEnd = html.IndexOf('>', closeNameStart);
                        index = bogusEnd < 0 ? html.Length : bogusEnd + 1;
                        continue;
                    }

                    var closeNameEnd = closeNameStart;
                    while (closeNameEnd < html.Length
                        && html[closeNameEnd] != '>'
                        && html[closeNameEnd] != '/'
                        && !HtmlIsSpace(html[closeNameEnd]))
                    {
                        closeNameEnd++;
                    }

                    if (!TryFindHtmlTagEnd(html, closeNameEnd, out var closeTagEnd))
                    {
                        break;
                    }

                    if (closeNameEnd > closeNameStart)
                    {
                        var closeName = html.Substring(
                            closeNameStart,
                            closeNameEnd - closeNameStart);
                        HtmlPopForForeignContentEndTagBreakout(elements, closeName);
                        if (HtmlForeignEndTagRequiresFailClosed(elements, closeName))
                        {
                            return html;
                        }

                        if (HtmlEndTagCrossesScopeBoundary(elements, closeName))
                        {
                            return html;
                        }

                        if (!closeName.Equals("body", StringComparison.OrdinalIgnoreCase)
                            && !closeName.Equals("html", StringComparison.OrdinalIgnoreCase))
                        {
                            HtmlPopElement(elements, closeName);
                        }
                    }

                    index = closeTagEnd + 1;
                    continue;
                }

                if (!HtmlIsAsciiLetter(html[open + 1]))
                {
                    index = open + 1;
                    continue;
                }

                var nameEnd = open + 1;
                while (nameEnd < html.Length
                    && html[nameEnd] != '>'
                    && html[nameEnd] != '/'
                    && !HtmlIsSpace(html[nameEnd]))
                {
                    nameEnd++;
                }

                var name = html.Substring(open + 1, nameEnd - open - 1);
                if (!TryReadHtmlTag(
                        html,
                        nameEnd,
                        out var tagEnd,
                        out var pluginValueStart,
                        out var pluginValueLength))
                {
                    break;
                }

                HtmlPopForForeignContentStartTagBreakout(
                    elements,
                    name,
                    name.Equals("font", StringComparison.OrdinalIgnoreCase)
                        && (HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "color",
                                out _,
                                out _)
                            || HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "face",
                                out _,
                                out _)
                            || HtmlTryGetAttributeValue(
                                html,
                                nameEnd,
                                tagEnd,
                                "size",
                                out _,
                                out _)));
                var elementNamespace = HtmlResolveStartTagNamespace(elements, name);
                var annotationXmlHtmlIntegrationPoint =
                    HtmlIsAnnotationXmlHtmlIntegrationPoint(
                        html,
                        name,
                        elementNamespace,
                        nameEnd,
                        tagEnd);
                if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("script", StringComparison.OrdinalIgnoreCase)
                    && pluginValueStart >= 0
                    && WebUtility.HtmlDecode(
                        html.Substring(pluginValueStart, pluginValueLength))
                        .Equals(pluginName, StringComparison.Ordinal))
                {
                    var close = tagEnd + 1;
                    while (close < html.Length && HtmlIsSpace(html[close]))
                    {
                        close++;
                    }

                    if (HtmlIsTagSequence(html, close, closing: true, name: "script")
                        && TryFindHtmlTagEnd(html, close + 2 + "script".Length, out var closeEnd))
                    {
                        var removalEnd = closeEnd + 1;
                        if (removalEnd < html.Length && html[removalEnd] == '\r')
                        {
                            removalEnd++;
                        }

                        if (removalEnd < html.Length && html[removalEnd] == '\n')
                        {
                            removalEnd++;
                        }

                        output ??= new StringBuilder(html.Length);
                        output.Append(html, copyFrom, open - copyFrom);
                        copyFrom = removalEnd;
                        index = removalEnd;
                        continue;
                    }
                }

                index = tagEnd + 1;
                var selfClosing = HtmlStartTagIsSelfClosing(html, nameEnd, tagEnd);
                HtmlPushElement(
                    elements,
                    name,
                    elementNamespace,
                    selfClosing,
                    annotationXmlHtmlIntegrationPoint);
                if (elementNamespace == HtmlMarkupNamespace.Html
                    && name.Equals("plaintext", StringComparison.OrdinalIgnoreCase))
                {
                    index = html.Length;
                }
                else if (elementNamespace == HtmlMarkupNamespace.Html
                    && HtmlIsRawTextElement(name))
                {
                    var rawClose = name.Equals("script", StringComparison.OrdinalIgnoreCase)
                        ? IndexOfHtmlScriptClose(html, index)
                        : IndexOfHtmlClosingTag(html, index, name);
                    index = rawClose < 0 ? html.Length : rawClose;
                }
            }

            if (output == null)
            {
                return html;
            }

            output.Append(html, copyFrom, html.Length - copyFrom);
            return output.ToString();
        }

        private static bool TryReadHtmlTag(
            string html,
            int from,
            out int tagEnd,
            out int pluginValueStart,
            out int pluginValueLength)
        {
            tagEnd = -1;
            pluginValueStart = -1;
            pluginValueLength = 0;
            var index = from;
            while (index < html.Length)
            {
                while (index < html.Length && HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (index >= html.Length)
                {
                    return false;
                }

                if (html[index] == '>')
                {
                    tagEnd = index;
                    return true;
                }

                if (html[index] == '/')
                {
                    index++;
                    continue;
                }

                var attributeNameStart = index;
                while (index < html.Length
                       && html[index] != '='
                       && html[index] != '>'
                       && html[index] != '/'
                       && !HtmlIsSpace(html[index]))
                {
                    index++;
                }

                var attributeNameLength = index - attributeNameStart;
                while (index < html.Length && HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (index >= html.Length)
                {
                    return false;
                }

                if (html[index] != '=')
                {
                    continue;
                }

                index++;
                while (index < html.Length && HtmlIsSpace(html[index]))
                {
                    index++;
                }

                if (index >= html.Length)
                {
                    return false;
                }

                var quote = html[index];
                int valueStart;
                int valueLength;
                if (quote == '"' || quote == '\'')
                {
                    valueStart = index + 1;
                    var valueEnd = html.IndexOf(quote, valueStart);
                    if (valueEnd < 0)
                    {
                        return false;
                    }

                    valueLength = valueEnd - valueStart;
                    index = valueEnd + 1;
                }
                else
                {
                    valueStart = index;
                    while (index < html.Length && html[index] != '>' && !HtmlIsSpace(html[index]))
                    {
                        index++;
                    }

                    valueLength = index - valueStart;
                }

                if (pluginValueStart < 0
                    && attributeNameLength == "plugin".Length
                    && string.Compare(
                        html,
                        attributeNameStart,
                        "plugin",
                        0,
                        "plugin".Length,
                        StringComparison.OrdinalIgnoreCase) == 0)
                {
                    pluginValueStart = valueStart;
                    pluginValueLength = valueLength;
                }
            }

            return false;
        }

        private static bool TryFindHtmlTagEnd(string html, int from, out int tagEnd)
        {
            var quote = '\0';
            for (var index = from; index < html.Length; index++)
            {
                var character = html[index];
                if (quote != '\0')
                {
                    if (character == quote)
                    {
                        quote = '\0';
                    }

                    continue;
                }

                if (character == '"' || character == '\'')
                {
                    quote = character;
                }
                else if (character == '>')
                {
                    tagEnd = index;
                    return true;
                }
            }

            tagEnd = -1;
            return false;
        }

        private static int FindHtmlCommentEnd(string html, int open)
        {
            var contentStart = open + 4;
            if (contentStart >= html.Length)
            {
                return html.Length;
            }

            // Abrupt closes from the HTML comment-start states.
            if (html[contentStart] == '>')
            {
                return contentStart + 1;
            }

            if (html[contentStart] == '-'
                && contentStart + 1 < html.Length
                && html[contentStart + 1] == '>')
            {
                return contentStart + 2;
            }

            for (var index = contentStart; index < html.Length; index++)
            {
                // In the HTML comment less-than-sign-bang-dash-dash state,
                // a greater-than sign abruptly ends the comment.  This makes
                // a nested literal "<!-->" expose the markup that follows it.
                if (HtmlStartsWith(html, index, "<!-->"))
                {
                    return index + 5;
                }

                if (HtmlStartsWith(html, index, "-->"))
                {
                    return index + 3;
                }

                // The comment-end-bang recovery state closes this spelling.
                if (HtmlStartsWith(html, index, "--!>"))
                {
                    return index + 4;
                }
            }

            return html.Length;
        }

        private static int FindHtmlSpecialMarkupEnd(
            string html,
            int open,
            bool cdataAllowed)
        {
            if (cdataAllowed && HtmlStartsWith(html, open, "<![CDATA["))
            {
                var cdataEnd = html.IndexOf("]]>", open + 9, StringComparison.Ordinal);
                return cdataEnd < 0 ? html.Length : cdataEnd + 3;
            }

            if (HtmlStartsWithIgnoreCase(html, open, "<!doctype"))
            {
                var doctypeClose = html.IndexOf('>', open + 2);
                if (doctypeClose < 0)
                {
                    return html.Length;
                }

                for (var index = open + 2; index < doctypeClose; index++)
                {
                    if (html[index] == '"' || html[index] == '\'')
                    {
                        return -1;
                    }
                }

                return doctypeClose + 1;
            }

            if (html[open + 1] == '/')
            {
                return TryFindHtmlTagEnd(html, open + 2, out var tagEnd)
                    ? tagEnd + 1
                    : html.Length;
            }

            var close = html.IndexOf('>', open + 1);
            return close < 0 ? html.Length : close + 1;
        }

        private static int IndexOfHtmlClosingTag(string html, int from, string name)
        {
            var index = from;
            while ((index = html.IndexOf("</", index, StringComparison.Ordinal)) >= 0)
            {
                if (HtmlIsTagSequence(html, index, closing: true, name: name))
                {
                    return index;
                }

                index += 2;
            }

            return -1;
        }

        private static int IndexOfHtmlScriptClose(string html, int from)
        {
            var state = HtmlScriptTextState.Normal;
            var index = from;
            while (index < html.Length)
            {
                if (state != HtmlScriptTextState.DoubleEscaped
                    && HtmlIsTagSequence(html, index, closing: true, name: "script"))
                {
                    return index;
                }

                if (state == HtmlScriptTextState.Normal && HtmlStartsWith(html, index, "<!--"))
                {
                    state = HtmlScriptTextState.Escaped;
                    index += 4;
                    continue;
                }

                if (state != HtmlScriptTextState.Normal && HtmlStartsWith(html, index, "-->"))
                {
                    state = HtmlScriptTextState.Normal;
                    index += 3;
                    continue;
                }

                if (state == HtmlScriptTextState.Escaped
                    && HtmlIsTagSequence(html, index, closing: false, name: "script"))
                {
                    state = HtmlScriptTextState.DoubleEscaped;
                    index += "<script".Length;
                    continue;
                }

                if (state == HtmlScriptTextState.DoubleEscaped
                    && HtmlIsTagSequence(html, index, closing: true, name: "script"))
                {
                    state = HtmlScriptTextState.Escaped;
                    index += "</script".Length;
                    continue;
                }

                index++;
            }

            return -1;
        }

        private static bool HtmlIsTagSequence(
            string html,
            int open,
            bool closing,
            string name)
        {
            var prefixLength = closing ? 2 : 1;
            if (open < 0 || open + prefixLength + name.Length > html.Length
                || html[open] != '<'
                || (closing && html[open + 1] != '/'))
            {
                return false;
            }

            var nameStart = open + prefixLength;
            if (string.Compare(
                    html,
                    nameStart,
                    name,
                    0,
                    name.Length,
                    StringComparison.OrdinalIgnoreCase) != 0)
            {
                return false;
            }

            var tail = nameStart + name.Length;
            return tail >= html.Length || html[tail] == '>' || html[tail] == '/'
                || HtmlIsSpace(html[tail]);
        }

        private static bool HtmlIsRawTextElement(string name) =>
            name.Equals("script", StringComparison.OrdinalIgnoreCase)
            || name.Equals("style", StringComparison.OrdinalIgnoreCase)
            || name.Equals("textarea", StringComparison.OrdinalIgnoreCase)
            || name.Equals("title", StringComparison.OrdinalIgnoreCase)
            || name.Equals("xmp", StringComparison.OrdinalIgnoreCase)
            || name.Equals("iframe", StringComparison.OrdinalIgnoreCase)
            || name.Equals("noembed", StringComparison.OrdinalIgnoreCase)
            || name.Equals("noframes", StringComparison.OrdinalIgnoreCase)
            || name.Equals("noscript", StringComparison.OrdinalIgnoreCase);

        private static bool HtmlStartsWith(string html, int index, string value) =>
            index >= 0 && index + value.Length <= html.Length
            && string.CompareOrdinal(html, index, value, 0, value.Length) == 0;

        private static bool HtmlStartsWithIgnoreCase(string html, int index, string value) =>
            index >= 0 && index + value.Length <= html.Length
            && string.Compare(
                html,
                index,
                value,
                0,
                value.Length,
                StringComparison.OrdinalIgnoreCase) == 0;

        private static bool HtmlIsSpace(char value) =>
            value == '\u0009' || value == '\u000A' || value == '\u000C'
            || value == '\u000D' || value == '\u0020';

        private static bool HtmlIsAsciiLetter(char value) =>
            (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z');

        private static bool HtmlIsNameCharacter(char value) =>
            HtmlIsAsciiLetter(value) || (value >= '0' && value <= '9')
            || value == '-' || value == ':' || value == '_';

        private enum HtmlScriptTextState
        {
            Normal,
            Escaped,
            DoubleEscaped,
        }

        private enum HtmlMarkupNamespace
        {
            Html,
            Svg,
            MathMl,
        }

        private readonly struct HtmlElementContext
        {
            public HtmlElementContext(
                string name,
                HtmlMarkupNamespace elementNamespace,
                bool isHtmlIntegrationPoint)
            {
                Name = name;
                Namespace = elementNamespace;
                IsHtmlIntegrationPoint = isHtmlIntegrationPoint;
            }

            public string Name { get; }

            public HtmlMarkupNamespace Namespace { get; }

            public bool IsHtmlIntegrationPoint { get; }
        }

        private enum HtmlAttributeScanState
        {
            BeforeName,
            Name,
            AfterName,
            BeforeValue,
            QuotedValue,
            UnquotedValue,
            AfterQuotedValue,
        }

        // Minimal HTML attribute escaping. Realistic plugin names contain nothing
        // that needs it, but a name is author-supplied data landing inside a
        // double-quoted attribute, so it gets escaped anyway. The scrubber decodes
        // the parsed attribute before comparing it with this original value.
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
    /// Payload of the optional version endpoint. Four flat strings:
    /// <list type="bullet">
    /// <item><description><c>Version</c> — the plugin's assembly version.</description></item>
    /// <item><description><c>BuildId</c> — identity of the loaded module; changes when a rebuilt module is activated.</description></item>
    /// <item><description><c>CacheKey</c> — exactly what the injected tags carry.</description></item>
    /// <item><description><c>Epoch</c> — optional process incarnation; never part of the cache key.</description></item>
    /// </list>
    /// <c>BuildId</c> and <c>CacheKey</c> are opaque tokens: consumers must not
    /// parse or depend on their format, length, segments, or digest algorithm.
    /// Point the JS kit's <c>versionJsonField</c> at <c>CacheKey</c> and, when an
    /// epoch provider is configured, <c>versionEpochJsonField</c> at <c>Epoch</c>
    /// (or use the plain-text variant and skip JSON/epoch parsing entirely).
    /// </summary>
    public sealed class RefreshKitVersionInfo
    {
        public RefreshKitVersionInfo(string version, string buildId, string cacheKey)
            : this(version, buildId, cacheKey, string.Empty)
        {
        }

        public RefreshKitVersionInfo(string version, string buildId, string cacheKey, string epoch)
        {
            Version = version;
            BuildId = buildId;
            CacheKey = cacheKey;
            Epoch = epoch;
        }

        public string Version { get; }

        public string BuildId { get; }

        public string CacheKey { get; }

        public string Epoch { get; }
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
        /// The current identity plus optional process epoch, with <c>no-store</c> applied. Uses the
        /// registered options' cache key when available so the reported value is
        /// byte-identical to what the injected tags carry.
        /// </summary>
        protected RefreshKitVersionInfo GetVersionCore()
        {
            RefreshKit.ApplyNoStore(Response);
            return CreateVersionInfo(RefreshKit.Options);
        }

        /// <summary>Build the endpoint payload from one resolved option set.</summary>
        internal static RefreshKitVersionInfo CreateVersionInfo(RefreshKitOptions? options)
        {
            return new RefreshKitVersionInfo(
                RefreshKit.Version,
                RefreshKit.BuildId,
                options != null ? options.ResolveVersion() : RefreshKit.CacheKey,
                options != null ? options.ResolveEpoch() : string.Empty);
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
        // validator-only static-file revalidation plus fixed-bound cache work; it
        // does not buffer or rewrite the shell again. Four fixed admission stripes
        // single-flight each representation key and bound aggregate cold/source-change
        // work. Encoded buffering and decoded/re-encoded transforms stop at 2 MiB;
        // larger or untransformable responses switch to the original host bytes in the
        // same downstream pass. The singleton retains at most 12 representations /
        // 8 MiB. Scheme, compression policy and source Vary tuples compete in that
        // same LRU; each tuple is capped at 16 fields / 16 KiB of request metadata,
        // so adversarial variant cardinality can cause recomputation but cannot make
        // retained memory scale with users or requests.
        internal const int MaxCachedRepresentations = 12;
        internal const int MaxCachedBodyBytes = 8 * 1024 * 1024;
        internal const int MaxCacheableRepresentationBytes = 2 * 1024 * 1024;
        internal const int MaxTransformBodyBytes = MaxCacheableRepresentationBytes;
        internal const int RepresentationGateCount = 4;
        internal const int MaxVaryHeaders = 16;
        internal const int MaxVaryMetadataCharacters = 16 * 1024;
        internal const int MaxCachedRequestHeaders = 128;
        internal const int MaxCachedRequestHeaderValues = 512;
        internal const int MaxCachedRequestHeaderCharacters = 64 * 1024;
        internal const int MaxCachedResponseHeaders = 64;
        internal const int MaxCachedResponseHeaderValues = 256;
        internal const int MaxCachedResponseHeaderCharacters = 32 * 1024;

        private static readonly Encoding StrictUtf8 =
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

        private static readonly string[] SharedCacheSensitiveRequestHeaders =
        {
            "Authorization",
            "Cookie",
            "Cookie2",
            "Proxy-Authorization",
            "X-Emby-Authorization",
            "X-Emby-Token",
            "X-MediaBrowser-Token",
        };

        private static readonly string[] ForwardingCacheKeyHeaders =
        {
            "Forwarded",
            "X-Forwarded-Proto",
            "X-Forwarded-Host",
            "X-Forwarded-Prefix",
            "X-Forwarded-PathBase",
            "X-Forwarded-Scheme",
            "X-Original-Proto",
            "X-Original-Host",
            "X-Original-Prefix",
            "Front-End-Https",
        };

        private static readonly HashSet<string> NonReplayableResponseHeaders =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                // Managed from the rewritten representation itself.
                "Accept-Ranges",
                "Cache-Control",
                "Content-Encoding",
                "Content-Length",
                "Content-Range",
                "Content-Type",
                "ETag",
                "Last-Modified",
                "Vary",

                // The body changed, so source integrity metadata is invalid.
                "Content-MD5",
                "Digest",
                "Content-Digest",
                "Repr-Digest",
                "Signature",
                "Signature-Input",

                // Hop-by-hop fields and fields nominated by Connection are never
                // properties of a reusable representation.
                "Connection",
                "Keep-Alive",
                "Proxy-Authenticate",
                "Proxy-Authentication-Info",
                "Proxy-Authorization",
                "Proxy-Connection",
                "TE",
                "Trailer",
                "Transfer-Encoding",
                "Upgrade",

                // Per-request transport/diagnostic metadata must come from the
                // current response, not a prior source 200.
                "Age",
                "Date",
                "Request-Id",
                "Server",
                "Server-Timing",
                "Traceparent",
                "Tracestate",
                "X-Request-ID",
                "X-Response-Time-ms",

                // Replaying these as a newly generated response would repeat
                // origin-wide browser side effects or restart a freshness clock.
                "Alt-Svc",
                "Clear-Site-Data",

                // These make admission fail closed and must never be replayed from
                // process-shared state even if a future caller bypasses admission.
                "Authentication-Info",
                "Set-Cookie",
                "Set-Cookie2",
                "WWW-Authenticate",
            };

        private static readonly HashSet<string> ManagedRepresentationHeaders =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "Accept-Ranges",
                "Cache-Control",
                "Content-Encoding",
                "Content-Length",
                "Content-MD5",
                "Content-Range",
                "Content-Type",
                "Digest",
                "Content-Digest",
                "Repr-Digest",
                "ETag",
                "Last-Modified",
                "Signature",
                "Signature-Input",
                "TE",
                "Trailer",
                "Transfer-Encoding",
                "Upgrade",
                "Vary",
            };

        private readonly RefreshKitOptions _options;
        private readonly ILogger _logger;
        private readonly Func<string> _scriptTagProvider;
        private readonly IResponseCompressionProvider? _responseCompressionProvider;
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
            : this(options, logger, () => RefreshKit.BuildScriptTags(options), null)
        {
        }

        internal RefreshKitScriptInjectionFilter(
            RefreshKitOptions options,
            ILogger logger,
            IResponseCompressionProvider? responseCompressionProvider)
            : this(
                options,
                logger,
                () => RefreshKit.BuildScriptTags(options),
                responseCompressionProvider)
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
            : this(options, logger, scriptTagProvider, null)
        {
        }

        internal RefreshKitScriptInjectionFilter(
            RefreshKitOptions options,
            ILogger logger,
            Func<string> scriptTagProvider,
            IResponseCompressionProvider? responseCompressionProvider)
        {
            _options = options;
            _logger = logger;
            _scriptTagProvider = scriptTagProvider;
            _responseCompressionProvider = responseCompressionProvider;
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

            // Registered before any downstream callback so this runs last (ASP.NET
            // invokes OnStarting callbacks LIFO). It rechecks cache admission after
            // late source metadata is present and performs final body-integrity/412
            // cleanup immediately before headers are committed.
            var requestSnapshot = RequestCacheSnapshot.Capture(context);
            var baseCacheKey = CreateBaseCacheKey(
                context,
                context.Request.Path.Value ?? string.Empty,
                scriptTags);
            var representationGate = _representationGates[baseCacheKey[0] % RepresentationGateCount];
            var representationGateLease = 0;
            void ReleaseRepresentationGateLease()
            {
                if (Interlocked.Exchange(ref representationGateLease, 0) == 1)
                {
                    representationGate.Release();
                }
            }

            var responseFinalization = new ResponseFinalizationState(
                this,
                context,
                requestSnapshot,
                ReleaseRepresentationGateLease);
            context.Response.OnStarting(responseFinalization.OnStartingAsync);

            await representationGate.WaitAsync(context.RequestAborted).ConfigureAwait(false);
            Volatile.Write(ref representationGateLease, 1);
            var requestCanUseSharedCache = requestSnapshot.IsSafe;
            var cached = requestCanUseSharedCache
                ? GetCached(baseCacheKey, requestSnapshot)
                : null;

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
            var originalMethod = context.Request.Method;

            try
            {
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
                var originalBodyFeature = context.Features.Get<IHttpResponseBodyFeature>();
                byte[]? committedReplacementBody = null;
                void StartOriginalResponse()
                {
                    var downstreamMethod = context.Request.Method;
                    if (isHead)
                    {
                        context.Request.Method = originalMethod;
                    }

                    try
                    {
                        (originalBodyFeature?.StartAsync(context.RequestAborted)
                            ?? context.Response.StartAsync(context.RequestAborted))
                            .GetAwaiter()
                            .GetResult();
                        if (!context.Response.HasStarted)
                        {
                            responseFinalization.EnsureProvisionalFinalizedAsync()
                                .GetAwaiter()
                                .GetResult();
                        }
                    }
                    finally
                    {
                        context.Request.Method = downstreamMethod;
                    }
                }

                using var buffer = new BoundedPassthroughStream(
                    originalBody,
                    MaxTransformBodyBytes,
                    () =>
                    {
                        if (cached != null
                            && context.Response.StatusCode
                                == StatusCodes.Status304NotModified)
                        {
                            // An explicitly started source 304 still has to be mapped
                            // back to the transformed entity the client knows.
                            var mappedPrecondition = EvaluateClientPreconditions(
                                clientIfMatch,
                                clientIfNoneMatch,
                                cached);
                            responseFinalization.SetRepresentation(
                                cached,
                                mappedPrecondition,
                                isNewRepresentation: false,
                                sourceFallbackBodyLength: 0);
                            StartOriginalResponse();
                            if (!isHead
                                && responseFinalization.ShouldWriteRepresentation
                                && responseFinalization.BodySelection
                                    == ResponseBodySelection.Representation)
                            {
                                committedReplacementBody = cached.Body;
                            }

                            return false;
                        }

                        var shouldWrite = PrepareSourceFallback(
                            context,
                            writeBody: !isHead);
                        responseFinalization.SetSourceFallback(
                            baseCacheKey,
                            context.Response.StatusCode,
                            shouldWrite,
                            isHead,
                            context.Response.ContentLength,
                            context.Response.Headers["Content-Encoding"].ToString(),
                            clientIfMatch,
                            clientIfNoneMatch,
                            originalIfUnmodifiedSince,
                            originalIfModifiedSince);
                        StartOriginalResponse();
                        return responseFinalization.SourceFallbackBodyAllowed;
                    });
                // Do not use HttpResponse.Body's setter here. Its
                // StreamResponseBodyFeature retains the prior transport feature, and
                // SendFileAsync is then allowed to delegate straight to that feature,
                // bypassing the bounded stream entirely. StaticFileMiddleware takes
                // that optimized path for an identity response. Install a feature
                // without a prior SendFile delegate so file sends fall back through
                // the same bounded stream as ordinary writes.
                context.Features.Set<IHttpResponseBodyFeature>(
                    new BoundedResponseBodyFeature(buffer));
                // HEAD describes the representation a corresponding GET would send.
                // Run the downstream shell producer once as GET so a cold HEAD can
                // derive the exact transformed ETag/length and evaluate concrete
                // conditions correctly; suppress only the final response body.
                if (isHead)
                {
                    context.Request.Method = HttpMethods.Get;
                }

                try
                {
                    await nextMw().ConfigureAwait(false);
                }
                finally
                {
                    context.Features.Set(originalBodyFeature);
                    context.Request.Method = originalMethod;
                }

                // The bounded stream already committed or discarded the original source
                // response. In either case the downstream pipeline ran exactly once.
                if (buffer.IsPassthrough)
                {
                    if (committedReplacementBody != null
                        && !context.RequestAborted.IsCancellationRequested)
                    {
                        await originalBody.WriteAsync(
                            committedReplacementBody.AsMemory(),
                            context.RequestAborted).ConfigureAwait(false);
                    }

                    if (buffer.HasWrittenToDestination
                        || committedReplacementBody?.Length > 0)
                    {
                        responseFinalization.MarkOuterBodyPending();
                    }

                    // A promoted HEAD deliberately discards the source GET body, but
                    // still starts the response while suppressed source conditions are
                    // in force so late downstream callbacks cannot validate the wrong
                    // (untransformed) entity.
                    await context.Response.StartAsync(context.RequestAborted).ConfigureAwait(false);
                    return;
                }

                // A downstream component that explicitly starts the response has
                // frozen its own headers before a transformed representation can be
                // finalized. Preserve that source response instead of attempting a
                // half-committed rewrite.
                if (context.Response.HasStarted)
                {
                    if (!isHead)
                    {
                        await buffer.CopyBufferedToAsync(
                            originalBody,
                            context.RequestAborted).ConfigureAwait(false);
                    }

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
                    var cachedClientPrecondition = EvaluateClientPreconditions(
                        clientIfMatch,
                        clientIfNoneMatch,
                        cached);
                    responseFinalization.SetRepresentation(
                        cached,
                        cachedClientPrecondition,
                        isNewRepresentation: false,
                        sourceFallbackBodyLength: 0);
                    await WriteRepresentationAsync(
                        context,
                        cached,
                        writeBody: !isHead,
                        responseFinalization,
                        sourceFallbackBody: null,
                        originalBody).ConfigureAwait(false);
                    return;
                }

                var isHtml = context.Response.StatusCode == StatusCodes.Status200OK
                    && IsHtmlContentType(context.Response.ContentType);

                if (!isHtml)
                {
                    await WriteSourceFallbackAsync(
                        context,
                        buffer,
                        baseCacheKey,
                        clientIfMatch,
                        clientIfNoneMatch,
                            originalIfUnmodifiedSince,
                            originalIfModifiedSince,
                            writeBody: !isHead,
                            responseFinalization,
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
                        using (var reader = new StreamReader(decoded, StrictUtf8, true, 1024, leaveOpen: false))
                        {
                            html = await reader.ReadToEndAsync().ConfigureAwait(false);
                        }

                        // ReplaceOwnedScriptTags returns the same string instance when
                        // no real body end-tag token exists. That is also the signature
                        // of the partial SendFileAsync result seen on disconnect. A raw
                        // byte search is insufficient here because comments/raw text can
                        // contain inert </body> decoys. Never cache either shape.
                        var rewrittenHtml = RefreshKit.ReplaceOwnedScriptTags(
                            html,
                            _options.PluginName,
                            scriptTags);
                        if (!ReferenceEquals(rewrittenHtml, html))
                        {
                            html = rewrittenHtml;

                            if (Encoding.UTF8.GetByteCount(html) <= MaxTransformBodyBytes)
                            {
                                var rewrittenUtf8 = Encoding.UTF8.GetBytes(html);
                                if (TryEncode(
                                    rewrittenUtf8,
                                    context.Response.Headers["Content-Encoding"],
                                    out var rewrittenBody))
                                {
                                    // Source-owned cache policy and replay headers are
                                    // captured later by our final OnStarting callback,
                                    // after every downstream callback has had its say.
                                    representation = CachedRepresentation.CreateCandidate(
                                        baseCacheKey,
                                        rewrittenBody,
                                        CreateETag(rewrittenBody),
                                        "text/html;charset=utf-8",
                                        context.Response.Headers["Content-Encoding"].ToString());
                                    if (Interlocked.Exchange(ref _loggedOnce, 1) == 0)
                                    {
                                        _logger.LogInformation(
                                            "{Plugin}: injected the client script via request-time middleware (IStartupFilter).",
                                            _options.PluginName);
                                    }
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
                    await WriteSourceFallbackAsync(
                        context,
                        buffer,
                        baseCacheKey,
                        clientIfMatch,
                        clientIfNoneMatch,
                        originalIfUnmodifiedSince,
                        originalIfModifiedSince,
                        writeBody: !isHead,
                        responseFinalization,
                        originalBody).ConfigureAwait(false);
                    return;
                }

                if (context.RequestAborted.IsCancellationRequested)
                {
                    return;
                }

                var clientPrecondition = EvaluateClientPreconditions(
                    clientIfMatch,
                    clientIfNoneMatch,
                    representation);
                responseFinalization.SetRepresentation(
                    representation,
                    clientPrecondition,
                    isNewRepresentation: true,
                    sourceFallbackBodyLength: encodedSource.Length);
                await WriteRepresentationAsync(
                    context,
                    representation,
                    writeBody: !isHead,
                    responseFinalization,
                    encodedSource,
                    originalBody).ConfigureAwait(false);
            }
            finally
            {
                // If an outer response buffer absorbed StartAsync, register this
                // only after the complete RK/downstream unwind. LIFO then lets it
                // snapshot the outer owner's final framing before older downstream
                // callbacks run at the real transport boundary.
                responseFinalization.RegisterOuterFramingSnapshotCallback();
                // Everything this middleware borrowed from the request is handed
                // back exactly as found after body finalization has run, whether via
                // the server's OnStarting stack or provisionally for an outer buffer.
                // Keeping conditions suppressed until then prevents source metadata
                // from validating them against the untransformed shell.
                context.Request.Method = originalMethod;
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
                ReleaseRepresentationGateLease();
            }
        }

        private void LogWarning(Exception ex) =>
            _logger.LogWarning(
                "{Plugin}: refresh-kit injection error (serving original HTML): {Message}",
                _options.PluginName,
                ex.Message);

        // The base cache key covers everything known before the source runs: the
        // complete request target (authority, PathBase, path and query), request
        // scheme, ASP.NET's effective compression decision and selected provider,
        // and the exact script tags (which carry the plugin version / build id, so
        // a plugin upgrade never reuses an old entry). Source-declared Vary
        // dimensions are added to each CachedRepresentation and matched separately
        // because they are not known until after the source response is produced.
        // Keep path spelling and query exact: custom middleware can legally vary a
        // shell by either even when Jellyfin's ordinary static-file route does not.
        // Host is lower-cased because URI authority host names are case-insensitive.
        private string CreateBaseCacheKey(HttpContext context, string path, string scriptTags)
        {
            var material = new StringBuilder();
            AppendLengthPrefixed(
                material,
                (context.Request.Host.Value ?? string.Empty).ToLowerInvariant());
            AppendLengthPrefixed(material, context.Request.PathBase.Value ?? string.Empty);
            AppendLengthPrefixed(material, path);
            AppendLengthPrefixed(material, context.Request.QueryString.Value ?? string.Empty);
            AppendLengthPrefixed(
                material,
                context.Features.Get<IHttpRequestFeature>()?.RawTarget ?? string.Empty);
            foreach (var header in ForwardingCacheKeyHeaders)
            {
                AppendLengthPrefixed(material, header);
                VaryRequestValue.Capture(context.Request.Headers, header)
                    .AppendKeyMaterial(material);
            }

            AppendLengthPrefixed(material, CreateCompressionPolicyKey(context));
            AppendLengthPrefixed(material, scriptTags);
            return Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(material.ToString())));
        }

        private string CreateCompressionPolicyKey(HttpContext context)
        {
            var originalMethod = context.Request.Method;
            if (HttpMethods.IsHead(originalMethod))
            {
                context.Request.Method = HttpMethods.Get;
            }

            try
            {
                return CreateCompressionPolicyKeyForGet(context);
            }
            finally
            {
                context.Request.Method = originalMethod;
            }
        }

        private string CreateCompressionPolicyKeyForGet(HttpContext context)
        {
            var scheme = (context.Request.Scheme ?? string.Empty).ToLowerInvariant();
            var fallbackEncoding = SelectResponseEncoding(context.Request.Headers["Accept-Encoding"]);
            if (_responseCompressionProvider == null)
            {
                // A single-file adopter may not install ASP.NET response compression.
                // "unknown" keeps scheme and negotiated request coding separated even
                // though there is no provider whose HTTPS policy we can interrogate.
                return scheme + ":unknown:" + fallbackEncoding;
            }

            try
            {
                if (!_responseCompressionProvider.CheckRequestAcceptsCompression(context))
                {
                    return scheme + ":off:identity";
                }

                var provider = _responseCompressionProvider.GetCompressionProvider(context);
                var encoding = provider?.EncodingName;
                return scheme
                    + ":on:"
                    + (string.IsNullOrWhiteSpace(encoding)
                        ? fallbackEncoding
                        : encoding.ToLowerInvariant());
            }
            catch
            {
                // Cache partitioning is a safety aid, never a reason to fail the shell.
                // Retaining the scheme and request negotiation still prevents the known
                // HTTP/HTTPS and cross-coding reuse classes if a custom provider throws.
                return scheme + ":unknown:" + fallbackEncoding;
            }
        }

        private static CacheAdmission CreateCacheAdmission(
            RequestCacheSnapshot requestSnapshot,
            HttpRequest request,
            HttpResponse response,
            string baseKey)
        {
            var requestIsSafe = requestSnapshot.IsSafe
                && IsSharedCacheSafeRequest(request);
            var varyIsSafe = TryCaptureVary(
                response.Headers["Vary"],
                requestSnapshot,
                out var varyHeaders,
                out var varyRequestValues);
            var responseIsSafe = varyIsSafe
                && !varyHeaders.Any(IsSensitiveCacheDimension)
                && IsSharedCacheSafeResponse(response);
            var canStore = requestIsSafe && responseIsSafe;
            return new CacheAdmission(
                CreateVariantKey(baseKey, varyHeaders, varyRequestValues),
                varyHeaders,
                varyRequestValues,
                canStore,
                // Do not let a personalized request flush otherwise safe anonymous
                // tuples. Origin cache directives and unsafe Vary metadata, however,
                // invalidate retained tuples learned under the old policy.
                requestIsSafe && !responseIsSafe);
        }

        private static CachedRepresentation FinalizeCandidateRepresentation(
            RequestCacheSnapshot requestSnapshot,
            HttpRequest request,
            HttpResponse response,
            CachedRepresentation candidate)
        {
            var admission = CreateCacheAdmission(
                requestSnapshot,
                request,
                response,
                candidate.BaseKey);
            var sourceETag = SanitizeSourceETag(response.Headers["ETag"]);
            var sourceLastModified = SanitizeSourceLastModified(
                response.Headers["Last-Modified"]);
            var sourceContentType = response.ContentType ?? string.Empty;
            var cacheControl = response.Headers["Cache-Control"].ToString();
            var vary = string.Join(", ", admission.VaryHeaders);
            var codingWasNotChangedLate = response.Headers["Content-Encoding"]
                .ToString()
                .Equals(candidate.ContentEncoding, StringComparison.Ordinal);
            var replayHeadersAreSafe = TryCaptureReplayHeaders(
                response.Headers,
                out var replayHeaders,
                out var connectionNominated)
                && !connectionNominated.Any(ManagedRepresentationHeaders.Contains)
                && StoredResponseMetadataIsBounded(
                    candidate.ETag,
                    sourceETag,
                    sourceLastModified,
                    sourceContentType,
                    candidate.ContentType,
                    candidate.ContentEncoding,
                    cacheControl,
                    vary,
                    replayHeaders);
            var canStore = admission.CanStore
                && codingWasNotChangedLate
                && replayHeadersAreSafe;
            return new CachedRepresentation(
                admission.Key,
                candidate.BaseKey,
                candidate.Body,
                candidate.ETag,
                sourceETag,
                sourceLastModified,
                sourceContentType,
                candidate.ContentType,
                candidate.ContentEncoding,
                cacheControl,
                vary,
                replayHeaders,
                admission.VaryHeaders,
                admission.VaryRequestValues,
                canStore,
                admission.EvictBaseOnRejection
                    || (admission.CanStore && !canStore));
        }

        private static CacheRetentionDecision EvaluateRevalidatedRepresentation(
            RequestCacheSnapshot requestSnapshot,
            HttpResponse response,
            CachedRepresentation representation,
            ResponseHeaderPresence initialHeaders)
        {
            if (!IsSharedCacheSafeResponse(response))
            {
                return CacheRetentionDecision.EvictBase;
            }

            var headers = response.Headers;
            var cacheControlPresent = headers.ContainsKey("Cache-Control");
            var cacheControl = headers["Cache-Control"].ToString();
            if ((cacheControlPresent
                    && !cacheControl.Equals(
                        representation.CacheControl,
                        StringComparison.Ordinal))
                || (!cacheControlPresent && initialHeaders.WasPresent("Cache-Control")))
            {
                return CacheRetentionDecision.EvictEntry;
            }

            var varyPresent = headers.ContainsKey("Vary");
            string[] effectiveVaryHeaders;
            if (varyPresent)
            {
                if (!TryCaptureVary(
                        headers["Vary"],
                        requestSnapshot,
                        out effectiveVaryHeaders,
                        out var varyRequestValues)
                    || effectiveVaryHeaders.Any(IsSensitiveCacheDimension)
                    || !representation.HasCompatibleRevalidationVary(effectiveVaryHeaders)
                    || !representation.MatchesVary(
                        effectiveVaryHeaders,
                        varyRequestValues))
                {
                    return CacheRetentionDecision.EvictBase;
                }
            }
            else
            {
                if (initialHeaders.WasPresent("Vary"))
                {
                    return CacheRetentionDecision.EvictBase;
                }

                effectiveVaryHeaders = representation.VaryHeaders;
            }

            if (HeaderChangedOrWasRemoved(
                    headers,
                    initialHeaders,
                    "Content-Encoding",
                    representation.ContentEncoding))
            {
                return CacheRetentionDecision.EvictEntry;
            }

            if (HeaderChangedOrWasRemoved(
                    headers,
                    initialHeaders,
                    "Content-Type",
                    representation.SourceContentType))
            {
                return CacheRetentionDecision.EvictEntry;
            }

            if (SourceValidatorChangedOrWasRemoved(
                    headers,
                    initialHeaders,
                    "ETag",
                    representation.SourceETag,
                    SanitizeSourceETag)
                || SourceValidatorChangedOrWasRemoved(
                    headers,
                    initialHeaders,
                    "Last-Modified",
                    representation.SourceLastModified,
                    SanitizeSourceLastModified))
            {
                return CacheRetentionDecision.EvictEntry;
            }

            if (!TryCaptureReplayHeaders(
                    headers,
                    out var currentReplayHeaders,
                    out var connectionNominated))
            {
                return CacheRetentionDecision.EvictBase;
            }

            if (connectionNominated.Any(ManagedRepresentationHeaders.Contains))
            {
                return CacheRetentionDecision.EvictBase;
            }

            if (!representation.HasCompatibleRevalidationHeaders(currentReplayHeaders))
            {
                return CacheRetentionDecision.EvictEntry;
            }

            var effectiveReplayHeaders = currentReplayHeaders.ToList();
            foreach (var cachedHeader in representation.ReplayHeaders)
            {
                var current = currentReplayHeaders.FirstOrDefault(header =>
                    header.Name.Equals(
                        cachedHeader.Name,
                        StringComparison.OrdinalIgnoreCase));
                if (current.Name != null)
                {
                    continue;
                }

                if (initialHeaders.WasPresent(cachedHeader.Name)
                    || connectionNominated.Contains(cachedHeader.Name))
                {
                    return CacheRetentionDecision.EvictEntry;
                }

                effectiveReplayHeaders.Add(cachedHeader);
            }

            var effectiveCacheControl = cacheControlPresent
                ? cacheControl
                : representation.CacheControl;
            var effectiveVary = varyPresent
                ? string.Join(", ", effectiveVaryHeaders)
                : representation.Vary;
            if (!StoredResponseMetadataIsBounded(
                    representation.ETag,
                    representation.SourceETag,
                    representation.SourceLastModified,
                    representation.SourceContentType,
                    representation.ContentType,
                    representation.ContentEncoding,
                    effectiveCacheControl,
                    effectiveVary,
                    effectiveReplayHeaders))
            {
                return CacheRetentionDecision.EvictBase;
            }

            return CacheRetentionDecision.Retain;
        }

        private static bool HeaderChangedOrWasRemoved(
            IHeaderDictionary headers,
            ResponseHeaderPresence initialHeaders,
            string name,
            string expected)
        {
            if (headers.ContainsKey(name))
            {
                return !headers[name].ToString().Equals(expected, StringComparison.Ordinal);
            }

            return initialHeaders.WasPresent(name);
        }

        private static bool SourceValidatorChangedOrWasRemoved(
            IHeaderDictionary headers,
            ResponseHeaderPresence initialHeaders,
            string name,
            string expected,
            Func<StringValues, string> sanitize)
        {
            if (headers.ContainsKey(name))
            {
                var current = sanitize(headers[name]);
                return string.IsNullOrEmpty(current)
                    || !current.Equals(expected, StringComparison.Ordinal);
            }

            return initialHeaders.WasPresent(name);
        }

        private static void MergeOmittedCachedHeaders(
            RequestCacheSnapshot requestSnapshot,
            HttpResponse response,
            CachedRepresentation representation,
            ResponseHeaderPresence initialHeaders)
        {
            var headers = response.Headers;
            if (!TryCaptureConnectionNominations(headers, out var connectionNominated))
            {
                return;
            }

            if (!connectionNominated.Contains("Cache-Control")
                && !headers.ContainsKey("Cache-Control")
                && !initialHeaders.WasPresent("Cache-Control"))
            {
                SetOrRemove(headers, "Cache-Control", representation.CacheControl);
            }

            if (!connectionNominated.Contains("Vary")
                && !headers.ContainsKey("Vary")
                && !initialHeaders.WasPresent("Vary"))
            {
                SetOrRemove(headers, "Vary", representation.Vary);
            }
            else if (headers.ContainsKey("Vary")
                && TryCaptureVary(
                    headers["Vary"],
                    requestSnapshot,
                    out var currentVary,
                    out _)
                && representation.HasCompatibleRevalidationVary(currentVary)
                && !representation.HasSameVaryHeaders(currentVary))
            {
                // ResponseCompression may omit only its Accept-Encoding dimension
                // on a source 304. Restore the full cached selection model before
                // forwarding the transformed representation.
                SetOrRemove(headers, "Vary", representation.Vary);
            }

            foreach (var header in representation.ReplayHeaders)
            {
                if (!headers.ContainsKey(header.Name)
                    && !initialHeaders.WasPresent(header.Name)
                    && !connectionNominated.Contains(header.Name))
                {
                    headers[header.Name] = new StringValues(header.Values);
                }
            }
        }

        private static string SanitizeSourceETag(StringValues value)
        {
            return EntityTagHeaderValue.TryParse(
                    new StringSegment(value.ToString()),
                    out var parsed)
                && parsed != null
                && !parsed.Equals(EntityTagHeaderValue.Any)
                ? parsed.ToString()
                : string.Empty;
        }

        private static string SanitizeSourceLastModified(StringValues value) =>
            TryParseHttpDate(value, out var parsed)
                ? parsed.ToUniversalTime().ToString("R")
                : string.Empty;

        private static bool StoredResponseMetadataIsBounded(
            string transformedETag,
            string sourceETag,
            string sourceLastModified,
            string sourceContentType,
            string contentType,
            string contentEncoding,
            string cacheControl,
            string vary,
            IReadOnlyCollection<CachedHeader> replayHeaders)
        {
            var values = new[]
            {
                new KeyValuePair<string, string>("ETag", transformedETag),
                new KeyValuePair<string, string>("Source-ETag", sourceETag),
                new KeyValuePair<string, string>("Source-Last-Modified", sourceLastModified),
                new KeyValuePair<string, string>("Source-Content-Type", sourceContentType),
                new KeyValuePair<string, string>("Content-Type", contentType),
                new KeyValuePair<string, string>("Content-Encoding", contentEncoding),
                new KeyValuePair<string, string>("Cache-Control", cacheControl),
                new KeyValuePair<string, string>("Vary", vary),
            };
            var headerCount = replayHeaders.Count;
            var valueCount = replayHeaders.Sum(header => header.Values.Length);
            long characterCount = replayHeaders.Sum(header =>
                (long)header.Name.Length + header.Values.Sum(value => (long)value.Length));
            foreach (var value in values)
            {
                if (string.IsNullOrEmpty(value.Value))
                {
                    continue;
                }

                headerCount++;
                valueCount++;
                characterCount += value.Key.Length + value.Value.Length;
            }

            return headerCount <= MaxCachedResponseHeaders
                && valueCount <= MaxCachedResponseHeaderValues
                && characterCount <= MaxCachedResponseHeaderCharacters;
        }

        private static void RemoveInvalidatedRepresentationMetadata(
            HttpContext context,
            bool transformed)
        {
            var response = context.Response;
            var headers = response.Headers;
            headers.Remove("Content-MD5");
            headers.Remove("Digest");
            headers.Remove("Content-Digest");
            headers.Remove("Repr-Digest");
            headers.Remove("Signature");
            headers.Remove("Signature-Input");

            if (transformed || response.StatusCode == StatusCodes.Status412PreconditionFailed)
            {
                headers.Remove("TE");
                headers.Remove("Trailer");
                headers.Remove("Transfer-Encoding");
                headers.Remove("Upgrade");
                var trailers = context.Features.Get<IHttpResponseTrailersFeature>()?.Trailers;
                trailers?.Clear();
            }

            if (response.StatusCode != StatusCodes.Status412PreconditionFailed)
            {
                return;
            }

            // A synthesized conditional failure is not the cacheable shell and
            // must never acquire the source 200's explicit freshness lifetime.
            RefreshKit.ApplyNoStore(response);
            headers.Remove("CDN-Cache-Control");
            headers.Remove("Surrogate-Control");
            headers.Remove("Cloudflare-CDN-Cache-Control");
            headers.Remove("Akamai-Cache-Control");
            response.ContentType = null;
            response.ContentLength = null;
            headers.Remove("Content-Encoding");
            headers.Remove("Content-Range");
            headers.Remove("Accept-Ranges");
            headers.Remove("ETag");
            headers.Remove("Last-Modified");
        }

        private static void PrepareLateStatusResponse(
            HttpContext context,
            bool preserveSourceBody)
        {
            var response = context.Response;
            var headers = response.Headers;
            RefreshKit.ApplyNoStore(response);
            headers.Remove("CDN-Cache-Control");
            headers.Remove("Surrogate-Control");
            headers.Remove("Cloudflare-CDN-Cache-Control");
            headers.Remove("Akamai-Cache-Control");
            headers.Remove("ETag");
            headers.Remove("Last-Modified");
            headers.Remove("Accept-Ranges");
            headers.Remove("Content-Range");
            headers.Remove("Content-MD5");
            headers.Remove("Digest");
            headers.Remove("Content-Digest");
            headers.Remove("Repr-Digest");
            headers.Remove("Signature");
            headers.Remove("Signature-Input");
            headers.Remove("Trailer");
            context.Features.Get<IHttpResponseTrailersFeature>()?.Trailers?.Clear();
            NormalizeManagedConnectionNominations(headers);

            if (preserveSourceBody)
            {
                return;
            }

            response.ContentType = null;
            response.ContentLength = null;
            headers.Remove("Content-Encoding");
            headers.Remove("TE");
            headers.Remove("Transfer-Encoding");
            headers.Remove("Upgrade");
        }

        private static void PreventSharedCaching(HttpResponse response)
        {
            RefreshKit.ApplyNoStore(response);
            response.Headers.Remove("CDN-Cache-Control");
            response.Headers.Remove("Surrogate-Control");
            response.Headers.Remove("Cloudflare-CDN-Cache-Control");
            response.Headers.Remove("Akamai-Cache-Control");
        }

        private static bool NormalizeManagedConnectionNominations(
            IHeaderDictionary headers)
        {
            if (!TryCaptureConnectionNominations(headers, out var nominated))
            {
                headers.Remove("Connection");
                return true;
            }

            if (!nominated.Any(ManagedRepresentationHeaders.Contains))
            {
                return false;
            }

            var retained = nominated
                .Where(name => !ManagedRepresentationHeaders.Contains(name))
                .OrderBy(name => name, StringComparer.OrdinalIgnoreCase)
                .ToArray();
            SetOrRemove(headers, "Connection", string.Join(", ", retained));
            return true;
        }

        private static bool IsSharedCacheSafeRequest(HttpRequest request)
        {
            if (request.HttpContext.User?.Identity?.IsAuthenticated == true)
            {
                return false;
            }

            foreach (var header in SharedCacheSensitiveRequestHeaders)
            {
                if (request.Headers.ContainsKey(header))
                {
                    return false;
                }
            }

            var cacheControl = request.Headers["Cache-Control"];
            if (!StringValues.IsNullOrEmpty(cacheControl))
            {
                if (!CacheControlHeaderValue.TryParse(
                        new StringSegment(cacheControl.ToString()),
                        out var parsed)
                    || parsed.NoStore)
                {
                    return false;
                }
            }

            return true;
        }

        private static bool IsSharedCacheSafeResponse(HttpResponse response)
        {
            if (response.Headers.ContainsKey("Set-Cookie")
                || response.Headers.ContainsKey("Set-Cookie2")
                || response.Headers.ContainsKey("Clear-Site-Data")
                || response.Headers.ContainsKey("WWW-Authenticate")
                || response.Headers.ContainsKey("Authentication-Info")
                || response.Headers.ContainsKey("Proxy-Authenticate")
                || response.Headers.ContainsKey("Proxy-Authentication-Info"))
            {
                return false;
            }

            var cacheControl = response.Headers["Cache-Control"];
            if (StringValues.IsNullOrEmpty(cacheControl))
            {
                return true;
            }

            // A malformed policy is not evidence that a shared process cache may
            // retain the response. Failing closed here only forfeits an optimization;
            // the transformed response is still served from this downstream pass.
            return CacheControlHeaderValue.TryParse(
                    new StringSegment(cacheControl.ToString()),
                    out var parsed)
                && !parsed.NoStore
                && !parsed.Private;
        }

        private static bool TryCaptureReplayHeaders(
            IHeaderDictionary headers,
            out CachedHeader[] captured) =>
            TryCaptureReplayHeaders(headers, out captured, out _);

        private static bool TryCaptureReplayHeaders(
            IHeaderDictionary headers,
            out CachedHeader[] captured,
            out HashSet<string> connectionNominated)
        {
            if (!TryCaptureConnectionNominations(headers, out connectionNominated))
            {
                captured = Array.Empty<CachedHeader>();
                return false;
            }

            var result = new List<CachedHeader>();
            long characterCount = 0;
            var valueCount = 0;
            foreach (var header in headers.OrderBy(
                pair => pair.Key,
                StringComparer.OrdinalIgnoreCase))
            {
                if (NonReplayableResponseHeaders.Contains(header.Key)
                    || connectionNominated.Contains(header.Key))
                {
                    continue;
                }

                var values = header.Value
                    .Select(value => value ?? string.Empty)
                    .ToArray();
                characterCount += header.Key.Length;
                characterCount += values.Sum(value => (long)value.Length);
                valueCount += values.Length;
                if (result.Count >= MaxCachedResponseHeaders
                    || valueCount > MaxCachedResponseHeaderValues
                    || characterCount > MaxCachedResponseHeaderCharacters)
                {
                    captured = Array.Empty<CachedHeader>();
                    return false;
                }

                result.Add(new CachedHeader(header.Key, values));
            }

            captured = result.ToArray();
            return true;
        }

        private static bool TryCaptureConnectionNominations(
            IHeaderDictionary headers,
            out HashSet<string> connectionNominated)
        {
            connectionNominated = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long connectionMetadataCharacters = 0;
            foreach (var value in headers["Connection"])
            {
                var raw = value ?? string.Empty;
                var offset = 0;
                while (offset <= raw.Length)
                {
                    var comma = raw.IndexOf(',', offset);
                    var end = comma >= 0 ? comma : raw.Length;
                    while (offset < end && IsOptionalWhitespace(raw[offset]))
                    {
                        offset++;
                    }

                    while (end > offset && IsOptionalWhitespace(raw[end - 1]))
                    {
                        end--;
                    }

                    if (end > offset)
                    {
                        var name = raw.Substring(offset, end - offset);
                        if (!IsHttpToken(name))
                        {
                            return false;
                        }

                        if (connectionNominated.Add(name))
                        {
                            connectionMetadataCharacters += name.Length;
                            if (connectionNominated.Count > MaxCachedResponseHeaders
                                || connectionMetadataCharacters
                                    > MaxCachedResponseHeaderCharacters)
                            {
                                return false;
                            }
                        }
                    }

                    if (comma < 0)
                    {
                        break;
                    }

                    offset = comma + 1;
                }
            }

            return true;
        }

        private static bool TryCaptureVary(
            StringValues vary,
            RequestCacheSnapshot requestSnapshot,
            out string[] varyHeaders,
            out VaryRequestValue[] varyRequestValues)
        {
            var names = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
            long metadataCharacters = 0;
            long rawCharacters = 0;
            foreach (var value in vary)
            {
                var raw = value ?? string.Empty;
                rawCharacters += raw.Length;
                if (rawCharacters > MaxVaryMetadataCharacters)
                {
                    varyHeaders = Array.Empty<string>();
                    varyRequestValues = Array.Empty<VaryRequestValue>();
                    return false;
                }

                var offset = 0;
                while (offset <= raw.Length)
                {
                    var comma = raw.IndexOf(',', offset);
                    var end = comma >= 0 ? comma : raw.Length;
                    while (offset < end && IsOptionalWhitespace(raw[offset]))
                    {
                        offset++;
                    }

                    while (end > offset && IsOptionalWhitespace(raw[end - 1]))
                    {
                        end--;
                    }

                    var nameLength = end - offset;
                    if (nameLength > 0)
                    {
                        if (nameLength > MaxVaryMetadataCharacters)
                        {
                            varyHeaders = Array.Empty<string>();
                            varyRequestValues = Array.Empty<VaryRequestValue>();
                            return false;
                        }

                        var name = raw.Substring(offset, nameLength);
                        if (name.Equals("*", StringComparison.Ordinal) || !IsHttpToken(name))
                        {
                            varyHeaders = Array.Empty<string>();
                            varyRequestValues = Array.Empty<VaryRequestValue>();
                            return false;
                        }

                        if (names.Add(name.ToLowerInvariant()))
                        {
                            metadataCharacters += name.Length;
                            if (names.Count > MaxVaryHeaders
                                || metadataCharacters > MaxVaryMetadataCharacters)
                            {
                                varyHeaders = Array.Empty<string>();
                                varyRequestValues = Array.Empty<VaryRequestValue>();
                                return false;
                            }
                        }
                    }

                    if (comma < 0)
                    {
                        break;
                    }

                    offset = comma + 1;
                }
            }

            varyHeaders = names.ToArray();
            varyRequestValues = varyHeaders
                .Select(name => VaryRequestValue.Capture(requestSnapshot, name))
                .ToArray();
            metadataCharacters += varyRequestValues.Sum(value => value.CharacterCount);
            if (metadataCharacters > MaxVaryMetadataCharacters)
            {
                varyHeaders = Array.Empty<string>();
                varyRequestValues = Array.Empty<VaryRequestValue>();
                return false;
            }

            return true;
        }

        private static bool IsSensitiveCacheDimension(string header) =>
            header.Equals("authorization", StringComparison.OrdinalIgnoreCase)
            || header.Equals("cookie", StringComparison.OrdinalIgnoreCase)
            || header.Equals("cookie2", StringComparison.OrdinalIgnoreCase)
            || header.Equals("proxy-authorization", StringComparison.OrdinalIgnoreCase)
            || header.Equals("x-emby-authorization", StringComparison.OrdinalIgnoreCase)
            || header.Equals("x-emby-token", StringComparison.OrdinalIgnoreCase)
            || header.Equals("x-mediabrowser-token", StringComparison.OrdinalIgnoreCase);

        // RFC 9110 optional whitespace is exactly SP / HTAB. Treating Unicode
        // whitespace as trim material could turn a malformed field-name into a
        // valid cache dimension instead of failing admission closed.
        private static bool IsOptionalWhitespace(char value) => value == ' ' || value == '\t';

        private static bool IsHttpToken(string value)
        {
            const string OtherTokenCharacters = "!#$%&'*+-.^_`|~";
            foreach (var character in value)
            {
                if ((character >= 'a' && character <= 'z')
                    || (character >= 'A' && character <= 'Z')
                    || (character >= '0' && character <= '9')
                    || OtherTokenCharacters.IndexOf(character, StringComparison.Ordinal) >= 0)
                {
                    continue;
                }

                return false;
            }

            return value.Length > 0;
        }

        private static bool IsHtmlContentType(string? value)
        {
            if (string.IsNullOrWhiteSpace(value)
                || !MediaTypeHeaderValue.TryParse(new StringSegment(value), out var parsed)
                || parsed == null
                || !parsed.MediaType.ToString().Equals(
                    "text/html",
                    StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            var foundCharset = false;
            foreach (var parameter in parsed.Parameters)
            {
                if (!parameter.Name.ToString().Equals(
                    "charset",
                    StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                // Multiple charset parameters are ambiguous. A declared
                // non-UTF-8 representation cannot be decoded and re-emitted as
                // UTF-8 without corrupting its bytes, so leave either shape to
                // the source response unchanged.
                if (foundCharset)
                {
                    return false;
                }

                foundCharset = true;
                var charset = parameter.Value.ToString().Trim();
                if (charset.Length >= 2
                    && charset[0] == '"'
                    && charset[charset.Length - 1] == '"')
                {
                    charset = charset.Substring(1, charset.Length - 2);
                }

                if (!charset.Equals("utf-8", StringComparison.OrdinalIgnoreCase)
                    && !charset.Equals("utf8", StringComparison.OrdinalIgnoreCase))
                {
                    return false;
                }
            }

            return true;
        }

        private static string CreateVariantKey(
            string baseKey,
            IReadOnlyList<string> varyHeaders,
            IReadOnlyList<VaryRequestValue> varyRequestValues)
        {
            var material = new StringBuilder(baseKey);
            for (var index = 0; index < varyHeaders.Count; index++)
            {
                AppendLengthPrefixed(material, varyHeaders[index]);
                varyRequestValues[index].AppendKeyMaterial(material);
            }

            return baseKey
                + ":"
                + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material.ToString())));
        }

        private static void AppendLengthPrefixed(StringBuilder destination, string value) =>
            destination.Append('\0').Append(value.Length).Append(':').Append(value);

        // Strong validator for the transformed representation, derived from the exact
        // encoded bytes we are about to send. The "rk-" prefix makes it obvious in a
        // trace that the response came from the refresh kit, not the static-file
        // handler, and guarantees it can never collide with the source's ETag.
        private static string CreateETag(byte[] body) =>
            "\"rk-" + Convert.ToHexString(SHA256.HashData(body)).ToLowerInvariant() + "\"";

        private CachedRepresentation? GetCached(
            string baseKey,
            RequestCacheSnapshot requestSnapshot)
        {
            lock (_cacheLock)
            {
                // The cache is globally capped at 12 entries, so matching a source
                // Vary tuple is bounded O(12), independent of attacker-controlled
                // header cardinality or the number of requests ever observed.
                var node = _leastRecentlyUsed.Last;
                while (node != null)
                {
                    var candidateNode = node;
                    node = node.Previous;
                    if (_cache.TryGetValue(candidateNode.Value, out var candidate)
                        && candidate.BaseKey.Equals(baseKey, StringComparison.Ordinal)
                        && candidate.MatchesVary(requestSnapshot))
                    {
                        _leastRecentlyUsed.Remove(candidateNode);
                        _leastRecentlyUsed.AddLast(candidateNode);
                        return candidate;
                    }
                }

                return null;
            }
        }

        private void RemoveCached(string key)
        {
            lock (_cacheLock)
            {
                RemoveCachedUnderLock(key);
            }
        }

        private void RemoveCachedBase(string baseKey)
        {
            lock (_cacheLock)
            {
                var keys = _cache
                    .Where(pair => pair.Value.BaseKey.Equals(baseKey, StringComparison.Ordinal))
                    .Select(pair => pair.Key)
                    .ToArray();
                foreach (var key in keys)
                {
                    RemoveCachedUnderLock(key);
                }
            }
        }

        private void RemoveCachedUnderLock(string key)
        {
            if (_cache.Remove(key, out var cached))
            {
                _cachedBodyBytes -= cached.Body.Length;
                _leastRecentlyUsed.Remove(key);
            }
        }

        private void PutCached(CachedRepresentation representation)
        {
            // Without a source validator, a later request cannot prove that the host
            // file is unchanged. Serve this request correctly but do not retain bytes
            // that could become stale.
            var hasUsableSourceValidator = HasUsableSourceValidator(representation);
            if (!representation.CanStore
                || representation.Body.Length > MaxCacheableRepresentationBytes
                || !hasUsableSourceValidator)
            {
                // An authenticated request bypasses this process-shared cache without
                // disturbing anonymous entries. In contrast, an origin response that
                // is no-store/private, sets a cookie, declares Vary: *, or otherwise
                // cannot be selected safely invalidates every old tuple for this base.
                if (representation.EvictBaseOnRejection
                    || (representation.CanStore
                        && (representation.Body.Length > MaxCacheableRepresentationBytes
                            || !hasUsableSourceValidator)))
                {
                    RemoveCachedBase(representation.BaseKey);
                }

                return;
            }

            lock (_cacheLock)
            {
                // A changed Vary field-name set changes the cache key model itself.
                // Remove tuples created under the old model while retaining sibling
                // values (for example X-Variant=A and X-Variant=B) under the same one.
                var incompatibleKeys = _cache
                    .Where(pair => pair.Value.BaseKey.Equals(representation.BaseKey, StringComparison.Ordinal)
                        && !pair.Value.HasSameVaryHeaders(representation.VaryHeaders))
                    .Select(pair => pair.Key)
                    .ToArray();
                foreach (var incompatibleKey in incompatibleKeys)
                {
                    RemoveCachedUnderLock(incompatibleKey);
                }

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

        private static bool HasUsableSourceValidator(CachedRepresentation representation)
            => !string.IsNullOrEmpty(representation.SourceETag)
                || !string.IsNullOrEmpty(representation.SourceLastModified);

        private static async Task WriteRepresentationAsync(
            HttpContext context,
            CachedRepresentation representation,
            bool writeBody,
            ResponseFinalizationState finalization,
            byte[]? sourceFallbackBody,
            Stream responseBody)
        {
            await StartAndFinalizeResponseAsync(context, finalization).ConfigureAwait(false);
            if (!writeBody)
            {
                return;
            }

            if (finalization.BodySelection == ResponseBodySelection.Representation
                && finalization.ShouldWriteRepresentation)
            {
                await responseBody.WriteAsync(
                    representation.Body.AsMemory(),
                    context.RequestAborted).ConfigureAwait(false);
                if (representation.Body.Length > 0)
                {
                    finalization.MarkOuterBodyPending();
                }
            }
            else if (finalization.BodySelection == ResponseBodySelection.Source
                && sourceFallbackBody != null)
            {
                await responseBody.WriteAsync(
                    sourceFallbackBody.AsMemory(),
                    context.RequestAborted).ConfigureAwait(false);
                if (sourceFallbackBody.Length > 0)
                {
                    finalization.MarkOuterBodyPending();
                }
            }
        }

        private static void ApplyOwnedRepresentationHeaders(
            HttpContext context,
            CachedRepresentation representation,
            ClientPrecondition precondition)
        {
            context.Response.StatusCode = precondition switch
            {
                ClientPrecondition.NotModified => StatusCodes.Status304NotModified,
                ClientPrecondition.Failed => StatusCodes.Status412PreconditionFailed,
                _ => StatusCodes.Status200OK,
            };
            // The source timestamp cannot describe the injected representation:
            // script generation can change while index.html does not. Publishing it
            // would let an IMS-only client validate a stale generation. The strong,
            // body-derived ETag is the transformed representation's validator.
            context.Response.Headers.Remove("Last-Modified");
            context.Response.Headers["ETag"] = representation.ETag;
            context.Response.Headers.Remove("Accept-Ranges");
            context.Response.Headers.Remove("Content-Range");
            context.Response.Headers.Remove("Content-MD5");
            context.Response.Headers.Remove("Digest");
            context.Response.Headers.Remove("Content-Digest");
            context.Response.Headers.Remove("Repr-Digest");
            context.Response.Headers.Remove("Signature");
            context.Response.Headers.Remove("Signature-Input");

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
        }

        private static async Task WriteSourceFallbackAsync(
            HttpContext context,
            BoundedPassthroughStream bufferedSource,
            string baseCacheKey,
            StringValues ifMatch,
            StringValues ifNoneMatch,
            StringValues ifUnmodifiedSince,
            StringValues ifModifiedSince,
            bool writeBody,
            ResponseFinalizationState finalization,
            Stream responseBody)
        {
            var shouldWriteBody = PrepareSourceFallback(context, writeBody);
            if (context.RequestAborted.IsCancellationRequested)
            {
                return;
            }

            finalization.SetSourceFallback(
                baseCacheKey,
                context.Response.StatusCode,
                shouldWriteBody,
                isHead: !writeBody,
                bufferedSource.BufferedLength,
                context.Response.Headers["Content-Encoding"].ToString(),
                ifMatch,
                ifNoneMatch,
                ifUnmodifiedSince,
                ifModifiedSince);
            await StartAndFinalizeResponseAsync(context, finalization).ConfigureAwait(false);
            if (finalization.SourceFallbackBodyAllowed)
            {
                await bufferedSource.CopyBufferedToAsync(
                    responseBody,
                    context.RequestAborted).ConfigureAwait(false);
                if (bufferedSource.BufferedLength > 0)
                {
                    finalization.MarkOuterBodyPending();
                }
            }
        }

        // The final OnStarting callback makes the authoritative body and conditional
        // decision after every downstream callback has supplied its final headers.
        // Here we only record whether this request could carry source bytes at all.
        private static bool PrepareSourceFallback(HttpContext context, bool writeBody)
        {
            return !context.RequestAborted.IsCancellationRequested && writeBody;
        }

        private static async Task StartAndFinalizeResponseAsync(
            HttpContext context,
            ResponseFinalizationState finalization)
        {
            await context.Response.StartAsync(context.RequestAborted).ConfigureAwait(false);
            // A StreamResponseBodyFeature installed by an outer buffering
            // middleware starts only its MemoryStream. In that shape the server's
            // OnStarting stack has not run, so make a provisional body selection
            // now. When the outer owner eventually reaches the transport, the real
            // callback performs metadata cleanup without reapplying our intermediate
            // Content-Length, status, media type, or content coding.
            if (!context.Response.HasStarted)
            {
                await finalization.EnsureProvisionalFinalizedAsync().ConfigureAwait(false);
            }
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
            var parsedIfMatch = ParseEntityTags(ifMatch);
            if (parsedIfMatch.Count > 0)
            {
                var matched = parsedIfMatch.Any(candidate =>
                    candidate.Equals(EntityTagHeaderValue.Any)
                    || (sourceTag != null
                        && candidate.Compare(sourceTag, useStrongComparison: true)));
                if (!matched)
                {
                    return StatusCodes.Status412PreconditionFailed;
                }
            }
            else if (TryParseHttpDate(sourceLastModified, out var lastModifiedForIus)
                && TryParseHttpDate(ifUnmodifiedSince, out var unmodifiedSince)
                && unmodifiedSince <= DateTimeOffset.UtcNow
                && unmodifiedSince < lastModifiedForIus)
            {
                // RFC 9110 §13.1.1: If-Unmodified-Since is ignored whenever
                // If-Match is present, and otherwise a failed date condition is 412.
                return StatusCodes.Status412PreconditionFailed;
            }

            var parsedIfNoneMatch = ParseEntityTags(ifNoneMatch);
            if (parsedIfNoneMatch.Count > 0)
            {
                // RFC 9110 §13.1.2: If-None-Match uses weak comparison for GET/HEAD.
                var matched = parsedIfNoneMatch.Any(candidate =>
                    candidate.Equals(EntityTagHeaderValue.Any)
                    || (sourceTag != null
                        && candidate.Compare(sourceTag, useStrongComparison: false)));
                if (matched)
                {
                    return StatusCodes.Status304NotModified;
                }
            }
            else if (TryParseHttpDate(sourceLastModified, out var lastModifiedForIms)
                && TryParseHttpDate(ifModifiedSince, out var modifiedSince)
                && modifiedSince <= DateTimeOffset.UtcNow
                && modifiedSince >= lastModifiedForIms)
            {
                // RFC 9110 §13.1.3: If-Modified-Since is ignored whenever
                // If-None-Match is present.
                return StatusCodes.Status304NotModified;
            }

            return StatusCodes.Status200OK;
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
        /// Adapts a bounded response stream without letting SendFileAsync start
        /// the transport first. StreamResponseBodyFeature normally calls
        /// StartAsync before copying a file; for this middleware that flush would
        /// deliberately enter passthrough mode before the first byte could be
        /// inspected. Direct file sends instead use the framework fallback copy
        /// through the bounded stream. An explicit downstream StartAsync or
        /// FlushAsync retains its normal commit/passthrough semantics.
        /// </summary>
        private sealed class BoundedResponseBodyFeature : StreamResponseBodyFeature
        {
            public BoundedResponseBodyFeature(Stream stream)
                : base(stream)
            {
            }

            public override Task SendFileAsync(
                string path,
                long offset,
                long? count,
                CancellationToken cancellationToken) =>
                SendFileFallback.SendFileAsync(
                    Stream,
                    path,
                    offset,
                    count,
                cancellationToken);
        }

        /// <summary>
        /// Buffers the downstream response while it stays under the cap so it can be
        /// rewritten, and transparently switches to writing straight to the real body
        /// once it does not. The switch happens inside the single downstream pass, so
        /// an oversized or non-rewritable response is still served correctly and the
        /// pipeline is never run twice.
        /// </summary>
        private sealed class BoundedPassthroughStream : Stream
        {
            public delegate bool PreparePassthroughCallback();

            private readonly Stream _destination;
            private readonly MemoryStream _buffer = new MemoryStream();
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

            public bool HasWrittenToDestination { get; private set; }

            public long BufferedLength => _buffer.Length;

            public override bool CanRead => false;

            public override bool CanSeek => false;

            public override bool CanWrite => true;

            public override long Length => throw new NotSupportedException();

            public override long Position
            {
                get => throw new NotSupportedException();
                set => throw new NotSupportedException();
            }

            public override long Seek(long offset, SeekOrigin loc) =>
                throw new NotSupportedException();

            public override int Read(byte[] buffer, int offset, int count) =>
                throw new NotSupportedException();

            public override void SetLength(long value) =>
                throw new NotSupportedException();

            public async Task CopyBufferedToAsync(Stream destination, CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                _buffer.Position = 0;
                await _buffer.CopyToAsync(destination, cancellationToken).ConfigureAwait(false);
            }

            public byte[] ToArray() => _buffer.ToArray();

            public override void Flush()
            {
                // Flush/StartAsync is an explicit commitment boundary. Once a
                // downstream component asks for it, preserve that same-pass source
                // response rather than silently buffering and later rewriting it.
                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.Flush();
                }
            }

            public override async Task FlushAsync(CancellationToken cancellationToken)
            {
                await EnsurePassthroughAsync(cancellationToken).ConfigureAwait(false);
                if (_writeToDestination)
                {
                    await _destination.FlushAsync(cancellationToken).ConfigureAwait(false);
                }
            }

            public override void Write(byte[] buffer, int offset, int count)
            {
                if (!IsPassthrough && count <= _limit - _buffer.Length)
                {
                    _buffer.Write(buffer, offset, count);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.Write(buffer, offset, count);
                    HasWrittenToDestination |= count > 0;
                }
            }

            public override void Write(ReadOnlySpan<byte> buffer)
            {
                if (!IsPassthrough && buffer.Length <= _limit - _buffer.Length)
                {
                    _buffer.Write(buffer);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.Write(buffer);
                    HasWrittenToDestination |= !buffer.IsEmpty;
                }
            }

            public override void WriteByte(byte value)
            {
                if (!IsPassthrough && _buffer.Length < _limit)
                {
                    _buffer.WriteByte(value);
                    return;
                }

                EnsurePassthrough();
                if (_writeToDestination)
                {
                    _destination.WriteByte(value);
                    HasWrittenToDestination = true;
                }
            }

            public override async Task WriteAsync(
                byte[] buffer,
                int offset,
                int count,
                CancellationToken cancellationToken)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!IsPassthrough && count <= _limit - _buffer.Length)
                {
                    await _buffer.WriteAsync(
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
                    HasWrittenToDestination |= count > 0;
                }
            }

            public override async ValueTask WriteAsync(
                ReadOnlyMemory<byte> buffer,
                CancellationToken cancellationToken = default)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!IsPassthrough && buffer.Length <= _limit - _buffer.Length)
                {
                    await _buffer.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
                    return;
                }

                await EnsurePassthroughAsync(cancellationToken).ConfigureAwait(false);
                if (_writeToDestination)
                {
                    await _destination.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
                    HasWrittenToDestination |= !buffer.IsEmpty;
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
                    var bufferedLength = _buffer.Length;
                    _buffer.Position = 0;
                    _buffer.CopyTo(_destination);
                    HasWrittenToDestination |= bufferedLength > 0;
                }

                _buffer.SetLength(0);
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
                    var bufferedLength = _buffer.Length;
                    _buffer.Position = 0;
                    await _buffer.CopyToAsync(
                        _destination,
                        cancellationToken).ConfigureAwait(false);
                    HasWrittenToDestination |= bufferedLength > 0;
                }

                _buffer.SetLength(0);
            }

            protected override void Dispose(bool disposing)
            {
                if (disposing)
                {
                    _buffer.Dispose();
                }

                base.Dispose(disposing);
            }
        }

        private sealed class RequestCacheSnapshot
        {
            private readonly IReadOnlyDictionary<string, string[]> _headers;

            private RequestCacheSnapshot(
                IReadOnlyDictionary<string, string[]> headers,
                bool isSafe)
            {
                _headers = headers;
                IsSafe = isSafe;
            }

            public bool IsSafe { get; }

            public static RequestCacheSnapshot Capture(HttpContext context)
            {
                var headers = new Dictionary<string, string[]>(
                    StringComparer.OrdinalIgnoreCase);
                long characterCount = 0;
                var valueCount = 0;
                var withinBounds = true;
                foreach (var header in context.Request.Headers)
                {
                    var values = header.Value
                        .Select(value => value ?? string.Empty)
                        .ToArray();
                    characterCount += header.Key.Length;
                    characterCount += values.Sum(value => (long)value.Length);
                    valueCount += values.Length;
                    if (headers.Count >= MaxCachedRequestHeaders
                        || valueCount > MaxCachedRequestHeaderValues
                        || characterCount > MaxCachedRequestHeaderCharacters)
                    {
                        withinBounds = false;
                        headers.Clear();
                        break;
                    }

                    headers[header.Key] = values;
                }

                return new RequestCacheSnapshot(
                    headers,
                    withinBounds && IsSharedCacheSafeRequest(context.Request));
            }

            public bool TryGetValue(string name, out string[] values) =>
                _headers.TryGetValue(name, out values!);
        }

        private sealed class ResponseFinalizationState
        {
            private readonly RefreshKitScriptInjectionFilter _owner;
            private readonly HttpContext _context;
            private readonly RequestCacheSnapshot _requestSnapshot;
            private readonly Action _releaseRepresentationGateLease;
            private int _bodyFinalized;
            private int _provisionallyFinalized;
            private int _outerMetadataFinalized;
            private int _outerFramingSnapshotRegistered;
            private int _outerBodyPending;
            private OuterResponseFramingSnapshot? _outerFramingSnapshot;
            private CachedRepresentation? _representation;
            private ResponseHeaderPresence? _initialHeaders;
            private ClientPrecondition _precondition;
            private int _expectedSourceStatus;
            private bool _isNewRepresentation;
            private bool _hasSourceFallback;
            private string _sourceFallbackBaseKey = string.Empty;
            private bool _sourceFallbackIsHead;
            private long? _sourceFallbackBodyLength;
            private string _sourceFallbackContentEncoding = string.Empty;
            private StringValues _sourceFallbackIfMatch;
            private StringValues _sourceFallbackIfNoneMatch;
            private StringValues _sourceFallbackIfUnmodifiedSince;
            private StringValues _sourceFallbackIfModifiedSince;

            public ResponseFinalizationState(
                RefreshKitScriptInjectionFilter owner,
                HttpContext context,
                RequestCacheSnapshot requestSnapshot,
                Action releaseRepresentationGateLease)
            {
                _owner = owner;
                _context = context;
                _requestSnapshot = requestSnapshot;
                _releaseRepresentationGateLease = releaseRepresentationGateLease;
            }

            public ResponseBodySelection BodySelection { get; private set; }

            public bool SourceFallbackBodyAllowed { get; private set; }

            public bool ShouldWriteRepresentation =>
                _precondition == ClientPrecondition.ShouldProcess;

            public void MarkOuterBodyPending() =>
                Volatile.Write(ref _outerBodyPending, 1);

            public void RegisterOuterFramingSnapshotCallback()
            {
                if (Volatile.Read(ref _provisionallyFinalized) == 0
                    || _context.Response.HasStarted
                    || Interlocked.Exchange(
                        ref _outerFramingSnapshotRegistered,
                        1) != 0)
                {
                    return;
                }

                try
                {
                    _context.Response.OnStarting(CaptureOuterFramingAsync);
                }
                catch (Exception ex)
                {
                    // Registration is a compatibility guard for a nested buffer.
                    // If an unusual response feature rejects it, keep fail-open
                    // behavior and retain the metadata safety callback below.
                    _owner.LogWarning(ex);
                }
            }

            public void SetSourceFallback(
                string baseKey,
                int expectedStatus,
                bool hasSourceBody,
                bool isHead,
                long? sourceBodyLength,
                string sourceContentEncoding,
                StringValues ifMatch,
                StringValues ifNoneMatch,
                StringValues ifUnmodifiedSince,
                StringValues ifModifiedSince)
            {
                _hasSourceFallback = true;
                _sourceFallbackBaseKey = baseKey;
                _expectedSourceStatus = expectedStatus;
                SourceFallbackBodyAllowed = hasSourceBody;
                _sourceFallbackIsHead = isHead;
                _sourceFallbackBodyLength = sourceBodyLength;
                _sourceFallbackContentEncoding = sourceContentEncoding;
                _sourceFallbackIfMatch = ifMatch;
                _sourceFallbackIfNoneMatch = ifNoneMatch;
                _sourceFallbackIfUnmodifiedSince = ifUnmodifiedSince;
                _sourceFallbackIfModifiedSince = ifModifiedSince;
            }

            public void SetRepresentation(
                CachedRepresentation representation,
                ClientPrecondition precondition,
                bool isNewRepresentation,
                long sourceFallbackBodyLength)
            {
                _representation = representation;
                _precondition = precondition;
                _isNewRepresentation = isNewRepresentation;
                _expectedSourceStatus = _context.Response.StatusCode;
                _initialHeaders = ResponseHeaderPresence.Capture(
                    _context.Response,
                    representation);
                _sourceFallbackBodyLength = sourceFallbackBodyLength;
                _sourceFallbackContentEncoding = representation.ContentEncoding;
                BodySelection = ResponseBodySelection.None;
            }

            public Task OnStartingAsync()
            {
                return Volatile.Read(ref _provisionallyFinalized) != 0
                    ? FinalizeOuterOwnedMetadataAsync()
                    : EnsureBodyFinalizedAsync();
            }

            private Task CaptureOuterFramingAsync()
            {
                _outerFramingSnapshot = OuterResponseFramingSnapshot.Capture(
                    _context.Response,
                    Volatile.Read(ref _outerBodyPending) != 0);
                return Task.CompletedTask;
            }

            public async Task EnsureProvisionalFinalizedAsync()
            {
                Volatile.Write(ref _provisionallyFinalized, 1);
                // The final bytes belong to an outer middleware, so conditions
                // against Refresh Kit's intermediate representation cannot produce
                // a bodyless response.
                _precondition = ClientPrecondition.ShouldProcess;
                _sourceFallbackIfMatch = StringValues.Empty;
                _sourceFallbackIfNoneMatch = StringValues.Empty;
                _sourceFallbackIfUnmodifiedSince = StringValues.Empty;
                _sourceFallbackIfModifiedSince = StringValues.Empty;
                await EnsureBodyFinalizedAsync().ConfigureAwait(false);
                if (_representation != null)
                {
                    // PutCached runs inside normal finalization. Remove the candidate
                    // before releasing this key's gate: an outer middleware will
                    // change these bytes, so the intermediate entity is not reusable.
                    _owner.RemoveCachedBase(_representation.BaseKey);
                }

                try
                {
                    ApplyOuterOwnedMetadataSafety();
                }
                catch (Exception ex)
                {
                    // Metadata hardening must never turn a complete buffered shell
                    // into an application failure.
                    _owner.LogWarning(ex);
                }
            }

            private Task EnsureBodyFinalizedAsync()
            {
                if (Interlocked.Exchange(ref _bodyFinalized, 1) != 0)
                {
                    return Task.CompletedTask;
                }

                try
                {
                    if (_representation != null)
                    {
                        if (_context.Response.StatusCode != _expectedSourceStatus)
                        {
                            // A late authorization/rate-limit/error callback owns this
                            // response. Never replace its status with a shell status.
                            if (_requestSnapshot.IsSafe
                                && IsSharedCacheSafeRequest(_context.Request))
                            {
                                _owner.RemoveCachedBase(_representation.BaseKey);
                            }

                            var preserveSourceBody = _isNewRepresentation
                                && StatusAllowsResponseBody(
                                    _context.Response.StatusCode)
                                && CanPreserveSourceBody();
                            PrepareLateStatusResponse(
                                _context,
                                preserveSourceBody);
                            if (preserveSourceBody)
                            {
                                BodySelection = ResponseBodySelection.Source;
                            }

                            return Task.CompletedTask;
                        }

                        if (_isNewRepresentation)
                        {
                            if (!IsHtmlContentType(_context.Response.ContentType))
                            {
                                // A downstream OnStarting callback changed the source
                                // representation after it was buffered. Do not override
                                // that final media type with an injected HTML candidate;
                                // preserve the exact source bytes and evict older shells.
                                if (_requestSnapshot.IsSafe
                                    && IsSharedCacheSafeRequest(_context.Request))
                                {
                                    _owner.RemoveCachedBase(_representation.BaseKey);
                                }
                                if (StatusAllowsResponseBody(_context.Response.StatusCode)
                                    && CanPreserveSourceBody())
                                {
                                    BodySelection = ResponseBodySelection.Source;
                                }
                                else
                                {
                                    PrepareLateStatusResponse(
                                        _context,
                                        preserveSourceBody: false);
                                }

                                return Task.CompletedTask;
                            }

                            _representation = FinalizeCandidateRepresentation(
                                _requestSnapshot,
                                _context.Request,
                                _context.Response,
                                _representation);
                            _owner.PutCached(_representation);
                            if (!_representation.CanStore)
                            {
                                PreventSharedCaching(_context.Response);
                            }

                            if (NormalizeManagedConnectionNominations(
                                _context.Response.Headers))
                            {
                                PreventSharedCaching(_context.Response);
                            }
                        }
                        else
                        {
                            // A shared entry selected before downstream authentication
                            // must not become personalized after that decision. This is
                            // rare (headerless authentication should normally populate
                            // User before the startup filter), but fail closed rather
                            // than serving anonymous bytes into an authenticated request.
                            if (!_requestSnapshot.IsSafe
                                || !IsSharedCacheSafeRequest(_context.Request))
                            {
                                _context.Response.StatusCode =
                                    StatusCodes.Status503ServiceUnavailable;
                                PrepareLateStatusResponse(
                                    _context,
                                    preserveSourceBody: false);
                                return Task.CompletedTask;
                            }

                            var retention = EvaluateRevalidatedRepresentation(
                                _requestSnapshot,
                                _context.Response,
                                _representation,
                                _initialHeaders!);
                            if (retention == CacheRetentionDecision.EvictBase)
                            {
                                _owner.RemoveCachedBase(_representation.BaseKey);
                            }
                            else if (retention == CacheRetentionDecision.EvictEntry)
                            {
                                _owner.RemoveCached(_representation.Key);
                            }

                            if (retention != CacheRetentionDecision.Retain)
                            {
                                PreventSharedCaching(_context.Response);
                            }

                            MergeOmittedCachedHeaders(
                                _requestSnapshot,
                                _context.Response,
                                _representation,
                                _initialHeaders!);
                            if (NormalizeManagedConnectionNominations(
                                _context.Response.Headers))
                            {
                                PreventSharedCaching(_context.Response);
                            }
                        }

                        ApplyOwnedRepresentationHeaders(
                            _context,
                            _representation,
                            _precondition);
                        RemoveInvalidatedRepresentationMetadata(
                            _context,
                            transformed: true);
                        BodySelection = ResponseBodySelection.Representation;
                    }
                    else if (_hasSourceFallback)
                    {
                        if (_requestSnapshot.IsSafe
                            && IsSharedCacheSafeRequest(_context.Request))
                        {
                            // A safe full source response means the retained injected
                            // shell can no longer be proven to describe this resource.
                            _owner.RemoveCachedBase(_sourceFallbackBaseKey);
                        }

                        if (_context.Response.StatusCode != _expectedSourceStatus)
                        {
                            var preserveSourceBody = SourceFallbackBodyAllowed
                                && StatusAllowsResponseBody(
                                    _context.Response.StatusCode)
                                && CanPreserveSourceBody();
                            PrepareLateStatusResponse(
                                _context,
                                preserveSourceBody);
                            SourceFallbackBodyAllowed = preserveSourceBody;
                        }
                        else
                        {
                            if (_expectedSourceStatus == StatusCodes.Status200OK)
                            {
                                _context.Response.StatusCode = EvaluateSourcePreconditions(
                                    _sourceFallbackIfMatch,
                                    _sourceFallbackIfNoneMatch,
                                    _sourceFallbackIfUnmodifiedSince,
                                    _sourceFallbackIfModifiedSince,
                                    _context.Response.Headers["ETag"].ToString(),
                                    _context.Response.Headers["Last-Modified"].ToString());
                            }

                            if (_context.Response.StatusCode
                                == StatusCodes.Status412PreconditionFailed)
                            {
                                NormalizeManagedConnectionNominations(
                                    _context.Response.Headers);
                                RemoveInvalidatedRepresentationMetadata(
                                    _context,
                                    transformed: false);
                                _context.Response.Headers.Remove("Vary");
                                SourceFallbackBodyAllowed = false;
                                return Task.CompletedTask;
                            }

                            if (_context.Response.StatusCode
                                == StatusCodes.Status304NotModified)
                            {
                                if (NormalizeManagedConnectionNominations(
                                    _context.Response.Headers))
                                {
                                    PreventSharedCaching(_context.Response);
                                }

                                RemoveInvalidatedRepresentationMetadata(
                                    _context,
                                    transformed: false);
                                _context.Response.ContentLength = null;
                                _context.Response.Headers.Remove("Content-Encoding");
                                _context.Response.Headers.Remove("TE");
                                _context.Response.Headers.Remove("Transfer-Encoding");
                                _context.Response.Headers.Remove("Upgrade");
                                _context.Response.Headers.Remove("Content-Range");
                                _context.Response.Headers.Remove("Accept-Ranges");
                                _context.Response.Headers.Remove("Trailer");
                                _context.Features
                                    .Get<IHttpResponseTrailersFeature>()?
                                    .Trailers?
                                    .Clear();
                                SourceFallbackBodyAllowed = false;
                                return Task.CompletedTask;
                            }

                            SourceFallbackBodyAllowed = SourceFallbackBodyAllowed
                                && StatusAllowsResponseBody(
                                    _context.Response.StatusCode)
                                && CanPreserveSourceBody();
                            if (!SourceFallbackBodyAllowed && !_sourceFallbackIsHead)
                            {
                                PrepareLateStatusResponse(
                                    _context,
                                    preserveSourceBody: false);
                            }
                        }

                    }
                }
                catch (Exception ex)
                {
                    // Header finalization is a cache/safety guard, never permission
                    // to turn a usable Jellyfin shell response into a 500.
                    BodySelection = ResponseBodySelection.None;
                    SourceFallbackBodyAllowed = false;
                    _owner.LogWarning(ex);
                }
                finally
                {
                    // All shared cache admission/retention/eviction work is now
                    // final. A provisional outer-buffer path keeps the lease until
                    // EnsureProvisionalFinalizedAsync has removed the intermediate
                    // cache candidate; every other path releases before transport
                    // backpressure can serialize this stripe.
                    if (Volatile.Read(ref _provisionallyFinalized) == 0)
                    {
                        _releaseRepresentationGateLease();
                    }
                }

                return Task.CompletedTask;
            }

            private Task FinalizeOuterOwnedMetadataAsync()
            {
                if (Interlocked.Exchange(ref _outerMetadataFinalized, 1) != 0)
                {
                    return Task.CompletedTask;
                }

                try
                {
                    // The snapshot callback ran first at the real transport boundary,
                    // before older downstream callbacks could republish framing for
                    // RK's intermediate bytes. Restore the outer owner's exact framing
                    // while retaining a later body-allowed authorization/rate-limit/
                    // error status. A body-forbidden late status cannot accompany
                    // bytes already pending in the outer buffer.
                    RestoreOuterOwnedFraming();
                    ApplyOuterOwnedMetadataSafety();
                }
                catch (Exception ex)
                {
                    _owner.LogWarning(ex);
                }

                return Task.CompletedTask;
            }

            private void RestoreOuterOwnedFraming()
            {
                var snapshot = _outerFramingSnapshot;
                if (snapshot == null)
                {
                    return;
                }

                var response = _context.Response;
                if (snapshot.BodyPending)
                {
                    if (!StatusAllowsResponseBody(response.StatusCode)
                        && StatusAllowsResponseBody(snapshot.StatusCode))
                    {
                        response.StatusCode = snapshot.StatusCode;
                    }

                    if (StatusAllowsResponseBody(response.StatusCode))
                    {
                        snapshot.RestoreHeaders(response.Headers);
                    }
                    else
                    {
                        ClearEntityFraming(response.Headers);
                    }

                    return;
                }

                if (StatusAllowsResponseBody(response.StatusCode))
                {
                    snapshot.RestoreHeaders(response.Headers);
                }
                else
                {
                    // With no bytes pending, a late 1xx/204/205/304 remains
                    // authoritative and must not retain entity framing.
                    ClearEntityFraming(response.Headers);
                }
            }

            private static void ClearEntityFraming(IHeaderDictionary headers)
            {
                headers.Remove("Content-Length");
                headers.Remove("Content-Type");
                headers.Remove("Content-Encoding");
                headers.Remove("TE");
                headers.Remove("Transfer-Encoding");
                headers.Remove("Upgrade");
            }

            private void ApplyOuterOwnedMetadataSafety()
            {
                PreventSharedCaching(_context.Response);
                _context.Response.Headers.Remove("ETag");
                _context.Response.Headers.Remove("Last-Modified");
                _context.Response.Headers.Remove("Accept-Ranges");
                _context.Response.Headers.Remove("Content-Range");
                _context.Response.Headers.Remove("Content-MD5");
                _context.Response.Headers.Remove("Digest");
                _context.Response.Headers.Remove("Content-Digest");
                _context.Response.Headers.Remove("Repr-Digest");
                _context.Response.Headers.Remove("Signature");
                _context.Response.Headers.Remove("Signature-Input");
                _context.Response.Headers.Remove("Trailer");
                _context.Features.Get<IHttpResponseTrailersFeature>()?.Trailers?.Clear();
                NormalizeManagedConnectionNominations(_context.Response.Headers);
            }

            private bool CanPreserveSourceBody()
            {
                var response = _context.Response;
                return (!response.ContentLength.HasValue
                        || (_sourceFallbackBodyLength.HasValue
                            && response.ContentLength.Value
                                == _sourceFallbackBodyLength.Value))
                    && response.Headers["Content-Encoding"].ToString().Equals(
                        _sourceFallbackContentEncoding,
                        StringComparison.Ordinal)
                    && !response.Headers.ContainsKey("Content-Range");
            }
        }

        private sealed class OuterResponseFramingSnapshot
        {
            private readonly ResponseHeaderValue _contentLength;
            private readonly ResponseHeaderValue _contentType;
            private readonly ResponseHeaderValue _contentEncoding;
            private readonly ResponseHeaderValue _transferEncoding;

            private OuterResponseFramingSnapshot(
                int statusCode,
                bool bodyPending,
                ResponseHeaderValue contentLength,
                ResponseHeaderValue contentType,
                ResponseHeaderValue contentEncoding,
                ResponseHeaderValue transferEncoding)
            {
                StatusCode = statusCode;
                BodyPending = bodyPending;
                _contentLength = contentLength;
                _contentType = contentType;
                _contentEncoding = contentEncoding;
                _transferEncoding = transferEncoding;
            }

            public int StatusCode { get; }

            public bool BodyPending { get; }

            public static OuterResponseFramingSnapshot Capture(
                HttpResponse response,
                bool bodyPending) =>
                new OuterResponseFramingSnapshot(
                    response.StatusCode,
                    bodyPending,
                    ResponseHeaderValue.Capture(response.Headers, "Content-Length"),
                    ResponseHeaderValue.Capture(response.Headers, "Content-Type"),
                    ResponseHeaderValue.Capture(response.Headers, "Content-Encoding"),
                    ResponseHeaderValue.Capture(response.Headers, "Transfer-Encoding"));

            public void RestoreHeaders(IHeaderDictionary headers)
            {
                _contentLength.Restore(headers, "Content-Length");
                _contentType.Restore(headers, "Content-Type");
                _contentEncoding.Restore(headers, "Content-Encoding");
                _transferEncoding.Restore(headers, "Transfer-Encoding");
            }
        }

        private readonly struct ResponseHeaderValue
        {
            private ResponseHeaderValue(bool wasPresent, StringValues value)
            {
                WasPresent = wasPresent;
                Value = value;
            }

            private bool WasPresent { get; }

            private StringValues Value { get; }

            public static ResponseHeaderValue Capture(
                IHeaderDictionary headers,
                string name) =>
                new ResponseHeaderValue(
                    headers.TryGetValue(name, out var value),
                    value);

            public void Restore(IHeaderDictionary headers, string name)
            {
                if (WasPresent)
                {
                    headers[name] = Value;
                }
                else
                {
                    headers.Remove(name);
                }
            }
        }

        private sealed class ResponseHeaderPresence
        {
            private static readonly string[] ManagedNames =
            {
                "Cache-Control",
                "Content-Encoding",
                "Content-Type",
                "ETag",
                "Last-Modified",
                "Vary",
            };

            private readonly HashSet<string> _present;

            private ResponseHeaderPresence(HashSet<string> present)
            {
                _present = present;
            }

            public static ResponseHeaderPresence Capture(
                HttpResponse response,
                CachedRepresentation representation)
            {
                var present = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (var name in ManagedNames)
                {
                    if (response.Headers.ContainsKey(name))
                    {
                        present.Add(name);
                    }
                }

                foreach (var header in representation.ReplayHeaders)
                {
                    if (response.Headers.ContainsKey(header.Name))
                    {
                        present.Add(header.Name);
                    }
                }

                return new ResponseHeaderPresence(present);
            }

            public bool WasPresent(string name) => _present.Contains(name);
        }

        private enum CacheRetentionDecision
        {
            Retain,
            EvictEntry,
            EvictBase,
        }

        private enum ResponseBodySelection
        {
            None,
            Representation,
            Source,
        }

        private static bool StatusAllowsResponseBody(int statusCode) =>
            statusCode >= StatusCodes.Status200OK
            && statusCode != StatusCodes.Status204NoContent
            && statusCode != StatusCodes.Status205ResetContent
            && statusCode != StatusCodes.Status304NotModified;

        private readonly struct CacheAdmission
        {
            public CacheAdmission(
                string key,
                string[] varyHeaders,
                VaryRequestValue[] varyRequestValues,
                bool canStore,
                bool evictBaseOnRejection)
            {
                Key = key;
                VaryHeaders = varyHeaders;
                VaryRequestValues = varyRequestValues;
                CanStore = canStore;
                EvictBaseOnRejection = evictBaseOnRejection;
            }

            public string Key { get; }

            public string[] VaryHeaders { get; }

            public VaryRequestValue[] VaryRequestValues { get; }

            public bool CanStore { get; }

            public bool EvictBaseOnRejection { get; }
        }

        private readonly struct VaryRequestValue
        {
            private VaryRequestValue(bool isPresent, string[] values)
            {
                IsPresent = isPresent;
                Values = values;
            }

            public bool IsPresent { get; }

            public string[] Values { get; }

            public long CharacterCount => Values.Sum(value => (long)value.Length);

            public static VaryRequestValue Capture(IHeaderDictionary headers, string name)
            {
                if (!headers.TryGetValue(name, out var values))
                {
                    return new VaryRequestValue(false, Array.Empty<string>());
                }

                return new VaryRequestValue(
                    true,
                    values.Select(value => value ?? string.Empty).ToArray());
            }

            public static VaryRequestValue Capture(
                RequestCacheSnapshot snapshot,
                string name)
            {
                if (!snapshot.TryGetValue(name, out var values))
                {
                    return new VaryRequestValue(false, Array.Empty<string>());
                }

                return new VaryRequestValue(true, values);
            }

            public bool Matches(RequestCacheSnapshot snapshot, string name) =>
                Equals(Capture(snapshot, name));

            public bool Equals(VaryRequestValue other)
            {
                if (IsPresent != other.IsPresent || Values.Length != other.Values.Length)
                {
                    return false;
                }

                for (var index = 0; index < Values.Length; index++)
                {
                    if (!Values[index].Equals(other.Values[index], StringComparison.Ordinal))
                    {
                        return false;
                    }
                }

                return true;
            }

            public void AppendKeyMaterial(StringBuilder destination)
            {
                destination.Append(IsPresent ? "\0present" : "\0missing");
                destination.Append(':').Append(Values.Length);
                foreach (var value in Values)
                {
                    AppendLengthPrefixed(destination, value);
                }
            }
        }

        private readonly struct CachedHeader
        {
            public CachedHeader(string name, string[] values)
            {
                Name = name;
                Values = values;
            }

            public string Name { get; }

            public string[] Values { get; }

            public bool HasSameValues(CachedHeader other)
            {
                if (!Name.Equals(other.Name, StringComparison.OrdinalIgnoreCase)
                    || Values.Length != other.Values.Length)
                {
                    return false;
                }

                for (var index = 0; index < Values.Length; index++)
                {
                    if (!Values[index].Equals(other.Values[index], StringComparison.Ordinal))
                    {
                        return false;
                    }
                }

                return true;
            }
        }

        /// <summary>
        /// One fully-formed, ready-to-send injected representation plus the source
        /// validators and Vary tuple needed to select/revalidate it on the next request.
        /// </summary>
        private sealed class CachedRepresentation
        {
            public static CachedRepresentation CreateCandidate(
                string baseKey,
                byte[] body,
                string etag,
                string contentType,
                string contentEncoding) =>
                new CachedRepresentation(
                    baseKey,
                    baseKey,
                    body,
                    etag,
                    string.Empty,
                    string.Empty,
                    string.Empty,
                    contentType,
                    contentEncoding,
                    string.Empty,
                    string.Empty,
                    Array.Empty<CachedHeader>(),
                    Array.Empty<string>(),
                    Array.Empty<VaryRequestValue>(),
                    canStore: false,
                    evictBaseOnRejection: false);

            public CachedRepresentation(
                string key,
                string baseKey,
                byte[] body,
                string etag,
                string sourceETag,
                string sourceLastModified,
                string sourceContentType,
                string contentType,
                string contentEncoding,
                string cacheControl,
                string vary,
                CachedHeader[] replayHeaders,
                string[] varyHeaders,
                VaryRequestValue[] varyRequestValues,
                bool canStore,
                bool evictBaseOnRejection)
            {
                Key = key;
                BaseKey = baseKey;
                Body = body;
                ETag = etag;
                SourceETag = sourceETag;
                SourceLastModified = sourceLastModified;
                SourceContentType = sourceContentType;
                ContentType = contentType;
                ContentEncoding = contentEncoding;
                CacheControl = cacheControl;
                Vary = vary;
                ReplayHeaders = replayHeaders;
                VaryHeaders = varyHeaders;
                VaryRequestValues = varyRequestValues;
                CanStore = canStore;
                EvictBaseOnRejection = evictBaseOnRejection;
            }

            public string Key { get; }

            public string BaseKey { get; }

            public byte[] Body { get; }

            public string ETag { get; }

            public string SourceETag { get; }

            public string SourceLastModified { get; }

            public string SourceContentType { get; }

            public string ContentType { get; }

            public string ContentEncoding { get; }

            public string CacheControl { get; }

            public string Vary { get; }

            public CachedHeader[] ReplayHeaders { get; }

            public string[] VaryHeaders { get; }

            public VaryRequestValue[] VaryRequestValues { get; }

            public bool CanStore { get; }

            public bool EvictBaseOnRejection { get; }

            public bool MatchesVary(RequestCacheSnapshot requestSnapshot)
            {
                for (var index = 0; index < VaryHeaders.Length; index++)
                {
                    if (!VaryRequestValues[index].Matches(requestSnapshot, VaryHeaders[index]))
                    {
                        return false;
                    }
                }

                return true;
            }

            public bool MatchesVary(
                IReadOnlyList<string> varyHeaders,
                IReadOnlyList<VaryRequestValue> varyRequestValues)
            {
                for (var currentIndex = 0; currentIndex < varyHeaders.Count; currentIndex++)
                {
                    var cachedIndex = Array.FindIndex(
                        VaryHeaders,
                        name => name.Equals(varyHeaders[currentIndex], StringComparison.OrdinalIgnoreCase));
                    if (cachedIndex < 0
                        || !VaryRequestValues[cachedIndex].Equals(varyRequestValues[currentIndex]))
                    {
                        return false;
                    }
                }

                return true;
            }

            public bool HasSameVaryHeaders(IReadOnlyList<string> varyHeaders) =>
                VaryHeaders.Length == varyHeaders.Count
                && VaryHeaders.SequenceEqual(varyHeaders, StringComparer.OrdinalIgnoreCase);

            public bool HasCompatibleRevalidationVary(IReadOnlyList<string> varyHeaders)
            {
                if (HasSameVaryHeaders(varyHeaders))
                {
                    return true;
                }

                // ResponseCompression adds Accept-Encoding to a compressible 200,
                // but it can omit that middleware-owned value from a bodyless 304.
                // Keeping the old dimension is conservative: it narrows future reuse.
                var withoutAcceptEncoding = VaryHeaders
                    .Where(name => !name.Equals(
                        "accept-encoding",
                        StringComparison.OrdinalIgnoreCase))
                    .ToArray();
                return withoutAcceptEncoding.Length == VaryHeaders.Length - 1
                    && withoutAcceptEncoding.Length == varyHeaders.Count
                    && withoutAcceptEncoding.SequenceEqual(
                        varyHeaders,
                        StringComparer.OrdinalIgnoreCase);
            }

            public bool HasCompatibleRevalidationHeaders(
                IReadOnlyList<CachedHeader> revalidationHeaders)
            {
                foreach (var current in revalidationHeaders)
                {
                    var cachedIndex = Array.FindIndex(
                        ReplayHeaders,
                        header => header.Name.Equals(
                            current.Name,
                            StringComparison.OrdinalIgnoreCase));
                    if (cachedIndex < 0
                        || !ReplayHeaders[cachedIndex].HasSameValues(current))
                    {
                        return false;
                    }
                }

                return true;
            }

            public bool HasSameReplayHeaders(IReadOnlyList<CachedHeader> headers)
            {
                if (ReplayHeaders.Length != headers.Count)
                {
                    return false;
                }

                foreach (var current in headers)
                {
                    var cachedIndex = Array.FindIndex(
                        ReplayHeaders,
                        header => header.Name.Equals(
                            current.Name,
                            StringComparison.OrdinalIgnoreCase));
                    if (cachedIndex < 0
                        || !ReplayHeaders[cachedIndex].HasSameValues(current))
                    {
                        return false;
                    }
                }

                return true;
            }
        }
    }
}
