using System;
using System.Collections.Generic;
using Jellyfin.Plugin.RefreshKitLifecycleProbe.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.RefreshKitLifecycleProbe;

/// <summary>
/// Purpose-built third-party plugin used only by the disposable lifecycle lab.
/// </summary>
public sealed class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    /// <summary>The immutable fixture release compiled into this assembly.</summary>
#if PROBE_V1
    public const string ReleaseLabel = "v1";
#elif PROBE_V2
    public const string ReleaseLabel = "v2";
#else
#error A lifecycle-probe release symbol must be selected.
#endif

    /// <summary>Initializes the lifecycle probe.</summary>
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
    }

    /// <inheritdoc />
    public override string Name => "Refresh Kit Lifecycle Probe";

    /// <inheritdoc />
    public override Guid Id => new("8f42f34a-a7d1-4b6e-9b77-17ed99d7a216");

    /// <inheritdoc />
    public override string Description =>
        $"Disposable third-party lifecycle fixture compiled as {ReleaseLabel}.";

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        yield return new PluginPageInfo
        {
            Name = Name,
            EmbeddedResourcePath =
                "Jellyfin.Plugin.RefreshKitLifecycleProbe.Configuration.probe.html",
        };
    }
}
