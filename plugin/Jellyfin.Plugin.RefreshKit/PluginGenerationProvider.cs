using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// THE GENERATION AGGREGATOR.
    ///
    /// <para>
    /// A "generation" is one short token that changes whenever ANY installed
    /// plugin changes. It is the whole point of the standalone plugin: other
    /// plugins do not (and must not have to) publish a version endpoint, so this
    /// plugin derives one identity covering all of them, from what the server
    /// already knows — for every folder under <c>PluginsPath</c>: the plugin id
    /// and version out of its <c>meta.json</c>, plus the NEWEST DLL write-ticks
    /// anywhere in that folder.
    /// </para>
    ///
    /// <para>WHY THOSE THREE INPUTS</para>
    /// <list type="bullet">
    /// <item><description><b>id</b> — installing or removing a plugin changes the
    /// set even when nothing else moves.</description></item>
    /// <item><description><b>version</b> — a marketplace upgrade lands in a NEW
    /// folder (<c>Name_1.2.3.0</c>), so the version moves with it.</description></item>
    /// <item><description><b>newest DLL ticks</b> — the case version alone misses:
    /// a same-version binary replaced in place (a dev copying a DLL over, a
    /// re-published build). This is exactly why RefreshKit.CacheKey carries the
    /// DLL ticks for its own assembly.</description></item>
    /// </list>
    ///
    /// <para>
    /// The scan deliberately reads the FILESYSTEM rather than the host's loaded
    /// plugin list: a plugin that is installed-but-disabled, or that failed to
    /// load, still changes what the web client should be running, and a folder
    /// dropped in before a restart should be visible the moment it exists.
    /// </para>
    ///
    /// <para>
    /// Results are cached for <see cref="CacheTtlSeconds"/> seconds. Every
    /// index.html request and every client poll asks for the generation, so the
    /// directory walk must not run per request; a few seconds of lag on a plugin
    /// change is irrelevant against a 60s client poll.
    /// </para>
    /// </summary>
    public sealed class PluginGenerationProvider
    {
        /// <summary>How long a computed generation is reused before rescanning.</summary>
        public const int CacheTtlSeconds = 5;

        /// <summary>
        /// Hard cap on files examined per plugin folder, so a plugin shipping a
        /// large asset tree cannot turn the scan into a per-request stat storm.
        /// </summary>
        private const int MaxFilesPerPlugin = 4000;

        private static readonly Lazy<PluginGenerationProvider> _instance =
            new Lazy<PluginGenerationProvider>(() => new PluginGenerationProvider());

        private readonly object _lock = new object();
        private string _cached = string.Empty;
        private IReadOnlyList<PluginFingerprint> _cachedDetails = Array.Empty<PluginFingerprint>();
        private DateTime _cachedAtUtc = DateTime.MinValue;

        /// <summary>Process-wide singleton; the aggregate is host state, not per-request state.</summary>
        public static PluginGenerationProvider Instance => _instance.Value;

        /// <summary>
        /// The current generation token: <c>{plugin count}p-{16 hex}</c>, e.g.
        /// <c>4p-9f2a1c0b7d3e5a64</c>. URL- and attribute-safe by construction,
        /// short enough to read in a network trace, and it changes on any
        /// install / uninstall / upgrade / in-place DLL replacement.
        /// <para>
        /// Never throws: an unreadable plugins directory degrades to this
        /// plugin's own cache key, which still detects updates to ITSELF.
        /// </para>
        /// </summary>
        public string Generation => GetSnapshot().Generation;

        /// <summary>Per-plugin fingerprints behind the current generation (diagnostics).</summary>
        public IReadOnlyList<PluginFingerprint> Details => GetSnapshot().Details;

        /// <summary>Recompute on the next read, whatever the TTL says.</summary>
        public void Invalidate()
        {
            lock (_lock)
            {
                _cachedAtUtc = DateTime.MinValue;
            }
        }

        private (string Generation, IReadOnlyList<PluginFingerprint> Details) GetSnapshot()
        {
            lock (_lock)
            {
                var now = DateTime.UtcNow;
                if (_cached.Length > 0 && (now - _cachedAtUtc).TotalSeconds < CacheTtlSeconds)
                {
                    return (_cached, _cachedDetails);
                }

                var details = ScanPlugins();
                _cachedDetails = details;
                _cached = Fold(details);
                _cachedAtUtc = now;
                return (_cached, _cachedDetails);
            }
        }

        /// <summary>
        /// Folds the fingerprints into the token. Ordinal-sorted first so the
        /// value depends on the SET of plugins, never on directory enumeration
        /// order (which is filesystem-dependent and would otherwise make the
        /// generation flap between identical servers — the exact failure the JS
        /// kit's flap guard exists to survive).
        /// </summary>
        private static string Fold(IReadOnlyList<PluginFingerprint> details)
        {
            var material = new StringBuilder("rk-generation-v1");
            foreach (var line in details.Select(d => d.ToMaterial()).OrderBy(s => s, StringComparer.Ordinal))
            {
                material.Append('\n').Append(line);
            }

            // The kit's own binary is folded in explicitly as well: it is
            // normally one of the scanned folders, but a manual/dev install from
            // an unusual path must still change the generation when it changes.
            material.Append("\nself|").Append(RefreshKit.CacheKey);

            var hash = SHA256.HashData(Encoding.UTF8.GetBytes(material.ToString()));
            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}p-{1}",
                details.Count,
                Convert.ToHexString(hash, 0, 8).ToLowerInvariant());
        }

        private static IReadOnlyList<PluginFingerprint> ScanPlugins()
        {
            var pluginsPath = ResolvePluginsPath();
            if (string.IsNullOrEmpty(pluginsPath) || !Directory.Exists(pluginsPath))
            {
                return Array.Empty<PluginFingerprint>();
            }

            var results = new List<PluginFingerprint>();
            IEnumerable<string> directories;
            try
            {
                directories = Directory.EnumerateDirectories(pluginsPath);
            }
            catch
            {
                return Array.Empty<PluginFingerprint>();
            }

            foreach (var directory in directories)
            {
                try
                {
                    results.Add(Fingerprint(directory));
                }
                catch
                {
                    // One unreadable plugin folder must not blind the aggregate
                    // to the other twenty. Record its name so it still counts.
                    results.Add(new PluginFingerprint(Path.GetFileName(directory), string.Empty, string.Empty, 0));
                }
            }

            return results;
        }

        private static PluginFingerprint Fingerprint(string directory)
        {
            var folder = Path.GetFileName(directory);
            var id = string.Empty;
            var version = string.Empty;

            var metaPath = Path.Combine(directory, "meta.json");
            if (File.Exists(metaPath))
            {
                try
                {
                    using var document = JsonDocument.Parse(File.ReadAllBytes(metaPath));
                    id = ReadStringProperty(document.RootElement, "guid");
                    version = ReadStringProperty(document.RootElement, "version");
                }
                catch
                {
                    // A hand-edited or truncated meta.json falls back to the
                    // folder name, which already encodes "Name_1.2.3.0".
                }
            }

            long newestTicks = 0;
            var seen = 0;
            foreach (var file in Directory.EnumerateFiles(directory, "*.dll", SearchOption.AllDirectories))
            {
                if (++seen > MaxFilesPerPlugin)
                {
                    break;
                }

                try
                {
                    var ticks = new FileInfo(file).LastWriteTimeUtc.Ticks;
                    if (ticks > newestTicks)
                    {
                        newestTicks = ticks;
                    }
                }
                catch
                {
                    // Skip a file that vanished mid-scan (an upgrade in flight).
                }
            }

            return new PluginFingerprint(folder, id, version, newestTicks);
        }

        private static string ReadStringProperty(JsonElement root, string name)
        {
            foreach (var property in root.EnumerateObject())
            {
                if (string.Equals(property.Name, name, StringComparison.OrdinalIgnoreCase)
                    && property.Value.ValueKind == JsonValueKind.String)
                {
                    return property.Value.GetString() ?? string.Empty;
                }
            }

            return string.Empty;
        }

        /// <summary>
        /// The plugins root. Prefers the host's own <c>IApplicationPaths</c>
        /// (captured by the plugin constructor); falls back to the parent of the
        /// folder this assembly was loaded from, which is what
        /// <c>/config/plugins/Name_1.0.0.0/Jellyfin.Plugin.RefreshKit.dll</c>
        /// makes it.
        /// </summary>
        private static string ResolvePluginsPath()
        {
            try
            {
                var fromHost = Plugin.Paths?.PluginsPath;
                if (!string.IsNullOrEmpty(fromHost))
                {
                    return fromHost!;
                }
            }
            catch
            {
                // Fall through to the assembly-relative derivation.
            }

            try
            {
                var location = typeof(PluginGenerationProvider).Assembly.Location;
                if (string.IsNullOrEmpty(location))
                {
                    return string.Empty;
                }

                var ownFolder = Path.GetDirectoryName(location);
                return ownFolder == null ? string.Empty : Path.GetDirectoryName(ownFolder) ?? string.Empty;
            }
            catch
            {
                return string.Empty;
            }
        }
    }

    /// <summary>One installed plugin's contribution to the generation.</summary>
    public sealed class PluginFingerprint
    {
        public PluginFingerprint(string folder, string id, string version, long newestDllTicks)
        {
            Folder = folder;
            Id = id;
            Version = version;
            NewestDllTicks = newestDllTicks;
        }

        public string Folder { get; }

        public string Id { get; }

        public string Version { get; }

        public long NewestDllTicks { get; }

        internal string ToMaterial() => string.Format(
            CultureInfo.InvariantCulture,
            "{0}|{1}|{2}|{3}",
            Folder,
            Id,
            Version,
            NewestDllTicks);
    }
}
