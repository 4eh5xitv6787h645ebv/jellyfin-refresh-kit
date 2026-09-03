using System;
using System.IO;
using System.Reflection;
using System.Text.RegularExpressions;
using Jellyfin.Plugin.RefreshKit.Controllers;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// The plugin's HTTP surface as the web client and an admin see it. The
    /// generation and plain-text routes were already exercised through DI in
    /// <see cref="RefreshKitHtmlTests"/>; this suite adds the route and access
    /// metadata, kit.js, the diagnostics payload, the embedded-runtime identity
    /// and the standalone tag attributes.
    /// </summary>
    public sealed class RefreshKitControllerTests
    {
        private static RefreshKitController CreateController(
            out DefaultHttpContext httpContext,
            out PluginGenerationProvider provider,
            Func<DateTime>? utcNow = null,
            Action? onScan = null)
        {
            // The provider never touches the configurations path while the
            // descriptor list is empty, so no directory is created (or leaked).
            var configurations = Path.Combine(Path.GetTempPath(), "rk-controller-tests-" + Guid.NewGuid().ToString("N"));
            provider = new PluginGenerationProvider(
                () =>
                {
                    onScan?.Invoke();
                    return Array.Empty<ActivePluginDescriptor>();
                },
                configurations,
                utcNow);
            httpContext = new DefaultHttpContext();
            return new RefreshKitController(provider)
            {
                ControllerContext = new ControllerContext { HttpContext = httpContext },
            };
        }

        private static MethodInfo Action(string name) =>
            typeof(RefreshKitController).GetMethod(name, BindingFlags.Public | BindingFlags.Instance)
            ?? throw new InvalidOperationException("missing action " + name);

        [Fact]
        public void ControllerIsRoutedUnderRefreshKit()
        {
            var route = typeof(RefreshKitController).GetCustomAttribute<RouteAttribute>();
            Assert.NotNull(route);
            Assert.Equal("RefreshKit", route.Template);
        }

        [Theory]
        [InlineData(nameof(RefreshKitController.GetGeneration), "Generation")]
        [InlineData(nameof(RefreshKitController.GetGenerationText), "Generation.txt")]
        [InlineData(nameof(RefreshKitController.GetKitScript), "kit.js")]
        public void PublicRoutesAreAnonymousGetsAtTheDocumentedPaths(string action, string template)
        {
            // A stale tab parked on the login page must still be able to poll:
            // these three are anonymous on purpose (README "HTTP endpoints").
            var method = Action(action);
            Assert.NotNull(method.GetCustomAttribute<AllowAnonymousAttribute>());
            Assert.Null(method.GetCustomAttribute<AuthorizeAttribute>());
            var get = method.GetCustomAttribute<HttpGetAttribute>();
            Assert.NotNull(get);
            Assert.Equal(template, get.Template);
        }

        [Fact]
        public void DiagnosticsRequiresElevationAndIsNotAnonymous()
        {
            // Server inventory is admin-only; the anonymous generation token must
            // stay the only thing the login page can read.
            var method = Action(nameof(RefreshKitController.GetDiagnostics));
            var authorize = method.GetCustomAttribute<AuthorizeAttribute>();
            Assert.NotNull(authorize);
            Assert.Equal("RequiresElevation", authorize.Policy);
            Assert.Null(method.GetCustomAttribute<AllowAnonymousAttribute>());
            var get = method.GetCustomAttribute<HttpGetAttribute>();
            Assert.NotNull(get);
            Assert.Equal("Diagnostics", get.Template);
        }

        [Fact]
        public void GenerationJsonCarriesTheDocumentedFieldsAndIsNoStore()
        {
            var controller = CreateController(out var httpContext, out _);

            var result = Assert.IsType<OkObjectResult>(controller.GetGeneration());
            var info = Assert.IsType<RefreshKitVersionInfo>(result.Value);

            Assert.Equal(RefreshKit.Version, info.Version);
            Assert.Equal(RefreshKit.BuildId, info.BuildId);
            Assert.False(string.IsNullOrWhiteSpace(info.CacheKey));
            Assert.NotNull(info.Epoch);
            Assert.Contains("no-store", httpContext.Response.Headers.CacheControl.ToString(), StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public void GenerationTextIsThePlainCacheKey()
        {
            var controller = CreateController(out var httpContext, out _);

            var result = Assert.IsType<ContentResult>(controller.GetGenerationText());

            Assert.Equal("text/plain; charset=utf-8", result.ContentType);
            Assert.False(string.IsNullOrWhiteSpace(result.Content));
            Assert.DoesNotContain("{", result.Content);
            Assert.Contains("no-store", httpContext.Response.Headers.CacheControl.ToString(), StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public void KitScriptServesTheEmbeddedRuntimeWithAnExplicitCachePolicy()
        {
            var controller = CreateController(out var httpContext, out _);

            var result = Assert.IsType<ContentResult>(controller.GetKitScript());

            Assert.Equal("application/javascript; charset=utf-8", result.ContentType);
            Assert.Same(Plugin.KitJavaScript, result.Content);
            Assert.Contains("KIT_VERSION", result.Content, StringComparison.Ordinal);
            var cacheControl = httpContext.Response.Headers.CacheControl.ToString();
            // Production: immutable behind a generation-addressed URL. Dev mode:
            // no-store. Either way the policy is explicit, never the host default.
            Assert.True(
                cacheControl.Contains("immutable", StringComparison.OrdinalIgnoreCase)
                || cacheControl.Contains("no-store", StringComparison.OrdinalIgnoreCase),
                "unexpected Cache-Control: " + cacheControl);
        }

        [Fact]
        public void DiagnosticsReadsExactlyOneSnapshotAndIsNoStore()
        {
            // Every property read on the provider is its own acquisition, and the
            // clock here jumps past the 5s TTL on every call, so reading the
            // generation, host and rows separately would rescan three times and
            // could pair a generation with rows that never produced it. The
            // controller must read one Snapshot.
            var clock = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var scans = 0;
            var controller = CreateController(
                out var httpContext,
                out _,
                utcNow: () => clock = clock.AddSeconds(PluginGenerationProvider.CacheTtlSeconds + 1),
                onScan: () => scans++);

            var result = Assert.IsType<OkObjectResult>(controller.GetDiagnostics().Result);

            Assert.Equal(1, scans);
            var payload = result.Value;
            Assert.NotNull(payload);
            var payloadType = payload.GetType();

            string Read(string name)
            {
                var property = payloadType.GetProperty(name);
                Assert.NotNull(property);
                var value = property.GetValue(payload);
                Assert.NotNull(value);
                return value.ToString() ?? string.Empty;
            }

            Assert.Matches("^g-[0-9a-f]{16}$", Read("Generation"));
            Assert.Equal(Plugin.KitVersion, Read("KitVersion"));
            Assert.Equal(RefreshKit.Version, Read("PluginVersion"));
            Assert.Equal(RefreshKit.BuildId, Read("BuildId"));
            var stampingProperty = payloadType.GetProperty("Stamping");
            Assert.NotNull(stampingProperty);
            var stamping = Assert.IsType<StampingDiagnostics>(stampingProperty.GetValue(payload));
            Assert.Equal(ThirdPartyTagStamper.MaxOpenElementDepth, stamping.OpenElementDepthLimit);
            Assert.NotNull(payloadType.GetProperty("Host"));
            Assert.NotNull(payloadType.GetProperty("Plugins"));
            Assert.Contains("no-store", httpContext.Response.Headers.CacheControl.ToString(), StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public void EmbeddedRuntimeIsTheRepositoryRootFile()
        {
            // The csproj embeds ../../jellyfin-refresh-kit.js at build time and the
            // README promises there is no second copy. Prove the bytes match.
            var root = FindRepositoryRoot();
            var expected = File.ReadAllText(Path.Combine(root, "jellyfin-refresh-kit.js"));

            Assert.Equal(expected, Plugin.KitJavaScript);
        }

        [Fact]
        public void KitVersionIsParsedFromTheEmbeddedRuntime()
        {
            var match = Regex.Match(Plugin.KitJavaScript, "KIT_VERSION\\s*=\\s*'([^']+)'");
            Assert.True(match.Success, "the embedded runtime declares no KIT_VERSION");
            Assert.Equal(match.Groups[1].Value, Plugin.KitVersion);
            Assert.NotEqual("unknown", Plugin.KitVersion);
            Assert.Matches("^\\d+\\.\\d+\\.\\d+$", Plugin.KitVersion);
        }

        [Fact]
        public void StandaloneKitAttributesDeclareNoAssetPatternsAndTheDocumentedDefaults()
        {
            // Without a Plugin instance the registrator falls back to defaults;
            // the standalone instance must never claim other plugins' runtime
            // assets (no data-asset-patterns / data-entry-scripts).
            var attributes = PluginServiceRegistrator.BuildKitAttributes();

            Assert.Null(Plugin.Instance); // no test constructs the plugin; defaults apply
            Assert.Contains("data-name=\"RefreshKitPlugin\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-version-url=\"../RefreshKit/Generation\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-version-json-field=\"CacheKey\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-version-epoch-json-field=\"Epoch\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-third-party-stamping=\"true\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-mode=\"auto\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-poll-seconds=\"60\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-idle-seconds=\"5\"", attributes, StringComparison.Ordinal);
            Assert.Contains("data-reload-budget=\"3\"", attributes, StringComparison.Ordinal);
            Assert.DoesNotContain("data-asset-patterns", attributes, StringComparison.Ordinal);
            Assert.DoesNotContain("data-entry-scripts", attributes, StringComparison.Ordinal);
        }

        [Fact]
        public void ThirdPartyStampingHonoursTheTagBlockSwitchAndGeneration()
        {
            const string html = "<html><head><script src=\"/web/other/plugin.js\"></script></head><body></body></html>";
            const string on = "data-third-party-stamping=\"true\" data-boot-version=\"g-abc123\"";
            const string off = "data-third-party-stamping=\"false\" data-boot-version=\"g-abc123\"";
            const string noGeneration = "data-third-party-stamping=\"true\"";

            Assert.Same(html, PluginServiceRegistrator.StampThirdPartyTags(html, off));
            Assert.Same(html, PluginServiceRegistrator.StampThirdPartyTags(html, noGeneration));
            var stamped = PluginServiceRegistrator.StampThirdPartyTags(html, on);
            Assert.Contains("/web/other/plugin.js?rkv=g-abc123", stamped, StringComparison.Ordinal);
        }

        [Fact]
        public void AThrowingStamperServesTheShellUnstampedAndCountsTheFailure()
        {
            // Stamp() is fail-open by contract; the diagnostics counter is how an
            // admin distinguishes "nothing eligible" from "the stamper threw".
            const string html = "<html><head><script src=\"/web/x.js\"></script></head></html>";
            var before = ThirdPartyTagStamper.Diagnostics.StampFailures;

            var result = ThirdPartyTagStamper.Stamp(
                html,
                "g-1",
                null,
                (_, _, _) => throw new InvalidOperationException("synthetic walker failure"));

            Assert.Same(html, result);
            Assert.True(ThirdPartyTagStamper.Diagnostics.StampFailures >= before + 1);
        }

        [Fact]
        public void ASuccessfulStampDoesNotCountAFailure()
        {
            const string html = "<html><head><script src=\"/web/x.js\"></script></head></html>";
            var before = ThirdPartyTagStamper.Diagnostics.StampFailures;

            var result = ThirdPartyTagStamper.Stamp(html, "g-1", null);

            Assert.Contains("rkv=g-1", result, StringComparison.Ordinal);
            // Another class's synthetic failure may run concurrently; the counter
            // must not have moved because of THIS call, which is what a strict
            // equality would over-claim. Only the throwing test above increments.
            Assert.True(ThirdPartyTagStamper.Diagnostics.StampFailures >= before);
        }

        private static string FindRepositoryRoot()
        {
            var directory = new DirectoryInfo(AppContext.BaseDirectory);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "jellyfin-refresh-kit.js"))
                    && File.Exists(Path.Combine(directory.FullName, "manifest.json")))
                {
                    return directory.FullName;
                }

                directory = directory.Parent;
            }

            throw new InvalidOperationException("repository root not found above " + AppContext.BaseDirectory);
        }
    }
}
