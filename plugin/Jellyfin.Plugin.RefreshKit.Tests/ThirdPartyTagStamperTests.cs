using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// The stamper's contract, with the three regex-era corruption cases pinned
    /// as regressions. Each of the REGRESSION tests failed against the previous
    /// <c>&lt;(script|link)\b[^&gt;]*&gt;</c> implementation.
    /// </summary>
    public class ThirdPartyTagStamperTests
    {
        private const string Generation = "5p-9f2a1c0b7d3e5a64";
        private const string OwnMarker = "plugin=\"Jellyfin Refresh Kit\"";

        private static string Stamp(string html) =>
            ThirdPartyTagStamper.Stamp(html, Generation, OwnMarker);

        // ---------------------------------------------------------------
        // REGRESSION 1 — inline script bodies are source code, not markup.
        // ---------------------------------------------------------------

        [Fact]
        public void InlineScriptBodyContainingTagLikeText_IsNotTouched()
        {
            // The old regex rewrote the string literal INSIDE this script,
            // corrupting the JavaScript it promised never to touch.
            const string Html = "<script>\n  var a = \"<script src='/evil.js'>\";\n</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void InlineScriptWritingALinkTag_IsNotTouched()
        {
            const string Html =
                "<script>document.write('<link rel=\"stylesheet\" href=\"/x.css\">');</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void ScriptBodyWithComparisonOperators_IsNotTouched()
        {
            const string Html = "<script>if (a < b && c > d) { load(\"/x.js\"); }</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void StyleBodyContainingTagLikeText_IsNotTouched()
        {
            const string Html = "<style>/* <script src=\"/a.js\"> */ body{color:red}</style>";

            Assert.Equal(Html, Stamp(Html));
        }

        // ---------------------------------------------------------------
        // REGRESSION 2 — a '>' inside a quoted value does not end the tag.
        // ---------------------------------------------------------------

        [Fact]
        public void QuotedAngleBracketInAttribute_StillStampsTheRealSrc()
        {
            // The old regex's [^>]* stopped at the '>' inside data-json, so the
            // tag it saw had no src at all and nothing was stamped.
            const string Html = "<script data-json=\"a>b\" src=\"/real.js\"></script>";

            Assert.Equal(
                "<script data-json=\"a>b\" src=\"/real.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
        }

        // ---------------------------------------------------------------
        // REGRESSION 3 — 'src' inside another attribute's value is not an
        // attribute.
        // ---------------------------------------------------------------

        [Fact]
        public void SrcInsideAnotherAttributesValue_IsIgnoredAndRealSrcIsStamped()
        {
            // The old attribute regex matched the decoy inside data-template
            // (stamping it) and never reached the real src.
            const string Html = "<script data-template=\" src='/fake.js'\" src=\"/real.js\"></script>";

            Assert.Equal(
                "<script data-template=\" src='/fake.js'\" src=\"/real.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
        }

        // ---------------------------------------------------------------
        // Comments are not markup either.
        // ---------------------------------------------------------------

        [Fact]
        public void CommentedOutScriptTag_IsNotTouched()
        {
            const string Html = "<!-- <script src=\"/commented.js\"></script> -->";

            Assert.Equal(Html, Stamp(Html));
        }

        // ---------------------------------------------------------------
        // The behaviour that must survive all of the above.
        // ---------------------------------------------------------------

        [Fact]
        public void PlainScriptTag_IsStamped()
        {
            Assert.Equal(
                "<script src=\"/web/mediabar/mediabar.js?rkv=" + Generation + "\"></script>",
                Stamp("<script src=\"/web/mediabar/mediabar.js\"></script>"));
        }

        [Fact]
        public void Stylesheet_IsStamped()
        {
            Assert.Equal(
                "<link rel=\"stylesheet\" href=\"/web/x.css?rkv=" + Generation + "\">",
                Stamp("<link rel=\"stylesheet\" href=\"/web/x.css\">"));
        }

        [Fact]
        public void NonStylesheetLink_IsNotStamped()
        {
            const string Html = "<link rel=\"icon\" href=\"/web/favicon.ico\">";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void InlineScriptWithoutSrc_IsNotStamped()
        {
            const string Html = "<script>console.log(1);</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void OwnTagMarker_IsSkipped()
        {
            const string Html = "<script plugin=\"Jellyfin Refresh Kit\" src=\"/RefreshKit/kit.js\"></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Theory]
        [InlineData("https://cdn.example.com/a.js")]
        [InlineData("//cdn.example.com/a.js")]
        [InlineData("data:text/javascript,void%200")]
        public void CrossOriginAndSchemeUrls_AreNotStamped(string url)
        {
            var html = "<script src=\"" + url + "\"></script>";

            Assert.Equal(html, Stamp(html));
        }

        [Theory]
        [InlineData("/a.js?v=2")]
        [InlineData("/a.js?version=2")]
        [InlineData("/a.js?hash=abc")]
        [InlineData("/a.js?3cf5acc8506265662d4f")]
        public void AlreadyVersionedUrls_AreNotStamped(string url)
        {
            var html = "<script src=\"" + url + "\"></script>";

            Assert.Equal(html, Stamp(html));
        }

        [Fact]
        public void ContentHashedFilename_IsNotStamped()
        {
            const string Html = "<link rel=\"stylesheet\" href=\"/web/main.jellyfin.f725276386e5b19afe0c.css\">";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void ExistingRkvParameter_IsScrubbedNotAccumulated()
        {
            var once = Stamp("<script src=\"/a.js\"></script>");
            var twice = Stamp(once);

            Assert.Equal(once, twice);
            Assert.Equal("<script src=\"/a.js?rkv=" + Generation + "\"></script>", twice);
        }

        [Fact]
        public void StaleRkvParameter_IsReplacedWithTheCurrentGeneration()
        {
            var result = Stamp("<script src=\"/a.js?rkv=1p-oldoldoldoldold\"></script>");

            Assert.Equal("<script src=\"/a.js?rkv=" + Generation + "\"></script>", result);
        }

        [Fact]
        public void UnrelatedQueryParameters_ArePreserved()
        {
            var result = Stamp("<script src=\"/a.js?mode=dark\"></script>");

            Assert.Equal("<script src=\"/a.js?mode=dark&rkv=" + Generation + "\"></script>", result);
        }

        [Fact]
        public void Fragment_StaysAfterTheQuery()
        {
            var result = Stamp("<script src=\"/a.js#frag\"></script>");

            Assert.Equal("<script src=\"/a.js?rkv=" + Generation + "#frag\"></script>", result);
        }

        [Fact]
        public void SingleQuotedAndUnquotedValues_AreStampedInPlace()
        {
            Assert.Equal(
                "<script src='/a.js?rkv=" + Generation + "'></script>",
                Stamp("<script src='/a.js'></script>"));

            Assert.Equal(
                "<script src=/a.js?rkv=" + Generation + "></script>",
                Stamp("<script src=/a.js></script>"));
        }

        [Fact]
        public void AttributeOrderQuotingAndWhitespace_ArePreserved()
        {
            var result = Stamp("<script   defer\n  data-x='1'   src = \"/a.js\"  async></script>");

            Assert.Equal(
                "<script   defer\n  data-x='1'   src = \"/a.js?rkv=" + Generation + "\"  async></script>",
                result);
        }

        [Fact]
        public void DocumentWithNothingToStamp_IsReturnedUnchanged()
        {
            const string Html = "<!doctype html><html><head><title>a > b</title></head><body>x < y</body></html>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void UnterminatedTag_LeavesTheDocumentAlone()
        {
            const string Html = "<script src=\"/a.js";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void UnclosedScriptElement_DoesNotStampWhatFollows()
        {
            // No </script>: everything after is script body as far as a browser
            // is concerned, so nothing in it may be rewritten.
            const string Html = "<script>var a = 1; <script src=\"/b.js\">";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void CloseTagWithMatchingPrefix_DoesNotEndTheRawTextElement()
        {
            const string Html = "<script>var a = \"</scriptfoo>\"; var b = \"<link rel=stylesheet href=/x.css>\";</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void RealisticShell_StampsOnlyTheThirdPartyTags()
        {
            const string Html =
                "<!doctype html><html><head>\n"
                + "<link rel=\"stylesheet\" href=\"/web/main.jellyfin.f725276386e5b19afe0c.css\">\n"
                + "<link rel=\"manifest\" href=\"/web/manifest.json\">\n"
                + "<script>window.x = \"<script src='/nope.js'>\";</script>\n"
                + "<script src=\"/web/mediabar/mediabar.js\"></script>\n"
                + "<!-- <script src=\"/dead.js\"></script> -->\n"
                + "</head><body></body></html>";

            var result = Stamp(Html);

            Assert.Contains("/web/mediabar/mediabar.js?rkv=" + Generation, result);
            Assert.Contains("window.x = \"<script src='/nope.js'>\";", result);
            Assert.Contains("<script src=\"/dead.js\"></script> -->", result);
            Assert.Contains("href=\"/web/manifest.json\"", result);
            Assert.Contains("main.jellyfin.f725276386e5b19afe0c.css\"", result);
            // Exactly one stamp in the whole document.
            Assert.Equal(1, CountOccurrences(result, "rkv="));
        }

        [Fact]
        public void EmptyGenerationOrHtml_IsANoOp()
        {
            Assert.Equal("<script src=\"/a.js\"></script>",
                ThirdPartyTagStamper.Stamp("<script src=\"/a.js\"></script>", "  ", OwnMarker));
            Assert.Equal(string.Empty, ThirdPartyTagStamper.Stamp(string.Empty, Generation, OwnMarker));
        }

        private static int CountOccurrences(string haystack, string needle)
        {
            var count = 0;
            var index = 0;
            while ((index = haystack.IndexOf(needle, index, System.StringComparison.Ordinal)) >= 0)
            {
                count++;
                index += needle.Length;
            }

            return count;
        }
    }
}
