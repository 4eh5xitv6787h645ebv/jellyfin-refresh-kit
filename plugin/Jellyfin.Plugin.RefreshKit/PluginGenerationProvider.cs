using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using MediaBrowser.Common.Plugins;

[assembly: System.Runtime.CompilerServices.InternalsVisibleTo("Jellyfin.Plugin.RefreshKit.Tests")]

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// Produces one deterministic generation for the code and client state that
    /// this Jellyfin process is actually running.
    ///
    /// <para>
    /// Loaded plugin modules contribute assembly name, version and MVID. Only
    /// plugin records whose DLL paths match loaded assemblies participate, so a
    /// staged install/upgrade/disable is invisible until the required restart.
    /// A record removed by uninstall is retained for this process lifetime
    /// because its assembly cannot be unloaded; the next process publishes the
    /// removal.
    /// </para>
    ///
    /// <para>
    /// Loose browser assets (<c>.js</c>, <c>.mjs</c>, <c>.css</c> and
    /// <c>.html</c>) and each plugin's exact Jellyfin configuration XML are
    /// content-hashed. Absolute paths, mutable manifest status and timestamps are
    /// diagnostics only. Traversal, file count and content bytes are bounded;
    /// budget exhaustion publishes a deterministic sentinel and is exposed in
    /// diagnostics. Configuration identities retain the existing debounce and
    /// cooldown behavior.
    /// </para>
    ///
    /// <para>
    /// Selected loaded Jellyfin host assemblies also contribute name, version
    /// and MVID, so a Jellyfin update changes the generation after restart even
    /// if Refresh Kit and every third-party plugin stayed unchanged. Results are
    /// cached for <see cref="CacheTtlSeconds"/> seconds.
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
        internal const int MaxFilesPerPlugin = 4000;

        /// <summary>
        /// Hard cap on DIRECTORIES descended per plugin folder. The file cap
        /// alone does not bound a recursive enumeration: the walk still visits
        /// every directory looking for matches, so a deep tree costs a full
        /// traversal every <see cref="CacheTtlSeconds"/> seconds however few
        /// files match. Both budgets are enforced in
        /// <see cref="ScanActiveClientAssets"/>.
        /// </summary>
        internal const int MaxDirectoriesPerPlugin = 512;

        /// <summary>
        /// Upper bound on client-asset bytes hashed for one loaded plugin in one
        /// scan. Content, rather than timestamps, is authoritative, but a single
        /// unexpectedly large script bundle must not create unbounded periodic I/O.
        /// </summary>
        internal const long MaxAssetBytesPerPlugin = 8L * 1024 * 1024;

        /// <summary>Aggregate content-I/O ceiling across all loaded plugins in one scan.</summary>
        internal const long MaxTotalAssetBytesPerScan = 32L * 1024 * 1024;

        /// <summary>Upper bound on exact plugin-configuration bytes hashed per scan.</summary>
        internal const long MaxConfigurationBytesPerPlugin = 2L * 1024 * 1024;

        /// <summary>Aggregate configuration content-I/O ceiling in one scan.</summary>
        internal const long MaxTotalConfigurationBytesPerScan = 8L * 1024 * 1024;

        /// <summary>
        /// Extensions whose content is folded into the generation: files a
        /// browser executes or renders. Source maps and runtime data are omitted
        /// because neither changes the delivered application behavior.
        /// </summary>
        private static readonly string[] ClientAssetExtensions =
        {
            ".js", ".mjs", ".css", ".html",
        };

        private static readonly HashSet<string> HostAssemblyNames = new HashSet<string>(
            new[]
            {
                "jellyfin",
                "Jellyfin.Server",
                "Jellyfin.Api",
                "Emby.Server.Implementations",
            },
            StringComparer.OrdinalIgnoreCase);

        /// <summary>Jellyfin's plugin-configuration folder, a sibling of the plugin folders.</summary>
        private const string ConfigurationsFolderName = "configurations";

        /// <summary>
        /// How long a new configuration content identity must stand still before it is
        /// allowed to move the generation. A settings page that writes three
        /// times as the admin clicks Save is one change, not three.
        /// </summary>
        private const int ConfigDebounceSeconds = 10;

        private readonly object _lock = new object();
        private readonly Func<IReadOnlyList<ActivePluginDescriptor>> _activePluginProvider;
        private readonly Func<HostFingerprint> _hostFingerprintProvider;
        private readonly string? _configurationsPathOverride;
        private readonly Func<DateTime> _utcNow;
        private readonly Dictionary<string, ConfigSignal> _configSignals =
            new Dictionary<string, ConfigSignal>(StringComparer.Ordinal);
        private readonly Dictionary<string, ActiveAssetSnapshot> _activeAssetSnapshots =
            new Dictionary<string, ActiveAssetSnapshot>(StringComparer.Ordinal);
        private readonly Dictionary<string, ActiveConfigurationSnapshot> _activeConfigurationSnapshots =
            new Dictionary<string, ActiveConfigurationSnapshot>(StringComparer.Ordinal);
        private readonly Dictionary<string, ActivePluginDescriptor> _lastKnownActivePlugins =
            new Dictionary<string, ActivePluginDescriptor>(StringComparer.Ordinal);
        private readonly Dictionary<string, PluginFingerprint> _lastKnownActiveFingerprints =
            new Dictionary<string, PluginFingerprint>(StringComparer.Ordinal);

        private string _cached = string.Empty;
        private IReadOnlyList<PluginFingerprint> _cachedDetails = Array.Empty<PluginFingerprint>();
        private HostFingerprint _cachedHost = HostFingerprint.Empty;
        private DateTime _cachedAtUtc = DateTime.MinValue;

        /// <summary>
        /// Initializes a provider from Jellyfin's actual loaded plugin state.
        /// Staged manifests and folders are deliberately excluded until their
        /// assemblies are loaded by a subsequent server start.
        /// </summary>
        /// <param name="pluginManager">Jellyfin's plugin manager.</param>
        public PluginGenerationProvider(IPluginManager pluginManager)
        {
            ArgumentNullException.ThrowIfNull(pluginManager);
            _utcNow = static () => DateTime.UtcNow;
            _activePluginProvider = () => DiscoverActivePlugins(
                SnapshotPlugins(pluginManager),
                CaptureLoadedModules());
            _hostFingerprintProvider = () => DiscoverHostFingerprint(CaptureLoadedModules());
        }

        /// <summary>Test seam for deterministic loaded-state lifecycle scenarios.</summary>
        internal PluginGenerationProvider(
            Func<IReadOnlyList<ActivePluginDescriptor>> activePluginProvider,
            string configurationsPath,
            Func<DateTime>? utcNow = null,
            Func<HostFingerprint>? hostFingerprintProvider = null)
        {
            _activePluginProvider = activePluginProvider
                ?? throw new ArgumentNullException(nameof(activePluginProvider));
            _configurationsPathOverride = configurationsPath;
            _utcNow = utcNow ?? (static () => DateTime.UtcNow);
            _hostFingerprintProvider = hostFingerprintProvider ?? (static () => HostFingerprint.Empty);
        }

        /// <summary>
        /// The current generation token: <c>{plugin count}p-{16 hex}</c>, e.g.
        /// <c>4p-9f2a1c0b7d3e5a64</c>. URL- and attribute-safe by construction,
        /// short enough to read in a network trace, and it changes when loaded
        /// host/plugin code or active browser/configuration content changes.
        /// <para>
        /// Read races retain the last coherent process state instead of briefly
        /// publishing an empty generation.
        /// </para>
        /// </summary>
        public string Generation => GetSnapshot().Generation;

        /// <summary>Per-plugin fingerprints behind the current generation (diagnostics).</summary>
        public IReadOnlyList<PluginFingerprint> Details => GetSnapshot().Details;

        /// <summary>Deterministic identity of the loaded Jellyfin host assemblies.</summary>
        public HostFingerprint Host => GetSnapshot().Host;

        /// <summary>Recompute on the next read, whatever the TTL says.</summary>
        public void Invalidate()
        {
            lock (_lock)
            {
                _cachedAtUtc = DateTime.MinValue;
            }
        }

        private (string Generation, IReadOnlyList<PluginFingerprint> Details, HostFingerprint Host) GetSnapshot()
        {
            lock (_lock)
            {
                var now = _utcNow();
                if (_cached.Length > 0 && (now - _cachedAtUtc).TotalSeconds < CacheTtlSeconds)
                {
                    return (_cached, _cachedDetails, _cachedHost);
                }

                var details = ScanActivePlugins(now);
                HostFingerprint host;
                try
                {
                    host = _hostFingerprintProvider();
                }
                catch
                {
                    host = _cachedHost;
                }

                _cachedDetails = details;
                _cachedHost = host;
                _cached = Fold(details, host);
                _cachedAtUtc = now;
                return (_cached, _cachedDetails, _cachedHost);
            }
        }

        /// <summary>
        /// Folds the fingerprints into the token. Ordinal-sorted first so the
        /// value depends on the SET of plugins, never on directory enumeration
        /// order (which is filesystem-dependent and would otherwise make the
        /// generation flap between identical servers — the exact failure the JS
        /// kit's flap guard exists to survive).
        /// </summary>
        private static string Fold(
            IReadOnlyList<PluginFingerprint> details,
            HostFingerprint host)
        {
            var material = new StringBuilder("rk-generation-v2");
            material.Append("\nhost|").Append(host.Identity);
            foreach (var line in details.Select(d => d.ToMaterial()).OrderBy(s => s, StringComparer.Ordinal))
            {
                material.Append('\n').Append(line);
            }

            var hash = SHA256.HashData(Encoding.UTF8.GetBytes(material.ToString()));
            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}p-{1}",
                details.Count,
                Convert.ToHexString(hash, 0, 8).ToLowerInvariant());
        }

        private IReadOnlyList<PluginFingerprint> ScanActivePlugins(DateTime now)
        {
            IReadOnlyList<ActivePluginDescriptor> activePlugins;
            try
            {
                activePlugins = _activePluginProvider();
            }
            catch
            {
                // A concurrent plugin install can mutate Jellyfin's backing list
                // while it is being copied. Keep the last coherent answer rather
                // than publishing an empty generation and flapping back five
                // seconds later.
                return _cachedDetails;
            }

            foreach (var plugin in activePlugins)
            {
                // Jellyfin removes a successfully uninstalled plugin from
                // IPluginManager.Plugins immediately, even though its assembly
                // cannot be unloaded and continues executing until restart.
                // A provider is process-scoped, so retaining descriptors here is
                // both deterministic and the closest representation of reality.
                _lastKnownActivePlugins[plugin.StableIdentity] = plugin;
            }

            var currentPluginKeys = new HashSet<string>(
                activePlugins.Select(plugin => plugin.StableIdentity),
                StringComparer.Ordinal);
            activePlugins = _lastKnownActivePlugins.Values.ToList();

            var configurationsPath = !string.IsNullOrEmpty(_configurationsPathOverride)
                ? _configurationsPathOverride!
                : ResolveConfigurationsPath(ResolvePluginsPath());
            var results = new List<PluginFingerprint>(activePlugins.Count);
            long remainingAssetBytes = MaxTotalAssetBytesPerScan;
            long remainingConfigurationBytes = MaxTotalConfigurationBytesPerScan;
            var contentBuffer = new byte[81920];

            foreach (var plugin in activePlugins
                .OrderBy(p => p.StableIdentity, StringComparer.Ordinal))
            {
                var isCurrentPluginRecord = currentPluginKeys.Contains(plugin.StableIdentity);
                if (!isCurrentPluginRecord
                    && _lastKnownActiveFingerprints.TryGetValue(plugin.StableIdentity, out var retained))
                {
                    // Freeze every contribution once Jellyfin removes the live
                    // record. Uninstall can also delete assets/config before the
                    // required restart, but the running code is still the old
                    // active state and must keep its exact last-published token.
                    results.Add(retained.AsRetainedPluginRecord());
                    continue;
                }

                try
                {
                    var assets = ScanActiveClientAssets(
                        plugin.DirectoryPath,
                        remainingAssetBytes,
                        contentBuffer);
                    remainingAssetBytes = Math.Max(0, remainingAssetBytes - assets.BytesHashed);
                    var usingLastGoodAssets = false;
                    if (assets.IsUsable)
                    {
                        _activeAssetSnapshots[plugin.StableIdentity] = assets;
                    }
                    else if (_activeAssetSnapshots.TryGetValue(plugin.StableIdentity, out var previous))
                    {
                        // On Linux Jellyfin can unlink a loaded plugin folder
                        // immediately during uninstall. The running process still
                        // executes that assembly until restart, so dropping its
                        // assets here would publish the lifecycle change too early.
                        assets = previous;
                        usingLastGoodAssets = true;
                    }

                    var configurationSnapshot = ScanActiveConfiguration(
                        configurationsPath,
                        plugin.ConfigurationFileNames,
                        remainingConfigurationBytes,
                        contentBuffer);
                    remainingConfigurationBytes = Math.Max(
                        0,
                        remainingConfigurationBytes - configurationSnapshot.BytesHashed);
                    var usingLastGoodConfiguration = false;
                    if (configurationSnapshot.IsUsable)
                    {
                        _activeConfigurationSnapshots[plugin.StableIdentity] = configurationSnapshot;
                    }
                    else if (_activeConfigurationSnapshots.TryGetValue(
                        plugin.StableIdentity,
                        out var previousConfiguration))
                    {
                        configurationSnapshot = previousConfiguration;
                        usingLastGoodConfiguration = true;
                    }

                    var publishedConfigurationIdentity = PublishConfigIdentity(
                        plugin.Folder,
                        plugin.Id,
                        plugin.AssemblyNames,
                        configurationSnapshot.Identity,
                        now);

                    var fingerprint = new PluginFingerprint(
                        plugin.Folder,
                        plugin.Id,
                        plugin.Version,
                        plugin.ManifestStatus,
                        assets.NewestTicks,
                        configurationSnapshot.NewestTicks,
                        plugin.ModuleIdentity,
                        assets.Identity,
                        assets.FileCount,
                        assets.DirectoriesScanned,
                        assets.BytesHashed,
                        assets.IsTruncated,
                        !assets.IsUsable,
                        usingLastGoodAssets,
                        publishedConfigurationIdentity,
                        configurationSnapshot.FileCount,
                        configurationSnapshot.BytesHashed,
                        configurationSnapshot.IsTruncated,
                        !configurationSnapshot.IsUsable,
                        usingLastGoodConfiguration,
                        usingLastKnownPluginRecord: false);
                    _lastKnownActiveFingerprints[plugin.StableIdentity] = fingerprint;
                    results.Add(fingerprint);
                }
                catch
                {
                    // The module identity is captured from loaded metadata and is
                    // still authoritative even if a loose-asset/configuration read
                    // races an installer. Preserve the last complete row when one
                    // exists rather than publishing a transient empty identity.
                    if (_lastKnownActiveFingerprints.TryGetValue(
                        plugin.StableIdentity,
                        out var previousFingerprint))
                    {
                        results.Add(previousFingerprint);
                        continue;
                    }

                    var fingerprint = new PluginFingerprint(
                        plugin.Folder,
                        plugin.Id,
                        plugin.Version,
                        plugin.ManifestStatus,
                        0,
                        0,
                        plugin.ModuleIdentity,
                        string.Empty,
                        0,
                        0,
                        0,
                        assetScanTruncated: true,
                        assetScanUnavailable: true,
                        usingLastGoodAssets: false,
                        configurationIdentity: string.Empty,
                        configurationFileCount: 0,
                        configurationBytesHashed: 0,
                        configurationScanTruncated: true,
                        configurationScanUnavailable: true,
                        usingLastGoodConfiguration: false,
                        usingLastKnownPluginRecord: false);
                    _lastKnownActiveFingerprints[plugin.StableIdentity] = fingerprint;
                    results.Add(fingerprint);
                }
            }

            PrunePerPluginState(results);
            return results;
        }

        /// <summary>
        /// Drops debounce/cooldown state for identities that are not active in
        /// this process, so the dictionary cannot grow without bound.
        /// </summary>
        private void PrunePerPluginState(IReadOnlyList<PluginFingerprint> results)
        {
            if (_configSignals.Count == 0)
            {
                return;
            }

            var live = new HashSet<string>(results.Select(r => r.Folder), StringComparer.Ordinal);
            foreach (var stale in _configSignals.Keys.Where(k => !live.Contains(k)).ToList())
            {
                _configSignals.Remove(stale);
            }
        }

        private static IReadOnlyList<LocalPlugin> SnapshotPlugins(IPluginManager pluginManager)
        {
            // IPluginManager exposes its mutable backing list as IReadOnlyList.
            // Copy it before doing any filesystem work; if an install mutates it
            // concurrently, let the caller retain the last coherent generation.
            return pluginManager.Plugins.ToArray();
        }

        internal static IReadOnlyList<LoadedModuleFingerprint> CaptureLoadedModules()
        {
            var modules = new List<LoadedModuleFingerprint>();
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                try
                {
                    if (assembly.IsDynamic)
                    {
                        continue;
                    }

                    modules.Add(new LoadedModuleFingerprint(
                        assembly.Location,
                        assembly.GetName().Name ?? Path.GetFileNameWithoutExtension(assembly.Location),
                        assembly.GetName().Version?.ToString() ?? string.Empty,
                        assembly.ManifestModule.ModuleVersionId));
                }
                catch
                {
                    // Dynamic/single-file/native-backed assemblies can reject one
                    // of these metadata reads; skip only that unreadable module.
                }
            }

            return modules;
        }

        /// <summary>
        /// Builds a path-independent identity for the assemblies that define the
        /// running Jellyfin host. A server/web package update therefore moves the
        /// generation after restart even when Refresh Kit and every plugin are
        /// byte-identical.
        /// </summary>
        internal static HostFingerprint DiscoverHostFingerprint(
            IReadOnlyList<LoadedModuleFingerprint> loadedModules)
        {
            var modules = loadedModules
                .Where(module => HostAssemblyNames.Contains(module.Name))
                .GroupBy(module => module.ToMaterial(), StringComparer.Ordinal)
                .Select(group => group.First().ToMaterial())
                .OrderBy(material => material, StringComparer.Ordinal)
                .ToList();
            var identityMaterial = new StringBuilder("rk-loaded-jellyfin-host-v1");
            foreach (var module in modules)
            {
                identityMaterial.Append('\n').Append(module);
            }

            return new HostFingerprint(HashMaterial(identityMaterial.ToString()), modules);
        }

        /// <summary>
        /// Joins Jellyfin's plugin records to assemblies that are loaded in this
        /// process. Disk-only records are staged state and deliberately do not
        /// contribute until the next server start loads them.
        /// </summary>
        internal static IReadOnlyList<ActivePluginDescriptor> DiscoverActivePlugins(
            IReadOnlyList<LocalPlugin> plugins,
            IReadOnlyList<LoadedModuleFingerprint> loadedModules)
        {
            var comparer = OperatingSystem.IsWindows() || OperatingSystem.IsMacOS()
                ? StringComparer.OrdinalIgnoreCase
                : StringComparer.Ordinal;
            var modulesByPath = new Dictionary<string, List<LoadedModuleFingerprint>>(comparer);
            foreach (var module in loadedModules)
            {
                var normalized = NormalizePath(module.Path);
                if (normalized.Length == 0)
                {
                    continue;
                }

                if (!modulesByPath.TryGetValue(normalized, out var matches))
                {
                    matches = new List<LoadedModuleFingerprint>();
                    modulesByPath[normalized] = matches;
                }

                matches.Add(module);
            }

            var active = new List<ActivePluginDescriptor>();
            foreach (var plugin in plugins)
            {
                try
                {
                    var candidatePaths = new HashSet<string>(comparer);
                    foreach (var dll in plugin.DllFiles)
                    {
                        var normalized = NormalizePath(dll);
                        if (normalized.Length > 0)
                        {
                            candidatePaths.Add(normalized);
                        }
                    }

                    if (plugin.Instance != null)
                    {
                        var instancePath = NormalizePath(plugin.Instance.AssemblyFilePath);
                        if (instancePath.Length > 0)
                        {
                            candidatePaths.Add(instancePath);
                        }
                    }

                    var matchedModules = candidatePaths
                        .Where(modulesByPath.ContainsKey)
                        .SelectMany(path => modulesByPath[path])
                        .GroupBy(module => module.ToMaterial(), StringComparer.Ordinal)
                        .Select(group => group.First())
                        .OrderBy(module => module.ToMaterial(), StringComparer.Ordinal)
                        .ToList();
                    if (matchedModules.Count == 0)
                    {
                        continue;
                    }

                    var id = plugin.Instance?.Id.ToString("D", CultureInfo.InvariantCulture)
                        ?? plugin.Id.ToString("D", CultureInfo.InvariantCulture);
                    var version = plugin.Instance?.Version?.ToString()
                        ?? matchedModules.Select(module => module.Version)
                            .FirstOrDefault(candidate => candidate.Length > 0)
                        ?? plugin.Manifest.Version
                        ?? string.Empty;
                    var configurationFileNames = ResolveConfigurationFileNames(plugin);
                    active.Add(new ActivePluginDescriptor(
                        plugin.Path,
                        id,
                        version,
                        plugin.Manifest.Status.ToString(),
                        matchedModules,
                        configurationFileNames));
                }
                catch
                {
                    // One malformed/transient record must not prevent the other
                    // loaded plugins from participating in the generation.
                }
            }

            return active;
        }

        private static IReadOnlyList<string> ResolveConfigurationFileNames(LocalPlugin plugin)
        {
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (plugin.Instance != null)
            {
                var foundConfiguredName = false;
                try
                {
                    var configuredName = plugin.Instance.GetPluginInfo().ConfigurationFileName;
                    if (IsSafeConfigurationFileName(configuredName))
                    {
                        names.Add(configuredName!);
                        foundConfiguredName = true;
                    }
                }
                catch
                {
                    // Fall back to the primary assembly filename below.
                }

                if (!foundConfiguredName)
                {
                    try
                    {
                        var assemblyName = Path.GetFileNameWithoutExtension(plugin.Instance.AssemblyFilePath);
                        if (!string.IsNullOrWhiteSpace(assemblyName))
                        {
                            names.Add(assemblyName + ".xml");
                        }
                    }
                    catch
                    {
                        // A service-only plugin might not expose a BasePlugin config.
                    }
                }
            }

            return names.OrderBy(name => name, StringComparer.Ordinal).ToList();
        }

        private static bool IsSafeConfigurationFileName(string? name) =>
            !string.IsNullOrWhiteSpace(name)
            && name.IndexOf('/') < 0
            && name.IndexOf('\\') < 0
            && Path.GetFileName(name).Equals(name, StringComparison.Ordinal);

        private static string NormalizePath(string? path)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return string.Empty;
            }

            try
            {
                return Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            }
            catch
            {
                return string.Empty;
            }
        }

        /// <summary>
        /// Fingerprints loose browser assets for one plugin that is loaded in the
        /// current process. DLLs are intentionally excluded: their authoritative
        /// identity is the loaded module MVID, not whatever bytes an installer has
        /// staged at the same path for the next restart.
        /// </summary>
        private static ActiveAssetSnapshot ScanActiveClientAssets(
            string directory,
            long remainingGlobalBytes,
            byte[] buffer)
        {
            if (string.IsNullOrEmpty(directory) || !Directory.Exists(directory))
            {
                return ActiveAssetSnapshot.Unavailable;
            }

            var byteLimit = Math.Min(MaxAssetBytesPerPlugin, Math.Max(0, remainingGlobalBytes));
            if (byteLimit == 0)
            {
                return ActiveAssetSnapshot.Truncated(0, 0, 0, 0);
            }

            long newestTicks = 0;
            long bytesHashed = 0;
            var filesSeen = 0;
            var directoriesSeen = 0;
            var material = new List<string>();
            var pending = new Stack<string>();
            pending.Push(directory);

            while (pending.Count > 0)
            {
                if (directoriesSeen >= MaxDirectoriesPerPlugin)
                {
                    return ActiveAssetSnapshot.Truncated(
                        newestTicks,
                        material.Count,
                        directoriesSeen,
                        bytesHashed);
                }

                var current = pending.Pop();
                directoriesSeen++;
                try
                {
                    var remainingFiles = MaxFilesPerPlugin - filesSeen;
                    var files = Directory.EnumerateFiles(current)
                        .Take(remainingFiles + 1)
                        .OrderBy(path => path, StringComparer.Ordinal)
                        .ToList();
                    if (files.Count > remainingFiles)
                    {
                        // The native enumeration order is filesystem-dependent.
                        // Do not hash an arbitrary prefix: publish one stable
                        // budget-exceeded identity and expose truncation instead.
                        return ActiveAssetSnapshot.Truncated(
                            newestTicks,
                            material.Count,
                            directoriesSeen,
                            bytesHashed);
                    }

                    foreach (var file in files)
                    {
                        filesSeen++;
                        var extension = Path.GetExtension(file);
                        if (!IsClientAsset(extension))
                        {
                            continue;
                        }

                        try
                        {
                            var info = new FileInfo(file);
                            if ((info.Attributes & FileAttributes.ReparsePoint) != 0)
                            {
                                continue;
                            }

                            using var stream = new FileStream(
                                file,
                                FileMode.Open,
                                FileAccess.Read,
                                FileShare.ReadWrite | FileShare.Delete,
                                buffer.Length,
                                FileOptions.SequentialScan);
                            var length = stream.Length;
                            if (length < 0 || length > byteLimit - bytesHashed)
                            {
                                return ActiveAssetSnapshot.Truncated(
                                    newestTicks,
                                    material.Count,
                                    directoriesSeen,
                                    bytesHashed);
                            }

                            var ticks = info.LastWriteTimeUtc.Ticks;
                            var contentHash = HashBoundedStream(stream, length, buffer);
                            info.Refresh();
                            if (!info.Exists || info.Length != length || info.LastWriteTimeUtc.Ticks != ticks)
                            {
                                return ActiveAssetSnapshot.Unavailable;
                            }

                            bytesHashed += length;
                            newestTicks = Math.Max(newestTicks, ticks);
                            var relative = Path.GetRelativePath(directory, file)
                                .Replace(Path.DirectorySeparatorChar, '/');
                            material.Add(string.Format(
                                CultureInfo.InvariantCulture,
                                "{0}|{1}|{2}",
                                Convert.ToBase64String(Encoding.UTF8.GetBytes(relative)),
                                length,
                                contentHash));
                        }
                        catch
                        {
                            return ActiveAssetSnapshot.Unavailable;
                        }
                    }

                    var remainingDirectories = MaxDirectoriesPerPlugin - directoriesSeen;
                    var subdirectories = Directory.EnumerateDirectories(current)
                        .Take(remainingDirectories + 1)
                        .OrderBy(path => path, StringComparer.Ordinal)
                        .ToList();
                    if (subdirectories.Count > remainingDirectories)
                    {
                        return ActiveAssetSnapshot.Truncated(
                            newestTicks,
                            material.Count,
                            directoriesSeen,
                            bytesHashed);
                    }

                    // Push in reverse so the ordinal-lowest directory is visited first.
                    for (var index = subdirectories.Count - 1; index >= 0; index--)
                    {
                        try
                        {
                            if ((File.GetAttributes(subdirectories[index]) & FileAttributes.ReparsePoint) == 0)
                            {
                                pending.Push(subdirectories[index]);
                            }
                        }
                        catch
                        {
                            return ActiveAssetSnapshot.Unavailable;
                        }
                    }
                }
                catch
                {
                    return ActiveAssetSnapshot.Unavailable;
                }
            }

            var identityMaterial = new StringBuilder("rk-active-assets-v2");
            foreach (var line in material.OrderBy(line => line, StringComparer.Ordinal))
            {
                identityMaterial.Append('\n').Append(line);
            }

            return new ActiveAssetSnapshot(
                HashMaterial(identityMaterial.ToString()),
                newestTicks,
                material.Count,
                directoriesSeen,
                bytesHashed,
                isTruncated: false,
                isUsable: true);
        }

        private static string HashBoundedStream(Stream stream, long length, byte[] buffer)
        {
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            long remaining = length;
            while (remaining > 0)
            {
                var requested = (int)Math.Min(buffer.Length, remaining);
                var read = stream.Read(buffer, 0, requested);
                if (read <= 0)
                {
                    throw new EndOfStreamException("File changed while its active content was fingerprinted.");
                }

                hash.AppendData(buffer, 0, read);
                remaining -= read;
            }

            if (stream.ReadByte() != -1)
            {
                throw new IOException("File grew while its active content was fingerprinted.");
            }

            return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
        }

        internal static string HashMaterial(string material) =>
            Convert.ToHexString(
                    SHA256.HashData(Encoding.UTF8.GetBytes(material)),
                    0,
                    16)
                .ToLowerInvariant();

        private static bool IsClientAsset(string extension)
        {
            foreach (var candidate in ClientAssetExtensions)
            {
                if (extension.Equals(candidate, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static ActiveConfigurationSnapshot ScanActiveConfiguration(
            string configurationsPath,
            IReadOnlyList<string> configurationFileNames,
            long remainingGlobalBytes,
            byte[] buffer)
        {
            if (configurationFileNames.Count == 0)
            {
                return ActiveConfigurationSnapshot.Empty;
            }

            if (string.IsNullOrEmpty(configurationsPath) || !Directory.Exists(configurationsPath))
            {
                return ActiveConfigurationSnapshot.Unavailable;
            }

            var byteLimit = Math.Min(
                MaxConfigurationBytesPerPlugin,
                Math.Max(0, remainingGlobalBytes));
            if (byteLimit == 0)
            {
                return ActiveConfigurationSnapshot.Truncated(0, 0, 0);
            }

            long newestTicks = 0;
            long bytesHashed = 0;
            var material = new List<string>();
            foreach (var configuredName in configurationFileNames
                .OrderBy(name => name, StringComparer.Ordinal))
            {
                if (!IsSafeConfigurationFileName(configuredName))
                {
                    // A plugin-controlled configuration filename must not turn a
                    // diagnostics scan into an arbitrary filesystem probe.
                    continue;
                }

                var fileName = configuredName;

                try
                {
                    var file = Path.Combine(configurationsPath, fileName);
                    if (!File.Exists(file))
                    {
                        continue;
                    }

                    var info = new FileInfo(file);
                    if ((info.Attributes & FileAttributes.ReparsePoint) != 0)
                    {
                        // Do not let a plugin-selected filename turn generation
                        // polling into a content oracle outside the configuration
                        // store through a symbolic link.
                        continue;
                    }

                    using var stream = new FileStream(
                        file,
                        FileMode.Open,
                        FileAccess.Read,
                        FileShare.ReadWrite | FileShare.Delete,
                        buffer.Length,
                        FileOptions.SequentialScan);
                    var length = stream.Length;
                    if (length < 0 || length > byteLimit - bytesHashed)
                    {
                        return ActiveConfigurationSnapshot.Truncated(
                            newestTicks,
                            material.Count,
                            bytesHashed);
                    }

                    var ticks = info.LastWriteTimeUtc.Ticks;
                    var contentHash = HashBoundedStream(stream, length, buffer);
                    info.Refresh();
                    if (!info.Exists || info.Length != length || info.LastWriteTimeUtc.Ticks != ticks)
                    {
                        return ActiveConfigurationSnapshot.Unavailable;
                    }

                    bytesHashed += length;
                    newestTicks = Math.Max(newestTicks, ticks);
                    material.Add(string.Format(
                        CultureInfo.InvariantCulture,
                        "{0}|{1}|{2}",
                        Convert.ToBase64String(Encoding.UTF8.GetBytes(fileName)),
                        length,
                        contentHash));
                }
                catch
                {
                    return ActiveConfigurationSnapshot.Unavailable;
                }
            }

            var identityMaterial = new StringBuilder("rk-active-configuration-v1");
            foreach (var line in material.OrderBy(line => line, StringComparer.Ordinal))
            {
                identityMaterial.Append('\n').Append(line);
            }

            return new ActiveConfigurationSnapshot(
                HashMaterial(identityMaterial.ToString()),
                newestTicks,
                material.Count,
                bytesHashed,
                isTruncated: false,
                isUsable: true);
        }

        /// <summary>
        /// The debounce + cooldown gate between "the config file changed" and
        /// "every open client reloads".
        ///
        /// <list type="bullet">
        /// <item><description>FIRST OBSERVATION of a plugin adopts whatever is on
        /// disk silently — a server restart is not a settings change.</description></item>
        /// <item><description>DEBOUNCE: a new content identity must stand still for
        /// <see cref="ConfigDebounceSeconds"/>; each further write restarts the
        /// clock, so a burst collapses into one bump.</description></item>
        /// <item><description>COOLDOWN, LEADING EDGE: a change that arrives while
        /// no cooldown window is open publishes IMMEDIATELY and opens a window of
        /// <see cref="Configuration.PluginConfiguration.ConfigCooldownMinutes"/>.
        /// Only changes that arrive *inside* that window are held; they coalesce
        /// into a single publish at the window's end, carrying the latest
        /// identity. Nothing is ever dropped.</description></item>
        /// <item><description>A publish that was held back closes the window
        /// instead of opening a new one. Without that, every deferred publish
        /// would re-arm the cooldown and a single later save could sit unseen for
        /// another full window — the "my setting did nothing for five minutes"
        /// behaviour this gate exists to avoid. The cost is that a plugin
        /// rewriting its configuration continuously can produce two bumps per
        /// window rather than one; the benefit is that an ordinary single save is
        /// always live within the debounce plus one client poll.</description></item>
        /// <item><description>A change that arrives after a window has already
        /// expired is a leading edge in its own right: it publishes at once and
        /// opens a fresh window, so burst protection re-arms itself.</description></item>
        /// </list>
        /// </summary>
        private string PublishConfigIdentity(
            string folder,
            string id,
            IReadOnlyList<string> assemblyNames,
            string rawIdentity,
            DateTime now)
        {
            var configuration = ReadConfiguration();
            if (configuration?.EnableConfigWatching == false
                || IsConfigWatchExcluded(configuration, folder, id, assemblyNames))
            {
                // Excluded plugins fall back to version/DLL detection only.
                _configSignals.Remove(folder);
                return string.Empty;
            }

            if (!_configSignals.TryGetValue(folder, out var signal))
            {
                signal = new ConfigSignal { PublishedIdentity = rawIdentity };
                _configSignals[folder] = signal;
                return signal.PublishedIdentity;
            }

            if (rawIdentity.Equals(signal.PublishedIdentity, StringComparison.Ordinal))
            {
                signal.PendingIdentity = null;
                return signal.PublishedIdentity;
            }

            if (!rawIdentity.Equals(signal.PendingIdentity, StringComparison.Ordinal))
            {
                signal.PendingIdentity = rawIdentity;
                signal.PendingFirstSeenUtc = now;
                return signal.PublishedIdentity;
            }

            if ((now - signal.PendingFirstSeenUtc).TotalSeconds < ConfigDebounceSeconds)
            {
                return signal.PublishedIdentity;
            }

            var cooldownMinutes = Math.Clamp(configuration?.ConfigCooldownMinutes ?? 5, 0, 1440);
            var windowOpen = signal.CooldownUntilUtc != DateTime.MinValue;

            if (windowOpen && now < signal.CooldownUntilUtc)
            {
                // Inside the window: hold the newest identity until the window ends.
                return signal.PublishedIdentity;
            }

            // A change first seen while the window was still running is a held
            // (trailing) publish; one first seen after the window had already
            // expired is a fresh leading edge.
            var wasHeldBack = windowOpen && signal.PendingFirstSeenUtc < signal.CooldownUntilUtc;

            signal.PublishedIdentity = rawIdentity;
            signal.PendingIdentity = null;
            signal.CooldownUntilUtc = wasHeldBack || cooldownMinutes <= 0
                ? DateTime.MinValue
                : now.AddMinutes(cooldownMinutes);
            return signal.PublishedIdentity;
        }

        private static Configuration.PluginConfiguration? ReadConfiguration()
        {
            try
            {
                return Plugin.Instance?.Configuration;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>
        /// An exclusion entry matches a plugin by any of the names an admin
        /// could reasonably type: the install folder (<c>Media Bar_2.4.12.0</c>),
        /// its display-name part (<c>Media Bar</c>), the plugin GUID, or an
        /// assembly name (<c>Jellyfin.Plugin.MediaBar</c>).
        /// </summary>
        private static bool IsConfigWatchExcluded(
            Configuration.PluginConfiguration? configuration,
            string folder,
            string id,
            IReadOnlyList<string> assemblyNames)
        {
            var exclusions = configuration?.ConfigWatchExclusions;
            if (exclusions == null || exclusions.Length == 0)
            {
                return false;
            }

            var underscore = folder.LastIndexOf('_');
            var displayName = underscore > 0 ? folder.Substring(0, underscore) : folder;

            foreach (var raw in exclusions)
            {
                var entry = (raw ?? string.Empty).Trim();
                if (entry.Length == 0)
                {
                    continue;
                }

                if (entry.Equals(folder, StringComparison.OrdinalIgnoreCase)
                    || entry.Equals(displayName, StringComparison.OrdinalIgnoreCase)
                    || (id.Length > 0 && entry.Replace("-", string.Empty, StringComparison.Ordinal)
                        .Equals(id.Replace("-", string.Empty, StringComparison.Ordinal), StringComparison.OrdinalIgnoreCase))
                    || assemblyNames.Any(a => entry.Equals(a, StringComparison.OrdinalIgnoreCase)))
                {
                    return true;
                }
            }

            return false;
        }

        /// <summary>Debounce/cooldown state for one plugin's configuration file.</summary>
        private sealed class ConfigSignal
        {
            /// <summary>The value currently folded into the generation.</summary>
            public string PublishedIdentity { get; set; } = string.Empty;

            /// <summary>A newer value waiting out the debounce and/or cooldown.</summary>
            public string? PendingIdentity { get; set; }

            public DateTime PendingFirstSeenUtc { get; set; }

            /// <summary>
            /// End of the open cooldown window, or <see cref="DateTime.MinValue"/>
            /// when no window is open — in which case the next debounced change
            /// publishes on the leading edge.
            /// </summary>
            public DateTime CooldownUntilUtc { get; set; } = DateTime.MinValue;
        }

        private readonly struct ActiveAssetSnapshot
        {
            public static readonly ActiveAssetSnapshot Unavailable = new ActiveAssetSnapshot(
                HashMaterial("rk-active-assets-v2\n<unavailable>"),
                0,
                0,
                0,
                0,
                isTruncated: false,
                isUsable: false);

            public static ActiveAssetSnapshot Truncated(
                long newestTicks,
                int fileCount,
                int directoriesScanned,
                long bytesHashed) =>
                new ActiveAssetSnapshot(
                    HashMaterial("rk-active-assets-v2\n<budget-exceeded>"),
                    newestTicks,
                    fileCount,
                    directoriesScanned,
                    bytesHashed,
                    isTruncated: true,
                    isUsable: true);

            public ActiveAssetSnapshot(
                string identity,
                long newestTicks,
                int fileCount,
                int directoriesScanned,
                long bytesHashed,
                bool isTruncated,
                bool isUsable)
            {
                Identity = identity;
                NewestTicks = newestTicks;
                FileCount = fileCount;
                DirectoriesScanned = directoriesScanned;
                BytesHashed = bytesHashed;
                IsTruncated = isTruncated;
                IsUsable = isUsable;
            }

            public string Identity { get; }

            public long NewestTicks { get; }

            public int FileCount { get; }

            public int DirectoriesScanned { get; }

            public long BytesHashed { get; }

            public bool IsTruncated { get; }

            public bool IsUsable { get; }
        }

        private readonly struct ActiveConfigurationSnapshot
        {
            public static readonly ActiveConfigurationSnapshot Empty = new ActiveConfigurationSnapshot(
                HashMaterial("rk-active-configuration-v1"),
                0,
                0,
                0,
                isTruncated: false,
                isUsable: true);

            public static readonly ActiveConfigurationSnapshot Unavailable = new ActiveConfigurationSnapshot(
                HashMaterial("rk-active-configuration-v1\n<unavailable>"),
                0,
                0,
                0,
                isTruncated: false,
                isUsable: false);

            public static ActiveConfigurationSnapshot Truncated(
                long newestTicks,
                int fileCount,
                long bytesHashed) =>
                new ActiveConfigurationSnapshot(
                    HashMaterial("rk-active-configuration-v1\n<budget-exceeded>"),
                    newestTicks,
                    fileCount,
                    bytesHashed,
                    isTruncated: true,
                    isUsable: true);

            public ActiveConfigurationSnapshot(
                string identity,
                long newestTicks,
                int fileCount,
                long bytesHashed,
                bool isTruncated,
                bool isUsable)
            {
                Identity = identity;
                NewestTicks = newestTicks;
                FileCount = fileCount;
                BytesHashed = bytesHashed;
                IsTruncated = isTruncated;
                IsUsable = isUsable;
            }

            public string Identity { get; }

            public long NewestTicks { get; }

            public int FileCount { get; }

            public long BytesHashed { get; }

            public bool IsTruncated { get; }

            public bool IsUsable { get; }
        }

        /// <summary>
        /// The plugin-configuration root. Prefers the host's own
        /// <c>PluginConfigurationsPath</c>; falls back to the
        /// <c>configurations</c> folder Jellyfin keeps beside the plugin
        /// folders.
        /// </summary>
        private static string ResolveConfigurationsPath(string pluginsPath)
        {
            try
            {
                var fromHost = Plugin.Paths?.PluginConfigurationsPath;
                if (!string.IsNullOrEmpty(fromHost))
                {
                    return fromHost!;
                }
            }
            catch
            {
                // Fall through to the conventional layout.
            }

            return string.IsNullOrEmpty(pluginsPath)
                ? string.Empty
                : Path.Combine(pluginsPath, ConfigurationsFolderName);
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

    /// <summary>Path-independent fingerprint of the loaded Jellyfin host.</summary>
    public sealed class HostFingerprint
    {
        internal static readonly HostFingerprint Empty = new HostFingerprint(
            PluginGenerationProvider.HashMaterial("rk-loaded-jellyfin-host-v1"),
            Array.Empty<string>());

        internal HostFingerprint(string identity, IReadOnlyList<string> modules)
        {
            Identity = identity ?? string.Empty;
            Modules = modules?.ToArray() ?? Array.Empty<string>();
        }

        public string Identity { get; }

        /// <summary>Loaded assembly name, version and MVID; absolute paths are excluded.</summary>
        public IReadOnlyList<string> Modules { get; }
    }

    /// <summary>One assembly module that is loaded in the current server process.</summary>
    internal sealed class LoadedModuleFingerprint
    {
        public LoadedModuleFingerprint(string path, string name, string version, Guid moduleVersionId)
        {
            Path = path ?? string.Empty;
            Name = name ?? string.Empty;
            Version = version ?? string.Empty;
            ModuleVersionId = moduleVersionId;
        }

        public string Path { get; }

        public string Name { get; }

        public string Version { get; }

        public Guid ModuleVersionId { get; }

        internal string ToMaterial() => string.Format(
            CultureInfo.InvariantCulture,
            "{0}|{1}|{2:N}",
            Name,
            Version,
            ModuleVersionId);
    }

    /// <summary>
    /// Stable, path-independent description of one plugin whose assemblies are
    /// loaded in the current server process.
    /// </summary>
    internal sealed class ActivePluginDescriptor
    {
        public ActivePluginDescriptor(
            string directoryPath,
            string id,
            string version,
            string manifestStatus,
            IReadOnlyList<LoadedModuleFingerprint> modules,
            IReadOnlyList<string> configurationFileNames)
        {
            DirectoryPath = directoryPath ?? string.Empty;
            Folder = Path.GetFileName(
                DirectoryPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            Id = id ?? string.Empty;
            Version = version ?? string.Empty;
            ManifestStatus = manifestStatus ?? string.Empty;
            Modules = modules
                .OrderBy(module => module.ToMaterial(), StringComparer.Ordinal)
                .ToList();
            ConfigurationFileNames = configurationFileNames
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .OrderBy(name => name, StringComparer.Ordinal)
                .ToList();
            AssemblyNames = Modules
                .Select(module => module.Name)
                .Where(name => name.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .OrderBy(name => name, StringComparer.Ordinal)
                .ToList();

            var moduleMaterial = new StringBuilder("rk-loaded-modules-v1");
            foreach (var module in Modules)
            {
                moduleMaterial.Append('\n').Append(module.ToMaterial());
            }

            ModuleIdentity = Convert.ToHexString(
                    SHA256.HashData(Encoding.UTF8.GetBytes(moduleMaterial.ToString())),
                    0,
                    16)
                .ToLowerInvariant();
            // The loaded modules already carry their AssemblyName versions and
            // MVIDs. Excluding mutable manifest version text keeps this process-
            // lifetime key stable if an installer rewrites the record in place.
            StableIdentity = Id + "|" + ModuleIdentity;
        }

        public string DirectoryPath { get; }

        public string Folder { get; }

        public string Id { get; }

        public string Version { get; }

        public string ManifestStatus { get; }

        public IReadOnlyList<LoadedModuleFingerprint> Modules { get; }

        public IReadOnlyList<string> AssemblyNames { get; }

        public IReadOnlyList<string> ConfigurationFileNames { get; }

        public string ModuleIdentity { get; }

        public string StableIdentity { get; }
    }

    /// <summary>One loaded plugin's contribution to the generation.</summary>
    public sealed class PluginFingerprint
    {
        internal PluginFingerprint(
            string folder,
            string id,
            string version,
            string status,
            long newestDllTicks,
            long newestConfigTicks,
            string loadedModuleIdentity,
            string assetIdentity,
            int assetFileCount,
            int assetDirectoriesScanned,
            long assetBytesHashed,
            bool assetScanTruncated,
            bool assetScanUnavailable,
            bool usingLastGoodAssets,
            string configurationIdentity,
            int configurationFileCount,
            long configurationBytesHashed,
            bool configurationScanTruncated,
            bool configurationScanUnavailable,
            bool usingLastGoodConfiguration,
            bool usingLastKnownPluginRecord)
        {
            Folder = folder;
            Id = id;
            Version = version;
            Status = status;
            NewestDllTicks = newestDllTicks;
            NewestConfigTicks = newestConfigTicks;
            IsLoaded = true;
            LoadedModuleIdentity = loadedModuleIdentity;
            AssetIdentity = assetIdentity;
            AssetFileCount = assetFileCount;
            AssetDirectoriesScanned = assetDirectoriesScanned;
            AssetBytesHashed = assetBytesHashed;
            AssetScanTruncated = assetScanTruncated;
            AssetScanUnavailable = assetScanUnavailable;
            UsingLastGoodAssets = usingLastGoodAssets;
            ConfigurationIdentity = configurationIdentity;
            ConfigurationFileCount = configurationFileCount;
            ConfigurationBytesHashed = configurationBytesHashed;
            ConfigurationScanTruncated = configurationScanTruncated;
            ConfigurationScanUnavailable = configurationScanUnavailable;
            UsingLastGoodConfiguration = usingLastGoodConfiguration;
            UsingLastKnownPluginRecord = usingLastKnownPluginRecord;
        }

        public string Folder { get; }

        public string Id { get; }

        public string Version { get; }

        /// <summary>
        /// Current manifest status for diagnostics. It is deliberately not
        /// folded: staged status changes do not alter already-loaded code.
        /// </summary>
        public string Status { get; }

        /// <summary>
        /// Historical diagnostics property name. For loaded-state rows this is
        /// the newest active asset timestamp; DLL identity comes from loaded MVIDs.
        /// </summary>
        public long NewestDllTicks { get; }

        /// <summary>Newest loose active-asset timestamp, for diagnostics only.</summary>
        public long NewestAssetTicks => NewestDllTicks;

        /// <summary>
        /// Newest write time of this plugin's exact configuration file, for
        /// diagnostics only. Content identity is what the generation folds.
        /// </summary>
        public long NewestConfigTicks { get; }

        /// <summary>Whether this row represents assemblies loaded in the current process.</summary>
        public bool IsLoaded { get; }

        /// <summary>Path-independent hash of the loaded modules' names, versions and MVIDs.</summary>
        public string LoadedModuleIdentity { get; } = string.Empty;

        /// <summary>Path-independent hash of the active plugin's loose client assets.</summary>
        public string AssetIdentity { get; } = string.Empty;

        public int AssetFileCount { get; }

        public int AssetDirectoriesScanned { get; }

        public long AssetBytesHashed { get; }

        public bool AssetScanTruncated { get; }

        public bool AssetScanUnavailable { get; }

        public bool UsingLastGoodAssets { get; }

        /// <summary>Path- and timestamp-independent identity of exact active config files.</summary>
        public string ConfigurationIdentity { get; } = string.Empty;

        public int ConfigurationFileCount { get; }

        public long ConfigurationBytesHashed { get; }

        public bool ConfigurationScanTruncated { get; }

        public bool ConfigurationScanUnavailable { get; }

        public bool UsingLastGoodConfiguration { get; }

        /// <summary>
        /// Jellyfin removed the disk/plugin-manager record, but the assembly is
        /// still loaded and this process-scoped last-known descriptor is active.
        /// </summary>
        public bool UsingLastKnownPluginRecord { get; }

        internal PluginFingerprint AsRetainedPluginRecord() =>
            new PluginFingerprint(
                Folder,
                Id,
                Version,
                Status,
                NewestDllTicks,
                NewestConfigTicks,
                LoadedModuleIdentity,
                AssetIdentity,
                AssetFileCount,
                AssetDirectoriesScanned,
                AssetBytesHashed,
                AssetScanTruncated,
                AssetScanUnavailable,
                true,
                ConfigurationIdentity,
                ConfigurationFileCount,
                ConfigurationBytesHashed,
                ConfigurationScanTruncated,
                ConfigurationScanUnavailable,
                true,
                usingLastKnownPluginRecord: true);

        internal string ToMaterial()
        {
            // Folder/path, manifest version/status and timestamps are diagnostics
            // only. This is the code/content the process actually loaded.
            return string.Format(
                CultureInfo.InvariantCulture,
                "loaded|{0}|{1}|{2}|{3}",
                Id,
                LoadedModuleIdentity,
                AssetIdentity,
                ConfigurationIdentity);
        }
    }
}
