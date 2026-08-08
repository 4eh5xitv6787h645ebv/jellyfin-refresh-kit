using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Jellyfin.Plugin.RefreshKit;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;

internal static class Program
{
    private const string Generation = "g-benchmark012345";
    private const string Schema = "refresh-kit-benchmark-v1";
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    private static async Task Main()
    {
        Emit(new
        {
            schema = Schema,
            record = "environment",
            component = "server",
            capturedAtUtc = DateTimeOffset.UtcNow,
            sourceRevision = Environment.GetEnvironmentVariable("RK_BENCH_SOURCE_REVISION") ?? "unknown",
            sourceDirty = Environment.GetEnvironmentVariable("RK_BENCH_SOURCE_DIRTY") ?? "unknown",
            framework = RuntimeInformation.FrameworkDescription,
            os = RuntimeInformation.OSDescription,
            architecture = RuntimeInformation.ProcessArchitecture.ToString(),
            processorCount = Environment.ProcessorCount,
            stopwatchFrequency = Stopwatch.Frequency,
            serverGc = System.Runtime.GCSettings.IsServerGC,
            latencyMode = System.Runtime.GCSettings.LatencyMode.ToString(),
        });

        await WarmJit().ConfigureAwait(false);
        foreach (var count in new[] { 5, 25, 50, 100 })
        {
            MeasureGeneration(count);
        }

        foreach (var row in new[]
        {
            (Plugins: 5, Kib: 64),
            (Plugins: 25, Kib: 256),
            (Plugins: 50, Kib: 1024),
            (Plugins: 100, Kib: 1900),
        })
        {
            var html = MakeHtml(row.Plugins, row.Kib * 1024);
            MeasureStamp(row.Plugins, html);
            await MeasureMiddleware(row.Plugins, html, gzip: false).ConfigureAwait(false);
            await MeasureMiddleware(row.Plugins, html, gzip: true).ConfigureAwait(false);
        }
    }

    private static async Task WarmJit()
    {
        _ = ThirdPartyTagStamper.Stamp(
            "<html><body><script src='/x.js'></script></body></html>",
            Generation,
            null);
        await MeasureMiddlewareCore(
            pluginCount: 1,
            MakeHtml(1, 8192),
            gzip: false,
            coldIterations: 1,
            warmIterations: 2,
            emit: false).ConfigureAwait(false);
    }

