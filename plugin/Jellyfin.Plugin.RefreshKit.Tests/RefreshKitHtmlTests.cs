using System;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Jellyfin.Plugin.RefreshKit.Controllers;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    [CollectionDefinition(RefreshKitStaticOptionsCollection.Name)]
    public sealed class RefreshKitStaticOptionsCollection
    {
        public const string Name = "RefreshKit static options";
    }

    [Collection(RefreshKitStaticOptionsCollection.Name)]
    public class RefreshKitHtmlTests
    {
        private const string PluginName = "Kit & Co";
        private const string CurrentTag =
            "<script plugin=\"Kit &amp; Co\" src=\"/kit.js?v=new\" defer></script>";

        public class NullInterfaceProxy : DispatchProxy
        {
            protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
            {
                if (targetMethod == null || targetMethod.ReturnType == typeof(void))
                {
                    return null;
                }

                return targetMethod.ReturnType.IsValueType
                    ? Activator.CreateInstance(targetMethod.ReturnType)
                    : null;
            }
        }

        [Fact]
        public void BuildIdentity_ComesFromTheLoadedModule()
        {
            var assembly = typeof(RefreshKit).Assembly;
            var material = string.Concat(
                assembly.FullName ?? assembly.GetName().Name ?? string.Empty,
                "\n",
                assembly.ManifestModule.ModuleVersionId.ToString("N"));
            var expected = Convert.ToHexString(
                    SHA256.HashData(Encoding.UTF8.GetBytes(material)))
                .ToLowerInvariant();

            Assert.Equal(expected, RefreshKit.BuildId);
            Assert.Equal(64, RefreshKit.BuildId.Length);
            Assert.Equal(
                RefreshKit.Version + "-" + RefreshKit.BuildId.Substring(0, 16),
                RefreshKit.CacheKey);
        }

        [Fact]
        public void VersionInfo_PreservesThreeArgumentConstructorAndAddsEpoch()
        {
            var legacy = new RefreshKitVersionInfo("1", "build", "cache");
            var current = new RefreshKitVersionInfo("1", "build", "cache", "epoch");

            Assert.Equal(string.Empty, legacy.Epoch);
            Assert.Equal("epoch", current.Epoch);
            Assert.Equal(legacy.Version, current.Version);
            Assert.Equal(legacy.BuildId, current.BuildId);
            Assert.Equal(legacy.CacheKey, current.CacheKey);
        }

        [Fact]
        public void ProcessEpoch_IsStableInJsonPayloadAndNeverStampedAsAnHtmlValue()
        {
            var epoch = PluginServiceRegistrator.ProcessEpoch;
            var options = new RefreshKitOptions
            {
                PluginName = "Plugin",
                BasePath = "Plugin",
                ScriptPaths = new[] { "kit.js" },
                VersionProvider = () => "generation",
                EpochProvider = () => epoch,
                ExtraAttributes = _ => PluginServiceRegistrator.BuildKitAttributes(),
            };

            var first = RefreshKitVersionControllerBase.CreateVersionInfo(options);
            var second = RefreshKitVersionControllerBase.CreateVersionInfo(options);
            var tags = RefreshKit.BuildScriptTags(options);

            Assert.Matches("^[0-9a-f]{32}$", epoch);
            Assert.Equal(epoch, first.Epoch);
            Assert.Equal(first.Epoch, second.Epoch);
            Assert.Equal("generation", first.CacheKey);
            Assert.Equal(first.CacheKey, second.CacheKey);
            Assert.Contains("data-version-epoch-json-field=\"Epoch\"", tags, StringComparison.Ordinal);
            Assert.Contains("?v=generation", tags, StringComparison.Ordinal);
            Assert.DoesNotContain(epoch, tags, StringComparison.Ordinal);
        }

        [Fact]
        public void RegistratorDiAndController_ExposeOneStableEpochOnlyInJson()
        {
            var applicationHost = DispatchProxy.Create<IServerApplicationHost, NullInterfaceProxy>();
            var pluginManager = DispatchProxy.Create<IPluginManager, NullInterfaceProxy>();
            var services = new ServiceCollection();
            services.AddSingleton<IPluginManager>(pluginManager);
            new PluginServiceRegistrator().RegisterServices(services, applicationHost);
            services.AddTransient<RefreshKitController>();

            using var provider = services.BuildServiceProvider();
            var firstJson = SerializeGenerationRequest(provider);
            var secondJson = SerializeGenerationRequest(provider);
            using var firstDocument = JsonDocument.Parse(firstJson);
            using var secondDocument = JsonDocument.Parse(secondJson);
            var first = firstDocument.RootElement;
            var second = secondDocument.RootElement;
            var epoch = first.GetProperty("Epoch").GetString();
            var cacheKey = first.GetProperty("CacheKey").GetString();

            Assert.NotNull(epoch);
            Assert.NotNull(cacheKey);
            Assert.Matches("^[0-9a-f]{32}$", epoch!);
            Assert.Equal(epoch, second.GetProperty("Epoch").GetString());
            Assert.Equal(cacheKey, second.GetProperty("CacheKey").GetString());

            var plainText = ExecuteGenerationTextRequest(provider);
            Assert.Equal(cacheKey, plainText);
            Assert.DoesNotContain(epoch!, plainText, StringComparison.Ordinal);

            var options = Assert.IsType<RefreshKitOptions>(RefreshKit.Options);
            var tags = RefreshKit.BuildScriptTags(options);
            Assert.Contains("data-version-epoch-json-field=\"Epoch\"", tags, StringComparison.Ordinal);
            Assert.Contains("data-boot-version=\"" + cacheKey + "\"", tags, StringComparison.Ordinal);
            Assert.Contains("?v=" + cacheKey, tags, StringComparison.Ordinal);
            Assert.DoesNotContain(epoch!, tags, StringComparison.Ordinal);
            Assert.DoesNotContain(epoch!, cacheKey!, StringComparison.Ordinal);
            Assert.DoesNotContain(epoch!, first.GetProperty("Version").GetString()!, StringComparison.Ordinal);
            Assert.DoesNotContain(epoch!, first.GetProperty("BuildId").GetString()!, StringComparison.Ordinal);
        }

        private static string SerializeGenerationRequest(IServiceProvider provider)
        {
            var controller = provider.GetRequiredService<RefreshKitController>();
            controller.ControllerContext = new ControllerContext
            {
                HttpContext = new DefaultHttpContext(),
            };
            var result = Assert.IsType<OkObjectResult>(controller.GetGeneration());
            Assert.Equal(
                "no-store, no-cache, must-revalidate, max-age=0",
                controller.Response.Headers.CacheControl.ToString());
            Assert.NotNull(result.Value);
            return JsonSerializer.Serialize(result.Value, result.Value!.GetType());
        }

        private static string ExecuteGenerationTextRequest(IServiceProvider provider)
        {
            var controller = provider.GetRequiredService<RefreshKitController>();
            controller.ControllerContext = new ControllerContext
            {
                HttpContext = new DefaultHttpContext(),
            };
            var result = Assert.IsType<ContentResult>(controller.GetGenerationText());
            Assert.Equal("text/plain; charset=utf-8", result.ContentType);
            return Assert.IsType<string>(result.Content);
        }

        [Fact]
        public void EpochProvider_IsNormalizedAndBoundedAtTheServerBoundary()
        {
            var options = new RefreshKitOptions { EpochProvider = () => "  epoch-token  " };
            Assert.Equal("epoch-token", options.ResolveEpoch());

            options.EpochProvider = () => new string('x', 201);
            Assert.Equal(string.Empty, options.ResolveEpoch());

            options.EpochProvider = () => "  <html>proxy error</html>";
            Assert.Equal(string.Empty, options.ResolveEpoch());

            options.EpochProvider = () => "   ";
            Assert.Equal(string.Empty, options.ResolveEpoch());

            options.EpochProvider = () => throw new InvalidOperationException("provider failed");
            Assert.Equal(string.Empty, options.ResolveEpoch());
        }

        [Fact]
        public void BuildScriptTags_UsesADistinctUrlForLiveDeveloperMode()
        {
            var production = RefreshKit.BuildScriptTags(
                "Plugin",
                "Plugin",
                new[] { "kit.js" },
                "g-key&one",
                devMode: false,
                buildId: "build");
            var development = RefreshKit.BuildScriptTags(
                "Plugin",
                "Plugin",
                new[] { "kit.js" },
                "g-key&one",
                devMode: true,
                buildId: "build");

            Assert.Contains("version=\"g-key&amp;one\"", production, StringComparison.Ordinal);
            Assert.Contains("version=\"g-key&amp;one\"", development, StringComparison.Ordinal);
            Assert.Contains("data-boot-version=\"g-key&amp;one\"", development, StringComparison.Ordinal);
            Assert.Contains("src=\"../Plugin/kit.js?v=g-key%26one\"", production, StringComparison.Ordinal);
            Assert.Contains(
                "src=\"../Plugin/kit.js?v=g-key%26one&amp;dev=1\"",
                development,
                StringComparison.Ordinal);
            Assert.DoesNotContain("dev=1", production, StringComparison.Ordinal);
        }

        [Fact]
        public void DevelopmentUrlMarker_RemainsNoStoreAcrossALiveFlagRace()
        {
            var marked = new DefaultHttpContext();
            marked.Request.QueryString = new QueryString("?v=g-key&dev=1");

            // The shell was generated while development mode was enabled, but
            // the setting changed before this script request reached its endpoint.
            RefreshKit.ApplyScriptCacheHeaders(marked.Response, devMode: false);

            Assert.Equal(
                "no-store, no-cache, must-revalidate, max-age=0",
                marked.Response.Headers.CacheControl.ToString());
            Assert.Equal("no-cache", marked.Response.Headers.Pragma.ToString());

            var production = new DefaultHttpContext();
            production.Request.QueryString = new QueryString("?v=g-key");
            RefreshKit.ApplyScriptCacheHeaders(production.Response, devMode: false);
            Assert.Equal(
                "public, max-age=31536000, immutable",
                production.Response.Headers.CacheControl.ToString());
        }

        [Fact]
        public void EmbeddedConfigurationPage_UsesItsInlineController()
        {
            const string Resource =
                "Jellyfin.Plugin.RefreshKit.Configuration.configPage.html";
            using var stream = typeof(Plugin).Assembly.GetManifestResourceStream(Resource);
            Assert.NotNull(stream);
            using var reader = new StreamReader(stream!, Encoding.UTF8);
            var html = reader.ReadToEnd();

            // data-controller="__plugin/name" asks Jellyfin Web to import a
            // separate ConfigurationPage resource with that name. This page's
            // controller is inline, so declaring one produces a 404 and prevents
            // pageshow/config initialization on both real supported hosts.
            Assert.DoesNotContain("data-controller=", html, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(
                "data-require=\"emby-input,emby-checkbox\"",
                html,
                StringComparison.Ordinal);
            Assert.Contains("addEventListener('pageshow'", html, StringComparison.Ordinal);
        }

        [Fact]
        public void ReplaceOwnedScriptTags_RemovesOnlyRealOwnedElements()
        {
            const string Decoy =
                "<script plugin=\"Kit &amp; Co\" src=\"/decoy.js\"></script>";
            var html = "<html><body>"
                + "<script>var literal = '" + Decoy + "';</script>"
                + "<!-- " + Decoy + " -->"
                + "<textarea>" + Decoy + "</textarea>\n"
                + "<script data-note='plugin=\"Kit &amp; Co\"' src=\"/third.js\"></script>\n"
                + "<script data-json=\"a>b\" plugin=\"Kit &amp; Co\" src=\"/old.js?v=old\"></script>\n"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.Contains("var literal = '" + Decoy + "';", result, StringComparison.Ordinal);
            Assert.Contains("<!-- " + Decoy + " -->", result, StringComparison.Ordinal);
            Assert.Contains("<textarea>" + Decoy + "</textarea>", result, StringComparison.Ordinal);
            Assert.Contains("data-note='plugin=\"Kit &amp; Co\"'", result, StringComparison.Ordinal);
            Assert.DoesNotContain("/old.js?v=old", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Theory]
        [InlineData("<!-->")]
        [InlineData("<!--->")]
        [InlineData("<!-- recovered --!>")]
        public void ReplaceOwnedScriptTags_SeesMarkupAfterCommentRecoveryClose(string comment)
        {
            var html = "<html><body>" + comment
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.Contains(comment, result, StringComparison.Ordinal);
            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_SeesMarkupAfterNestedAbruptCommentClose()
        {
            const string Comment = "<!--outer<!-->";
            var html = "<html><body>" + Comment
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.Contains(Comment, result, StringComparison.Ordinal);
            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ScrubsHtmlScriptInsideSvgTitleIntegrationPoint()
        {
            const string Html = "<html><body><svg><title>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</title></svg></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains("<svg><title></title></svg>", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
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
        public void ReplaceOwnedScriptTags_ScrubsHtmlScriptInsideMathMlIntegrationPoint(
            string prefix,
            string suffix)
        {
            var html = "<html><body>" + prefix
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + suffix + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(prefix + suffix, result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_PreservesForeignScriptInsideMathMlMglyph()
        {
            const string OldTag =
                "<script plugin=\"Kit &amp; Co\" src=\"/foreign.js\"></script>";
            const string Html = "<html><body><math><mtext><mglyph>" + OldTag
                + "</mglyph></mtext></math></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(OldTag, result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_DoesNotDecodeAnnotationEncodingTwice()
        {
            const string OldTag =
                "<script plugin=\"Kit &amp; Co\" src=\"/foreign.js\"></script>";
            const string Html = "<html><body><math>"
                + "<annotation-xml encoding=\"text&amp;sol;html\">"
                + OldTag + "</annotation-xml></math></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(OldTag, result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Theory]
        [InlineData("<math>", "</math>")]
        [InlineData("<svg>", "</svg>")]
        public void ReplaceOwnedScriptTags_ScrubsAfterForeignContentHtmlBreakout(
            string foreignOpen,
            string foreignClose)
        {
            var html = "<html><body>" + foreignOpen + "<div>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</div>" + foreignClose + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Theory]
        [InlineData("p")]
        [InlineData("br")]
        public void ReplaceOwnedScriptTags_ScrubsAfterForeignContentSpecialEndTag(
            string endTag)
        {
            var html = "<html><body><math></" + endTag + ">"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</math></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Theory]
        [InlineData("<script>", "</script>")]
        [InlineData("<script/>", "</script>")]
        [InlineData("<style>", "</style>")]
        public void ReplaceOwnedScriptTags_ScrubsAfterForeignScriptOrStyleBreakout(
            string foreignElement,
            string trailingClose)
        {
            var html = "<html><body><svg>" + foreignElement + "<div>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</div>" + trailingClose + "</svg></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ForeignEndTagAcrossNamespaceFailsClosed()
        {
            const string Html = "<html><body><div><svg><g></div>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</svg></body></html>";

            Assert.Same(Html, RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_MismatchedEndTagAcrossTemplateFailsClosed()
        {
            const string Html = "<html><body><div><template></div>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/inert.js\"></script>"
                + "</template>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/live.js\"></script>"
                + "</body></html>";

            Assert.Same(Html, RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_InvalidEndTagUsesBogusCommentClose()
        {
            const string Html = "<html><body></!x \""
                + "><script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>\">"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_HtmlCdataSpellingExposesOwnedTag()
        {
            const string Html = "<html><body><![CDATA[>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>]]>"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_QuotedDoctypeFailsClosed()
        {
            const string OldTag =
                "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>";
            const string Html = "<!DOCTYPE html PUBLIC \"fake > identifier\">"
                + "<html><body>" + OldTag + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Same(Html, result);
            Assert.Contains(OldTag, result, StringComparison.Ordinal);
            Assert.DoesNotContain(CurrentTag, result, StringComparison.Ordinal);
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ForeignCdataKeepsOwnedTagTextInert()
        {
            const string OldTag =
                "<script plugin=\"Kit &amp; Co\" src=\"/foreign.js\"></script>";
            const string Html = "<html><body><svg><![CDATA[>" + OldTag
                + "]]></svg></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(OldTag, result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Theory]
        [InlineData("<svg><title>", "</title></svg>")]
        [InlineData("<math><mtext>", "</mtext></math>")]
        [InlineData(
            "<math><annotation-xml encoding=\"text/html\">",
            "</annotation-xml></math>")]
        public void ReplaceOwnedScriptTags_CdataAtHtmlIntegrationPointExposesOwnedTag(
            string prefix,
            string suffix)
        {
            var html = "<html><body>" + prefix + "<![CDATA[>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>]]>"
                + suffix + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_PreservesTagShapedTextInsideOrdinaryHtmlTitle()
        {
            const string Decoy =
                "<script plugin=\"Kit &amp; Co\" src=\"/title-text.js\"></script>";
            const string Html = "<html><head><title>" + Decoy
                + "</title></head><body></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains("<title>" + Decoy + "</title>", result, StringComparison.Ordinal);
            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ScrubsRealTagNestedUnderPunctuatedElementName()
        {
            const string Html = "<html><body><script.foo>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</script.foo></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Contains("<script.foo></script.foo>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_PreservesDoubleEscapedScriptSource()
        {
            const string Source = "<script><!--<script></script>"
                + "<script plugin=\"Kit &amp; Co\" src=\"/source-text.js\"></script></script>";
            const string Html = "<html><body>" + Source + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(Source, result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_PreservesTagTextInsideEndTagAttributes()
        {
            const string Decoy =
                "<script plugin=\"Kit &amp; Co\" src=\"/source-text.js\"></script>";
            var closingTag = "</script data-x=\"> " + Decoy + "\">";
            var html = "<html><body><script>window.ready = true;" + closingTag
                + "<script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>"
                + "</body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(html, PluginName, CurrentTag);

            Assert.Contains(closingTag, result, StringComparison.Ordinal);
            Assert.DoesNotContain("/old.js", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ConvergesOnOneCurrentTag()
        {
            const string Html = "<html><body>\n"
                + "<script plugin='Kit &amp; Co' src='/kit.js?v=old'></script>\n"
                + "</body></html>";

            var once = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);
            var twice = RefreshKit.ReplaceOwnedScriptTags(once, PluginName, CurrentTag);

            Assert.Equal(once, twice);
            Assert.Equal(1, Count(twice, CurrentTag));
            Assert.DoesNotContain("v=old", twice, StringComparison.Ordinal);
        }

        [Fact]
        public void ReplaceOwnedScriptTags_LeavesTruncatedDocumentUntouched()
        {
            const string Html = "<html><body><script plugin=\"Kit &amp; Co\" src=\"/old.js\"></script>";

            Assert.Same(Html, RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_InsertsBeforeRealBodyAndIgnoresLaterDecoys()
        {
            const string Html = "<html><body data-note='</body>'>"
                + "<script>const close = '</body>';</script>"
                + "<template><p></body></p></template>"
                + "<template/><p></body></p></template>"
                + "<textarea></body></textarea>"
                + "<svg><g></body></g></svg>"
                + "<svg data-x=value/><g></body></g></svg>"
                + "<math><mrow></body></mrow></math>"
                + "<math data-x=value/><mrow></body></mrow></math>"
                + "</BoDy \t><!-- trailing </body> -->"
                + "<script>const trailing = '</body>';</script></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(
                CurrentTag + "\n</BoDy \t>",
                result,
                StringComparison.Ordinal);
            Assert.EndsWith(
                "<!-- trailing </body> --><script>const trailing = '</body>';</script></html>",
                result,
                StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_LeavesOnlyInertBodyCloseDecoysUntouched()
        {
            const string Html = "<html><body data-note='</body>'>"
                + "<!-- </body> -->"
                + "<script>const close = '</body>';</script>"
                + "<style>.x::after{content:'</body>'}</style>"
                + "<template><p></body></p></template>"
                + "<template/><p></body></p></template>"
                + "<textarea></body></textarea>"
                + "<svg><g></body></g></svg>"
                + "<svg data-x=value/><g></body></g></svg>"
                + "<math><mrow></body></mrow></math>"
                + "<math data-x=value/><mrow></body></mrow></math>"
                + "</body ></body!>";

            Assert.Same(Html, RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_ClosesTrueSelfClosingForeignRootsBeforeBody()
        {
            const string Html = "<html><body><svg/><math/><main></main></body></html>";

            var result = RefreshKit.ReplaceOwnedScriptTags(Html, PluginName, CurrentTag);

            Assert.Contains(CurrentTag + "\n</body>", result, StringComparison.Ordinal);
            Assert.Equal(1, Count(result, CurrentTag));
        }

        [Fact]
        public void ReplaceOwnedScriptTags_LeavesFramesetBodyDecoyUntouched()
        {
            const string InsideFrameset =
                "<html><head></head><frameset><frame></body></frameset></html>";
            const string AfterFrameset =
                "<html><head></head><frameset><frame></frameset></body></html>";
            const string AfterHtml =
                "<html><head></head><frameset><frame></frameset></html></body>";

            Assert.Same(
                InsideFrameset,
                RefreshKit.ReplaceOwnedScriptTags(InsideFrameset, PluginName, CurrentTag));
            Assert.Same(
                AfterFrameset,
                RefreshKit.ReplaceOwnedScriptTags(AfterFrameset, PluginName, CurrentTag));
            Assert.Same(
                AfterHtml,
                RefreshKit.ReplaceOwnedScriptTags(AfterHtml, PluginName, CurrentTag));
        }

        private static int Count(string source, string value)
        {
            var count = 0;
            var offset = 0;
            while ((offset = source.IndexOf(value, offset, StringComparison.Ordinal)) >= 0)
            {
                count++;
                offset += value.Length;
            }

            return count;
        }
    }
}
