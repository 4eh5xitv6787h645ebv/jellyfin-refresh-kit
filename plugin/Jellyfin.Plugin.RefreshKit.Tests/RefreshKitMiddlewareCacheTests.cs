using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Security.Claims;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.ResponseCompression;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Primitives;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    [Collection(RefreshKitStaticOptionsCollection.Name)]
    public class RefreshKitMiddlewareCacheTests
    {
        [Fact]
        public async Task VaryVariants_DoNotReuseValidatorsOrBodiesAcrossRequestValues()
        {
            var fullResponses = 0;
            var revalidations = 0;
            using var application = CreateApplication(async context =>
            {
                var variant = context.Request.Headers["X-Variant"].ToString();
                if (string.IsNullOrEmpty(variant))
                {
                    variant = "default";
                }

                var sourceETag = "\"source-" + variant + "\"";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-variant=\"" + variant + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = sourceETag;
                context.Response.Headers["Vary"] = "X-Variant";

                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    sourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref revalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                if (!HttpMethods.IsHead(context.Request.Method))
                {
                    await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
                }
            });

            var variantA = await application.SendAsync(
                headers: new Dictionary<string, string> { ["X-Variant"] = "A" });
            var variantB = await application.SendAsync(
                headers: new Dictionary<string, string>
                {
                    ["X-Variant"] = "B",
                    ["If-None-Match"] = variantA.Header("ETag"),
                });

            Assert.Equal(StatusCodes.Status200OK, variantA.StatusCode);
            Assert.Contains("data-variant=\"A\"", variantA.BodyText, StringComparison.Ordinal);
            Assert.Equal(StatusCodes.Status200OK, variantB.StatusCode);
            Assert.Contains("data-variant=\"B\"", variantB.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("data-variant=\"A\"", variantB.BodyText, StringComparison.Ordinal);
            Assert.NotEqual(variantA.Header("ETag"), variantB.Header("ETag"));
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref revalidations));

            var headA = await application.SendAsync(
                method: HttpMethods.Head,
                headers: new Dictionary<string, string> { ["X-Variant"] = "A" });
            Assert.Equal(StatusCodes.Status200OK, headA.StatusCode);
            Assert.Empty(headA.Body);
            Assert.Equal(variantA.Header("ETag"), headA.Header("ETag"));
            Assert.Equal(variantA.Body.Length.ToString(), headA.Header("Content-Length"));
            Assert.Contains("X-Variant", headA.Header("Vary"), StringComparison.OrdinalIgnoreCase);

            var notModifiedA = await application.SendAsync(
                headers: new Dictionary<string, string>
                {
                    ["X-Variant"] = "A",
                    ["If-None-Match"] = variantA.Header("ETag"),
                });
            Assert.Equal(StatusCodes.Status304NotModified, notModifiedA.StatusCode);
            Assert.Empty(notModifiedA.Body);
            Assert.Equal(variantA.Header("ETag"), notModifiedA.Header("ETag"));
            Assert.Equal(string.Empty, notModifiedA.Header("Content-Encoding"));
            Assert.Contains(
                "X-Variant",
                notModifiedA.Header("Vary"),
                StringComparison.OrdinalIgnoreCase);

            var variantBAgain = await application.SendAsync(
                headers: new Dictionary<string, string> { ["X-Variant"] = "B" });
            Assert.Equal(StatusCodes.Status200OK, variantBAgain.StatusCode);
            Assert.Equal(variantB.Body, variantBAgain.Body);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(3, Volatile.Read(ref revalidations));
        }

        [Fact]
        public async Task CompressionCache_DoesNotCrossHttpAndHttpsEligibility()
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-source=\"plain\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var http = await application.SendAsync(
                scheme: "http",
                headers: new Dictionary<string, string> { ["Accept-Encoding"] = "gzip" });
            Assert.Equal(StatusCodes.Status200OK, http.StatusCode);
            Assert.Equal("gzip", http.Header("Content-Encoding"));
            Assert.Contains(
                "data-source=\"plain\"",
                DecompressGzip(http.Body),
                StringComparison.Ordinal);

            var https = await application.SendAsync(
                scheme: "https",
                headers: new Dictionary<string, string> { ["Accept-Encoding"] = "gzip" });
            Assert.Equal(StatusCodes.Status200OK, https.StatusCode);
            Assert.Equal(string.Empty, https.Header("Content-Encoding"));
            Assert.Contains("data-source=\"plain\"", https.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Theory]
        [InlineData("host")]
        [InlineData("path-base")]
        [InlineData("path")]
        [InlineData("query")]
        [InlineData("raw-target")]
        public async Task CacheIdentity_IncludesTheCompleteRequestTarget(string dimension)
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                var identity = string.Concat(
                    context.Request.Host.Value,
                    "|",
                    context.Request.PathBase.Value,
                    "|",
                    context.Request.Path.Value,
                    "|",
                    context.Request.QueryString.Value,
                    "|",
                    context.Features.Get<IHttpRequestFeature>()?.RawTarget);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>" + identity + "</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync(
                host: dimension == "host" ? "alpha.example" : "jellyfin.example",
                pathBase: dimension == "path-base" ? "/alpha" : string.Empty,
                path: dimension == "path" ? "/web/index.html" : "/web/index.html",
                queryString: dimension == "query" ? "?tenant=alpha" : string.Empty,
                rawTarget: dimension == "raw-target"
                    ? "/web/index%2ehtml"
                    : null);
            var second = await application.SendAsync(
                host: dimension == "host" ? "beta.example" : "jellyfin.example",
                pathBase: dimension == "path-base" ? "/beta" : string.Empty,
                path: dimension == "path" ? "/WEB/index.html" : "/web/index.html",
                queryString: dimension == "query" ? "?tenant=beta" : string.Empty,
                rawTarget: dimension == "raw-target"
                    ? "/web/index.html"
                    : null);

            Assert.Equal(StatusCodes.Status200OK, first.StatusCode);
            Assert.Equal(StatusCodes.Status200OK, second.StatusCode);
            Assert.NotEqual(first.BodyText, second.BodyText);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task CachedRepresentation_PreservesSafeHeadersAndRemovesSourceDigests()
        {
            var fullResponses = 0;
            var revalidations = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref revalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.Headers["Content-Security-Policy"] = "default-src 'self'";
                context.Response.Headers["Content-Language"] = "en-AU";
                context.Response.Headers["X-Origin-Policy"] = "stable";
                context.Response.Headers["Content-MD5"] = "source-md5";
                context.Response.Headers["Digest"] = "sha-256=source";
                context.Response.Headers["Content-Digest"] = "sha-256=:source:";
                context.Response.Headers["Repr-Digest"] = "sha-256=:source:";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>safe headers</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var cached = await application.SendAsync();

            foreach (var response in new[] { first, cached })
            {
                Assert.Equal("default-src 'self'", response.Header("Content-Security-Policy"));
                Assert.Equal("en-AU", response.Header("Content-Language"));
                Assert.Equal("stable", response.Header("X-Origin-Policy"));
                Assert.Equal(string.Empty, response.Header("Content-MD5"));
                Assert.Equal(string.Empty, response.Header("Digest"));
                Assert.Equal(string.Empty, response.Header("Content-Digest"));
                Assert.Equal(string.Empty, response.Header("Repr-Digest"));
            }

            Assert.Equal(first.Body, cached.Body);
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref revalidations));
        }

        [Fact]
        public async Task ChangedReplayHeaderOn304_IsUsedOnceThenEvictsTheOldSnapshot()
        {
            var invocations = 0;
            var fullResponses = 0;
            var revalidations = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                var invocation = Interlocked.Increment(ref invocations);
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                context.Response.Headers["X-Origin-Policy"] = invocation == 1 ? "A" : "B";
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref revalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-invocation=\"" + invocation + "\"></main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var changed = await application.SendAsync();
            var afterEviction = await application.SendAsync();

            Assert.Equal("A", first.Header("X-Origin-Policy"));
            Assert.Equal("B", changed.Header("X-Origin-Policy"));
            Assert.Equal("B", afterEviction.Header("X-Origin-Policy"));
            Assert.Contains("data-invocation=\"3\"", afterEviction.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref revalidations));
        }

        [Fact]
        public async Task InertBodyCloseDecoys_ArePassedThroughAndNeverCachedAsInjected()
        {
            var fullResponses = 0;
            var revalidations = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref revalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main>"
                    + "<!-- </body> --><script>const close = '</body>';</script>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var second = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, first.StatusCode);
            Assert.Equal(StatusCodes.Status200OK, second.StatusCode);
            Assert.Contains("data-sequence=\"1\"", first.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-sequence=\"2\"", second.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("plugin=\"Jellyfin Refresh Kit\"", first.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("plugin=\"Jellyfin Refresh Kit\"", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref revalidations));
        }

        [Fact]
        public async Task ChangedCacheControlOn304_EvictsOldPolicyBeforeTheNextRequest()
        {
            var invocations = 0;
            var fullResponses = 0;
            var conditionalRequests = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source-shell\"";
                var invocation = Interlocked.Increment(ref invocations);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-source=\"stable\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalRequests);
                    if (invocation == 2)
                    {
                        context.Response.Headers["Cache-Control"] = "public, max-age=60";
                    }

                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.Headers["Cache-Control"] = invocation == 1
                    ? "no-cache"
                    : "public, max-age=60";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var changedPolicy = await application.SendAsync();
            var afterEviction = await application.SendAsync();

            Assert.Equal("no-cache", first.Header("Cache-Control"));
            Assert.Contains(
                "no-store",
                changedPolicy.Header("Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            Assert.Equal("public, max-age=60", afterEviction.Header("Cache-Control"));
            Assert.Equal(3, Volatile.Read(ref invocations));
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref conditionalRequests));
        }

        [Fact]
        public async Task PerRequestResponseTimeHeader_IsCurrentAndDoesNotEvictTheShell()
        {
            var invocations = 0;
            var fullResponses = 0;
            var conditionalResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"response-time-source\"";
                var invocation = Interlocked.Increment(ref invocations);
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                context.Response.Headers["X-Response-Time-ms"] = invocation.ToString();
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>stable shell</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "identity",
            });
            var notModified = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "identity",
                ["If-None-Match"] = first.Header("ETag"),
            });
            var revalidated = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "identity",
            });

            Assert.Equal(StatusCodes.Status304NotModified, notModified.StatusCode);
            Assert.Empty(notModified.Body);
            Assert.Equal(first.Header("ETag"), notModified.Header("ETag"));
            Assert.Equal(first.Body, revalidated.Body);
            Assert.Equal("1", first.Header("X-Response-Time-ms"));
            Assert.Equal("2", notModified.Header("X-Response-Time-ms"));
            Assert.Equal("3", revalidated.Header("X-Response-Time-ms"));
            Assert.Equal("no-cache", notModified.Header("Cache-Control"));
            Assert.Equal("no-cache", revalidated.Header("Cache-Control"));
            Assert.Equal(3, Volatile.Read(ref invocations));
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(2, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task ClearSiteDataIntroducedBySource304_IsDeliveredOnceAndEvictsTheBase()
        {
            var invocations = 0;
            var fullResponses = 0;
            var conditionalResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"clear-site-data-source\"";
                var invocation = Interlocked.Increment(ref invocations);
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    context.Response.Headers["Clear-Site-Data"] = "\"cache\"";
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-invocation=\"" + invocation + "\"></main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var introduced = await application.SendAsync();
            var afterEviction = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, introduced.StatusCode);
            Assert.Equal(first.Body, introduced.Body);
            Assert.Equal("\"cache\"", introduced.Header("Clear-Site-Data"));
            Assert.Contains(
                "no-store",
                introduced.Header("Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            Assert.Equal(string.Empty, afterEviction.Header("Clear-Site-Data"));
            Assert.Contains(
                "data-invocation=\"3\"",
                afterEviction.BodyText,
                StringComparison.Ordinal);
            Assert.Equal(3, Volatile.Read(ref invocations));
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref conditionalResponses));
        }

        [Theory]
        [InlineData("no-store")]
        [InlineData("private")]
        [InlineData("set-cookie")]
        [InlineData("clear-site-data")]
        [InlineData("vary-star")]
        [InlineData("vary-too-many")]
        [InlineData("vary-unicode-whitespace")]
        [InlineData("vary-authorization")]
        [InlineData("www-authenticate")]
        [InlineData("proxy-authentication-info")]
        [InlineData("oversized-response-metadata")]
        [InlineData("oversized-cache-control")]
        [InlineData("repeated-vary-metadata")]
        [InlineData("authorization-request")]
        [InlineData("cookie-request")]
        [InlineData("request-no-store")]
        public async Task UnsafeSourceOrRequest_IsTransformedButNotRetained(string scenario)
        {
            var fullResponses = 0;
            var internalRevalidations = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"fixed-source\"";
                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = scenario switch
                {
                    "no-store" => "no-store",
                    "private" => "private, max-age=60",
                    "oversized-cache-control" => "public, x=\""
                        + new string(
                            'x',
                            RefreshKitScriptInjectionFilter.MaxCachedResponseHeaderCharacters)
                        + "\"",
                    _ => "no-cache",
                };
                context.Response.Headers["ETag"] = SourceETag;

                switch (scenario)
                {
                    case "set-cookie":
                        context.Response.Headers["Set-Cookie"] = "session=secret; Path=/; HttpOnly";
                        break;
                    case "clear-site-data" when sequence == 1:
                        context.Response.Headers["Clear-Site-Data"] = "\"cookies\"";
                        break;
                    case "vary-star":
                        context.Response.Headers["Vary"] = "*";
                        break;
                    case "vary-too-many":
                        context.Response.Headers["Vary"] = string.Join(
                            ',',
                            Enumerable.Range(0, RefreshKitScriptInjectionFilter.MaxVaryHeaders + 1)
                                .Select(index => "X-Dimension-" + index));
                        break;
                    case "vary-unicode-whitespace":
                        context.Response.Headers["Vary"] = "\u00a0X-Variant\u00a0";
                        break;
                    case "vary-authorization":
                        context.Response.Headers["Vary"] = "Authorization";
                        break;
                    case "www-authenticate":
                        context.Response.Headers["WWW-Authenticate"] = "Bearer realm=\"test\"";
                        break;
                    case "proxy-authentication-info":
                        context.Response.Headers["Proxy-Authentication-Info"] = "nextnonce=secret";
                        break;
                    case "oversized-response-metadata":
                        context.Response.Headers["X-Oversized"] = new string(
                            'x',
                            RefreshKitScriptInjectionFilter.MaxCachedResponseHeaderCharacters + 1);
                        break;
                    case "repeated-vary-metadata":
                        context.Response.Headers["Vary"] = string.Join(
                            ',',
                            Enumerable.Repeat(
                                "X-Variant",
                                (RefreshKitScriptInjectionFilter.MaxVaryMetadataCharacters / 10)
                                    + 2));
                        break;
                }

                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref internalRevalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            Dictionary<string, string>? headers = scenario switch
            {
                "authorization-request" => new Dictionary<string, string>
                {
                    ["Authorization"] = "Bearer secret",
                },
                "cookie-request" => new Dictionary<string, string>
                {
                    ["Cookie"] = "session=secret",
                },
                "request-no-store" => new Dictionary<string, string>
                {
                    ["Cache-Control"] = "no-store",
                },
                _ => null,
            };
            var first = await application.SendAsync(headers: headers);
            var second = await application.SendAsync(headers: headers);

            Assert.Equal(StatusCodes.Status200OK, first.StatusCode);
            Assert.Equal(StatusCodes.Status200OK, second.StatusCode);
            Assert.Contains("data-sequence=\"1\"", first.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-sequence=\"2\"", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref internalRevalidations));
            Assert.Contains(
                "no-store",
                first.Header("Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            if (scenario.Equals("set-cookie", StringComparison.Ordinal))
            {
                Assert.Contains("session=secret", second.Header("Set-Cookie"), StringComparison.Ordinal);
            }
            else if (scenario.Equals("clear-site-data", StringComparison.Ordinal))
            {
                Assert.Equal("\"cookies\"", first.Header("Clear-Site-Data"));
                Assert.Equal(string.Empty, second.Header("Clear-Site-Data"));
            }
        }

        [Fact]
        public async Task AltSvcIsNotReplayedWhenTheRevalidatedOriginOmitsIt()
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"alt-svc-source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.Headers["Alt-Svc"] = "h2=\":8443\"; ma=1";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>alternative service</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var revalidated = await application.SendAsync();

            Assert.Equal("h2=\":8443\"; ma=1", first.Header("Alt-Svc"));
            Assert.Equal(string.Empty, revalidated.Header("Alt-Svc"));
            Assert.Equal(first.Body, revalidated.Body);
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task AltSvcIntroducedBySource304_IsDeliveredOnceAndNotReplayed()
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"introduced-alt-svc-source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    var conditional = Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    if (conditional == 1)
                    {
                        context.Response.Headers["Alt-Svc"] = "h3=\":8443\"; ma=1";
                    }

                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>introduced alternative service</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var introduced = await application.SendAsync();
            var omitted = await application.SendAsync();

            Assert.Equal(string.Empty, first.Header("Alt-Svc"));
            Assert.Equal("h3=\":8443\"; ma=1", introduced.Header("Alt-Svc"));
            Assert.Equal(string.Empty, omitted.Header("Alt-Svc"));
            Assert.Equal(first.Body, introduced.Body);
            Assert.Equal(first.Body, omitted.Body);
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(2, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task LateStatusGuard_IsPreservedAndNeverReceivesTheInjectedBody()
        {
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>source shell</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.Headers["Cache-Control"] = "public, max-age=600";
                context.Response.Headers["CDN-Cache-Control"] = "max-age=600";
                context.Response.Headers["Surrogate-Control"] = "max-age=600";
                context.Response.Headers["ETag"] = "\"source\"";
                context.Response.OnStarting(() =>
                {
                    context.Response.StatusCode = StatusCodes.Status403Forbidden;
                    context.Response.Headers["Connection"] = "Cache-Control";
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status403Forbidden, response.StatusCode);
            Assert.Contains("source shell", response.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("plugin=\"Jellyfin Refresh Kit\"", response.BodyText, StringComparison.Ordinal);
            Assert.Contains("no-store", response.Header("Cache-Control"), StringComparison.OrdinalIgnoreCase);
            Assert.Equal(string.Empty, response.Header("CDN-Cache-Control"));
            Assert.Equal(string.Empty, response.Header("Surrogate-Control"));
            Assert.Equal(string.Empty, response.Header("ETag"));
            Assert.Equal(string.Empty, response.Header("Connection"));
        }

        [Theory]
        [InlineData(StatusCodes.Status204NoContent)]
        [InlineData(StatusCodes.Status304NotModified)]
        public async Task LateBodyForbiddenStatus_NeverReceivesBufferedBytes(int statusCode)
        {
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>must not escape</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.Headers["ETag"] = "\"source\"";
                context.Response.OnStarting(() =>
                {
                    context.Response.StatusCode = statusCode;
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(statusCode, response.StatusCode);
            Assert.Empty(response.Body);
            Assert.Equal(string.Empty, response.Header("Content-Encoding"));
            Assert.Equal(string.Empty, response.Header("ETag"));
        }

        [Fact]
        public async Task LateNoStore_PreventsAdmissionBeforeAnyOtherRequestCanReuseIt()
        {
            var fullResponses = 0;
            var conditionalRequests = 0;
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                const string SourceETag = "\"source\"";
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalRequests);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.Headers["Cache-Control"] = "public, max-age=60";
                context.Response.Headers["ETag"] = SourceETag;
                context.Response.OnStarting(() =>
                {
                    context.Response.Headers["Cache-Control"] = "no-store";
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var second = await application.SendAsync();

            Assert.Contains("data-sequence=\"1\"", first.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-sequence=\"2\"", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref conditionalRequests));
        }

        [Fact]
        public async Task LateHeaderRemoval_IsNotResurrectedAndEvictsTheSnapshot()
        {
            var invocations = 0;
            var fullResponses = 0;
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                const string SourceETag = "\"source\"";
                var invocation = Interlocked.Increment(ref invocations);
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                context.Response.Headers["X-Origin-Policy"] = "A";
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    context.Response.OnStarting(() =>
                    {
                        context.Response.Headers.Remove("X-Origin-Policy");
                        return Task.CompletedTask;
                    });
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-invocation=\"" + invocation + "\"></main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var removed = await application.SendAsync();
            var afterEviction = await application.SendAsync();

            Assert.Equal("A", first.Header("X-Origin-Policy"));
            Assert.Equal(string.Empty, removed.Header("X-Origin-Policy"));
            Assert.Contains("data-invocation=\"3\"", afterEviction.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task LateUnsafeRevalidation_EvictsEverySiblingVariant()
        {
            var fullResponses = 0;
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                var variant = context.Request.Headers["X-Variant"].ToString();
                var sourceETag = "\"source-" + variant + "\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = sourceETag;
                context.Response.Headers["Vary"] = "X-Variant";
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    sourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    if (context.Request.Headers.ContainsKey("X-Late-NoStore"))
                    {
                        context.Response.OnStarting(() =>
                        {
                            context.Response.Headers["Cache-Control"] = "no-store";
                            return Task.CompletedTask;
                        });
                    }

                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-variant=\"" + variant + "\"></main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            await application.SendAsync(headers: new Dictionary<string, string> { ["X-Variant"] = "A" });
            await application.SendAsync(headers: new Dictionary<string, string> { ["X-Variant"] = "B" });
            await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["X-Variant"] = "A",
                ["X-Late-NoStore"] = "1",
            });
            var bAfterBaseEviction = await application.SendAsync(
                headers: new Dictionary<string, string> { ["X-Variant"] = "B" });

            Assert.Contains("data-variant=\"B\"", bAfterBaseEviction.BodyText, StringComparison.Ordinal);
            Assert.Equal(3, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task ConnectionNominatedHeader_IsNeverReplayedFromTheCache()
        {
            var fullResponses = 0;
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                const string SourceETag = "\"source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    context.Response.Headers["Connection"] = "X-Origin-Policy";
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.Headers["X-Origin-Policy"] = "A";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>connection</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var nominated = await application.SendAsync();
            await application.SendAsync();

            Assert.Equal("A", first.Header("X-Origin-Policy"));
            Assert.Equal(string.Empty, nominated.Header("X-Origin-Policy"));
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task ForwardedScheme_DoesNotCrossCompressionEligibilityBeforeForwardingRuns()
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                // Jellyfin applies forwarded headers inside the startup filter. Model
                // that ordering by changing Scheme only after Refresh Kit selected its
                // base key, but before response compression starts the response.
                context.Request.Scheme =
                    context.Request.Headers["X-Forwarded-Proto"].ToString();
                const string SourceETag = "\"forwarded-source\"";
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>forwarded</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var http = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "gzip",
                ["X-Forwarded-Proto"] = "http",
            });
            var https = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "gzip",
                ["X-Forwarded-Proto"] = "https",
            });

            Assert.Equal("gzip", http.Header("Content-Encoding"));
            Assert.Equal(string.Empty, https.Header("Content-Encoding"));
            Assert.Contains("forwarded", DecompressGzip(http.Body), StringComparison.Ordinal);
            Assert.Contains("forwarded", https.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Theory]
        [InlineData("application/x-text/html-data")]
        [InlineData("text/html-not")]
        public async Task HtmlSubstringMediaTypes_AreNeverPromotedToExecutableHtml(string contentType)
        {
            var source = Encoding.UTF8.GetBytes(
                "opaque prefix </body><script>mustRemainData()</script>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = contentType;
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Equal(source, response.Body);
            Assert.StartsWith(contentType, response.Header("Content-Type"), StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
        }

        [Theory]
        [InlineData("text/html; charset=iso-8859-1")]
        [InlineData("text/html; charset=\"windows-1252\"")]
        public async Task ExplicitNonUtf8Html_IsServedByteExactWithoutTransformation(
            string contentType)
        {
            var source = Encoding.Latin1.GetBytes(
                "<html><body><main>caf\u00e9</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = contentType;
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"latin-source\"";
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Equal(source, response.Body);
            Assert.StartsWith(contentType, response.Header("Content-Type"), StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
        }

        [Theory]
        [InlineData("text/html; charset=utf-8; CHARSET=UTF8")]
        [InlineData("text/html; charset=UTF-8; charset=iso-8859-1")]
        public async Task DuplicateCharsetParameters_AreServedByteExactAndNeverCached(
            string contentType)
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            var source = Encoding.UTF8.GetBytes(
                "<html><body><main>duplicate charset</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"duplicate-charset-source\"";
                context.Response.ContentType = contentType;
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var second = await application.SendAsync();

            Assert.Equal(source, first.Body);
            Assert.Equal(source, second.Body);
            Assert.DoesNotContain("Jellyfin Refresh Kit", first.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task ExplicitUtf8HtmlWithMalformedBytes_IsServedByteExactAndNeverCached()
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            var source = Encoding.UTF8.GetBytes("<html><body><main>malformed ")
                .Concat(new byte[] { 0xc3, 0x28 })
                .Concat(Encoding.UTF8.GetBytes("</main></body></html>"))
                .ToArray();
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"malformed-utf8-source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var second = await application.SendAsync();

            Assert.Equal(source, first.Body);
            Assert.Equal(source, second.Body);
            Assert.DoesNotContain("Jellyfin Refresh Kit", first.BodyText, StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task CharsetlessInvalidUtf8Html_IsServedByteExactWithoutTransformation()
        {
            var source = Encoding.Latin1.GetBytes(
                "<html><head><meta charset=\"windows-1252\"></head>" +
                "<body><main>caf\u00e9</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"charsetless-latin-source\"";
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Equal(source, response.Body);
            Assert.StartsWith("text/html", response.Header("Content-Type"), StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
        }

        [Fact]
        public async Task CharsetlessValidUtf8Html_RemainsTransformable()
        {
            var source = Encoding.UTF8.GetBytes(
                "<html><head><meta charset=\"utf-8\"></head>" +
                "<body><main>caf\u00e9</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html";
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Contains("caf\u00e9", response.BodyText, StringComparison.Ordinal);
            Assert.Contains("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
            Assert.Equal("text/html;charset=utf-8", response.Header("Content-Type"));
        }

        [Theory]
        [InlineData("assets/")]
        [InlineData("/elsewhere/")]
        [InlineData("https://cdn.example.invalid/root/")]
        [InlineData("//cdn.example.invalid/root/")]
        public async Task DocumentBaseHref_ServesSourceByteExactWithoutRuntimeInjection(
            string href)
        {
            var source = Encoding.UTF8.GetBytes(
                "<html><head><base href=\"" + href + "\"></head>"
                + "<body><main>source shell</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"base-source\"";
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Equal(source, response.Body);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
            Assert.Equal("\"base-source\"", response.Header("ETag"));
        }

        [Theory]
        [InlineData("text/html; charset=UTF-8")]
        [InlineData("text/html; charset=\"utf-8\"")]
        [InlineData("text/html; charset=utf8")]
        public async Task ExplicitUtf8CharsetAliasesRemainTransformable(string contentType)
        {
            var source = Encoding.UTF8.GetBytes(
                "<html><body><main>UTF-8 shell</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = contentType;
                context.Response.ContentLength = source.Length;
                await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Contains("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
            Assert.Equal("text/html;charset=utf-8", response.Header("Content-Type"));
        }

        [Fact]
        public async Task WildcardSourceTag_IsNeverStoredOrForwardedEvenWithLastModified()
        {
            var fullResponses = 0;
            var wildcardConditions = 0;
            using var application = CreateApplication(async context =>
            {
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    "*",
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref wildcardConditions);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "*";
                context.Response.Headers["Last-Modified"] = sequence == 1
                    ? "Wed, 21 Oct 2015 07:28:00 GMT"
                    : "Thu, 22 Oct 2015 07:28:00 GMT";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var second = await application.SendAsync();

            Assert.Contains("data-sequence=\"1\"", first.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-sequence=\"2\"", second.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref wildcardConditions));
        }

        [Fact]
        public async Task HeaderlessAuthenticatedPrincipal_NeverSeedsTheAnonymousCache()
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"common\"";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });
            var principal = new ClaimsPrincipal(
                new ClaimsIdentity(new[] { new Claim(ClaimTypes.Name, "alice") }, "fixture"));

            var authenticated = await application.SendAsync(user: principal);
            var anonymous = await application.SendAsync();

            Assert.Contains("data-sequence=\"1\"", authenticated.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-sequence=\"2\"", anonymous.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task ConsumedAuthorizationHeader_CannotMakeAPersonalizedResponseStoreable()
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                var personalized = context.Request.Headers.ContainsKey("Authorization");
                context.Request.Headers.Remove("Authorization");
                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-user=\""
                    + (personalized ? "alice" : "anonymous")
                    + "\" data-sequence=\""
                    + sequence
                    + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"common\"";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var personalized = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Authorization"] = "Bearer secret",
            });
            var anonymous = await application.SendAsync();

            Assert.Contains("data-user=\"alice\"", personalized.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-user=\"anonymous\"", anonymous.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Fact]
        public async Task ConsumedVaryHeader_IsKeyedFromTheRequestAsReceived()
        {
            var fullResponses = 0;
            using var application = CreateApplication(async context =>
            {
                var variant = context.Request.Headers["X-Variant"].ToString();
                context.Request.Headers.Remove("X-Variant");
                if (string.IsNullOrEmpty(variant))
                {
                    variant = "default";
                }

                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-variant=\"" + variant + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = "\"common\"";
                context.Response.Headers["Vary"] = "X-Variant";
                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var variant = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["X-Variant"] = "A",
            });
            var absent = await application.SendAsync();

            Assert.Contains("data-variant=\"A\"", variant.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-variant=\"default\"", absent.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
        }

        [Theory]
        [InlineData("if-match-precedes-date", StatusCodes.Status200OK)]
        [InlineData("if-none-match-precedes-date", StatusCodes.Status304NotModified)]
        [InlineData("if-match-then-if-none-match", StatusCodes.Status304NotModified)]
        public async Task SourceFallbackPreconditions_FollowRfcPrecedence(
            string scenario,
            int expectedStatus)
        {
            var body = Encoding.UTF8.GetBytes("{\"source\":true}");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "application/json";
                context.Response.Headers["ETag"] = "\"A\"";
                context.Response.Headers["Last-Modified"] = "Wed, 21 Oct 2015 07:28:00 GMT";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });
            var headers = scenario switch
            {
                "if-match-precedes-date" => new Dictionary<string, string>
                {
                    ["If-Match"] = "\"A\"",
                    ["If-Unmodified-Since"] = "Tue, 20 Oct 2015 07:28:00 GMT",
                },
                "if-none-match-precedes-date" => new Dictionary<string, string>
                {
                    ["If-None-Match"] = "\"A\"",
                    ["If-Modified-Since"] = "Tue, 20 Oct 2015 07:28:00 GMT",
                },
                _ => new Dictionary<string, string>
                {
                    ["If-Match"] = "\"A\"",
                    ["If-None-Match"] = "\"A\"",
                },
            };

            var response = await application.SendAsync(headers: headers);

            Assert.Equal(expectedStatus, response.StatusCode);
            if (expectedStatus == StatusCodes.Status200OK)
            {
                Assert.Equal(body, response.Body);
            }
            else
            {
                Assert.Empty(response.Body);
            }
        }

        [Theory]
        [InlineData(false)]
        [InlineData(true)]
        public async Task FinalSourceValidatorControlsFallbackPreconditions(bool oversized)
        {
            var body = Encoding.UTF8.GetBytes(oversized
                ? new string('x', RefreshKitScriptInjectionFilter.MaxTransformBodyBytes + 1)
                : "{\"source\":true}");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "application/json";
                context.Response.Headers["ETag"] = "\"A\"";
                context.Response.ContentLength = body.Length;
                context.Response.OnStarting(() =>
                {
                    context.Response.Headers["ETag"] = "\"B\"";
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["If-Match"] = "\"B\"",
            });

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Equal(body, response.Body);
            Assert.Equal("\"B\"", response.Header("ETag"));
        }

        [Theory]
        [InlineData(false)]
        [InlineData(true)]
        public async Task SourceFallbackHead_PreservesEntityHeadersAndStartsAsHead(bool oversized)
        {
            var callbackMethod = string.Empty;
            var body = Encoding.UTF8.GetBytes(oversized
                ? new string('x', RefreshKitScriptInjectionFilter.MaxTransformBodyBytes + 1)
                : "{\"source\":true}");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "application/json";
                context.Response.Headers["ETag"] = "\"source\"";
                context.Response.ContentLength = body.Length;
                context.Response.OnStarting(() =>
                {
                    callbackMethod = context.Request.Method;
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync(method: HttpMethods.Head);

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Empty(response.Body);
            Assert.Equal(body.Length.ToString(), response.Header("Content-Length"));
            Assert.Equal("\"source\"", response.Header("ETag"));
            Assert.Equal(HttpMethods.Head, callbackMethod);
        }

        [Fact]
        public async Task ExplicitSourceStart_CommitsUntouchedPassthrough()
        {
            var callbackRan = false;
            var body = Encoding.UTF8.GetBytes(
                "<html><body><main>explicit start</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.OnStarting(() =>
                {
                    callbackRan = true;
                    return Task.CompletedTask;
                });
                await context.Response.StartAsync().ConfigureAwait(false);
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.True(callbackRan);
            Assert.Equal(body, response.Body);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
        }

        [Fact]
        public async Task LateNonHtmlContentType_CancelsTransformationAndAdmission()
        {
            var body = Encoding.UTF8.GetBytes(
                "<html><body><main>source shape</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.OnStarting(() =>
                {
                    context.Response.ContentType = "application/json";
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(body, response.Body);
            Assert.StartsWith(
                "application/json",
                response.Header("Content-Type"),
                StringComparison.Ordinal);
            Assert.DoesNotContain("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
        }

        [Theory]
        [InlineData("GET")]
        [InlineData("HEAD")]
        public async Task IdentitySendFileResponse_IsStillTransformed(string method)
        {
            var sourcePath = Path.Combine(
                Path.GetTempPath(),
                "refresh-kit-send-file-" + Guid.NewGuid().ToString("N") + ".html");
            await File.WriteAllTextAsync(
                sourcePath,
                "<html><body><main>physical shell</main></body></html>",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            try
            {
                await using var application = await CreateKestrelApplicationAsync(async context =>
                {
                    context.Response.ContentType = "text/html; charset=utf-8";
                    context.Response.Headers["Cache-Control"] = "no-cache";
                    context.Response.Headers["ETag"] = "\"physical-source\"";
                    context.Response.Headers["Last-Modified"] =
                        "Sat, 06 Jun 2026 15:43:50 GMT";
                    await context.Response.SendFileAsync(sourcePath);
                });

                var response = await application.SendAsync(
                    method: method,
                    headers: new Dictionary<string, string>
                    {
                        ["Accept-Encoding"] = "identity",
                    });

                Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
                if (HttpMethods.IsHead(method))
                {
                    Assert.Empty(response.Body);
                    Assert.NotEqual("0", response.Header("Content-Length"));
                }
                else
                {
                    Assert.Contains(
                        "plugin=\"Jellyfin Refresh Kit\"",
                        response.BodyText,
                        StringComparison.Ordinal);
                    Assert.Equal(
                        response.Body.Length.ToString(),
                        response.Header("Content-Length"));
                }

                Assert.Contains("rk-", response.Header("ETag"), StringComparison.Ordinal);
                Assert.Equal(string.Empty, response.Header("Last-Modified"));
            }
            finally
            {
                File.Delete(sourcePath);
            }
        }

        [Fact]
        public async Task NestedIdentityBufferingMiddleware_PreservesTheTransformedBody()
        {
            var fullSourceResponses = 0;
            var conditionalSourceResponses = 0;
            var sourcePath = Path.Combine(
                Path.GetTempPath(),
                "refresh-kit-nested-send-file-" + Guid.NewGuid().ToString("N") + ".html");
            var source = Encoding.UTF8.GetBytes(
                "<html><body><main>physical shell</main></body></html>");
            await File.WriteAllBytesAsync(sourcePath, source);
            try
            {
                var callbacks = new List<string>();
                var outer = new NestedIdentityBufferingStartupFilter(
                    "<script src=\"/jellyfin-enhanced.js\"></script>",
                    ownsFinalRepresentation: true);
                await using var application = await CreateNestedKestrelApplicationAsync(
                    async context =>
                    {
                        context.Response.ContentType = "text/html; charset=utf-8";
                        context.Response.ContentLength = source.Length;
                        context.Response.Headers["Cache-Control"] = "no-cache";
                        context.Response.Headers["ETag"] = "\"source\"";
                        context.Response.OnStarting(() =>
                        {
                            callbacks.Add("source");
                            // This callback cannot run when RK provisionally starts
                            // the outer MemoryStream. It runs only at Kestrel and
                            // deliberately tries to publish metadata for bytes that
                            // the outer middleware has since changed.
                            context.Response.Headers["Cache-Control"] = "public, max-age=600";
                            context.Response.Headers["ETag"] = "\"late-inner\"";
                            context.Response.Headers["Content-Digest"] = "sha-256=:stale:";
                            context.Response.Headers["Connection"] =
                                "Cache-Control, Content-Digest, X-Preserved";
                            context.Response.Headers["Trailer"] = "Content-Digest";
                            var trailers = context.Features
                                .Get<IHttpResponseTrailersFeature>()?
                                .Trailers;
                            if (trailers != null)
                            {
                                trailers["Content-Digest"] = "sha-256=:stale-trailer:";
                            }

                            return Task.CompletedTask;
                        });
                        if (context.Request.Headers["If-None-Match"].ToString().Contains(
                            "\"source\"",
                            StringComparison.Ordinal))
                        {
                            Interlocked.Increment(ref conditionalSourceResponses);
                            context.Response.StatusCode = StatusCodes.Status304NotModified;
                            return;
                        }

                        Interlocked.Increment(ref fullSourceResponses);
                        await context.Response.SendFileAsync(sourcePath);
                    },
                    outer,
                    new NestedIdentityBufferingStartupFilter(
                        "<script src=\"/ratings.js\"></script>"),
                    new NestedIdentityBufferingStartupFilter(
                        "<script src=\"/startrack.js\"></script>"));

                var response = await application.SendAsync(headers: new Dictionary<string, string>
                {
                    ["Accept-Encoding"] = "identity",
                    ["If-None-Match"] = "*",
                });
                var repeated = await application.SendAsync(headers: new Dictionary<string, string>
                {
                    ["Accept-Encoding"] = "identity",
                    ["If-None-Match"] = "*",
                });

                Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
                Assert.Equal(StatusCodes.Status200OK, repeated.StatusCode);
                foreach (var current in new[] { response, repeated })
                {
                    Assert.Contains("/startrack.js", current.BodyText, StringComparison.Ordinal);
                    Assert.Contains("/ratings.js", current.BodyText, StringComparison.Ordinal);
                    Assert.Contains("/jellyfin-enhanced.js", current.BodyText, StringComparison.Ordinal);
                    Assert.Contains(
                        "plugin=\"Jellyfin Refresh Kit\"",
                        current.BodyText,
                        StringComparison.Ordinal);
                    Assert.Equal(current.Body.Length.ToString(), current.Header("Content-Length"));
                    Assert.Contains(
                        "no-store",
                        current.Header("Cache-Control"),
                        StringComparison.OrdinalIgnoreCase);
                    Assert.Equal(string.Empty, current.Header("ETag"));
                    Assert.Equal(string.Empty, current.Header("Content-Digest"));
                    Assert.Equal(string.Empty, current.Header("Trailer"));
                    Assert.DoesNotContain(
                        "Cache-Control",
                        current.Header("Connection"),
                        StringComparison.OrdinalIgnoreCase);
                    Assert.DoesNotContain(
                        "Content-Digest",
                        current.Header("Connection"),
                        StringComparison.OrdinalIgnoreCase);
                }

                Assert.False(outer.InnerHasStarted);
                Assert.Contains("no-store", outer.InnerCacheControl, StringComparison.OrdinalIgnoreCase);
                Assert.Equal(string.Empty, outer.InnerETag);
                Assert.Equal(0, outer.FinalTrailerCount);
                Assert.Equal(2, Volatile.Read(ref fullSourceResponses));
                Assert.Equal(0, Volatile.Read(ref conditionalSourceResponses));
                Assert.Equal(new[] { "source", "source" }, callbacks);
            }
            finally
            {
                File.Delete(sourcePath);
            }
        }

        [Theory]
        [InlineData("source-fallback")]
        [InlineData("overflow")]
        [InlineData("explicit-start")]
        [InlineData("late-status")]
        [InlineData("late-status-zero-length")]
        [InlineData("late-gzip")]
        [InlineData("late-transfer-encoding")]
        [InlineData("late-content-type-removal")]
        [InlineData("late-204")]
        [InlineData("late-205")]
        [InlineData("late-304")]
        [InlineData("late-401")]
        [InlineData("late-429")]
        [InlineData("late-500")]
        public async Task OuterIdentityBuffer_CompletesProvisionalWritePaths(string scenario)
        {
            var callbackCount = 0;
            var isHtml = !scenario.Equals("source-fallback", StringComparison.Ordinal)
                && !scenario.Equals("overflow", StringComparison.Ordinal);
            var source = Encoding.UTF8.GetBytes(scenario.Equals("overflow", StringComparison.Ordinal)
                ? new string('x', RefreshKitScriptInjectionFilter.MaxTransformBodyBytes + 1)
                : isHtml
                    ? "<html><body><main>source shell</main></body></html>"
                    : "{\"source\":true}");
            var outer = new NestedIdentityBufferingStartupFilter(
                "<script src=\"/jellyfin-enhanced.js\"></script>",
                ownsFinalRepresentation: true);
            await using var application = await CreateNestedKestrelApplicationAsync(
                async context =>
                {
                    context.Response.ContentType = isHtml
                        ? "text/html; charset=utf-8"
                        : "application/json";
                    context.Response.ContentLength = source.Length;
                    context.Response.Headers["Cache-Control"] = "no-cache";
                    context.Response.Headers["ETag"] = "\"source\"";
                    context.Response.OnStarting(() =>
                    {
                        Interlocked.Increment(ref callbackCount);
                        context.Response.Headers["Content-Digest"] = "sha-256=:late:";
                        context.Response.Headers["Connection"] = "Content-Digest";
                        if (scenario.StartsWith("late-status", StringComparison.Ordinal))
                        {
                            context.Response.StatusCode = StatusCodes.Status403Forbidden;
                            if (scenario.Equals(
                                "late-status-zero-length",
                                StringComparison.Ordinal))
                            {
                                context.Response.ContentLength = 0;
                            }
                        }
                        else if (scenario.Equals("late-gzip", StringComparison.Ordinal))
                        {
                            context.Response.Headers["Content-Encoding"] = "gzip";
                        }
                        else if (scenario.Equals(
                            "late-transfer-encoding",
                            StringComparison.Ordinal))
                        {
                            context.Response.ContentLength = 0;
                            context.Response.Headers["Transfer-Encoding"] = "chunked";
                        }
                        else if (scenario.Equals(
                            "late-content-type-removal",
                            StringComparison.Ordinal))
                        {
                            context.Response.ContentType = null;
                        }
                        else if (scenario.StartsWith("late-", StringComparison.Ordinal)
                            && int.TryParse(
                                scenario.Substring("late-".Length),
                                out var lateStatus))
                        {
                            context.Response.StatusCode = lateStatus;
                        }

                        return Task.CompletedTask;
                    });
                    if (scenario.Equals("explicit-start", StringComparison.Ordinal))
                    {
                        await context.Response.StartAsync().ConfigureAwait(false);
                    }

                    await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
                },
                outer);

            var response = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "identity",
                ["If-None-Match"] = "*",
            });

            var expectedStatus = scenario switch
            {
                "late-status" => StatusCodes.Status403Forbidden,
                "late-status-zero-length" => StatusCodes.Status403Forbidden,
                "late-401" => StatusCodes.Status401Unauthorized,
                "late-429" => StatusCodes.Status429TooManyRequests,
                "late-500" => StatusCodes.Status500InternalServerError,
                _ => StatusCodes.Status200OK,
            };
            Assert.Equal(expectedStatus, response.StatusCode);
            Assert.Equal(1, Volatile.Read(ref callbackCount));
            Assert.Equal(string.Empty, response.Header("Content-Digest"));
            Assert.DoesNotContain(
                "Content-Digest",
                response.Header("Connection"),
                StringComparison.OrdinalIgnoreCase);
            Assert.Equal(response.Body.Length.ToString(), response.Header("Content-Length"));
            Assert.Contains(
                "no-store",
                response.Header("Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            Assert.Equal(string.Empty, response.Header("ETag"));
            Assert.Equal(string.Empty, response.Header("Content-Encoding"));
            Assert.Equal(string.Empty, response.Header("Transfer-Encoding"));
            Assert.False(outer.InnerHasStarted);
            if (!isHtml)
            {
                Assert.Equal(source, response.Body);
                return;
            }

            Assert.Contains("/jellyfin-enhanced.js", response.BodyText, StringComparison.Ordinal);
            if (scenario.Equals("explicit-start", StringComparison.Ordinal))
            {
                Assert.DoesNotContain(
                    "plugin=\"Jellyfin Refresh Kit\"",
                    response.BodyText,
                    StringComparison.Ordinal);
            }
            else
            {
                Assert.Contains(
                    "plugin=\"Jellyfin Refresh Kit\"",
                    response.BodyText,
                    StringComparison.Ordinal);
            }

            Assert.Equal("text/html; charset=utf-8", response.Header("Content-Type"));
        }

        [Theory]
        [InlineData(StatusCodes.Status204NoContent)]
        [InlineData(StatusCodes.Status205ResetContent)]
        [InlineData(StatusCodes.Status304NotModified)]
        public async Task OuterIdentityBuffer_PreservesBodylessLateStatusWithoutPendingBytes(
            int lateStatus)
        {
            var outer = new NestedIdentityBufferingStartupFilter(
                "<script src=\"/jellyfin-enhanced.js\"></script>",
                ownsFinalRepresentation: true);
            await using var application = await CreateNestedKestrelApplicationAsync(
                context =>
                {
                    context.Response.StatusCode = StatusCodes.Status200OK;
                    context.Response.ContentType = "application/json";
                    context.Response.ContentLength = 0;
                    context.Response.OnStarting(() =>
                    {
                        context.Response.StatusCode = lateStatus;
                        context.Response.ContentType = "text/plain";
                        context.Response.ContentLength = 17;
                        context.Response.Headers["Content-Encoding"] = "gzip";
                        context.Response.Headers["TE"] = "trailers";
                        context.Response.Headers["Transfer-Encoding"] = "chunked";
                        context.Response.Headers["Upgrade"] = "websocket";
                        context.Response.Headers["ETag"] = "\"late\"";
                        context.Response.Headers["Connection"] =
                            "Cache-Control, Content-Encoding, Content-Length, "
                            + "Content-Type, Transfer-Encoding";
                        return Task.CompletedTask;
                    });
                    return Task.CompletedTask;
                },
                outer);

            var response = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["Accept-Encoding"] = "identity",
            });

            Assert.Equal(lateStatus, response.StatusCode);
            Assert.Empty(response.Body);
            Assert.Equal(string.Empty, response.Header("Content-Type"));
            Assert.Equal(string.Empty, response.Header("Content-Encoding"));
            Assert.Equal(string.Empty, response.Header("TE"));
            Assert.Equal(string.Empty, response.Header("Transfer-Encoding"));
            Assert.Equal(string.Empty, response.Header("Upgrade"));
            Assert.True(
                string.IsNullOrEmpty(response.Header("Content-Length"))
                    || (lateStatus == StatusCodes.Status205ResetContent
                        && response.Header("Content-Length") == "0"));
            Assert.Equal(string.Empty, response.Header("ETag"));
            Assert.Contains(
                "no-store",
                response.Header("Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain(
                "Cache-Control",
                response.Header("Connection"),
                StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public async Task OuterIdentityBuffer_ReleasesSameKeyGateBeforeFinalTransportWrite()
        {
            var outerReachedTransport = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var allowFirstTransportWrite = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var outerWriteCount = 0;
            var fullSourceResponses = 0;
            var outer = new NestedIdentityBufferingStartupFilter(
                "<script src=\"/jellyfin-enhanced.js\"></script>",
                ownsFinalRepresentation: true,
                async _ =>
                {
                    if (Interlocked.Increment(ref outerWriteCount) != 1)
                    {
                        return;
                    }

                    outerReachedTransport.TrySetResult(true);
                    await allowFirstTransportWrite.Task.ConfigureAwait(false);
                });
            var source = Encoding.UTF8.GetBytes(
                "<html><body><main>same-key gate</main></body></html>");
            await using var application = await CreateNestedKestrelApplicationAsync(
                async context =>
                {
                    Interlocked.Increment(ref fullSourceResponses);
                    context.Response.ContentType = "text/html; charset=utf-8";
                    context.Response.ContentLength = source.Length;
                    context.Response.Headers["Cache-Control"] = "no-cache";
                    context.Response.Headers["ETag"] = "\"same-key-source\"";
                    await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
                },
                outer);

            var first = application.SendAsync();
            Task<ResponseSnapshot>? second = null;
            ResponseSnapshot? secondResponse = null;
            var secondCompletedBeforeRelease = false;
            try
            {
                await outerReachedTransport.Task.WaitAsync(TimeSpan.FromSeconds(5));
                second = application.SendAsync();
                var completed = await Task.WhenAny(
                    second,
                    Task.Delay(TimeSpan.FromSeconds(5)));
                secondCompletedBeforeRelease = ReferenceEquals(completed, second);
                if (secondCompletedBeforeRelease)
                {
                    secondResponse = await second;
                }
            }
            finally
            {
                allowFirstTransportWrite.TrySetResult(true);
            }

            var firstResponse = await first;
            secondResponse ??= await second!;
            Assert.True(
                secondCompletedBeforeRelease,
                "the provisional response retained its same-key representation gate");
            Assert.Equal(2, Volatile.Read(ref fullSourceResponses));
            foreach (var response in new[] { firstResponse, secondResponse })
            {
                Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
                Assert.Equal(response.Body.Length.ToString(), response.Header("Content-Length"));
                Assert.Contains(
                    "plugin=\"Jellyfin Refresh Kit\"",
                    response.BodyText,
                    StringComparison.Ordinal);
                Assert.Contains(
                    "/jellyfin-enhanced.js",
                    response.BodyText,
                    StringComparison.Ordinal);
                Assert.Contains(
                    "no-store",
                    response.Header("Cache-Control"),
                    StringComparison.OrdinalIgnoreCase);
                Assert.Equal(string.Empty, response.Header("ETag"));
            }
        }

        [Fact]
        public async Task OuterIdentityBuffer_CallbackRegisteredBeforeNextRetainsPrecedence()
        {
            var callbacks = new List<string>();
            var source = Encoding.UTF8.GetBytes(
                "<html><body><main>outer callback precedence</main></body></html>");
            var outer = new NestedIdentityBufferingStartupFilter(
                "<script src=\"/jellyfin-enhanced.js\"></script>",
                ownsFinalRepresentation: true,
                outerOnStarting: context =>
                {
                    callbacks.Add("outer");
                    context.Response.StatusCode = StatusCodes.Status429TooManyRequests;
                    context.Response.ContentType = "application/xhtml+xml";
                    context.Response.Headers["X-Outer-Saw-ETag"] =
                        string.IsNullOrEmpty(context.Response.Headers["ETag"].ToString())
                            ? "absent"
                            : "present";
                    context.Response.Headers["X-Outer-Saw-Cache-Control"] =
                        context.Response.Headers["Cache-Control"];
                });
            await using var application = await CreateNestedKestrelApplicationAsync(
                async context =>
                {
                    context.Response.ContentType = "text/html; charset=utf-8";
                    context.Response.ContentLength = source.Length;
                    context.Response.Headers["Cache-Control"] = "no-cache";
                    context.Response.Headers["ETag"] = "\"source\"";
                    context.Response.OnStarting(() =>
                    {
                        callbacks.Add("source");
                        context.Response.ContentType = null;
                        return Task.CompletedTask;
                    });
                    await context.Response.Body.WriteAsync(source).ConfigureAwait(false);
                },
                outer);

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status429TooManyRequests, response.StatusCode);
            Assert.Equal("application/xhtml+xml", response.Header("Content-Type"));
            Assert.Equal(response.Body.Length.ToString(), response.Header("Content-Length"));
            Assert.Equal("absent", response.Header("X-Outer-Saw-ETag"));
            Assert.Contains(
                "no-store",
                response.Header("X-Outer-Saw-Cache-Control"),
                StringComparison.OrdinalIgnoreCase);
            Assert.Equal(new[] { "source", "outer" }, callbacks);
        }

        [Fact]
        public async Task LateStatusWithChangedFraming_DoesNotReceiveTheSourceBody()
        {
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>must not escape</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.ContentLength = body.Length;
                context.Response.OnStarting(() =>
                {
                    context.Response.StatusCode = StatusCodes.Status403Forbidden;
                    context.Response.ContentLength = 0;
                    return Task.CompletedTask;
                });
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status403Forbidden, response.StatusCode);
            Assert.Empty(response.Body);
            Assert.Contains("no-store", response.Header("Cache-Control"), StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public async Task ExplicitlyStartedSource304_IsMappedBackToTheCachedRepresentation()
        {
            var fullResponses = 0;
            var explicitRevalidations = 0;
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                const string SourceETag = "\"source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref explicitRevalidations);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    await context.Response.StartAsync().ConfigureAwait(false);
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>cached shell</main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var first = await application.SendAsync();
            var revalidated = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, revalidated.StatusCode);
            Assert.Equal(first.Body, revalidated.Body);
            Assert.Contains("Jellyfin Refresh Kit", revalidated.BodyText, StringComparison.Ordinal);
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref explicitRevalidations));
        }

        [Fact]
        public async Task FullNoStoreFallback_EvictsAnOlderInjectedRepresentation()
        {
            var invocation = 0;
            var fallback = false;
            var staleValidatorSeen = 0;
            using var application = CreateApplication(async context =>
            {
                var current = Interlocked.Increment(ref invocation);
                if (!fallback
                    && context.Request.Headers["If-None-Match"].ToString().Contains(
                        "\"v1\"",
                        StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref staleValidatorSeen);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                var body = Encoding.UTF8.GetBytes(fallback
                    ? "<html><body><main>no usable body close</main>"
                    : "<html><body><main data-invocation=\""
                        + current
                        + "\"></main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = fallback ? "no-store" : "no-cache";
                context.Response.Headers["ETag"] = fallback ? "\"v2\"" : "\"v1\"";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            await application.SendAsync();
            fallback = true;
            var unrewritable = await application.SendAsync();
            fallback = false;
            var afterFallback = await application.SendAsync();

            Assert.DoesNotContain("Jellyfin Refresh Kit", unrewritable.BodyText, StringComparison.Ordinal);
            Assert.Contains("data-invocation=\"3\"", afterFallback.BodyText, StringComparison.Ordinal);
            Assert.Equal(0, Volatile.Read(ref staleValidatorSeen));
        }

        [Fact]
        public async Task SynthesizedFallback304_NormalizesManagedConnectionNominations()
        {
            var body = Encoding.UTF8.GetBytes("{\"source\":true}");
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "application/json";
                context.Response.Headers["ETag"] = "\"source\"";
                context.Response.Headers["Vary"] = "X-Tenant";
                context.Response.Headers["Connection"] = "Vary, Upgrade";
                context.Response.Headers["TE"] = "trailers";
                context.Response.Headers["Trailer"] = "Content-Digest";
                context.Response.Headers["Transfer-Encoding"] = "chunked";
                context.Response.Headers["Upgrade"] = "websocket";
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync(headers: new Dictionary<string, string>
            {
                ["If-None-Match"] = "\"source\"",
                ["X-Tenant"] = "A",
            });

            Assert.Equal(StatusCodes.Status304NotModified, response.StatusCode);
            Assert.Empty(response.Body);
            Assert.Equal(string.Empty, response.Header("Connection"));
            Assert.Equal(string.Empty, response.Header("TE"));
            Assert.Equal(string.Empty, response.Header("Trailer"));
            Assert.Equal(string.Empty, response.Header("Transfer-Encoding"));
            Assert.Equal(string.Empty, response.Header("Upgrade"));
            Assert.Equal("X-Tenant", response.Header("Vary"));
            Assert.Contains("no-store", response.Header("Cache-Control"), StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public async Task TransformedResponse_RemovesApplicationTransferEncodingBeforeFraming()
        {
            await using var application = await CreateKestrelApplicationAsync(async context =>
            {
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main>chunked source</main></body></html>");
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Transfer-Encoding"] = "chunked";
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            var response = await application.SendAsync();

            Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
            Assert.Contains("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
            Assert.Equal(string.Empty, response.Header("Transfer-Encoding"));
            Assert.Equal(response.Body.Length.ToString(), response.Header("Content-Length"));
        }

        [Theory]
        [InlineData("content-encoding")]
        [InlineData("content-type")]
        [InlineData("etag")]
        public async Task ChangedSource304Metadata_EvictsTheRetainedRepresentation(string dimension)
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            using var application = CreateApplication(async context =>
            {
                const string SourceETag = "\"source\"";
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    switch (dimension)
                    {
                        case "content-encoding":
                            context.Response.Headers["Content-Encoding"] = "br";
                            break;
                        case "content-type":
                            context.Response.ContentType = "application/octet-stream";
                            break;
                        case "etag":
                            context.Response.Headers["ETag"] = "\"changed\"";
                            break;
                    }

                    return;
                }

                var sequence = Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><main data-sequence=\"" + sequence + "\"></main></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body).ConfigureAwait(false);
            });

            await application.SendAsync();
            var changed = await application.SendAsync();
            var afterEviction = await application.SendAsync();

            Assert.Contains("no-store", changed.Header("Cache-Control"), StringComparison.OrdinalIgnoreCase);
            Assert.Contains("data-sequence=\"2\"", afterEviction.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(1, Volatile.Read(ref conditionalResponses));
        }

        [Fact]
        public async Task SameTargetConcurrentRequests_AreSingleFlightAndShareInjectedRepresentation()
        {
            const int RequestCount = 12;
            var fullResponses = 0;
            var conditionalResponses = 0;
            var activeSourceRequests = 0;
            var maximumActiveSourceRequests = 0;
            var activityLock = new object();
            using var application = CreateApplication(async context =>
            {
                var active = Interlocked.Increment(ref activeSourceRequests);
                lock (activityLock)
                {
                    maximumActiveSourceRequests = Math.Max(maximumActiveSourceRequests, active);
                }

                try
                {
                    await Task.Delay(25);
                    const string SourceETag = "\"single-flight-source\"";
                    context.Response.ContentType = "text/html; charset=utf-8";
                    context.Response.Headers["Cache-Control"] = "no-cache";
                    context.Response.Headers["ETag"] = SourceETag;
                    if (context.Request.Headers["If-None-Match"].ToString().Contains(
                        SourceETag,
                        StringComparison.Ordinal))
                    {
                        Interlocked.Increment(ref conditionalResponses);
                        context.Response.StatusCode = StatusCodes.Status304NotModified;
                        return;
                    }

                    Interlocked.Increment(ref fullResponses);
                    var body = Encoding.UTF8.GetBytes(
                        "<html><body><main>single flight</main></body></html>");
                    context.Response.ContentLength = body.Length;
                    await context.Response.Body.WriteAsync(body);
                }
                finally
                {
                    Interlocked.Decrement(ref activeSourceRequests);
                }
            });

            var start = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var requests = Enumerable.Range(0, RequestCount)
                .Select(async _ =>
                {
                    await start.Task;
                    return await application.SendAsync();
                })
                .ToArray();
            start.SetResult(true);
            var responses = await Task.WhenAll(requests);

            Assert.Equal(1, Volatile.Read(ref maximumActiveSourceRequests));
            Assert.Equal(1, Volatile.Read(ref fullResponses));
            Assert.Equal(RequestCount - 1, Volatile.Read(ref conditionalResponses));
            var expectedBody = responses[0].Body;
            foreach (var response in responses)
            {
                Assert.Equal(StatusCodes.Status200OK, response.StatusCode);
                Assert.Equal(expectedBody, response.Body);
                Assert.Contains("Jellyfin Refresh Kit", response.BodyText, StringComparison.Ordinal);
            }
        }

        [Theory]
        [InlineData(false)]
        [InlineData(true)]
        public async Task SlowDestination_DoesNotHoldTheSharedCacheGate(bool oversized)
        {
            var fullResponses = 0;
            var conditionalResponses = 0;
            var sourceEntries = 0;
            const string SourceETag = "\"slow-destination-source\"";
            var body = Encoding.UTF8.GetBytes(
                oversized
                    ? "<html><body>" + new string(
                        'x',
                        RefreshKitScriptInjectionFilter.MaxTransformBodyBytes + 1)
                        + "</body></html>"
                    : "<html><body><main>slow destination</main></body></html>");
            using var application = CreateApplication(async context =>
            {
                Interlocked.Increment(ref sourceEntries);
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body);
            });

            var writeBlocked = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var allowWrite = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var firstTask = application.SendAsync(
                decorateDestination: inner => new BlockingWriteStream(
                    inner,
                    writeBlocked,
                    allowWrite));
            Task<ResponseSnapshot>? secondTask = null;
            ResponseSnapshot? second = null;
            var secondCompletedBeforeRelease = false;
            try
            {
                await writeBlocked.Task.WaitAsync(TimeSpan.FromSeconds(5));
                secondTask = application.SendAsync();
                var completed = await Task.WhenAny(
                    secondTask,
                    Task.Delay(TimeSpan.FromSeconds(5)));
                secondCompletedBeforeRelease = ReferenceEquals(completed, secondTask);
                if (secondCompletedBeforeRelease)
                {
                    second = await secondTask;
                }
            }
            finally
            {
                allowWrite.TrySetResult(true);
            }

            var first = await firstTask;
            second ??= await secondTask!;
            Assert.True(
                secondCompletedBeforeRelease,
                "a slow response destination retained the shared representation gate");
            Assert.Equal(2, Volatile.Read(ref sourceEntries));
            Assert.Equal(StatusCodes.Status200OK, first.StatusCode);
            Assert.Equal(StatusCodes.Status200OK, second.StatusCode);
            if (oversized)
            {
                Assert.Equal(2, Volatile.Read(ref fullResponses));
                Assert.Equal(0, Volatile.Read(ref conditionalResponses));
                Assert.Equal(body, first.Body);
                Assert.Equal(body, second.Body);
            }
            else
            {
                Assert.Equal(1, Volatile.Read(ref fullResponses));
                Assert.Equal(1, Volatile.Read(ref conditionalResponses));
                Assert.Equal(first.Body, second.Body);
                Assert.Contains(
                    "Jellyfin Refresh Kit",
                    second.BodyText,
                    StringComparison.Ordinal);
            }
        }

        [Fact]
        public async Task ThirdPartyStampingFlag_IsPartOfTheRepresentationIdentity()
        {
            var stampingEnabled = true;
            var fullResponses = 0;
            var conditionalResponses = 0;
            const string SourceETag = "\"third-party-source\"";
            var options = new RefreshKitOptions
            {
                PluginName = PluginServiceRegistrator.PluginName,
                BasePath = PluginServiceRegistrator.BasePath,
                ScriptPaths = new[] { "kit.js" },
                VersionProvider = () => "g-cache-identity",
                ExtraAttributes = _ => "data-third-party-stamping=\""
                    + (stampingEnabled ? "true" : "false")
                    + "\"",
                HtmlPostProcessWithTags = PluginServiceRegistrator.StampThirdPartyTags,
            };
            using var application = CreateApplication(async context =>
            {
                context.Response.ContentType = "text/html; charset=utf-8";
                context.Response.Headers["Cache-Control"] = "no-cache";
                context.Response.Headers["ETag"] = SourceETag;
                if (context.Request.Headers["If-None-Match"].ToString().Contains(
                    SourceETag,
                    StringComparison.Ordinal))
                {
                    Interlocked.Increment(ref conditionalResponses);
                    context.Response.StatusCode = StatusCodes.Status304NotModified;
                    return;
                }

                Interlocked.Increment(ref fullResponses);
                var body = Encoding.UTF8.GetBytes(
                    "<html><body><script src=\"/third-party.js\"></script></body></html>");
                context.Response.ContentLength = body.Length;
                await context.Response.Body.WriteAsync(body);
            }, options);

            var stamped = await application.SendAsync();
            Assert.Contains(
                "/third-party.js?rkv=g-cache-identity",
                stamped.BodyText,
                StringComparison.Ordinal);

            stampingEnabled = false;
            var unstamped = await application.SendAsync();
            Assert.Contains(
                "src=\"/third-party.js\"",
                unstamped.BodyText,
                StringComparison.Ordinal);
            Assert.DoesNotContain("/third-party.js?rkv=", unstamped.BodyText, StringComparison.Ordinal);
            Assert.Equal(2, Volatile.Read(ref fullResponses));
            Assert.Equal(0, Volatile.Read(ref conditionalResponses));
        }

        private static MiddlewareApplication CreateApplication(
            RequestDelegate source,
            RefreshKitOptions? options = null)
        {
            var services = new ServiceCollection();
            AddTestServices(services, options);
            var serviceProvider = services.BuildServiceProvider();
            var filter = serviceProvider
                .GetServices<IStartupFilter>()
                .OfType<RefreshKitScriptInjectionFilter>()
                .Single();
            var builder = new ApplicationBuilder(serviceProvider);
            filter.Configure(application =>
            {
                application.UseResponseCompression();
                application.Run(source);
            })(builder);
            return new MiddlewareApplication(builder.Build(), serviceProvider);
        }

        private static async Task<KestrelMiddlewareApplication> CreateKestrelApplicationAsync(
            RequestDelegate source,
            params IStartupFilter[] additionalStartupFilters)
        {
            var builder = WebApplication.CreateBuilder();
            builder.WebHost.ConfigureKestrel(options =>
                options.Listen(IPAddress.Loopback, 0));
            AddTestServices(builder.Services);
            foreach (var startupFilter in additionalStartupFilters)
            {
                builder.Services.AddSingleton(startupFilter);
            }

            var host = builder.Build();
            host.UseResponseCompression();
            host.Run(source);
            await host.StartAsync().ConfigureAwait(false);
            var address = host.Services
                .GetRequiredService<IServer>()
                .Features
                .Get<IServerAddressesFeature>()!
                .Addresses
                .Single();
            return new KestrelMiddlewareApplication(host, new Uri(address));
        }

        private static async Task<KestrelMiddlewareApplication> CreateNestedKestrelApplicationAsync(
            RequestDelegate source,
            IStartupFilter outerStartupFilter,
            params IStartupFilter[] innerStartupFilters)
        {
            var builder = WebApplication.CreateBuilder();
            builder.WebHost.ConfigureKestrel(options =>
                options.Listen(IPAddress.Loopback, 0));
            builder.Services.AddSingleton(outerStartupFilter);
            AddTestServices(builder.Services);
            foreach (var startupFilter in innerStartupFilters)
            {
                builder.Services.AddSingleton(startupFilter);
            }

            var host = builder.Build();
            host.UseResponseCompression();
            host.Run(source);
            await host.StartAsync().ConfigureAwait(false);
            var address = host.Services
                .GetRequiredService<IServer>()
                .Features
                .Get<IServerAddressesFeature>()!
                .Addresses
                .Single();
            return new KestrelMiddlewareApplication(host, new Uri(address));
        }

        private sealed class NestedIdentityBufferingStartupFilter : IStartupFilter
        {
            private readonly string _scriptTag;
            private readonly bool _ownsFinalRepresentation;
            private readonly Func<HttpContext, Task>? _beforeFinalWrite;
            private readonly Action<HttpContext>? _outerOnStarting;

            public NestedIdentityBufferingStartupFilter(
                string scriptTag,
                bool ownsFinalRepresentation = false,
                Func<HttpContext, Task>? beforeFinalWrite = null,
                Action<HttpContext>? outerOnStarting = null)
            {
                _scriptTag = scriptTag;
                _ownsFinalRepresentation = ownsFinalRepresentation;
                _beforeFinalWrite = beforeFinalWrite;
                _outerOnStarting = outerOnStarting;
            }

            public string InnerCacheControl { get; private set; } = string.Empty;

            public string InnerETag { get; private set; } = string.Empty;

            public bool InnerHasStarted { get; private set; }

            public int FinalTrailerCount { get; private set; }

            public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next)
            {
                return app =>
                {
                    app.Use(InvokeAsync);
                    next(app);
                };
            }

            private async Task InvokeAsync(HttpContext context, Func<Task> next)
            {
                if (_ownsFinalRepresentation && !HttpMethods.IsGet(context.Request.Method))
                {
                    await next().ConfigureAwait(false);
                    return;
                }

                if (_outerOnStarting != null)
                {
                    context.Response.OnStarting(() =>
                    {
                        _outerOnStarting(context);
                        return Task.CompletedTask;
                    });
                }

                context.Request.Headers.Remove("Accept-Encoding");
                if (_ownsFinalRepresentation)
                {
                    context.Request.Headers.Remove("Range");
                    context.Request.Headers.Remove("If-Range");
                }

                var originalBody = context.Response.Body;
                var restored = false;
                try
                {
                    using var buffer = new MemoryStream();
                    context.Response.Body = buffer;
                    await next().ConfigureAwait(false);
                    if (_ownsFinalRepresentation)
                    {
                        // Jellyfin Enhanced restores its outer feature before it
                        // transforms and writes the representation returned by RK.
                        context.Response.Body = originalBody;
                        restored = true;
                        InnerHasStarted = context.Response.HasStarted;
                        InnerCacheControl = context.Response.Headers["Cache-Control"].ToString();
                        InnerETag = context.Response.Headers["ETag"].ToString();
                    }

                    buffer.Position = 0;
                    var isHtml = context.Response.StatusCode == StatusCodes.Status200OK
                        && (context.Response.ContentType?.Contains(
                            "text/html",
                            StringComparison.OrdinalIgnoreCase) ?? false);
                    if (_ownsFinalRepresentation && !isHtml)
                    {
                        await buffer.CopyToAsync(originalBody).ConfigureAwait(false);
                        CaptureFinalTrailers(context);
                        return;
                    }

                    string html;
                    using (var reader = new StreamReader(
                        buffer,
                        Encoding.UTF8,
                        detectEncodingFromByteOrderMarks: true,
                        bufferSize: 1024,
                        leaveOpen: true))
                    {
                        html = await reader.ReadToEndAsync().ConfigureAwait(false);
                    }

                    var bodyClose = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
                    var modified = bodyClose < 0
                        ? html
                        : html.Insert(bodyClose, _scriptTag);
                    var bytes = Encoding.UTF8.GetBytes(modified);
                    if (_ownsFinalRepresentation)
                    {
                        context.Response.ContentType = "text/html;charset=utf-8";
                        context.Response.Headers.Remove("ETag");
                        context.Response.Headers.Remove("Last-Modified");
                        context.Response.Headers.Remove("Accept-Ranges");
                    }

                    context.Response.ContentLength = bytes.Length;
                    if (_beforeFinalWrite != null)
                    {
                        await _beforeFinalWrite(context).ConfigureAwait(false);
                    }

                    await originalBody.WriteAsync(bytes).ConfigureAwait(false);
                    CaptureFinalTrailers(context);
                }
                finally
                {
                    if (!restored)
                    {
                        context.Response.Body = originalBody;
                    }
                }
            }

            private void CaptureFinalTrailers(HttpContext context)
            {
                FinalTrailerCount = context.Features
                    .Get<IHttpResponseTrailersFeature>()?
                    .Trailers?
                    .Count ?? 0;
            }
        }

        private static void AddTestServices(
            IServiceCollection services,
            RefreshKitOptions? options = null)
        {
            services.AddLogging();
            services.AddResponseCompression(options =>
            {
                options.EnableForHttps = false;
                options.MimeTypes = new[] { "text/html" };
                options.Providers.Add<GzipCompressionProvider>();
            });
            options ??= new RefreshKitOptions
            {
                PluginName = "Jellyfin Refresh Kit",
                BasePath = "/RefreshKit",
                ScriptPaths = new[] { "kit.js" },
            };
            RefreshKit.AddRefreshKit(services, options);
        }

        private static string DecompressGzip(byte[] body)
        {
            using var input = new MemoryStream(body, writable: false);
            using var gzip = new GZipStream(input, CompressionMode.Decompress);
            using var reader = new StreamReader(gzip, Encoding.UTF8);
            return reader.ReadToEnd();
        }

        private sealed class MiddlewareApplication : IDisposable
        {
            private readonly RequestDelegate _application;
            private readonly ServiceProvider _serviceProvider;

            public MiddlewareApplication(RequestDelegate application, ServiceProvider serviceProvider)
            {
                _application = application;
                _serviceProvider = serviceProvider;
            }

            public async Task<ResponseSnapshot> SendAsync(
                string scheme = "http",
                string method = "GET",
                IReadOnlyDictionary<string, string>? headers = null,
                string host = "jellyfin.example",
                string pathBase = "",
                string path = "/web/index.html",
                string queryString = "",
                string? rawTarget = null,
                ClaimsPrincipal? user = null,
                Action<HttpContext>? configure = null,
                Func<Stream, Stream>? decorateDestination = null)
            {
                var context = new DefaultHttpContext
                {
                    RequestServices = _serviceProvider,
                };
                var responseFeature = new CallbackResponseFeature();
                context.Features.Set<IHttpResponseFeature>(responseFeature);
                context.Request.Scheme = scheme;
                context.Request.Method = method;
                context.Request.Host = new HostString(host);
                context.Request.PathBase = new PathString(pathBase);
                context.Request.Path = new PathString(path);
                context.Request.QueryString = new QueryString(queryString);
                context.Features.Get<IHttpRequestFeature>()!.RawTarget = rawTarget
                    ?? pathBase + path + queryString;
                if (user != null)
                {
                    context.User = user;
                }

                configure?.Invoke(context);
                if (headers != null)
                {
                    foreach (var header in headers)
                    {
                        context.Request.Headers[header.Key] = header.Value;
                    }
                }

                using var body = new MemoryStream();
                var destination = decorateDestination?.Invoke(body) ?? body;
                using var transport = new CallbackFiringStream(destination, responseFeature);
                context.Response.Body = transport;
                await _application(context).ConfigureAwait(false);
                await context.Response.CompleteAsync().ConfigureAwait(false);
                await responseFeature.FireOnCompletedAsync().ConfigureAwait(false);
                return new ResponseSnapshot(
                    context.Response.StatusCode,
                    context.Response.Headers.ToDictionary(
                        pair => pair.Key,
                        pair => pair.Value,
                        StringComparer.OrdinalIgnoreCase),
                    body.ToArray());
            }

            public void Dispose() => _serviceProvider.Dispose();
        }

        private sealed class BlockingWriteStream : Stream
        {
            private readonly Stream _inner;
            private readonly TaskCompletionSource<bool> _writeBlocked;
            private readonly TaskCompletionSource<bool> _allowWrite;
            private int _blockedOnce;

            public BlockingWriteStream(
                Stream inner,
                TaskCompletionSource<bool> writeBlocked,
                TaskCompletionSource<bool> allowWrite)
            {
                _inner = inner;
                _writeBlocked = writeBlocked;
                _allowWrite = allowWrite;
            }

            public override bool CanRead => _inner.CanRead;

            public override bool CanSeek => _inner.CanSeek;

            public override bool CanWrite => _inner.CanWrite;

            public override long Length => _inner.Length;

            public override long Position
            {
                get => _inner.Position;
                set => _inner.Position = value;
            }

            public override void Flush() => _inner.Flush();

            public override Task FlushAsync(CancellationToken cancellationToken) =>
                _inner.FlushAsync(cancellationToken);

            public override int Read(byte[] buffer, int offset, int count) =>
                _inner.Read(buffer, offset, count);

            public override long Seek(long offset, SeekOrigin origin) =>
                _inner.Seek(offset, origin);

            public override void SetLength(long value) => _inner.SetLength(value);

            public override void Write(byte[] buffer, int offset, int count)
            {
                BlockOnce(count);
                _inner.Write(buffer, offset, count);
            }

            public override void Write(ReadOnlySpan<byte> buffer)
            {
                BlockOnce(buffer.Length);
                _inner.Write(buffer);
            }

            public override async Task WriteAsync(
                byte[] buffer,
                int offset,
                int count,
                CancellationToken cancellationToken)
            {
                await BlockOnceAsync(count, cancellationToken);
                await _inner.WriteAsync(
                    buffer.AsMemory(offset, count),
                    cancellationToken);
            }

            public override async ValueTask WriteAsync(
                ReadOnlyMemory<byte> buffer,
                CancellationToken cancellationToken = default)
            {
                await BlockOnceAsync(buffer.Length, cancellationToken);
                await _inner.WriteAsync(buffer, cancellationToken);
            }

            private void BlockOnce(int count)
            {
                if (count <= 0 || Interlocked.Exchange(ref _blockedOnce, 1) != 0)
                {
                    return;
                }

                _writeBlocked.TrySetResult(true);
                _allowWrite.Task.GetAwaiter().GetResult();
            }

            private async Task BlockOnceAsync(int count, CancellationToken cancellationToken)
            {
                if (count <= 0 || Interlocked.Exchange(ref _blockedOnce, 1) != 0)
                {
                    return;
                }

                _writeBlocked.TrySetResult(true);
                await _allowWrite.Task.WaitAsync(cancellationToken);
            }

            protected override void Dispose(bool disposing)
            {
                // The caller owns the capture stream.
                base.Dispose(disposing);
            }
        }

        private sealed class CallbackResponseFeature : IHttpResponseFeature
        {
            private readonly List<(Func<object, Task> Callback, object State)> _starting = new();
            private readonly List<(Func<object, Task> Callback, object State)> _completed = new();

            public int StatusCode { get; set; } = StatusCodes.Status200OK;

            public string? ReasonPhrase { get; set; }

            public IHeaderDictionary Headers { get; set; } = new HeaderDictionary();

            public Stream Body { get; set; } = Stream.Null;

            public bool HasStarted { get; private set; }

            public void OnStarting(Func<object, Task> callback, object state)
            {
                if (HasStarted)
                {
                    throw new InvalidOperationException("The response has already started.");
                }

                _starting.Add((callback, state));
            }

            public void OnCompleted(Func<object, Task> callback, object state) =>
                _completed.Add((callback, state));

            public async Task FireOnStartingAsync()
            {
                if (HasStarted)
                {
                    return;
                }

                for (var index = _starting.Count - 1; index >= 0; index--)
                {
                    await _starting[index].Callback(_starting[index].State).ConfigureAwait(false);
                }

                HasStarted = true;
            }

            public async Task FireOnCompletedAsync()
            {
                for (var index = _completed.Count - 1; index >= 0; index--)
                {
                    await _completed[index].Callback(_completed[index].State).ConfigureAwait(false);
                }
            }
        }

        private sealed class CallbackFiringStream : Stream
        {
            private readonly Stream _inner;
            private readonly CallbackResponseFeature _responseFeature;

            public CallbackFiringStream(
                Stream inner,
                CallbackResponseFeature responseFeature)
            {
                _inner = inner;
                _responseFeature = responseFeature;
            }

            public override bool CanRead => _inner.CanRead;

            public override bool CanSeek => _inner.CanSeek;

            public override bool CanWrite => _inner.CanWrite;

            public override long Length => _inner.Length;

            public override long Position
            {
                get => _inner.Position;
                set => _inner.Position = value;
            }

            public override void Flush()
            {
                _responseFeature.FireOnStartingAsync().GetAwaiter().GetResult();
                _inner.Flush();
            }

            public override async Task FlushAsync(CancellationToken cancellationToken)
            {
                await _responseFeature.FireOnStartingAsync().ConfigureAwait(false);
                await _inner.FlushAsync(cancellationToken).ConfigureAwait(false);
            }

            public override int Read(byte[] buffer, int offset, int count) =>
                _inner.Read(buffer, offset, count);

            public override long Seek(long offset, SeekOrigin origin) =>
                _inner.Seek(offset, origin);

            public override void SetLength(long value) => _inner.SetLength(value);

            public override void Write(byte[] buffer, int offset, int count)
            {
                _responseFeature.FireOnStartingAsync().GetAwaiter().GetResult();
                _inner.Write(buffer, offset, count);
            }

            public override async Task WriteAsync(
                byte[] buffer,
                int offset,
                int count,
                CancellationToken cancellationToken)
            {
                await _responseFeature.FireOnStartingAsync().ConfigureAwait(false);
                await _inner.WriteAsync(
                    buffer.AsMemory(offset, count),
                    cancellationToken).ConfigureAwait(false);
            }

            public override async ValueTask WriteAsync(
                ReadOnlyMemory<byte> buffer,
                CancellationToken cancellationToken = default)
            {
                await _responseFeature.FireOnStartingAsync().ConfigureAwait(false);
                await _inner.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
            }

            protected override void Dispose(bool disposing)
            {
                // The caller owns the underlying capture stream.
                base.Dispose(disposing);
            }
        }

        private sealed class KestrelMiddlewareApplication : IAsyncDisposable
        {
            private readonly WebApplication _host;
            private readonly HttpClient _client;

            public KestrelMiddlewareApplication(WebApplication host, Uri baseAddress)
            {
                _host = host;
                _client = new HttpClient(new HttpClientHandler
                {
                    AutomaticDecompression = DecompressionMethods.None,
                    UseCookies = false,
                })
                {
                    BaseAddress = baseAddress,
                };
            }

            public async Task<ResponseSnapshot> SendAsync(
                string method = "GET",
                IReadOnlyDictionary<string, string>? headers = null,
                string target = "/web/index.html")
            {
                using var request = new HttpRequestMessage(new HttpMethod(method), target);
                if (headers != null)
                {
                    foreach (var header in headers)
                    {
                        request.Headers.TryAddWithoutValidation(header.Key, header.Value);
                    }
                }

                using var response = await _client.SendAsync(
                    request,
                    HttpCompletionOption.ResponseContentRead).ConfigureAwait(false);
                var responseHeaders = response.Headers
                    .Concat(response.Content.Headers)
                    .ToDictionary(
                        pair => pair.Key,
                        pair => new StringValues(pair.Value.ToArray()),
                        StringComparer.OrdinalIgnoreCase);
                return new ResponseSnapshot(
                    (int)response.StatusCode,
                    responseHeaders,
                    await response.Content.ReadAsByteArrayAsync().ConfigureAwait(false));
            }

            public async ValueTask DisposeAsync()
            {
                _client.Dispose();
                await _host.StopAsync().ConfigureAwait(false);
                await _host.DisposeAsync().ConfigureAwait(false);
            }
        }

        private sealed class ResponseSnapshot
        {
            private readonly IReadOnlyDictionary<string, StringValues> _headers;

            public ResponseSnapshot(
                int statusCode,
                IReadOnlyDictionary<string, StringValues> headers,
                byte[] body)
            {
                StatusCode = statusCode;
                _headers = headers;
                Body = body;
            }

            public int StatusCode { get; }

            public byte[] Body { get; }

            public string BodyText => Encoding.UTF8.GetString(Body);

            public string Header(string name) =>
                _headers.TryGetValue(name, out var value) ? value.ToString() : string.Empty;
        }
    }
}
