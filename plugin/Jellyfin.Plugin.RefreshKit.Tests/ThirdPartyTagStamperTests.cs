using System;
using System.Text;
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

        [Theory]
        [InlineData("<!-->")]
        [InlineData("<!--->")]
        [InlineData("<!-- recovered --!>")]
        public void HtmlCommentRecoveryClose_ExposesFollowingRealMarkup(string comment)
        {
            var html = comment + "<script src=\"/real.js\"></script>";

            Assert.Equal(
                comment + "<script src=\"/real.js?rkv=" + Generation + "\"></script>",
                Stamp(html));
        }

        [Fact]
        public void NestedAbruptCommentClose_ExposesFollowingRealMarkup()
        {
            const string Comment = "<!--outer<!-->";
            const string Html = Comment + "<script src=\"/real.js\"></script>";

            Assert.Equal(
                Comment + "<script src=\"/real.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
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

        [Theory]
        [InlineData("alternate stylesheet")]
        [InlineData("preload\tStyleSheet")]
        [InlineData("stylesheet\nnext")]
        [InlineData("stylesheet\fnext")]
        public void StylesheetRelToken_IsMatchedUsingHtmlTokenSemantics(string rel)
        {
            var html = "<link rel=\"" + rel + "\" href=\"/web/x.css\">";

            Assert.Contains("/web/x.css?rkv=" + Generation, Stamp(html), StringComparison.Ordinal);
        }

        [Theory]
        [InlineData("notstylesheet")]
        [InlineData("stylesheetish")]
        [InlineData("x-stylesheet")]
        [InlineData("style sheet")]
        public void StylesheetSubstring_IsNotARelToken(string rel)
        {
            var html = "<link rel=\"" + rel + "\" href=\"/web/x.css\">";

            Assert.Equal(html, Stamp(html));
        }

        [Fact]
        public void EncodedStylesheetRelToken_IsStamped()
        {
            const string Html = "<link rel=\"style&#x73;heet\" href=\"/web/x.css\">";

            Assert.Equal(
                "<link rel=\"style&#x73;heet\" href=\"/web/x.css?rkv=" + Generation + "\">",
                Stamp(Html));
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
        [InlineData("\\\\cdn.example.com\\a.js")]
        [InlineData("/\\cdn.example.com/a.js")]
        [InlineData("\\/cdn.example.com/a.js")]
        [InlineData("&#47;&#47;cdn.example.com/a.js")]
        [InlineData("https&#58;//cdn.example.com/a.js")]
        [InlineData("&sol;&sol;cdn.example.com/a.js")]
        [InlineData("https&colon;//cdn.example.com/a.js")]
        [InlineData("ht\ttps://cdn.example.com/a.js")]
        [InlineData("\u0001https://cdn.example.com/a.js")]
        public void BrowserNormalizedCrossOriginUrls_AreNotStamped(string url)
        {
            var html = "<script src=\"" + url + "\"></script>";

            Assert.Equal(html, Stamp(html));
        }

        [Fact]
        public void EncodedFragmentMarker_IsLeftAloneRatherThanAppendingAnUnsentQuery()
        {
            const string Html = "<script src=\"/a.js&#35;fragment\"></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void Html5NamedFragmentReferenceInQuery_IsLeftByteIdentical()
        {
            // `&num;` is HTML5's named reference for '#', but WebUtility's
            // smaller entity table leaves it encoded. Appending after it would
            // put rkv in the browser's decoded fragment, not in the request.
            const string Html =
                "<script src=\"/a.js?mode=dark&num;fragment=value\"></script>";

            Assert.Same(Html, Stamp(Html));
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

        [Theory]
        [InlineData("/web/main.jellyfin.f725276386e5b19afe0c.css")]
        [InlineData("/web/main.a1b2c3d4.chunk.js")]
        [InlineData("/web/main-a1b2c3d4.js")]
        [InlineData("/web/a1b2c3d4.js")]
        public void ContentHashedFilename_IsNotStamped(string url)
        {
            var html = url.EndsWith(".css", StringComparison.Ordinal)
                ? "<link rel=\"stylesheet\" href=\"" + url + "\">"
                : "<script src=\"" + url + "\"></script>";

            Assert.Equal(html, Stamp(html));
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
        public void HtmlEncodedVersionParameter_IsRespected()
        {
            const string Html = "<script src=\"/a.js?mode=dark&amp;v=2\"></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void PercentEncodedVersionParameter_IsRespected()
        {
            const string Html = "<script src=\"/a.js?mode=dark&%76=2\"></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void HtmlEncodedRkvSeparator_IsScrubbedWithoutDuplication()
        {
            var result = Stamp("<script src=\"/a.js?mode=dark&amp;rkv=old\"></script>");

            Assert.Equal(
                "<script src=\"/a.js?mode=dark&rkv=" + Generation + "\"></script>",
                result);
            Assert.Equal(1, CountOccurrences(result, "rkv="));
        }

        [Fact]
        public void PercentEncodedRkvKey_IsScrubbedWithoutSemanticDuplication()
        {
            var result = Stamp("<script src=\"/a.js?%72%6b%76=old\"></script>");

            Assert.Equal("<script src=\"/a.js?rkv=" + Generation + "\"></script>", result);
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

        [Theory]
        [InlineData("?&mode=dark", "?&mode=dark&rkv=")]
        [InlineData("?mode=dark&&theme=blue", "?mode=dark&&theme=blue&rkv=")]
        [InlineData("?mode=dark&", "?mode=dark&&rkv=")]
        [InlineData("?mode=dark&amp;theme=blue", "?mode=dark&amp;theme=blue&rkv=")]
        public void UnrelatedQuerySeparatorSpelling_IsPreserved(string query, string expectedPrefix)
        {
            var result = Stamp("<script src=\"/a.js" + query + "\"></script>");

            Assert.Equal(
                "<script src=\"/a.js" + expectedPrefix + Generation + "\"></script>",
                result);
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
        public void SvgTitleIntegrationPoint_ExposesRealHtmlScript()
        {
            const string Html =
                "<svg><title><script src=\"/inside-title.js\"></script></title></svg>";

            Assert.Equal(
                "<svg><title><script src=\"/inside-title.js?rkv=" + Generation
                + "\"></script></title></svg>",
                Stamp(Html));
        }

        [Theory]
        [InlineData("<math><mtext>", "</mtext></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text/html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"application/xhtml+xml\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&#x2f;html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&#47html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&sol;html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"application/xhtml&plus;xml\">",
            "</annotation-xml></math>")]
        public void MathMlHtmlIntegrationPoint_ExposesRealHtmlScript(
            string prefix,
            string suffix)
        {
            var html = prefix + "<script src=\"/inside-math.js\"></script>" + suffix;

            Assert.Equal(
                prefix + "<script src=\"/inside-math.js?rkv=" + Generation
                + "\"></script>" + suffix,
                Stamp(html));
        }

        [Fact]
        public void MathMlTextIntegrationPoint_LeavesMglyphChildInForeignContent()
        {
            const string Html = "<math><mtext><mglyph>"
                + "<script src=\"/foreign.js\"></script>"
                + "</mglyph></mtext></math>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void AnnotationXmlEncoding_DoesNotDecodeCharacterReferencesTwice()
        {
            const string Html = "<math><annotation-xml encoding=\"text&amp;sol;html\">"
                + "<script src=\"/foreign.js\"></script>"
                + "</annotation-xml></math>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void NoscriptDisablesTheWholePassBecauseUaScriptingStateIsUnknown()
        {
            const string Html = "<script src=\"/before.js\"></script>"
                + "<noscript><base href=\"https://cdn.example.invalid/root/\"></noscript>"
                + "<script src=\"/after.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void OrdinaryHtmlTitle_KeepsTagShapedTextInert()
        {
            const string Html =
                "<title><script src=\"/title-text.js\"></script></title>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void OrdinarySvgScriptSource_IsNotTreatedAsHtmlMarkup()
        {
            const string Html =
                "<svg><script>const x = '<script src=\"/source.js\">';</script></svg>";

            Assert.Same(Html, Stamp(Html));
        }

        [Theory]
        [InlineData("<script>", "</script>")]
        [InlineData("<script/>", "</script>")]
        [InlineData("<style>", "</style>")]
        public void ForeignScriptAndStyleContentsCanBreakOutToAnEffectiveBase(
            string foreignElement,
            string trailingClose)
        {
            var html = "<svg>" + foreignElement
                + "<div><base href=\"https://cdn.example.invalid/root/\"></div>"
                + trailingClose + "</svg><script src=\"plugin.js\"></script>";

            Assert.Same(html, Stamp(html));
        }

        [Theory]
        [InlineData("https://cdn.example.invalid/path/")]
        [InlineData("//cdn.example.invalid/path/")]
        [InlineData("data:text/html,")]
        [InlineData("https&colon;//cdn.example.invalid/path/")]
        public void UnsafeEffectiveBase_DisablesTheEntireStampingPass(string baseHref)
        {
            var html = "<script src=\"before.js\"></script><base href=\""
                + baseHref
                + "\"><script src=\"plugin.js\"></script>";

            Assert.Same(html, Stamp(html));
        }

        [Theory]
        [InlineData("<math><mtext>", "</mtext></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text/html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"application/xhtml+xml\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&#x2f;html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&#47html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text&sol;html\">",
            "</annotation-xml></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"application/xhtml&plus;xml\">",
            "</annotation-xml></math>")]
        public void UnsafeBaseInsideMathMlHtmlIntegrationPointDisablesStamping(
            string prefix,
            string suffix)
        {
            var html = prefix
                + "<base href=\"https://cdn.example.invalid/root/\">"
                + suffix
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(html, Stamp(html));
        }

        [Theory]
        [InlineData("<math>", "</math>")]
        [InlineData("<svg>", "</svg>")]
        public void ForeignContentHtmlBreakoutExposesEffectiveBase(
            string foreignOpen,
            string foreignClose)
        {
            var html = foreignOpen
                + "<div><base href=\"https://cdn.example.invalid/root/\"></div>"
                + foreignClose
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(html, Stamp(html));
        }

        [Fact]
        public void ForeignContentFontAttributeBreakoutExposesEffectiveBase()
        {
            const string Html = "<svg><font color=red>"
                + "<base href=\"https://cdn.example.invalid/root/\">"
                + "</font></svg><script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Theory]
        [InlineData("p")]
        [InlineData("br")]
        public void ForeignContentSpecialEndTagExposesEffectiveBase(string endTag)
        {
            var html = "<math></" + endTag + ">"
                + "<base href=\"https://cdn.example.invalid/root/\"></math>"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(html, Stamp(html));
        }

        [Fact]
        public void ForeignEndTagThatMayTargetAnHtmlAncestorFailsClosed()
        {
            const string Html = "<div><svg><g></div>"
                + "<base href=\"https://cdn.example.invalid/root/\"></svg>"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void InvalidEndTagOpenerUsesBogusCommentCloseBeforeEffectiveBase()
        {
            const string Html = "</!x \"><base href=\"https://cdn.example.invalid/root/\">\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void HtmlCdataSpellingIsABogusCommentAndExposesEffectiveBase()
        {
            const string Html = "<![CDATA[>"
                + "<base href=\"https://cdn.example.invalid/root/\">]]>"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void AmbiguousQuotedDoctypeDisablesTheStampingPass()
        {
            const string Html = "<!DOCTYPE foo \">"
                + "<base id=b href=\"https://cdn.example.invalid/root/\">\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void ForeignCdataKeepsBaseTextInert()
        {
            const string Html = "<svg><![CDATA[>"
                + "<base href=\"https://cdn.example.invalid/root/\">]]></svg>"
                + "<script src=\"plugin.js\"></script>";

            Assert.Equal(
                "<svg><![CDATA[><base href=\"https://cdn.example.invalid/root/\">]]></svg>"
                + "<script src=\"plugin.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
        }

        [Theory]
        [InlineData("<svg><title>", "</title></svg>")]
        [InlineData("<math><mtext>", "</mtext></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text/html\">",
            "</annotation-xml></math>")]
        public void CdataSpellingAtHtmlIntegrationPointExposesRealMarkup(
            string prefix,
            string suffix)
        {
            var html = prefix + "<![CDATA[><script src=\"/real.js\"></script>]]>" + suffix;

            Assert.Equal(
                prefix + "<![CDATA[><script src=\"/real.js?rkv=" + Generation
                + "\"></script>]]>" + suffix,
                Stamp(html));
        }

        [Fact]
        public void AnyUnsafeBaseCandidateDisablesStampingDespiteSourceOrder()
        {
            const string Html = "<base href=\"assets/\">"
                + "<base href=\"https://cdn.example.invalid/path/\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void FosterParentedUnsafeBaseCannotHideBehindEarlierSourceCandidate()
        {
            const string Html = "<table><tr><td><base href=\"/safe/\"></td></tr>"
                + "<base href=\"https://cdn.example.invalid/live/\"></table>"
                + "<script src=\"asset.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void BaseInsideTemplateDoesNotPreemptTheDocumentEffectiveBase()
        {
            const string Html = "<template><base href=\"/inert-template-base/\"></template>"
                + "<base href=\"https://cdn.example.invalid/root/\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void MismatchedEndTagCannotPopAcrossTemplateAndHideEffectiveBase()
        {
            const string Html = "<div><template></div>"
                + "<base href=\"/inert-template-base/\"></template>"
                + "<base href=\"https://cdn.example.invalid/root/\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void BaseWithoutHref_DoesNotPreemptTheFirstEffectiveBase()
        {
            const string Html = "<base target=\"_blank\">"
                + "<base href=\"https://cdn.example.invalid/path/\">"
                + "<script src=\"plugin.js\"></script>";

            Assert.Same(Html, Stamp(Html));
        }

        [Fact]
        public void BaseShapedTextInsideOrdinaryHtmlTitle_IsNotEffective()
        {
            const string Html = "<title><base href=\"https://cdn.example.invalid/\"></title>"
                + "<script src=\"plugin.js\"></script>";

            Assert.Equal(
                "<title><base href=\"https://cdn.example.invalid/\"></title>"
                + "<script src=\"plugin.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
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
        public void ScriptNameWithPunctuation_DoesNotHideNestedRealMarkup()
        {
            // HTML's tag-name state accepts punctuation. Chromium therefore
            // parses script.foo as an ordinary element, not a raw-text script.
            const string Html =
                "<script.foo><script src=\"/real.js\"></script></script.foo>";

            Assert.Equal(
                "<script.foo><script src=\"/real.js?rkv=" + Generation
                + "\"></script></script.foo>",
                Stamp(Html));
        }

        [Fact]
        public void CloseTagWithMatchingPrefix_DoesNotEndTheRawTextElement()
        {
            const string Html = "<script>var a = \"</scriptfoo>\"; var b = \"<link rel=stylesheet href=/x.css>\";</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void NonHtmlWhitespace_DoesNotEndARawTextElement()
        {
            const string Html = "<script>var a = '</script\u00a0><script src=\"/inside.js\">';</script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void ScriptDoubleEscapedEndTag_DoesNotExposeSourceAsMarkup()
        {
            // Chromium parses this as ONE script. Each inner </script> only
            // leaves the double-escaped state opened by its preceding <script>;
            // the final </script> closes the outer element.
            const string Html = "<script><!--<script></script>"
                + "<script src=\"/inside-source.js\"></script></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void ScriptEscapedStateCanCloseAndMarkupAfterItIsStamped()
        {
            const string Html = "<script><!-- legacy wrapper --></script>"
                + "<script src=\"/outside.js\"></script>";

            Assert.Equal(
                "<script><!-- legacy wrapper --></script>"
                + "<script src=\"/outside.js?rkv=" + Generation + "\"></script>",
                Stamp(Html));
        }

        [Fact]
        public void GreaterThanInsideEndTagAttribute_DoesNotExposeSourceAsMarkup()
        {
            // Chromium treats the quoted text as an attribute on the script end
            // tag; neither apparent tag inside it is a DOM element or request.
            const string Html = "<script>window.ready = true;</script data-x=\"> "
                + "<script src='/decoy.js'>\"><script src=\"/real.js\"></script>";

            Assert.Equal(
                "<script>window.ready = true;</script data-x=\"> "
                + "<script src='/decoy.js'>\"><script src=\"/real.js?rkv="
                + Generation + "\"></script>",
                Stamp(Html));
        }

        [Theory]
        [InlineData("xmp")]
        [InlineData("iframe")]
        [InlineData("noembed")]
        [InlineData("noframes")]
        [InlineData("noscript")]
        public void LegacyRawTextElementContents_AreNotTreatedAsMarkup(string element)
        {
            var html = "<" + element + "><script src=\"/inside.js\"></script></" + element + ">";

            Assert.Equal(html, Stamp(html));
        }

        [Fact]
        public void PlaintextConsumesTheRestOfTheDocument()
        {
            const string Html = "<plaintext><script src=\"/inside.js\"></script></plaintext>"
                + "<script src=\"/still-text.js\"></script>";

            Assert.Equal(Html, Stamp(Html));
        }

        [Fact]
        public void NonHtmlWhitespace_DoesNotSeparateAttributes()
        {
            const string Html = "<script data-x=one\u00a0src=/not-an-attribute.js></script>";

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

        [Fact]
        public void PluginPostProcessor_UsesTheExactGenerationAlreadyInTheInjectedTags()
        {
            const string ExactGeneration = "g-one&boundary";
            var currentTags = RefreshKit.BuildScriptTags(
                PluginServiceRegistrator.PluginName,
                PluginServiceRegistrator.BasePath,
                new[] { "kit.js" },
                ExactGeneration,
                devMode: false,
                buildId: "build",
                extraAttributes: _ => "data-third-party-stamping=\"true\"");
            var html = "<html><body><script src=\"/third-party.js\"></script>"
                + currentTags
                + "</body></html>";

            var result = PluginServiceRegistrator.StampThirdPartyTags(html, currentTags);

            Assert.Contains(
                "/third-party.js?rkv=g-one%26boundary",
                result,
                StringComparison.Ordinal);
            Assert.DoesNotContain("?rkv=" + Generation, result, StringComparison.Ordinal);
            Assert.Equal(1, CountOccurrences(result, "rkv="));
        }

        [Fact]
        public void PluginPostProcessor_FailsOpenWhenTheExactTagIdentityIsUnavailable()
        {
            const string Html = "<script src=\"/third-party.js\"></script>";

            Assert.Equal(
                Html,
                PluginServiceRegistrator.StampThirdPartyTags(
                    Html,
                    "<script src=\"/RefreshKit/kit.js\"></script>"));
        }

        [Fact]
        public void PluginPostProcessor_UsesTheStampingFlagFromTheExactInjectedTags()
        {
            const string Html = "<script src=\"/third-party.js\"></script>";
            var enabledTags = RefreshKit.BuildScriptTags(
                PluginServiceRegistrator.PluginName,
                PluginServiceRegistrator.BasePath,
                new[] { "kit.js" },
                Generation,
                devMode: false,
                buildId: "build",
                extraAttributes: _ => "data-third-party-stamping=\"true\"");
            var disabledTags = RefreshKit.BuildScriptTags(
                PluginServiceRegistrator.PluginName,
                PluginServiceRegistrator.BasePath,
                new[] { "kit.js" },
                Generation,
                devMode: false,
                buildId: "build",
                extraAttributes: _ => "data-third-party-stamping=\"false\"");

            Assert.Contains(
                "?rkv=" + Generation,
                PluginServiceRegistrator.StampThirdPartyTags(Html, enabledTags),
                StringComparison.Ordinal);
            Assert.Equal(
                Html,
                PluginServiceRegistrator.StampThirdPartyTags(Html, disabledTags));
        }

        [Fact]
        public void ReleasedOneArgumentPostProcessorSurfaceRemainsUsable()
        {
            var options = new RefreshKitOptions
            {
                HtmlPostProcess = html => html + "<!-- legacy -->",
            };

            Assert.Equal(
                "<html></html><!-- legacy -->",
                options.ApplyHtmlPostProcess("<html></html>", "<script></script>"));
        }

        [Fact]
        public void DeterministicMalformedMarkupFuzz_RemainsIdempotent()
        {
            // A compact grammar fuzzer catches state-machine interactions that
            // hand-picked examples tend to miss: broken quoting, raw-text exits,
            // comments, entities, duplicate attributes and arbitrary truncation.
            // The seed is fixed so any failure is immediately reproducible.
            var random = new Random(0x524b);
            var tokens = new[]
            {
                "<", ">", "</", "/>", " ", "\t", "\n", "\u00a0", "\"", "'", "=", "&amp;",
                "&#35;", "?", "#", "src", "href", "rel", "stylesheet", "script", "link",
                "style", "textarea", "title", "xmp", "plaintext", "data-x", "/a.js", "/a.css",
                "?mode=x", "&rkv=old", "&amp;v=2", "<!--", "-->", "<!doctype html>", "abc",
            };

            for (var iteration = 0; iteration < 1500; iteration++)
            {
                var builder = new StringBuilder();
                var tokenCount = random.Next(1, 48);
                for (var token = 0; token < tokenCount; token++)
                {
                    builder.Append(tokens[random.Next(tokens.Length)]);
                }

                var html = builder.ToString();
                var once = Stamp(html);
                var twice = Stamp(once);
                Assert.True(
                    string.Equals(once, twice, StringComparison.Ordinal),
                    "Stamper was not idempotent at deterministic fuzz iteration "
                    + iteration + ": " + html);
            }
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
