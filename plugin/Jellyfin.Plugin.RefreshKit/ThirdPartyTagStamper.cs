using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// THIRD-PARTY TAG STAMPING — mechanism 2 of the standalone plugin.
    ///
    /// <para>
    /// Other plugins put their client code into the page in two ways: they patch
    /// jellyfin-web's index.html ON DISK, or they inject a tag at serve time
    /// through their own middleware. Either way the tag lands in the HTML this
    /// plugin's middleware is holding, and either way the URL is usually
    /// UNVERSIONED — <c>&lt;script src="/web/mediabar/mediabar.js"&gt;</c>. A browser
    /// that has cached that URL keeps running the old file after the plugin is
    /// upgraded, because the URL never changed.
    /// </para>
    ///
    /// <para>
    /// This class appends <c>?rkv={generation}</c> to those URLs. The generation
    /// changes whenever ANY installed plugin changes
    /// (<see cref="PluginGenerationProvider"/>), so an upgrade of ANY plugin
    /// gives every stamped URL a new identity exactly once, and the browser must
    /// fetch it again.
    /// </para>
    ///
    /// <para>CONSERVATISM RULES (a wrong stamp is worse than a missing one)</para>
    /// <list type="number">
    /// <item><description>Only <c>&lt;script src&gt;</c> and
    /// <c>&lt;link rel="stylesheet" href&gt;</c>. Inline scripts have no URL;
    /// manifests, icons, preconnects and preloads are left alone.</description></item>
    /// <item><description>Same-origin relative URLs only. Anything with a scheme
    /// (<c>https:</c>, <c>data:</c>, <c>blob:</c>) or a protocol-relative
    /// <c>//host/…</c> is skipped: a CDN or a third-party origin may key its
    /// cache, its CORS or its 404 behaviour on the exact URL.</description></item>
    /// <item><description>Never double-stamp. Any pre-existing <c>rkv</c> param is
    /// SCRUBBED first and the current generation restamped, so repeated passes
    /// and generation changes converge instead of accumulating.</description></item>
    /// <item><description>Never touch a URL that is already version-addressed:
    /// a query param named v/ver/version/hash/rev/build/cb/… (see
    /// <see cref="VersionishKeys"/>), or an opaque valueless query such as
    /// jellyfin-web's own <c>?3cf5acc8506265662d4f</c>.</description></item>
    /// <item><description>Never touch a content-hashed filename
    /// (<c>main.jellyfin.f725276386e5b19afe0c.css</c>): those bytes are already
    /// immutable per URL, and restamping them on every generation change would
    /// throw away megabytes of a user's warm cache for nothing.</description></item>
    /// <item><description>Attribute order, quoting style and whitespace are
    /// preserved: only the characters INSIDE the src/href value are replaced.</description></item>
    /// </list>
    ///
    /// <para>ORDERING CAVEAT — read this before believing the stamps are complete</para>
    /// <para>
    /// This runs inside the refresh kit's index.html middleware, which is
    /// registered as an <see cref="Microsoft.AspNetCore.Hosting.IStartupFilter"/>.
    /// ASP.NET Core composes startup filters so that the FIRST-registered filter
    /// ends up OUTERMOST, and plugin service registrators run in the host's
    /// plugin load order. So:
    /// </para>
    /// <list type="bullet">
    /// <item><description>tags patched into index.html ON DISK are always seen and
    /// stamped (they are in the bytes the static-file handler produces);</description></item>
    /// <item><description>tags injected by another plugin's middleware INSIDE this
    /// one (i.e. that plugin registered its filter after this plugin) are seen
    /// and stamped;</description></item>
    /// <item><description>tags injected by a middleware OUTSIDE this one (that
    /// plugin registered first) are appended to the response AFTER this stamping
    /// pass and are therefore NOT stamped. Nothing breaks — that plugin's tags
    /// simply keep whatever cache behaviour they had — and the reload mechanism
    /// (mechanism 3) still gets the tab onto the new build.</description></item>
    /// </list>
    /// <para>
    /// There is no way for a plugin to force itself outermost, so this is a real
    /// limitation rather than a bug to fix: it is documented in plugin/README.md
    /// with the same wording. In practice the common cases (on-disk patching, and
    /// plugins that inject via Jellyfin's own web-file transformation) are all
    /// covered.
    /// </para>
    /// </summary>
    public static class ThirdPartyTagStamper
    {
        /// <summary>The query parameter this plugin owns. Nothing else may use it.</summary>
        public const string StampParameter = "rkv";

        /// <summary>
        /// Query keys that already make a URL change per release. A tag carrying
        /// one of these is somebody's deliberate cache-busting scheme and is left
        /// exactly as its author wrote it.
        /// </summary>
        private static readonly HashSet<string> VersionishKeys = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "v", "ver", "vers", "version", "rev", "revision", "hash", "build", "buildid",
            "cb", "cachebust", "cachebuster", "nocache", "_", StampParameter,
        };

        private static readonly Regex TagRegex = new Regex(
            "<(script|link)\\b[^>]*>",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Compiled);

        private static readonly Regex SchemeRegex = new Regex(
            "^[a-zA-Z][a-zA-Z0-9+.\\-]*:",
            RegexOptions.CultureInvariant | RegexOptions.Compiled);

        // webpack/vite content hashes: ".f725276386e5b19afe0c.css", ".a1b2c3d4.chunk.js".
        private static readonly Regex ContentHashedFileRegex = new Regex(
            "\\.[0-9a-fA-F]{8,}\\.[a-zA-Z0-9]+$",
            RegexOptions.CultureInvariant | RegexOptions.Compiled);

        /// <summary>
        /// Stamps every eligible third-party tag in <paramref name="html"/>.
        /// Pure, idempotent and total: any failure returns the input unchanged,
        /// because a shell served without stamps still works and a shell we broke
        /// does not.
        /// </summary>
        /// <param name="html">The full index.html document.</param>
        /// <param name="generation">The current generation token.</param>
        /// <param name="ownTagMarker">
        /// A substring identifying THIS plugin's own tags (e.g.
        /// <c>plugin="Jellyfin Refresh Kit"</c>); tags containing it are skipped,
        /// since the kit versions its own URL with <c>?v=</c> already.
        /// </param>
        public static string Stamp(string html, string generation, string? ownTagMarker)
        {
            if (string.IsNullOrEmpty(html) || string.IsNullOrWhiteSpace(generation))
            {
                return html;
            }

            try
            {
                var safeGeneration = Uri.EscapeDataString(generation);
                return TagRegex.Replace(html, match => StampTag(match.Value, safeGeneration, ownTagMarker));
            }
            catch
            {
                return html;
            }
        }

        private static string StampTag(string tag, string generation, string? ownTagMarker)
        {
            if (!string.IsNullOrEmpty(ownTagMarker)
                && tag.IndexOf(ownTagMarker, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return tag;
            }

            var isLink = tag.StartsWith("<link", StringComparison.OrdinalIgnoreCase);
            if (isLink && !IsStylesheet(tag))
            {
                return tag;
            }

            var attribute = isLink ? "href" : "src";
            if (!TryFindAttributeValue(tag, attribute, out var start, out var length))
            {
                // An inline <script> (no src) or a <link> without href.
                return tag;
            }

            var original = tag.Substring(start, length);
            var stamped = StampUrl(original, generation);
            if (stamped == null)
            {
                return tag;
            }

            return string.Concat(tag.AsSpan(0, start), stamped, tag.AsSpan(start + length));
        }

        private static bool IsStylesheet(string tag)
        {
            return TryFindAttributeValue(tag, "rel", out var start, out var length)
                && tag.Substring(start, length).IndexOf("stylesheet", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        /// <summary>
        /// Locates the VALUE span of an attribute inside a single tag, handling
        /// double-quoted, single-quoted and unquoted values. Returning a span
        /// (rather than a rewritten tag) is what keeps attribute order and
        /// formatting byte-identical.
        /// </summary>
        private static bool TryFindAttributeValue(string tag, string name, out int start, out int length)
        {
            start = 0;
            length = 0;
            var pattern = "[\\s\"']" + Regex.Escape(name)
                + "\\s*=\\s*(?:\"(?<v>[^\"]*)\"|'(?<v>[^']*)'|(?<v>[^\\s\"'>]+))";
            var match = Regex.Match(tag, pattern, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
            if (!match.Success)
            {
                return false;
            }

            var group = match.Groups["v"];
            start = group.Index;
            length = group.Length;
            return true;
        }

        /// <summary>
        /// The URL decision. Returns the stamped URL, or null when the URL must
        /// be left alone. Every rule in the class doc is implemented here, in the
        /// order a reviewer would want to check them.
        /// </summary>
        internal static string? StampUrl(string url, string generation)
        {
            if (string.IsNullOrWhiteSpace(url) || url != url.Trim())
            {
                // Leading/trailing whitespace in the attribute is somebody else's
                // formatting; do not normalise it as a side effect.
                return null;
            }

            if (url.StartsWith("//", StringComparison.Ordinal) || SchemeRegex.IsMatch(url))
            {
                return null;
            }

            var fragmentIndex = url.IndexOf('#', StringComparison.Ordinal);
            var fragment = fragmentIndex >= 0 ? url.Substring(fragmentIndex) : string.Empty;
            var withoutFragment = fragmentIndex >= 0 ? url.Substring(0, fragmentIndex) : url;

            var queryIndex = withoutFragment.IndexOf('?', StringComparison.Ordinal);
            var path = queryIndex >= 0 ? withoutFragment.Substring(0, queryIndex) : withoutFragment;
            var query = queryIndex >= 0 ? withoutFragment.Substring(queryIndex + 1) : string.Empty;

            if (path.Length == 0)
            {
                return null;
            }

            if (IsContentHashedPath(path))
            {
                return null;
            }

            if (!TryScrubQuery(query, out var scrubbed))
            {
                return null;
            }

            var builder = new StringBuilder(path);
            builder.Append('?');
            if (scrubbed.Length > 0)
            {
                builder.Append(scrubbed).Append('&');
            }

            builder.Append(StampParameter).Append('=').Append(generation).Append(fragment);
            var result = builder.ToString();
            return string.Equals(result, url, StringComparison.Ordinal) ? null : result;
        }

        private static bool IsContentHashedPath(string path)
        {
            var lastSlash = path.LastIndexOf('/');
            var file = lastSlash >= 0 ? path.Substring(lastSlash + 1) : path;
            return file.Length > 0 && ContentHashedFileRegex.IsMatch(file);
        }

        /// <summary>
        /// Removes this plugin's own <c>rkv</c> params (that is the "scrub" in
        /// scrub-then-restamp) and refuses the whole URL if what remains is
        /// already a version identity — including an opaque valueless query,
        /// which is jellyfin-web's own bundle-hash convention.
        /// </summary>
        private static bool TryScrubQuery(string query, out string scrubbed)
        {
            scrubbed = string.Empty;
            if (query.Length == 0)
            {
                return true;
            }

            var kept = new List<string>();
            foreach (var segment in query.Split('&'))
            {
                if (segment.Length == 0)
                {
                    continue;
                }

                var equals = segment.IndexOf('=', StringComparison.Ordinal);
                if (equals < 0)
                {
                    // "?3cf5acc8506265662d4f" — an opaque token whose meaning we
                    // cannot know. Assume it is already an identity.
                    return false;
                }

                var key = segment.Substring(0, equals);
                if (string.Equals(key, StampParameter, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                if (VersionishKeys.Contains(key))
                {
                    return false;
                }

                kept.Add(segment);
            }

            scrubbed = string.Join("&", kept);
            return true;
        }
    }
}
