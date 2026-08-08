using System;
using System.Collections.Generic;
using System.Net;
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
    /// through their own middleware. On-disk tags and middleware tags visible
    /// inside this plugin's transformation boundary can be stamped; tags appended
    /// later by an outer middleware cannot. These URLs are often UNVERSIONED —
    /// <c>&lt;script src="/web/mediabar/mediabar.js"&gt;</c> — so a browser that has
    /// cached one can otherwise keep running old bytes after an upgrade.
    /// </para>
    ///
    /// <para>
    /// This class appends <c>?rkv={generation}</c> to those URLs. The generation
    /// changes when new host/plugin code is actually activated, or when a loaded
    /// plugin's loose browser assets/settings change
    /// (<see cref="PluginGenerationProvider"/>). Thus a completed plugin upgrade
    /// gives each eligible, visible stamped URL a new identity, and the browser
    /// must fetch it again; merely staging restart-required bytes does not
    /// announce code that the server has not loaded yet.
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
    /// pass and are therefore NOT stamped. Those tags keep the owning plugin's
    /// cache behaviour and URL identity. The reload mechanism can move the tab
    /// onto a newly served shell, but cannot guarantee fresh bytes for an
    /// unchanged outer-owned asset URL.</description></item>
    /// </list>
    /// <para>
    /// There is no way for a plugin to force itself outermost, so this is a real
    /// limitation rather than a bug to fix: it is documented in plugin/README.md
    /// with the same wording. The common on-disk and Jellyfin web-file
    /// transformation cases remain inside the supported stamping boundary.
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
        private static readonly string[] RawTextElements =
        {
            "script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes",
        };

        private static readonly Regex SchemeRegex = new Regex(
            "^[a-zA-Z][a-zA-Z0-9+.\\-]*:",
            RegexOptions.CultureInvariant | RegexOptions.Compiled);

        // webpack-style content hashes: `.f725...css`, `.a1b2c3d4.chunk.js`,
        // and `main-f725...js`. The lookahead permits multiple suffix segments
        // without letting eight hex characters in a directory name qualify.
        private static readonly Regex ContentHashedFileRegex = new Regex(
            "(?:^|[.\\-])[0-9a-fA-F]{8,}(?=(?:\\.[a-zA-Z0-9_\\-]+)+$)",
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
            var elements = new List<ElementContext>();
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

                // HTML comments also close through the tokenizer's recovery
                // forms (`--!>`, `<!-->` and `<!--->`). Its contents are not markup; a
                // commented-out <script src> is not a tag the browser will fetch,
                // so stamping it would rewrite bytes for no benefit.
                if (StartsWith(html, open, "<!--"))
                {
                    var end = FindCommentEnd(html, open);
                    builder.Append(html, open, end - open);
                    index = end;
                    continue;
                }

                // "<!doctype…>", "<![CDATA[…]]>" and "<?…?>": nothing here
                // is ever stamped.
                if (open + 1 < html.Length
                    && (html[open + 1] == '!' || html[open + 1] == '?'))
                {
                    var cdataAllowed = IsCdataAllowedAtCurrentNode(elements);
                    var end = FindSpecialMarkupEnd(html, open, cdataAllowed);
                    if (end < 0)
                    {
                        return html;
                    }

                    builder.Append(html, open, end - open);
                    index = end;
                    continue;
                }

                // End tags can carry parser-recovered attributes containing
                // `>`. Consume the whole token, then update the namespace stack
                // so SVG integration points expose their HTML descendants.
                if (open + 1 < html.Length && html[open + 1] == '/')
                {
                    var nameStart = open + 2;
                    if (nameStart >= html.Length || !IsAsciiLetter(html[nameStart]))
                    {
                        // An invalid end-tag opener enters HTML's bogus-comment
                        // state. Quotes have no special meaning there; the first
                        // '>' ends the token.
                        var bogusEnd = html.IndexOf('>', nameStart);
                        var end = bogusEnd < 0 ? html.Length : bogusEnd + 1;
                        builder.Append(html, open, end - open);
                        index = end;
                        continue;
                    }

                    var closeNameEnd = nameStart;
                    while (closeNameEnd < html.Length
                        && html[closeNameEnd] != '>'
                        && html[closeNameEnd] != '/'
                        && !IsHtmlSpace(html[closeNameEnd]))
                    {
                        closeNameEnd++;
                    }

                    if (!TryFindTagEnd(html, closeNameEnd, out var closeTagEnd))
                    {
                        builder.Append(html, open, html.Length - open);
                        index = html.Length;
                        break;
                    }

                    builder.Append(html, open, closeTagEnd + 1 - open);
                    if (closeNameEnd > nameStart)
                    {
                        var closeName = html.Substring(nameStart, closeNameEnd - nameStart);
                        PopForForeignContentEndTagBreakout(elements, closeName);
                        if (ForeignEndTagRequiresFailClosed(elements, closeName))
                        {
                            return html;
                        }

                        if (EndTagCrossesHtmlScopeBoundary(elements, closeName))
                        {
                            return html;
                        }

                        if (!closeName.Equals("body", StringComparison.OrdinalIgnoreCase)
                            && !closeName.Equals("html", StringComparison.OrdinalIgnoreCase))
                        {
                            PopElement(elements, closeName);
                        }
                    }

                    index = closeTagEnd + 1;
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
                while (nameEnd < html.Length
                    && html[nameEnd] != '>'
                    && html[nameEnd] != '/'
                    && !IsHtmlSpace(html[nameEnd]))
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

                PopForForeignContentStartTagBreakout(elements, name, attributes);
                var elementNamespace = ResolveStartTagNamespace(elements, name);
                if (elementNamespace == MarkupNamespace.Html
                    && name.Equals("noscript", StringComparison.OrdinalIgnoreCase))
                {
                    // The contents of noscript are raw text when scripting is
                    // enabled and ordinary markup when it is disabled. A server
                    // cannot know the eventual UA flag, so URL stamping cannot
                    // safely infer an effective base in either parse.
                    return html;
                }

                if (elementNamespace == MarkupNamespace.Html
                    && name.Equals("base", StringComparison.OrdinalIgnoreCase)
                    && !IsInsideHtmlTemplate(elements)
                    && TryGetAttribute(attributes, "href", out var baseHref))
                {
                    var sourceHref = html.Substring(baseHref.ValueStart, baseHref.ValueLength);
                    if (HasUnsafeAbsoluteBase(sourceHref))
                    {
                        // The raw relative URLs below no longer imply the
                        // document's origin. Inspect every source candidate:
                        // foster parenting can reorder bases in the DOM, so
                        // source order cannot prove which one becomes effective.
                        return html;
                    }
                }

                if (elementNamespace == MarkupNamespace.Html
                    && StampTag(html, open, tagEnd, name, attributes, generation, ownTagMarker, builder))
                {
                    changed = true;
                }
                else if (elementNamespace != MarkupNamespace.Html)
                {
                    builder.Append(html, open, tagEnd + 1 - open);
                }

                index = tagEnd + 1;
                var selfClosing = StartTagIsSelfClosing(html, nameEnd, tagEnd);
                var annotationXmlHtmlIntegrationPoint =
                    IsAnnotationXmlHtmlIntegrationPoint(
                        html,
                        name,
                        elementNamespace,
                        attributes);
                PushElement(
                    elements,
                    name,
                    elementNamespace,
                    selfClosing,
                    annotationXmlHtmlIntegrationPoint);

                if (elementNamespace == MarkupNamespace.Html && IsRawTextElement(name))
                {
                    // Skip the body wholesale — it is script/style source or
                    // literal text, never markup. SVG <title> is deliberately
                    // excluded: it is an HTML integration point whose children
                    // are real HTML markup.
                    var close = name.Equals("script", StringComparison.OrdinalIgnoreCase)
                        ? IndexOfScriptClosingTag(html, index)
                        : IndexOfClosingTag(html, index, name);
                    var end = close < 0 ? html.Length : close;
                    builder.Append(html, index, end - index);
                    index = end;
                }
                else if (elementNamespace == MarkupNamespace.Html
                    && name.Equals("plaintext", StringComparison.OrdinalIgnoreCase))
                {
                    // HTML's plaintext state has no closing-tag escape: every
                    // remaining character, including `</plaintext>`, is text.
                    builder.Append(html, index, html.Length - index);
                    index = html.Length;
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
            if (!TryGetAttribute(attributes, "rel", out var rel))
            {
                return false;
            }

            // `rel` is an ASCII-whitespace-separated set of link-type tokens,
            // not a substring. Treating "notstylesheet" as a stylesheet changes
            // an unrelated URL, while missing `style&#x73;heet` fails to version
            // a real stylesheet because character references are decoded by the
            // HTML tokenizer before link types are interpreted.
            var value = WebUtility.HtmlDecode(html.Substring(rel.ValueStart, rel.ValueLength));
            var index = 0;
            while (index < value.Length)
            {
                while (index < value.Length && IsHtmlSpace(value[index]))
                {
                    index++;
                }

                var start = index;
                while (index < value.Length && !IsHtmlSpace(value[index]))
                {
                    index++;
                }

                if (index > start
                    && value.Substring(start, index - start)
                        .Equals("stylesheet", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool IsHtmlSpace(char value) =>
            value == '\u0009' || value == '\u000A' || value == '\u000C'
            || value == '\u000D' || value == '\u0020';

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
                while (index < html.Length && IsHtmlSpace(html[index]))
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
                       && !IsHtmlSpace(html[index]))
                {
                    index++;
                }

                var name = html.Substring(nameStart, index - nameStart);

                while (index < html.Length && IsHtmlSpace(html[index]))
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
                while (index < html.Length && IsHtmlSpace(html[index]))
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
                    while (index < html.Length && html[index] != '>' && !IsHtmlSpace(html[index]))
                    {
                        index++;
                    }

                    attributes.Add(new TagAttribute(name, valueStart, index - valueStart));
                }
            }

            return false;
        }

        /// <summary>
        /// Returns the first position after an HTML comment token. Besides the
        /// ordinary <c>--&gt;</c>, browsers recover <c>--!&gt;</c> as a close and
        /// abruptly close empty comments written as <c>&lt;!--&gt;</c> or
        /// <c>&lt;!---&gt;</c>. Stopping only at <c>--&gt;</c> hides real markup that
        /// follows any of those recovery forms.
        /// </summary>
        private static int FindCommentEnd(string html, int open)
        {
            var contentStart = open + 4;
            if (contentStart >= html.Length)
            {
                return html.Length;
            }

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
                // The same abrupt close is reachable after a nested comment
                // opener, so "<!--foo<!-->" ends at the second greater-than
                // sign rather than hiding all following markup to EOF.
                if (StartsWith(html, index, "<!-->"))
                {
                    return index + 5;
                }

                if (StartsWith(html, index, "-->"))
                {
                    return index + 3;
                }

                if (StartsWith(html, index, "--!>"))
                {
                    return index + 4;
                }
            }

            return html.Length;
        }

        private static bool HasUnsafeAbsoluteBase(string sourceHref)
        {
            // System.Net's entity table is intentionally smaller than HTML5's.
            // A named reference can synthesize ':', '/', '\\' or a control only
            // after tokenization, so an ampersand makes origin classification
            // ambiguous and therefore disables this optional rewrite.
            if (sourceHref.IndexOf('&', StringComparison.Ordinal) >= 0)
            {
                return true;
            }

            var href = NormalizeForOriginCheck(WebUtility.HtmlDecode(sourceHref));
            return href.StartsWith("//", StringComparison.Ordinal)
                || href.IndexOf('\\') >= 0
                || SchemeRegex.IsMatch(href);
        }

        private static MarkupNamespace ResolveStartTagNamespace(
            List<ElementContext> elements,
            string name)
        {
            var parent = elements.Count > 0
                ? elements[elements.Count - 1]
                : new ElementContext(string.Empty, MarkupNamespace.Html, false);
            var inHtml = parent.Namespace == MarkupNamespace.Html
                || parent.IsHtmlIntegrationPoint
                || (IsMathMlTextIntegrationPoint(parent)
                    && !name.Equals("mglyph", StringComparison.OrdinalIgnoreCase)
                    && !name.Equals("malignmark", StringComparison.OrdinalIgnoreCase))
                || (parent.Namespace == MarkupNamespace.MathMl
                    && parent.Name.Equals("annotation-xml", StringComparison.OrdinalIgnoreCase)
                    && name.Equals("svg", StringComparison.OrdinalIgnoreCase));
            if (inHtml)
            {
                if (name.Equals("svg", StringComparison.OrdinalIgnoreCase))
                {
                    return MarkupNamespace.Svg;
                }

                if (name.Equals("math", StringComparison.OrdinalIgnoreCase))
                {
                    return MarkupNamespace.MathMl;
                }

                return MarkupNamespace.Html;
            }

            return parent.Namespace;
        }

        private static bool IsMathMlTextIntegrationPoint(ElementContext element) =>
            element.Namespace == MarkupNamespace.MathMl
            && (element.Name.Equals("mi", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mo", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mn", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("ms", StringComparison.OrdinalIgnoreCase)
                || element.Name.Equals("mtext", StringComparison.OrdinalIgnoreCase));

        private static bool IsInsideHtmlTemplate(List<ElementContext> elements)
        {
            foreach (var element in elements)
            {
                if (element.Namespace == MarkupNamespace.Html
                    && element.Name.Equals("template", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool IsCdataAllowedAtCurrentNode(List<ElementContext> elements)
        {
            if (elements.Count == 0)
            {
                return false;
            }

            var current = elements[elements.Count - 1];
            return current.Namespace != MarkupNamespace.Html
                && !current.IsHtmlIntegrationPoint
                && !IsMathMlTextIntegrationPoint(current);
        }

        private static void PopForForeignContentStartTagBreakout(
            List<ElementContext> elements,
            string name,
            List<TagAttribute> attributes)
        {
            var isFontBreakout = name.Equals("font", StringComparison.OrdinalIgnoreCase)
                && (TryGetAttribute(attributes, "color", out _)
                    || TryGetAttribute(attributes, "face", out _)
                    || TryGetAttribute(attributes, "size", out _));
            if (!isFontBreakout && !IsForeignContentHtmlBreakoutStartTag(name))
            {
                return;
            }

            PopOrdinaryForeignAncestors(elements);
        }

        private static void PopForForeignContentEndTagBreakout(
            List<ElementContext> elements,
            string name)
        {
            if (name.Equals("br", StringComparison.OrdinalIgnoreCase)
                || name.Equals("p", StringComparison.OrdinalIgnoreCase))
            {
                PopOrdinaryForeignAncestors(elements);
            }
        }

        private static bool ForeignEndTagRequiresFailClosed(
            List<ElementContext> elements,
            string name)
        {
            if (elements.Count == 0
                || elements[elements.Count - 1].Namespace == MarkupNamespace.Html
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

            // The HTML parser may match an ancestor across the namespace
            // boundary or reprocess this token in an insertion mode our small
            // walker does not model. Optional stamping therefore fails closed.
            return true;
        }

        private static bool EndTagCrossesHtmlScopeBoundary(
            List<ElementContext> elements,
            string name)
        {
            if (name.Equals("body", StringComparison.OrdinalIgnoreCase)
                || name.Equals("html", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            var crossedBoundary = false;
            for (var index = elements.Count - 1; index >= 0; index--)
            {
                var element = elements[index];
                if (element.Namespace != MarkupNamespace.Html)
                {
                    continue;
                }

                if (element.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                {
                    return crossedBoundary;
                }

                if (IsHtmlScopeBoundary(element.Name))
                {
                    crossedBoundary = true;
                }
            }

            return false;
        }

        private static bool IsHtmlScopeBoundary(string name) =>
            name.Equals("select", StringComparison.OrdinalIgnoreCase)
            || name.Equals("applet", StringComparison.OrdinalIgnoreCase)
            || name.Equals("caption", StringComparison.OrdinalIgnoreCase)
            || name.Equals("marquee", StringComparison.OrdinalIgnoreCase)
            || name.Equals("object", StringComparison.OrdinalIgnoreCase)
            || name.Equals("table", StringComparison.OrdinalIgnoreCase)
            || name.Equals("td", StringComparison.OrdinalIgnoreCase)
            || name.Equals("th", StringComparison.OrdinalIgnoreCase)
            || name.Equals("template", StringComparison.OrdinalIgnoreCase);

        private static void PopOrdinaryForeignAncestors(List<ElementContext> elements)
        {
            while (elements.Count > 0)
            {
                var current = elements[elements.Count - 1];
                if (current.Namespace == MarkupNamespace.Html
                    || current.IsHtmlIntegrationPoint
                    || IsMathMlTextIntegrationPoint(current))
                {
                    return;
                }

                elements.RemoveAt(elements.Count - 1);
            }
        }

        private static bool IsForeignContentHtmlBreakoutStartTag(string name) =>
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

        private static bool IsAnnotationXmlHtmlIntegrationPoint(
            string html,
            string name,
            MarkupNamespace elementNamespace,
            List<TagAttribute> attributes)
        {
            if (elementNamespace != MarkupNamespace.MathMl
                || !name.Equals("annotation-xml", StringComparison.OrdinalIgnoreCase)
                || !TryGetAttribute(attributes, "encoding", out var encoding))
            {
                return false;
            }

            var value = DecodeAnnotationXmlEncoding(
                html.Substring(encoding.ValueStart, encoding.ValueLength));
            return value.Equals("text/html", StringComparison.OrdinalIgnoreCase)
                || value.Equals("application/xhtml+xml", StringComparison.OrdinalIgnoreCase);
        }

        private static string DecodeAnnotationXmlEncoding(string rawValue)
        {
            var decoded = new StringBuilder(rawValue.Length);
            for (var index = 0; index < rawValue.Length; index++)
            {
                if (rawValue[index] != '&')
                {
                    decoded.Append(rawValue[index]);
                    continue;
                }

                if (TryDecodeNumericCharacterReference(
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

        private static bool TryDecodeNumericCharacterReference(
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
                    ? HexDigitValue(value[index])
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

        private static int HexDigitValue(char value) =>
            value >= '0' && value <= '9'
                ? value - '0'
                : value >= 'a' && value <= 'f'
                    ? value - 'a' + 10
                    : value >= 'A' && value <= 'F'
                        ? value - 'A' + 10
                        : -1;

        private static void PushElement(
            List<ElementContext> elements,
            string name,
            MarkupNamespace elementNamespace,
            bool selfClosing,
            bool annotationXmlHtmlIntegrationPoint)
        {
            if ((elementNamespace != MarkupNamespace.Html && selfClosing)
                || (elementNamespace == MarkupNamespace.Html && IsHtmlVoidElement(name)))
            {
                return;
            }

            // The self-closing flag is ignored on non-void HTML elements.
            var isHtmlIntegrationPoint = annotationXmlHtmlIntegrationPoint
                || (elementNamespace == MarkupNamespace.Svg
                    && (name.Equals("foreignobject", StringComparison.OrdinalIgnoreCase)
                        || name.Equals("desc", StringComparison.OrdinalIgnoreCase)
                        || name.Equals("title", StringComparison.OrdinalIgnoreCase)));
            elements.Add(new ElementContext(name, elementNamespace, isHtmlIntegrationPoint));
        }

        private static void PopElement(List<ElementContext> elements, string name)
        {
            if (elements.Count == 0)
            {
                return;
            }

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

        private static bool IsHtmlVoidElement(string name) =>
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

        private static bool StartTagIsSelfClosing(string html, int nameEnd, int tagEnd)
        {
            if (tagEnd <= nameEnd || html[tagEnd - 1] != '/')
            {
                return false;
            }

            var state = AttributeScanState.BeforeName;
            var quote = '\0';
            for (var index = nameEnd; index < tagEnd - 1; index++)
            {
                var character = html[index];
                switch (state)
                {
                    case AttributeScanState.BeforeName:
                        if (!IsHtmlSpace(character) && character != '/')
                        {
                            state = AttributeScanState.Name;
                        }

                        break;
                    case AttributeScanState.Name:
                        if (IsHtmlSpace(character))
                        {
                            state = AttributeScanState.AfterName;
                        }
                        else if (character == '=')
                        {
                            state = AttributeScanState.BeforeValue;
                        }
                        else if (character == '/')
                        {
                            state = AttributeScanState.BeforeName;
                        }

                        break;
                    case AttributeScanState.AfterName:
                        if (IsHtmlSpace(character))
                        {
                            continue;
                        }

                        state = character == '='
                            ? AttributeScanState.BeforeValue
                            : character == '/'
                                ? AttributeScanState.BeforeName
                                : AttributeScanState.Name;
                        break;
                    case AttributeScanState.BeforeValue:
                        if (IsHtmlSpace(character))
                        {
                            continue;
                        }

                        if (character == '"' || character == '\'')
                        {
                            quote = character;
                            state = AttributeScanState.QuotedValue;
                        }
                        else
                        {
                            state = AttributeScanState.UnquotedValue;
                        }

                        break;
                    case AttributeScanState.QuotedValue:
                        if (character == quote)
                        {
                            quote = '\0';
                            state = AttributeScanState.AfterQuotedValue;
                        }

                        break;
                    case AttributeScanState.UnquotedValue:
                        if (IsHtmlSpace(character))
                        {
                            state = AttributeScanState.BeforeName;
                        }

                        break;
                    case AttributeScanState.AfterQuotedValue:
                        state = IsHtmlSpace(character) || character == '/'
                            ? AttributeScanState.BeforeName
                            : AttributeScanState.Name;
                        break;
                }
            }

            return state == AttributeScanState.BeforeName
                || state == AttributeScanState.Name
                || state == AttributeScanState.AfterName
                || state == AttributeScanState.AfterQuotedValue;
        }

        private static int FindSpecialMarkupEnd(string html, int open, bool cdataAllowed)
        {
            // In foreign content CDATA is one token whose body may contain any
            // number of '>' characters. At an HTML adjusted current node this
            // spelling is instead a bogus comment ending at the first '>'.
            if (cdataAllowed && StartsWith(html, open, "<![CDATA["))
            {
                var cdataEnd = html.IndexOf("]]>", open + 9, StringComparison.Ordinal);
                return cdataEnd < 0 ? html.Length : cdataEnd + 3;
            }

            if (StartsWithIgnoreCase(html, open, "<!doctype"))
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
                        // Correctly distinguishing a valid public/system ID from
                        // bogus-doctype recovery requires the full DOCTYPE state
                        // machine. Optional stamping fails closed instead.
                        return -1;
                    }
                }

                return doctypeClose + 1;
            }

            if (html[open + 1] == '/')
            {
                return TryFindTagEnd(html, open + 2, out var tagEnd)
                    ? tagEnd + 1
                    : html.Length;
            }

            // Processing instructions and other declarations are HTML bogus
            // comments; their first '>' ends the token regardless of quotes.
            var close = html.IndexOf('>', open + 1);
            return close < 0 ? html.Length : close + 1;
        }

        private static bool TryFindTagEnd(string html, int from, out int tagEnd)
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
                    if (tail >= html.Length || html[tail] == '>' || html[tail] == '/' || IsHtmlSpace(html[tail]))
                    {
                        return open;
                    }
                }

                index = open + 2;
            }

            return -1;
        }

        /// <summary>
        /// Finds the end of an HTML script-data element, including the legacy
        /// escaped/double-escaped states browsers still implement. In particular,
        /// inside <c>&lt;!--&lt;script&gt; ... &lt;/script&gt;</c> the first matching
        /// end-tag-shaped sequence only leaves DOUBLE-ESCAPED state; it does not
        /// close the outer element. Treating it as a close makes later JavaScript
        /// source look like HTML and can corrupt a string containing a script tag.
        /// </summary>
        private static int IndexOfScriptClosingTag(string html, int from)
        {
            var state = ScriptTextState.Normal;
            var index = from;
            while (index < html.Length)
            {
                if (state != ScriptTextState.DoubleEscaped
                    && IsTagSequence(html, index, closing: true, name: "script"))
                {
                    return index;
                }

                if (state == ScriptTextState.Normal && StartsWith(html, index, "<!--"))
                {
                    state = ScriptTextState.Escaped;
                    index += 4;
                    continue;
                }

                if (state != ScriptTextState.Normal && StartsWith(html, index, "-->"))
                {
                    // Both script-data escaped-dash-dash and double-escaped-
                    // dash-dash return to ordinary script data on `>`.
                    state = ScriptTextState.Normal;
                    index += 3;
                    continue;
                }

                if (state == ScriptTextState.Escaped
                    && IsTagSequence(html, index, closing: false, name: "script"))
                {
                    state = ScriptTextState.DoubleEscaped;
                    index += "<script".Length;
                    continue;
                }

                if (state == ScriptTextState.DoubleEscaped
                    && IsTagSequence(html, index, closing: true, name: "script"))
                {
                    // In double-escaped state this is source text, not the outer
                    // end tag; it only returns the tokenizer to escaped state.
                    state = ScriptTextState.Escaped;
                    index += "</script".Length;
                    continue;
                }

                index++;
            }

            return -1;
        }

        private static bool IsTagSequence(
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
                || IsHtmlSpace(html[tail]);
        }

        private enum ScriptTextState
        {
            Normal,
            Escaped,
            DoubleEscaped,
        }

        private enum MarkupNamespace
        {
            Html,
            Svg,
            MathMl,
        }

        private enum AttributeScanState
        {
            BeforeName,
            Name,
            AfterName,
            BeforeValue,
            QuotedValue,
            UnquotedValue,
            AfterQuotedValue,
        }

        private readonly struct ElementContext
        {
            public ElementContext(
                string name,
                MarkupNamespace elementNamespace,
                bool isHtmlIntegrationPoint)
            {
                Name = name;
                Namespace = elementNamespace;
                IsHtmlIntegrationPoint = isHtmlIntegrationPoint;
            }

            public string Name { get; }

            public MarkupNamespace Namespace { get; }

            public bool IsHtmlIntegrationPoint { get; }
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

        private static bool StartsWithIgnoreCase(string html, int index, string value)
        {
            return index + value.Length <= html.Length
                && string.Compare(
                    html,
                    index,
                    value,
                    0,
                    value.Length,
                    StringComparison.OrdinalIgnoreCase) == 0;
        }

        private static bool IsAsciiLetter(char value)
        {
            return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z');
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

            // Attribute character references are decoded before the browser's
            // URL parser runs, and special-scheme URL parsing treats backslashes
            // as slashes. Consequently each of `&#47;&#47;host/x.js`,
            // `\\\\host\\x.js`, `/\\host/x.js` and `https&#58;//host/x.js`
            // can be cross-origin even though the source text does not start
            // with `//` or a literal scheme. Never attach our query to one.
            var browserUrl = WebUtility.HtmlDecode(url);
            // A numeric reference contains a literal '#' in the SOURCE
            // (`&#35;`) that is not a fragment delimiter until the HTML parser
            // decodes it. The raw query/fragment splitter below cannot safely
            // distinguish every such reference without becoming an HTML entity
            // parser of its own, so keep the fail-safe promise and leave these
            // unusual URLs byte-for-byte alone.
            if (url.IndexOf("&#", StringComparison.Ordinal) >= 0)
            {
                return null;
            }

            // WebUtility knows the common character references, but HTML has a
            // much larger named-entity table (`&sol;`, `&colon;`, `&bsol;`, ...).
            // An entity in the path can therefore manufacture `//host`, a scheme
            // or a backslash only after the browser tokenizes the attribute. Do
            // not guess: an ampersand before query/fragment is an unusual path
            // spelling and is safer left byte-identical.
            var structuralEnd = url.Length;
            var rawQuery = url.IndexOf('?', StringComparison.Ordinal);
            var rawFragment = url.IndexOf('#', StringComparison.Ordinal);
            if (rawQuery >= 0)
            {
                structuralEnd = rawQuery;
            }

            if (rawFragment >= 0 && rawFragment < structuralEnd)
            {
                structuralEnd = rawFragment;
            }

            if (url.IndexOf('&', 0, structuralEnd) >= 0)
            {
                return null;
            }

            // WHATWG URL parsing strips ASCII tab/newline/CR anywhere and
            // leading/trailing C0 controls before deciding whether text is a
            // scheme. Check the browser-visible spelling, not just the source:
            // `ht\ttps://host/x.js` and `\u0001https://host/x.js` are external.
            var originCheckUrl = NormalizeForOriginCheck(browserUrl);
            if (originCheckUrl.StartsWith("//", StringComparison.Ordinal)
                || originCheckUrl.IndexOf('\\') >= 0
                || SchemeRegex.IsMatch(originCheckUrl))
            {
                return null;
            }

            // If a fragment delimiter exists only as a character reference,
            // appending to the raw source would put `?rkv=` *after* the decoded
            // `#`; the browser would then keep it in the fragment and never send
            // it. Leave the URL alone rather than claim to have versioned it.
            if (url.IndexOf('#', StringComparison.Ordinal) < 0
                && browserUrl.IndexOf('#', StringComparison.Ordinal) >= 0)
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

        private static string NormalizeForOriginCheck(string value)
        {
            var builder = new StringBuilder(value.Length);
            foreach (var character in value)
            {
                if (character != '\t' && character != '\n' && character != '\r')
                {
                    builder.Append(character);
                }
            }

            var start = 0;
            var end = builder.Length;
            while (start < end && builder[start] <= '\u0020')
            {
                start++;
            }

            while (end > start && builder[end - 1] <= '\u0020')
            {
                end--;
            }

            return builder.ToString(start, end - start);
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

            if (ContainsUnsupportedNamedCharacterReference(query))
            {
                // `WebUtility.HtmlDecode` does not implement HTML5's complete
                // named-reference table. In particular, source `&num;` becomes
                // `#` in the browser; appending rkv after it would put the stamp
                // in a fragment and falsely claim the request was versioned.
                return false;
            }

            var kept = new List<QuerySegment>();
            foreach (var part in SplitHtmlQuery(query))
            {
                var segment = part.Value;
                if (segment.Length == 0)
                {
                    // Empty segments are meaningful source spelling (`?&a=1`,
                    // `a=1&&b=2`, or a trailing `&`). Keep them so adding our
                    // parameter is the only byte-level normalization we make.
                    kept.Add(part);
                    continue;
                }

                var equals = segment.IndexOf('=', StringComparison.Ordinal);
                if (equals < 0)
                {
                    // "?3cf5acc8506265662d4f" — an opaque token whose meaning we
                    // cannot know. Assume it is already an identity.
                    return false;
                }

                var key = DecodeQueryKey(segment.Substring(0, equals));
                if (key == null)
                {
                    return false;
                }

                if (string.Equals(key, StampParameter, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                if (VersionishKeys.Contains(key))
                {
                    return false;
                }

                kept.Add(part);
            }

            var builder = new StringBuilder();
            for (var index = 0; index < kept.Count; index++)
            {
                if (index > 0)
                {
                    builder.Append(kept[index].Separator.Length > 0 ? kept[index].Separator : "&");
                }

                builder.Append(kept[index].Value);
            }

            scrubbed = builder.ToString();
            return true;
        }

        private static bool ContainsUnsupportedNamedCharacterReference(string query)
        {
            var index = 0;
            while ((index = query.IndexOf('&', index)) >= 0)
            {
                if (index + 5 <= query.Length
                    && query.Substring(index, 5).Equals("&amp;", StringComparison.OrdinalIgnoreCase))
                {
                    // This is the one named reference SplitHtmlQuery understands
                    // structurally. HTML character references are decoded once,
                    // so text exposed by it must not be decoded recursively.
                    index += 5;
                    continue;
                }

                var nameEnd = index + 1;
                while (nameEnd < query.Length
                    && ((query[nameEnd] >= 'a' && query[nameEnd] <= 'z')
                        || (query[nameEnd] >= 'A' && query[nameEnd] <= 'Z')
                        || (query[nameEnd] >= '0' && query[nameEnd] <= '9')))
                {
                    nameEnd++;
                }

                if (nameEnd > index + 1
                    && nameEnd < query.Length
                    && query[nameEnd] == ';')
                {
                    return true;
                }

                index++;
            }

            return false;
        }

        private static string? DecodeQueryKey(string source)
        {
            try
            {
                // HTML character references are resolved while parsing the
                // attribute; percent escapes are resolved by ordinary query
                // consumers. Recognise the key the destination sees so `%76`
                // is not treated as different from `v`, and an encoded old rkv
                // cannot survive beside the one we append.
                return Uri.UnescapeDataString(WebUtility.HtmlDecode(source));
            }
            catch
            {
                // Malformed escapes belong to somebody else. Leaving the URL
                // untouched is safer than guessing its cache semantics.
                return null;
            }
        }

        /// <summary>
        /// Splits a query as the HTML tokenizer plus URL parser will see it,
        /// while retaining the source spelling of separators so unrelated
        /// markup stays byte-identical. A raw ampersand or source
        /// <c>&amp;amp;</c> both become one query delimiter after the HTML parser's
        /// single character-reference pass; entities are not recursively
        /// decoded.
        /// Numeric references are rejected earlier by <see cref="StampUrl"/>
        /// because their literal <c>#</c> is structurally ambiguous here.
        /// </summary>
        private static List<QuerySegment> SplitHtmlQuery(string query)
        {
            var parts = new List<QuerySegment>();
            var separator = string.Empty;
            var start = 0;
            var index = 0;
            while (index < query.Length)
            {
                if (query[index] != '&')
                {
                    index++;
                    continue;
                }

                parts.Add(new QuerySegment(separator, query.Substring(start, index - start)));
                if (index + 5 <= query.Length
                    && query.Substring(index, 5).Equals("&amp;", StringComparison.OrdinalIgnoreCase))
                {
                    separator = query.Substring(index, 5);
                    index += 5;
                }
                else
                {
                    separator = "&";
                    index++;
                }

                start = index;
            }

            parts.Add(new QuerySegment(separator, query.Substring(start)));
            return parts;
        }

        private readonly struct QuerySegment
        {
            public QuerySegment(string separator, string value)
            {
                Separator = separator;
                Value = value;
            }

            public string Separator { get; }

            public string Value { get; }
        }
    }
}
