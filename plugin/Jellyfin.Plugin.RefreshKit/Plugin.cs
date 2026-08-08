using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using Jellyfin.Plugin.RefreshKit.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// Jellyfin Refresh Kit — the standalone plugin. It aligns eligible plugin
    /// shell assets and open tabs with the active server state without requiring
    /// participating plugins to adopt its API. Assets outside its documented
    /// transformation boundary remain the owning plugin's responsibility.
    /// </summary>
    public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
    {
        /// <summary>
        /// The resource name the client runtime is embedded under. It is copied
        /// from the repository root at build time (see the csproj), so the file
        /// shipped here is byte-identical to the single-file adoption copy.
        /// </summary>
        private const string KitResourceName = "Jellyfin.Plugin.RefreshKit.jellyfin-refresh-kit.js";

        private static readonly Lazy<string> _kitJavaScript = new Lazy<string>(LoadKitJavaScript);

        public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
            : base(applicationPaths, xmlSerializer)
        {
            Instance = this;
            Paths = applicationPaths;
        }

        public static Plugin? Instance { get; private set; }

        /// <summary>
        /// The host's application paths, captured in the constructor. The
        /// generation aggregator needs <c>PluginsPath</c> and must not depend on
        /// DI being resolvable at plugin-registration time.
        /// </summary>
        public static IApplicationPaths? Paths { get; private set; }

        public override string Name => "Jellyfin Refresh Kit";

        public override Guid Id => new Guid("515255fe-3332-49b0-b471-0be58c8221d8");

        public override string Description =>
            "Keeps eligible plugin shell assets and open tabs aligned with active server state: "
            + "revalidates or safely no-stores transformed index.html, stamps visible unversioned "
            + "plugin tags, and coordinates reloads through documented light-DOM safety gates.";

        /// <summary>
        /// The embedded jellyfin-refresh-kit.js source. Read once per process.
        /// </summary>
        public static string KitJavaScript => _kitJavaScript.Value;

        public IEnumerable<PluginPageInfo> GetPages()
        {
            return new[]
            {
                new PluginPageInfo
                {
                    Name = Name,
                    EmbeddedResourcePath = GetType().Namespace + ".Configuration.configPage.html",
                },
            };
        }

        /// <summary>
        /// The kit version string parsed out of the embedded runtime's
        /// <c>KIT_VERSION</c> declaration, for diagnostics. Never throws.
        /// </summary>
        public static string KitVersion
        {
            get
            {
                try
                {
                    var match = System.Text.RegularExpressions.Regex.Match(
                        KitJavaScript,
                        "KIT_VERSION\\s*=\\s*'([^']+)'");
                    return match.Success ? match.Groups[1].Value : "unknown";
                }
                catch
                {
                    return "unknown";
                }
            }
        }

        private static string LoadKitJavaScript()
        {
            try
            {
                var assembly = typeof(Plugin).Assembly;
                using var stream = assembly.GetManifestResourceStream(KitResourceName);
                if (stream == null)
                {
                    return BuildMissingResourceStub(assembly);
                }

                using var reader = new StreamReader(stream, Encoding.UTF8);
                return reader.ReadToEnd();
            }
            catch (Exception ex)
            {
                // A missing/unreadable resource must degrade to "no client
                // runtime", never to a 500 on a script the shell references.
                return "/* Jellyfin Refresh Kit: client runtime unavailable: "
                    + ex.Message.Replace("*/", string.Empty, StringComparison.Ordinal)
                    + " */\n";
            }
        }

        private static string BuildMissingResourceStub(Assembly assembly)
        {
            var names = string.Join(", ", assembly.GetManifestResourceNames());
            return string.Format(
                CultureInfo.InvariantCulture,
                "/* Jellyfin Refresh Kit: embedded runtime '{0}' not found. Present: {1} */\n",
                KitResourceName,
                names);
        }
    }
}
