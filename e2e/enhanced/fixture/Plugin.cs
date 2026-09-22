using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using JellyfinRefreshKit;

#if ADOPTER
namespace EnhancedAdoptionFixture;
#else
namespace SiblingRefreshFixture;
#endif

public sealed class Plugin : BasePlugin<BasePluginConfiguration>
{
    public Plugin(IApplicationPaths p, IXmlSerializer s) : base(p, s) { }
    public override string Name => Identity.Route;
    public override Guid Id => new(Identity.Guid);
}

internal static class Identity
{
    public static bool InjectionEnabled {
        get {
            var type = AppDomain.CurrentDomain.GetAssemblies()
                .First(a => a.GetName().Name == "Jellyfin.Plugin.JellyfinEnhanced")
                .GetType("Jellyfin.Plugin.JellyfinEnhanced.JellyfinEnhanced", true)!;
            var instance = type.GetProperty("Instance")!.GetValue(null);
            var config = type.GetProperty("Configuration")!.GetValue(instance)!;
            return !(bool)config.GetType().GetProperty("DisableScriptInjectionMiddleware")!.GetValue(config)!;
        }
    }
#if ADOPTER
    public const string Route = "EnhancedAdoption";
    public const string Guid = "c39e6da2-71fe-49ac-96ce-8bd3ba780e03";
#else
    public const string Route = "SiblingRefresh";
    public const string Guid = "b9d81601-6a04-4a9a-95d5-08e250da36d0";
#endif
    public static readonly string Epoch = System.Guid.NewGuid().ToString("N");
    public static int Revision;
    public static string Key => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
        typeof(Identity).Assembly.ManifestModule.ModuleVersionId + ":" + Revision + ":" +
        AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a => a.GetName().Name == "Jellyfin.Plugin.JellyfinEnhanced")?.ManifestModule.ModuleVersionId)));
    public static string Attributes => "data-name=\"" + Route + "\" data-version-url=\"../" + Route +
        "/version\" data-version-json-field=\"CacheKey\" data-version-epoch-json-field=\"Epoch\" data-idle-seconds=\"0\" data-poll-seconds=\"15\"";
    public static RefreshKitOptions Options => new() {
        PluginName = Route, BasePath = Route, ScriptPaths = new[] { "kit.js" },
        VersionProvider = () => Key, EpochProvider = () => Epoch, ExtraAttributes = _ => Attributes
    };
}

public sealed class Services : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection s, IServerApplicationHost h)
    {
        s.AddSingleton<IStartupFilter, TraceFilter>();
#if ADOPTER
        s.AddSingleton<IStartupFilter, IndependentInjector>();
#else
        s.AddRefreshKit(Identity.Options);
#endif
    }
}

// Shared BCL-only request state proves actual middleware order across assemblies.
public sealed class TraceFilter : IStartupFilter
{
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) => app => {
        app.Use(async (context, following) => {
            const string key = "rk-enhanced-fixture-order";
            if (!context.Items.TryGetValue(key, out var value)) {
                value = new List<string>(); context.Items[key] = value;
                context.Response.OnStarting(() => {
                    context.Response.Headers["X-RK-Fixture-Order"] = string.Join(",", (List<string>)context.Items[key]!);
                    return Task.CompletedTask;
                });
            }
            ((List<string>)value!).Add(Identity.Route);
            await following();
        });
        next(app);
    };
}

#if ADOPTER
// Models the supported integration: the existing independent Enhanced injector
// retains ownership of its entry tag, and adds the kit before its own entry.
// Official Enhanced binaries/resources remain unmodified in the lab.
public sealed class IndependentInjector : IStartupFilter
{
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) => app => {
        app.Use(async (context, following) => {
            var p = context.Request.Path.Value ?? "";
            if (!HttpMethods.IsGet(context.Request.Method) ||
                !(p.EndsWith("/web/") || p.EndsWith("/web/index.html") || p.EndsWith("/web"))) {
                await following(); return;
            }
            if (!Identity.InjectionEnabled) { await following(); return; }
            context.Request.Headers.Remove("If-None-Match");
            context.Request.Headers.Remove("If-Modified-Since");
            context.Request.Headers.Remove("Accept-Encoding");
            context.Request.Headers.Remove("Range");
            context.Request.Headers.Remove("If-Range");
            var original = context.Response.Body;
            using var buffer = new MemoryStream();
            context.Response.Body = buffer;
            try { await following(); } finally { context.Response.Body = original; }
            var bytes = buffer.ToArray();
            if (context.Response.StatusCode == 200 && context.Response.ContentType?.Contains("text/html") == true) {
                var html = Encoding.UTF8.GetString(bytes);
                var position = html.IndexOf("</head>", StringComparison.OrdinalIgnoreCase);
                if (position < 0) throw new InvalidOperationException("Fixture did not find the real Jellyfin shell head");
                html = html.Insert(position, RefreshKit.BuildScriptTags(Identity.Options));
                bytes = Encoding.UTF8.GetBytes(html);
                context.Response.Headers.Remove("ETag");
                context.Response.Headers.Remove("Last-Modified");
                context.Response.Headers["Cache-Control"] = "no-store";
                context.Response.ContentLength = bytes.Length;
            }
            await original.WriteAsync(bytes);
        });
        next(app);
    };
}
#endif

[ApiController, Route(Identity.Route)]
public sealed class ResourceController : ControllerBase
{
    [HttpGet("version"), AllowAnonymous]
    public object Version() {
        Response.Headers["Cache-Control"] = "no-store";
        return new { CacheKey = Identity.Key, Identity.Epoch };
    }
    [HttpPost("change"), Authorize(Policy = "RequiresElevation")]
    public IActionResult Change() { Interlocked.Increment(ref Identity.Revision); return NoContent(); }
    [HttpGet("kit.js"), AllowAnonymous]
    public IActionResult Kit() {
        Response.Headers["Cache-Control"] = Request.Query["v"] == Identity.Key
            ? "public,max-age=31536000,immutable" : "no-store";
        using var stream = typeof(ResourceController).Assembly.GetManifestResourceStream("kit.js")!;
        using var reader = new StreamReader(stream);
        return Content(reader.ReadToEnd(), "application/javascript");
    }
}
