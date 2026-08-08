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
    /// <para>WHY A TOKENIZER AND NOT A REGEX</para>
    /// <para>
    /// The obvious implementation — <c>&lt;(script|link)\b[^&gt;]*&gt;</c> over the
    /// whole document — is wrong in three ways that a real page hits, all three
    /// reproduced as unit tests in
    /// <c>Jellyfin.Plugin.RefreshKit.Tests/ThirdPartyTagStamperTests.cs</c>:
    /// </para>
    /// <list type="number">
    /// <item><description>It matches tag-like text INSIDE an inline script body.
    /// <c>var a = "&lt;script src='/x.js'&gt;";</c> is JavaScript source, not a
    /// tag, and rewriting it CORRUPTS the script — the one thing this class
    /// promises never to touch.</description></item>
    /// <item><description><c>[^&gt;]*</c> stops at the first <c>&gt;</c> even when
    /// it is inside a quoted attribute value, so
    /// <c>&lt;script data-json="a&gt;b" src="/real.js"&gt;</c> is cut short and the
    /// real <c>src</c> is never seen.</description></item>
    /// <item><description>Finding the attribute with a second regex over the tag
    /// text matches <c>src</c> inside ANOTHER attribute's quoted value, so
    /// <c>&lt;script data-template=" src='/fake.js'" src="/real.js"&gt;</c>
    /// stamped the decoy and missed the real one.</description></item>
    /// </list>
    /// <para>
    /// So the document is walked by a small state machine instead: it skips
    /// comments, doctypes and processing instructions, skips the CONTENTS of raw
    /// text elements (<c>script</c>, <c>style</c>, <c>textarea</c>, <c>title</c>)
    /// entirely, finds a tag's end by parsing its attributes rather than hunting
    /// for <c>&gt;</c>, and reads attribute values from that parse. Only real
    /// opening tags in ordinary markup are ever stamped. When nothing is stamped
    /// the ORIGINAL string instance is returned, so a page the kit does not
    /// change is byte-identical by construction.
    /// </para>
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

        /// <summary>
        /// Elements whose CONTENT is not markup. Everything between the opening
        /// and closing tag of one of these is skipped wholesale: a
        /// <c>&lt;script&gt;</c> body is JavaScript, and a <c>&lt;title&gt;</c> or
        /// <c>&lt;textarea&gt;</c> body is literal text, so tag-like strings in
        /// there are not tags and must never be rewritten.
        /// </summary>
        private static readonly string[] RawTextElements = { "script", "style", "textarea", "title" };

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
                return Walk(html, Uri.EscapeDataString(generation), ownTagMarker);
            }
            catch
            {
                return html;
            }
        }

        /// <summary>
        /// The state machine. Copies the document to a builder verbatim, dropping
        /// into <see cref="StampTag"/> only for opening <c>script</c>/<c>link</c>
        /// tags found in ordinary markup — never inside comments, doctypes,
        /// processing instructions or raw text element bodies.
        /// </summary>
        private static string Walk(string html, string generation, string? ownTagMarker)
        {
            var builder = new StringBuilder(html.Length + 64);
            var index = 0;
            var changed = false;

            while (index < html.Length)
            {
                var open = html.IndexOf('<', index);
                if (open < 0)
                {
                    builder.Append(html, index, html.Length - index);
                    break;
                }

                builder.Append(html, index, open - index);

                // "<!--…-->" — a comment. Its contents are not markup; a
                // commented-out <script src> is not a tag the browser will fetch,
                // so stamping it would rewrite bytes for no benefit.
                if (StartsWith(html, open, "<!--"))
                {
                    var close = html.IndexOf("-->", open + 4, StringComparison.Ordinal);
                    var end = close < 0 ? html.Length : close + 3;
                    builder.Append(html, open, end - open);
                    index = end;
                    continue;
                }

                // "<!doctype…>", "<![CDATA[…]]>", "<?…?>" and every closing tag:
                // nothing here is ever stamped, so copy through to the '>'.
                if (open + 1 < html.Length
                    && (html[open + 1] == '!' || html[open + 1] == '?' || html[open + 1] == '/'))
                {
                    var close = html.IndexOf('>', open + 1);
                    var end = close < 0 ? html.Length : close + 1;
                    builder.Append(html, open, end - open);
                    index = end;
                    continue;
                }

                // A '<' not followed by a letter is literal text ("a < b").
                if (open + 1 >= html.Length || !IsAsciiLetter(html[open + 1]))
                {
                    builder.Append('<');
                    index = open + 1;
                    continue;
                }

                var nameEnd = open + 1;
                while (nameEnd < html.Length && IsNameCharacter(html[nameEnd]))
                {
                    nameEnd++;
                }

                var name = html.Substring(open + 1, nameEnd - open - 1);
                if (!TryParseAttributes(html, nameEnd, out var attributes, out var tagEnd))
                {
                    // An unterminated tag: the rest of the document is not
                    // parseable markup, so hand it back untouched.
                    builder.Append(html, open, html.Length - open);
                    index = html.Length;
                    break;
                }

                if (StampTag(html, open, tagEnd, name, attributes, generation, ownTagMarker, builder))
                {
                    changed = true;
                }

                index = tagEnd + 1;

                if (IsRawTextElement(name))
                {
                    // Skip the body wholesale — it is script/style source or
                    // literal text, never markup.
                    var close = IndexOfClosingTag(html, index, name);
                    var end = close < 0 ? html.Length : close;
                    builder.Append(html, index, end - index);
                    index = end;
                }
            }

            // Byte-preservation is a promise, not an accident: when no URL moved,
            // hand back the very same instance the caller passed in.
            return changed ? builder.ToString() : html;
        }

        /// <summary>
        /// Appends one opening tag to <paramref name="builder"/>, stamping its
        /// URL if every conservatism rule allows it. Returns true when the tag
        /// was changed. The tag is emitted by copying the original characters
        /// around the URL span, which is what keeps attribute order, quoting and
        /// whitespace byte-identical.
        /// </summary>
        private static bool StampTag(
            string html,
            int tagStart,
            int tagEnd,
            string name,
            List<TagAttribute> attributes,
            string generation,
            string? ownTagMarker,
            StringBuilder builder)
        {
            var length = tagEnd - tagStart + 1;
            var isLink = name.Equals("link", StringComparison.OrdinalIgnoreCase);
            var isScript = name.Equals("script", StringComparison.OrdinalIgnoreCase);

            if (!isLink && !isScript)
            {
                builder.Append(html, tagStart, length);
                return false;
            }

            if (!string.IsNullOrEmpty(ownTagMarker)
                && html.IndexOf(ownTagMarker, tagStart, length, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                builder.Append(html, tagStart, length);
                return false;
            }

            if (isLink && !IsStylesheet(html, attributes))
            {
                builder.Append(html, tagStart, length);
                return false;
            }

            if (!TryGetAttribute(attributes, isLink ? "href" : "src", out var url))
            {
                // An inline <script> (no src) or a <link> without href.
                builder.Append(html, tagStart, length);
                return false;
            }

            var stamped = StampUrl(html.Substring(url.ValueStart, url.ValueLength), generation);
            if (stamped == null)
            {
                builder.Append(html, tagStart, length);
                return false;
            }

            builder.Append(html, tagStart, url.ValueStart - tagStart);
            builder.Append(stamped);
            builder.Append(html, url.ValueStart + url.ValueLength, tagEnd + 1 - url.ValueStart - url.ValueLength);
            return true;
        }

        private static bool IsStylesheet(string html, List<TagAttribute> attributes)
        {
            return TryGetAttribute(attributes, "rel", out var rel)
                && html.IndexOf("stylesheet", rel.ValueStart, rel.ValueLength, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static bool TryGetAttribute(List<TagAttribute> attributes, string name, out TagAttribute found)
        {
            foreach (var attribute in attributes)
            {
                if (attribute.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    found = attribute;
                    return true;
                }
            }

            found = default;
            return false;
        }

        /// <summary>
        /// Parses a tag's attribute list starting just after the tag name, and
        /// reports where the tag ends. Quoted values are consumed as units, which
        /// is what makes <c>data-json="a&gt;b"</c> a value containing '&gt;'
        /// rather than the end of the tag, and what stops a <c>src</c> written
        /// inside another attribute's value from being mistaken for a real
        /// attribute.
        /// </summary>
        /// <returns>False when the tag is never terminated.</returns>
        private static bool TryParseAttributes(string html, int from, out List<TagAttribute> attributes, out int tagEnd)
        {
            attributes = new List<TagAttribute>();
            tagEnd = -1;
            var index = from;

            while (index < html.Length)
            {
                while (index < html.Length && char.IsWhiteSpace(html[index]))
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

                // The '/' of a self-closing tag; the '>' decides where it ends.
                if (html[index] == '/')
                {
                    index++;
                    continue;
                }

                var nameStart = index;
                while (index < html.Length
                       && html[index] != '='
                       && html[index] != '>'
                       && html[index] != '/'
                       && !char.IsWhiteSpace(html[index]))
                {
                    index++;
                }

                var name = html.Substring(nameStart, index - nameStart);

                while (index < html.Length && char.IsWhiteSpace(html[index]))
                {
                    index++;
                }

                if (index >= html.Length)
                {
                    return false;
                }

                if (html[index] != '=')
                {
                    // A valueless attribute ("defer", "async").
                    attributes.Add(new TagAttribute(name, index, 0));
                    continue;
                }

                index++;
                while (index < html.Length && char.IsWhiteSpace(html[index]))
                {
                    index++;
                }

                if (index >= html.Length)
                {
                    return false;
                }

                var quote = html[index];
                if (quote == '"' || quote == '\'')
                {
                    var valueStart = index + 1;
                    var valueEnd = html.IndexOf(quote, valueStart);
                    if (valueEnd < 0)
                    {
                        return false;
                    }

                    attributes.Add(new TagAttribute(name, valueStart, valueEnd - valueStart));
                    index = valueEnd + 1;
                }
                else
                {
                    var valueStart = index;
                    while (index < html.Length && html[index] != '>' && !char.IsWhiteSpace(html[index]))
                    {
                        index++;
                    }

                    attributes.Add(new TagAttribute(name, valueStart, index - valueStart));
                }
            }

            return false;
        }

        /// <summary>
        /// Finds the <c>&lt;/name</c> that ends a raw text element, matching the
        /// way a browser's tokenizer does: the name must be followed by
        /// whitespace, '/' or '&gt;', so <c>&lt;/scriptfoo&gt;</c> does not close
        /// a <c>&lt;script&gt;</c>.
        /// </summary>
        private static int IndexOfClosingTag(string html, int from, string name)
        {
            var index = from;
            while (index < html.Length)
            {
                var open = html.IndexOf("</", index, StringComparison.Ordinal);
                if (open < 0)
                {
                    return -1;
                }

                var after = open + 2;
                if (after + name.Length <= html.Length
                    && string.Compare(html, after, name, 0, name.Length, StringComparison.OrdinalIgnoreCase) == 0)
                {
                    var tail = after + name.Length;
                    if (tail >= html.Length || html[tail] == '>' || html[tail] == '/' || char.IsWhiteSpace(html[tail]))
                    {
                        return open;
                    }
                }

                index = open + 2;
            }

            return -1;
        }

        private static bool IsRawTextElement(string name)
        {
            foreach (var candidate in RawTextElements)
            {
                if (name.Equals(candidate, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool StartsWith(string html, int index, string value)
        {
            return index + value.Length <= html.Length
                && string.CompareOrdinal(html, index, value, 0, value.Length) == 0;
        }

        private static bool IsAsciiLetter(char value)
        {
            return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z');
        }

        private static bool IsNameCharacter(char value)
        {
            return IsAsciiLetter(value) || (value >= '0' && value <= '9') || value == '-' || value == ':' || value == '_';
        }

        /// <summary>One parsed attribute: its name, and where its value sits in the document.</summary>
        private readonly struct TagAttribute
        {
            public TagAttribute(string name, int valueStart, int valueLength)
            {
                Name = name;
                ValueStart = valueStart;
                ValueLength = valueLength;
            }

            public string Name { get; }

            public int ValueStart { get; }

            public int ValueLength { get; }
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
