using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// The shell middleware against a faithful copy of Jellyfin Enhanced's
    /// request-time injector (ScriptInjectionStartupFilter, 12.8): a GET-only
    /// IStartupFilter that strips Accept-Encoding/Range/If-Range, buffers the
    /// downstream body, inserts its tag before the last &lt;/body&gt; of a 200
    /// text/html response, sets Content-Length and removes ETag/Last-Modified/
    /// Accept-Ranges. Both registration orders are real: Jellyfin sorts plugins
    /// by name, so "Jellyfin Enhanced" normally registers first and runs
    /// OUTSIDE Refresh Kit, but nothing guarantees it.
    /// </summary>
    [Collection(RefreshKitStaticOptionsCollection.Name)]
    public sealed class EnhancedInjectorInteropTests
    {
        private const string EnhancedTag =
            "<script plugin=\"Jellyfin Enhanced\" version=\"12.8.0.0-1\" dev=\"false\""
            + " src=\"../JellyfinEnhanced/script?v=12.8.0.0-1\" defer></script>";

        private static readonly byte[] Shell =
            Encoding.UTF8.GetBytes("<html><body><main>shell</main></body></html>");

        private sealed class EnhancedLikeInjector : IStartupFilter
        {
            public volatile bool Enabled = true;

            public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) =>
                app =>
                {
                    app.Use(InvokeAsync);
                    next(app);
                };

            private async Task InvokeAsync(HttpContext context, Func<Task> nextMw)
            {
                var path = context.Request.Path.Value;
                var isIndex = path != null
                    && (path.EndsWith("/web/index.html", StringComparison.OrdinalIgnoreCase)
                        || path.EndsWith("/web/", StringComparison.OrdinalIgnoreCase));
                if (!isIndex || !HttpMethods.IsGet(context.Request.Method) || !Enabled)
                {
                    await nextMw().ConfigureAwait(false);
                    return;
                }

                context.Request.Headers.Remove("Accept-Encoding");
                context.Request.Headers.Remove("Range");
                context.Request.Headers.Remove("If-Range");
                var originalBody = context.Response.Body;
                using var buffer = new MemoryStream();
                context.Response.Body = buffer;
                try
                {
                    await nextMw().ConfigureAwait(false);
                }
                catch
                {
                    context.Response.Body = originalBody;
                    throw;
                }

                context.Response.Body = originalBody;
                buffer.Seek(0, SeekOrigin.Begin);
                var isHtml = context.Response.StatusCode == 200
                    && (context.Response.ContentType?.Contains("text/html", StringComparison.OrdinalIgnoreCase) ?? false);
                if (!isHtml)
                {
                    await buffer.CopyToAsync(originalBody).ConfigureAwait(false);
                    return;
                }

                string html;
                using (var reader = new StreamReader(buffer, Encoding.UTF8, true, 1024, leaveOpen: true))
                {
                    html = await reader.ReadToEndAsync().ConfigureAwait(false);
                }

                var bodyClose = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
                if (html.IndexOf("/JellyfinEnhanced/script", StringComparison.OrdinalIgnoreCase) < 0 && bodyClose >= 0)
                {
                    html = html.Substring(0, bodyClose) + EnhancedTag + "\n" + html.Substring(bodyClose);
                }

                var bytes = Encoding.UTF8.GetBytes(html);
                context.Response.ContentType = "text/html;charset=utf-8";
                context.Response.ContentLength = bytes.Length;
                context.Response.Headers.Remove("ETag");
                context.Response.Headers.Remove("Last-Modified");
                context.Response.Headers.Remove("Accept-Ranges");
                await originalBody.WriteAsync(bytes, 0, bytes.Length).ConfigureAwait(false);
            }
        }

        private static async Task Source(HttpContext context)
        {
            context.Response.ContentType = "text/html; charset=utf-8";
            context.Response.Headers["Cache-Control"] = "no-cache";
            context.Response.Headers["ETag"] = "\"src\"";
            context.Response.Headers["Last-Modified"] = "Mon, 01 Jan 2024 00:00:00 GMT";
            if (context.Request.Headers["If-None-Match"].ToString().Contains("\"src\"", StringComparison.Ordinal))
            {
                context.Response.StatusCode = 304;
                return;
            }

            context.Response.ContentLength = Shell.Length;
            if (!HttpMethods.IsHead(context.Request.Method))
            {
                await context.Response.Body.WriteAsync(Shell).ConfigureAwait(false);
            }
        }

        private static RefreshKitScriptInjectionFilter Filter(WebApplication host) =>
            host.Services.GetServices<IStartupFilter>().OfType<RefreshKitScriptInjectionFilter>().Single();

        private static async Task<(WebApplication Host, HttpClient Client)> StartAsync(
            EnhancedLikeInjector injector,
            bool injectorOuter)
        {
            var builder = WebApplication.CreateBuilder();
            builder.WebHost.ConfigureKestrel(options => options.Listen(IPAddress.Loopback, 0));
            builder.Services.AddLogging();
            if (injectorOuter)
            {
                builder.Services.AddSingleton<IStartupFilter>(injector);
            }

            RefreshKit.AddRefreshKit(
                builder.Services,
                new RefreshKitOptions
                {
                    PluginName = "Jellyfin Refresh Kit",
                    BasePath = "RefreshKit",
                    ScriptPaths = new[] { "kit.js" },
                });
            if (!injectorOuter)
            {
                builder.Services.AddSingleton<IStartupFilter>(injector);
            }

            var host = builder.Build();
            host.Run(Source);
            await host.StartAsync().ConfigureAwait(false);
            var address = host.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!
                .Addresses.Single();
            var client = new HttpClient(new HttpClientHandler { AutomaticDecompression = DecompressionMethods.None })
            {
                BaseAddress = new Uri(address),
            };
            return (host, client);
        }

        private static async Task<(HttpResponseMessage Response, string Body)> SendAsync(
            HttpClient client,
            HttpMethod method,
            string? ifNoneMatch = null)
        {
            var request = new HttpRequestMessage(method, "/web/index.html");
            request.Headers.TryAddWithoutValidation("Accept-Encoding", "gzip, br");
            if (ifNoneMatch != null)
            {
                request.Headers.TryAddWithoutValidation("If-None-Match", ifNoneMatch);
            }

            var response = await client.SendAsync(request).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return (response, body);
        }

        [Fact]
        public async Task InjectorInsideRefreshKit_TurnedOnOverAWarmCache_IsServedWithinTwoRequests()
        {
            var injector = new EnhancedLikeInjector { Enabled = false };
            var (host, client) = await StartAsync(injector, injectorOuter: false);
            try
            {
                var (cold, coldBody) = await SendAsync(client, HttpMethod.Get);
                Assert.Equal(HttpStatusCode.OK, cold.StatusCode);
                Assert.Contains("Jellyfin Refresh Kit", coldBody, StringComparison.Ordinal);
                Assert.DoesNotContain("Jellyfin Enhanced", coldBody, StringComparison.Ordinal);
                var (warm, _) = await SendAsync(client, HttpMethod.Get);
                Assert.Equal(cold.Headers.ETag, warm.Headers.ETag);

                // The admin turns the injector's kill switch off: the source
                // still answers 304 to Refresh Kit's revalidation, so the cached
                // entry can be served at most once more before it is evicted.
                injector.Enabled = true;
                var (first, firstBody) = await SendAsync(client, HttpMethod.Get);
                var (second, secondBody) = await SendAsync(client, HttpMethod.Get);
                Assert.Equal(HttpStatusCode.OK, first.StatusCode);
                Assert.Equal(HttpStatusCode.OK, second.StatusCode);
                Assert.Contains("Jellyfin Enhanced", secondBody, StringComparison.Ordinal);
                Assert.Contains("Jellyfin Refresh Kit", secondBody, StringComparison.Ordinal);
                Assert.Equal(secondBody.Length, second.Content.Headers.ContentLength);

                // And it stays: nothing stale is retained for this base.
                var (third, thirdBody) = await SendAsync(client, HttpMethod.Get);
                Assert.Contains("Jellyfin Enhanced", thirdBody, StringComparison.Ordinal);
                Assert.NotEqual(cold.Headers.ETag, third.Headers.ETag);

                // A client revalidating the injector-less representation gets
                // the injected one, never a 304 for a shell it no longer matches.
                var (revalidated, revalidatedBody) = await SendAsync(
                    client,
                    HttpMethod.Get,
                    cold.Headers.ETag!.ToString());
                Assert.Equal(HttpStatusCode.OK, revalidated.StatusCode);
                Assert.Contains("Jellyfin Enhanced", revalidatedBody, StringComparison.Ordinal);
                _ = firstBody;
            }
            finally
            {
                client.Dispose();
                await host.DisposeAsync();
            }
        }

        [Fact]
        public async Task InjectorInsideRefreshKit_FromTheStart_IsValidatedAndRevalidated()
        {
            var injector = new EnhancedLikeInjector { Enabled = true };
            var (host, client) = await StartAsync(injector, injectorOuter: false);
            try
            {
                var (get, body) = await SendAsync(client, HttpMethod.Get);
                Assert.Equal(HttpStatusCode.OK, get.StatusCode);
                Assert.Contains("Jellyfin Enhanced", body, StringComparison.Ordinal);
                Assert.Contains("Jellyfin Refresh Kit", body, StringComparison.Ordinal);
                Assert.NotNull(get.Headers.ETag);
                Assert.StartsWith("\"rk-", get.Headers.ETag!.Tag, StringComparison.Ordinal);
                Assert.Equal(body.Length, get.Content.Headers.ContentLength);

                var (notModified, _) = await SendAsync(client, HttpMethod.Get, get.Headers.ETag.ToString());
                Assert.Equal(HttpStatusCode.NotModified, notModified.StatusCode);

                // Many requests in a row: the gate is skipped once it is clear
                // nothing can be retained, and every response is still complete.
                for (var i = 0; i < 6; i++)
                {
                    var (again, againBody) = await SendAsync(client, HttpMethod.Get);
                    Assert.Equal(HttpStatusCode.OK, again.StatusCode);
                    Assert.Equal(body, againBody);
                    Assert.Equal(get.Headers.ETag, again.Headers.ETag);
                }

                Assert.True(Filter(host).ConsecutiveUncachedShells >= 3, "the single-flight gate is skipped once nothing can be retained");

                var (head, headBody) = await SendAsync(client, HttpMethod.Head);
                Assert.Equal(HttpStatusCode.OK, head.StatusCode);
                Assert.Equal(string.Empty, headBody);
                Assert.Equal(get.Headers.ETag, head.Headers.ETag);
                Assert.Equal(body.Length, head.Content.Headers.ContentLength);
            }
            finally
            {
                client.Dispose();
                await host.DisposeAsync();
            }
        }

        [Fact]
        public async Task InjectorOutsideRefreshKit_HeadDescribesWhatGetActuallyCarries()
        {
            var injector = new EnhancedLikeInjector { Enabled = true };
            var (host, client) = await StartAsync(injector, injectorOuter: true);
            try
            {
                var (get, body) = await SendAsync(client, HttpMethod.Get);
                Assert.Equal(HttpStatusCode.OK, get.StatusCode);
                Assert.Contains("Jellyfin Enhanced", body, StringComparison.Ordinal);
                Assert.Contains("Jellyfin Refresh Kit", body, StringComparison.Ordinal);
                Assert.Equal(1, body.Split("Jellyfin Refresh Kit").Length - 1);
                Assert.Null(get.Headers.ETag);
                Assert.True(get.Headers.CacheControl?.NoStore, "outer-owned GET must be no-store");
                Assert.Equal(body.Length, get.Content.Headers.ContentLength);

                // The injector passes HEAD straight through, so Refresh Kit
                // answers it. It must not advertise a validator, length or cache
                // policy that no GET for this URL ever carries.
                Assert.True(Filter(host).ConsecutiveUncachedShells >= 3, "an outer owner makes the gate worthless from its first response");
                var (head, headBody) = await SendAsync(client, HttpMethod.Head);
                Assert.Equal(HttpStatusCode.OK, head.StatusCode);
                Assert.Equal(string.Empty, headBody);
                Assert.Null(head.Headers.ETag);
                Assert.False(head.Content.Headers.Contains("Content-Encoding"));
                Assert.False(head.Headers.Contains("Vary"));
                Assert.True(head.Headers.CacheControl?.NoStore, "HEAD after an outer owner must be no-store");
                // Refresh Kit drops its intermediate length; Kestrel then frames
                // the bodiless HEAD itself, so no length of ours is advertised.
                Assert.NotEqual(body.Length, head.Content.Headers.ContentLength);
                Assert.False(head.Content.Headers.Contains("Last-Modified"));

                // Even a HEAD carrying some validator gets a plain 200.
                var (conditionalHead, _) = await SendAsync(client, HttpMethod.Head, "\"rk-anything\"");
                Assert.Equal(HttpStatusCode.OK, conditionalHead.StatusCode);
                Assert.Null(conditionalHead.Headers.ETag);

                // A stale conditional GET gets the full injected shell.
                var (revalidated, revalidatedBody) = await SendAsync(client, HttpMethod.Get, "\"rk-anything\"");
                Assert.Equal(HttpStatusCode.OK, revalidated.StatusCode);
                Assert.Contains("Jellyfin Enhanced", revalidatedBody, StringComparison.Ordinal);

                // Turning the injector off hands the final bytes back: strong
                // validators return and HEAD agrees with GET again.
                injector.Enabled = false;
                var (own, ownBody) = await SendAsync(client, HttpMethod.Get);
                var (ownAgain, _) = await SendAsync(client, HttpMethod.Get);
                Assert.DoesNotContain("Jellyfin Enhanced", ownBody, StringComparison.Ordinal);
                Assert.NotNull(ownAgain.Headers.ETag);
                var (ownHead, _) = await SendAsync(client, HttpMethod.Head);
                Assert.Equal(ownAgain.Headers.ETag, ownHead.Headers.ETag);
                Assert.Equal(ownBody.Length, ownHead.Content.Headers.ContentLength);
                Assert.Equal(0, Filter(host).ConsecutiveUncachedShells);
                _ = own;
            }
            finally
            {
                client.Dispose();
                await host.DisposeAsync();
            }
        }
    }
}