    private static void MeasureGeneration(int pluginCount)
    {
        var fixture = Path.Combine(
            Path.GetTempPath(),
            "refresh-kit-provider-benchmark-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(fixture);
        var configurations = Path.Combine(fixture, "configurations");
        Directory.CreateDirectory(configurations);
        try
        {
            var descriptors = new List<ActivePluginDescriptor>(pluginCount);
            var payload = new string('a', 8192);
            for (var pluginIndex = 0; pluginIndex < pluginCount; pluginIndex++)
            {
                var directory = Path.Combine(
                    fixture,
                    "plugin-" + pluginIndex.ToString("D3", CultureInfo.InvariantCulture));
                var web = Path.Combine(directory, "web");
                var nested = Path.Combine(web, "nested");
                Directory.CreateDirectory(nested);
                for (var assetIndex = 0; assetIndex < 8; assetIndex++)
                {
                    var parent = assetIndex % 2 == 0 ? web : nested;
                    File.WriteAllText(
                        Path.Combine(parent, $"asset-{assetIndex:D2}.js"),
                        payload,
                        Encoding.UTF8);
                }

                var configName = $"plugin-{pluginIndex:D3}.xml";
                File.WriteAllText(
                    Path.Combine(configurations, configName),
                    "<Config><Value>benchmark</Value></Config>",
                    Encoding.UTF8);
                var module = new LoadedModuleFingerprint(
                    Path.Combine(directory, $"plugin-{pluginIndex:D3}.dll"),
                    $"Benchmark.Plugin.{pluginIndex:D3}",
                    "1.0.0.0",
                    Guid.Parse($"00000000-0000-0000-0000-{pluginIndex + 1:D12}"));
                descriptors.Add(new ActivePluginDescriptor(
                    directory,
                    Guid.Parse($"10000000-0000-0000-0000-{pluginIndex + 1:D12}").ToString("D"),
                    "1.0.0.0",
                    "Active",
                    new[] { module },
                    new[] { configName }));
            }

            var provider = new PluginGenerationProvider(() => descriptors, configurations);
            _ = provider.Generation;

            const int rescanCount = 9;
            var scanSamples = new List<double>(rescanCount);
            CollectNow();
            var allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
            for (var index = 0; index < rescanCount; index++)
            {
                provider.Invalidate();
                var started = Stopwatch.GetTimestamp();
                _ = provider.Generation;
                scanSamples.Add(ToMicroseconds(Stopwatch.GetTimestamp() - started));
            }

            var allocatedAfter = GC.GetAllocatedBytesForCurrentThread();
            EmitMeasurement(
                scenario: "generation.warm-filesystem-rescan",
                scale: pluginCount,
                unit: "microseconds",
                scanSamples,
                allocatedBytesPerOperation: (allocatedAfter - allocatedBefore) / (double)rescanCount,
                new
                {
                    operations = rescanCount,
                    assetsPerPlugin = 8,
                    bytesPerAsset = 8192,
                    configurationsPerPlugin = 1,
                });

            const int lookupCount = 100_000;
            const int batches = 10;
            var lookupSamples = new List<double>(batches);
            var checksum = 0;
            CollectNow();
            allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
            for (var batch = 0; batch < batches; batch++)
            {
                var started = Stopwatch.GetTimestamp();
                for (var index = 0; index < lookupCount / batches; index++)
                {
                    checksum ^= provider.Generation.Length;
                }

                lookupSamples.Add(
                    ToMicroseconds(Stopwatch.GetTimestamp() - started) / (lookupCount / batches));
            }

            allocatedAfter = GC.GetAllocatedBytesForCurrentThread();
            GC.KeepAlive(checksum);
            EmitMeasurement(
                scenario: "generation.ttl-cache-hit",
                scale: pluginCount,
                unit: "microseconds-per-lookup",
                lookupSamples,
                allocatedBytesPerOperation: (allocatedAfter - allocatedBefore) / (double)lookupCount,
                new
                {
                    operations = lookupCount,
                    lockShape = "single-threaded-uncontended",
                });
        }
        finally
        {
            Directory.Delete(fixture, recursive: true);
        }
    }

    private static void MeasureStamp(int pluginCount, string html)
    {
        var iterations = html.Length > 1_000_000 ? 15 : 50;
        var samples = new List<double>(iterations);
        var checksum = 0;
        CollectNow();
        var allocatedBefore = GC.GetAllocatedBytesForCurrentThread();
        for (var index = 0; index < iterations; index++)
        {
            var started = Stopwatch.GetTimestamp();
            var stamped = ThirdPartyTagStamper.Stamp(
                html,
                Generation,
                "plugin=\"Jellyfin Refresh Kit\"");
            var elapsed = ToMicroseconds(Stopwatch.GetTimestamp() - started);
            if (!stamped.Contains("rkv=" + Generation, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("third-party fixture was not stamped");
            }

            checksum ^= stamped.Length;
            samples.Add(elapsed);
        }

        var allocatedAfter = GC.GetAllocatedBytesForCurrentThread();
        GC.KeepAlive(checksum);
        EmitMeasurement(
            scenario: "html.third-party-stamp",
            scale: pluginCount,
            unit: "microseconds",
            samples,
            allocatedBytesPerOperation: (allocatedAfter - allocatedBefore) / (double)iterations,
            new
            {
                operations = iterations,
                decodedBytes = Encoding.UTF8.GetByteCount(html),
                scriptTags = pluginCount,
                stylesheetTags = pluginCount,
            });
    }

    private static Task MeasureMiddleware(int pluginCount, string html, bool gzip) =>
        MeasureMiddlewareCore(
            pluginCount,
            html,
            gzip,
            coldIterations: html.Length > 1_000_000 ? 7 : 15,
            warmIterations: 250,
            emit: true);

    private static async Task MeasureMiddlewareCore(
        int pluginCount,
        string html,
        bool gzip,
        int coldIterations,
        int warmIterations,
        bool emit)
    {
        var decodedSource = Encoding.UTF8.GetBytes(html);
        var encodedSource = gzip ? Gzip(decodedSource) : decodedSource;
        var sourceTag = "\"source-"
            + Convert.ToHexString(SHA256.HashData(encodedSource)).ToLowerInvariant()
            + "\"";
        var source200 = 0;
        var source304 = 0;
        var options = new RefreshKitOptions
        {
            PluginName = "Jellyfin Refresh Kit",
            BasePath = "RefreshKit",
            ScriptPaths = new[] { "kit.js" },
            VersionProvider = () => Generation,
            HtmlPostProcess = value => ThirdPartyTagStamper.Stamp(
                value,
                Generation,
                "plugin=\"Jellyfin Refresh Kit\""),
        };
        var filter = new RefreshKitScriptInjectionFilter(options, NullLogger.Instance);
        using var services = new ServiceCollection().BuildServiceProvider();
        var application = new ApplicationBuilder(services);
        filter.Configure(builder => builder.Run(async context =>
        {
            context.Response.ContentType = "text/html; charset=utf-8";
            context.Response.Headers.ETag = sourceTag;
            context.Response.Headers.CacheControl = "public, max-age=0";
            context.Response.Headers.Vary = "Accept-Encoding";
            if (gzip)
            {
                context.Response.Headers.ContentEncoding = "gzip";
            }

            if (context.Request.Headers.IfNoneMatch.ToString() == sourceTag)
            {
                source304++;
                context.Response.StatusCode = StatusCodes.Status304NotModified;
                return;
            }

            source200++;
            context.Response.StatusCode = StatusCodes.Status200OK;
            context.Response.ContentLength = encodedSource.Length;
            await context.Response.Body.WriteAsync(encodedSource).ConfigureAwait(false);
        }))(application);
        var pipeline = application.Build();

        async Task Invoke(string query)
        {
            var context = new DefaultHttpContext();
            var responseFeature = new CallbackResponseFeature();
            context.Features.Set<IHttpResponseFeature>(responseFeature);
            context.Request.Method = HttpMethods.Get;
            context.Request.Scheme = "http";
            context.Request.Host = new HostString("localhost", 8096);
            context.Request.Path = "/web/index.html";
            context.Request.QueryString = new QueryString(query);
            if (gzip)
            {
                context.Request.Headers.AcceptEncoding = "gzip";
            }

            var destination = new CountingStream();
            context.Response.Body = new CallbackFiringStream(destination, responseFeature);
            await pipeline(context).ConfigureAwait(false);
            await responseFeature.FireOnStartingAsync().ConfigureAwait(false);
            if (context.Response.StatusCode != StatusCodes.Status200OK
                || !context.Response.Headers.ETag.ToString().StartsWith("\"rk-", StringComparison.Ordinal)
                || destination.Length <= 0
                || context.Response.ContentLength != destination.Length)
            {
                throw new InvalidOperationException(
                    "middleware fixture did not emit a complete transformed 200 response");
            }
        }

        await Invoke("?warm=1").ConfigureAwait(false);
        var warmSamples = new List<double>(warmIterations);
        CollectNow();
        var allocatedBefore = GC.GetTotalAllocatedBytes(precise: true);
        for (var index = 0; index < warmIterations; index++)
        {
            var started = Stopwatch.GetTimestamp();
            await Invoke("?warm=1").ConfigureAwait(false);
            warmSamples.Add(ToMicroseconds(Stopwatch.GetTimestamp() - started));
        }

        var allocatedAfter = GC.GetTotalAllocatedBytes(precise: true);
        if (emit)
        {
            EmitMeasurement(
                scenario: "middleware.warm-source-304-cache",
                scale: pluginCount,
                unit: "microseconds",
                warmSamples,
                allocatedBytesPerOperation: (allocatedAfter - allocatedBefore) / (double)warmIterations,
                new
                {
                    operations = warmIterations,
                    coding = gzip ? "gzip" : "identity",
                    decodedBytes = decodedSource.Length,
                    encodedBytes = encodedSource.Length,
                    source200,
                    source304,
                });
        }

        var coldSamples = new List<double>(coldIterations);
        CollectNow();
        allocatedBefore = GC.GetTotalAllocatedBytes(precise: true);
        for (var index = 0; index < coldIterations; index++)
        {
            var started = Stopwatch.GetTimestamp();
            await Invoke("?cold=" + index.ToString(CultureInfo.InvariantCulture)).ConfigureAwait(false);
            coldSamples.Add(ToMicroseconds(Stopwatch.GetTimestamp() - started));
        }

        allocatedAfter = GC.GetTotalAllocatedBytes(precise: true);
        if (emit)
        {
            EmitMeasurement(
                scenario: "middleware.cold-transform",
                scale: pluginCount,
                unit: "microseconds",
                coldSamples,
                allocatedBytesPerOperation: (allocatedAfter - allocatedBefore) / (double)coldIterations,
                new
                {
                    operations = coldIterations,
                    coding = gzip ? "gzip" : "identity",
                    decodedBytes = decodedSource.Length,
                    encodedBytes = encodedSource.Length,
                });
        }
    }

    private static string MakeHtml(int pluginCount, int targetBytes)
    {
        var builder = new StringBuilder(targetBytes + 2048);
        builder.Append("<!doctype html><html><head>");
        for (var index = 0; index < pluginCount; index++)
        {
            builder.Append("<script src=\"/web/plugin-")
                .Append(index)
                .Append("/client.js\"></script>");
            builder.Append("<link rel=\"stylesheet\" href=\"/web/plugin-")
                .Append(index)
                .Append("/client.css\">");
        }

        builder.Append("</head><body>");
        const string suffix = "</body></html>";
        AppendDeterministicPadding(builder, Math.Max(0, targetBytes - builder.Length - suffix.Length));
        builder.Append(suffix);
        return builder.ToString();
    }

    private static void AppendDeterministicPadding(StringBuilder builder, int count)
    {
        const string alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ";
        uint state = 0x4f1bbcdc;
        for (var index = 0; index < count; index++)
        {
            state = (state * 1664525) + 1013904223;
            builder.Append(alphabet[(int)(state % alphabet.Length)]);
        }
    }

    private static byte[] Gzip(byte[] source)
    {
        using var output = new MemoryStream();
        using (var gzip = new GZipStream(output, CompressionLevel.Fastest, leaveOpen: true))
        {
            gzip.Write(source);
        }

        return output.ToArray();
    }

    private static void CollectNow()
    {
        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();
    }

    private static double ToMicroseconds(long ticks) =>
        ticks * 1_000_000.0 / Stopwatch.Frequency;

    private static void EmitMeasurement(
        string scenario,
        int scale,
        string unit,
        List<double> samples,
        double allocatedBytesPerOperation,
        object fixture)
    {
        var ordered = samples.OrderBy(value => value).ToArray();
        Emit(new
        {
            schema = Schema,
            record = "measurement",
            component = "server",
            scenario,
            scale,
            unit,
            sampleCount = ordered.Length,
            median = Median(ordered),
            p95 = NearestRank(ordered, 0.95),
            allocatedBytesPerOperation,
            samples = ordered,
            fixture,
        });
    }

    private static double Median(IReadOnlyList<double> ordered)
    {
        var middle = ordered.Count / 2;
        return ordered.Count % 2 == 0
            ? (ordered[middle - 1] + ordered[middle]) / 2
            : ordered[middle];
    }

    private static double NearestRank(IReadOnlyList<double> ordered, double percentile)
    {
        var index = Math.Clamp((int)Math.Ceiling(ordered.Count * percentile) - 1, 0, ordered.Count - 1);
        return ordered[index];
    }

    private static void Emit(object value) =>
        Console.WriteLine(JsonSerializer.Serialize(value, JsonOptions));

    private sealed class CountingStream : Stream
    {
        private long _length;

        public override bool CanRead => false;
        public override bool CanSeek => false;
        public override bool CanWrite => true;
        public override long Length => _length;
        public override long Position
        {
            get => _length;
            set => throw new NotSupportedException();
        }

        public override void Flush()
        {
        }

        public override Task FlushAsync(CancellationToken cancellationToken) => Task.CompletedTask;
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => _length = value;
        public override void Write(byte[] buffer, int offset, int count) => _length += count;

        public override Task WriteAsync(
            byte[] buffer,
            int offset,
            int count,
            CancellationToken cancellationToken)
        {
            _length += count;
            return Task.CompletedTask;
        }

        public override ValueTask WriteAsync(
            ReadOnlyMemory<byte> buffer,
            CancellationToken cancellationToken = default)
        {
            _length += buffer.Length;
            return ValueTask.CompletedTask;
        }
    }

    private sealed class CallbackResponseFeature : IHttpResponseFeature
    {
        private readonly List<(Func<object, Task> Callback, object State)> _starting = new();

        public int StatusCode { get; set; } = StatusCodes.Status200OK;
        public string? ReasonPhrase { get; set; }
        public IHeaderDictionary Headers { get; set; } = new HeaderDictionary();
        public Stream Body { get; set; } = Stream.Null;
        public bool HasStarted { get; private set; }

        public void OnStarting(Func<object, Task> callback, object state)
        {
            if (HasStarted)
            {
                throw new InvalidOperationException("response already started");
            }

            _starting.Add((callback, state));
        }

        public void OnCompleted(Func<object, Task> callback, object state)
        {
        }

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
    }

    private sealed class CallbackFiringStream : Stream
    {
        private readonly Stream _inner;
        private readonly CallbackResponseFeature _feature;

        public CallbackFiringStream(Stream inner, CallbackResponseFeature feature)
        {
            _inner = inner;
            _feature = feature;
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
            _feature.FireOnStartingAsync().GetAwaiter().GetResult();
            _inner.Flush();
        }

        public override async Task FlushAsync(CancellationToken cancellationToken)
        {
            await _feature.FireOnStartingAsync().ConfigureAwait(false);
            await _inner.FlushAsync(cancellationToken).ConfigureAwait(false);
        }

        public override int Read(byte[] buffer, int offset, int count) =>
            _inner.Read(buffer, offset, count);

        public override long Seek(long offset, SeekOrigin origin) => _inner.Seek(offset, origin);
        public override void SetLength(long value) => _inner.SetLength(value);

        public override void Write(byte[] buffer, int offset, int count)
        {
            _feature.FireOnStartingAsync().GetAwaiter().GetResult();
            _inner.Write(buffer, offset, count);
        }

        public override async Task WriteAsync(
            byte[] buffer,
            int offset,
            int count,
            CancellationToken cancellationToken)
        {
            await _feature.FireOnStartingAsync().ConfigureAwait(false);
            await _inner.WriteAsync(buffer.AsMemory(offset, count), cancellationToken).ConfigureAwait(false);
        }

        public override async ValueTask WriteAsync(
            ReadOnlyMemory<byte> buffer,
            CancellationToken cancellationToken = default)
        {
            await _feature.FireOnStartingAsync().ConfigureAwait(false);
            await _inner.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
        }
    }
}
