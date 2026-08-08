using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

[assembly: System.Runtime.CompilerServices.InternalsVisibleTo("Jellyfin.Plugin.RefreshKit.Tests")]

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
    /// already knows — for every folder under <c>PluginsPath</c>: the plugin id,
    /// version and status out of its <c>meta.json</c>, plus the NEWEST write-ticks
    /// across the binaries and client assets in that folder.
    /// </para>
    ///
    /// <para>WHY THOSE INPUTS</para>
    /// <list type="bullet">
    /// <item><description><b>id</b> — installing or removing a plugin changes the
    /// set even when nothing else moves.</description></item>
    /// <item><description><b>version</b> — a marketplace upgrade lands in a NEW
    /// folder (<c>Name_1.2.3.0</c>), so the version moves with it.</description></item>
    /// <item><description><b>status</b> — the <c>meta.json</c> field Jellyfin
    /// rewrites when an admin ENABLES or DISABLES a plugin. Measured on 10.11.11:
    /// a disable flips <c>"status": "Active"</c> to <c>"Disabled"</c> in place and
    /// changes NOTHING else — same folder, same version, byte-identical binaries
    /// with untouched timestamps. Without this field a disable was invisible and
    /// every open tab kept running the disabled plugin's code.</description></item>
    /// <item><description><b>newest binary/client-asset ticks</b> — the case
    /// version alone misses: a same-version binary replaced in place (a dev
    /// copying a DLL over, a re-published build), or a <c>.js</c>/<c>.css</c>
    /// edited without the DLL moving at all. This is exactly why
    /// RefreshKit.CacheKey carries the DLL ticks for its own assembly.</description></item>
    /// <item><description><b>newest CONFIGURATION ticks</b> — the case the binary
    /// misses entirely, and the one users hit most often: an admin enables a
    /// custom tab in a plugin's settings, or flips any option that the plugin
    /// renders from at page load. Nothing on disk changes except one XML file
    /// under <c>plugins/configurations</c>, yet every open tab is now running
    /// UI built from the old settings. A config save is a client-visible change
    /// and must move the generation.</description></item>
    /// </list>
    ///
    /// <para>
    /// Configuration files are matched to a plugin by ASSEMBLY NAME, which is
    /// how Jellyfin names them: the plugin folder holding
    /// <c>Jellyfin.Plugin.JellyfinEnhanced.dll</c> owns
    /// <c>configurations/Jellyfin.Plugin.JellyfinEnhanced.xml</c>. Only that
    /// file — Jellyfin's own plugin-configuration store, the one the dashboard
    /// writes when an admin saves settings — is watched.
    /// </para>
    ///
    /// <para>
    /// WHAT IS DELIBERATELY NOT WATCHED, and why. Plugins also get a private
    /// <c>configurations/&lt;assembly&gt;/</c> DIRECTORY, and what lands there
    /// is not admin settings. Measured on a live 10.11.11 server with Jellyfin
    /// Enhanced 12.1.0.0 installed, that directory holds
    /// <c>&lt;userId&gt;/settings.json</c> (PER-USER preferences),
    /// <c>tag-cache.json</c> and a <c>cdn-cache/</c> tree (runtime caches).
    /// Saving one user's personal preference rewrites
    /// <c>&lt;userId&gt;/settings.json</c> and leaves the admin XML untouched —
    /// so watching the directory would reload EVERY client on the server
    /// because ONE user toggled a personal setting, and the runtime caches
    /// would move the generation on their own schedule with no user-visible
    /// change at all. The signal has to stay clean: the client-side safety net
    /// (idle gate, media gate, reload budget, flap guard) is a backstop, not a
    /// licence to emit noise.
    /// </para>
    ///
    /// <para>
    /// Even watching only the XML, a plugin is free to persist churn there, so
    /// config-driven bumps additionally go through a DEBOUNCE (a burst of
    /// writes coalesces into one) and a per-plugin, LEADING-EDGE COOLDOWN: the
    /// change that opens a window publishes at once, and only further changes
    /// inside that
    /// <see cref="Configuration.PluginConfiguration.ConfigCooldownMinutes"/>
    /// window are held and coalesced into one publish at its end.
    /// Version/DLL changes bypass both — a new binary is unambiguous and rare.
    /// Admins can turn config watching off globally or exclude individual
    /// plugins (<see cref="Configuration.PluginConfiguration.ConfigWatchExclusions"/>).
    /// </para>
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

        /// <summary>
        /// Hard cap on DIRECTORIES descended per plugin folder. The file cap
        /// alone does not bound a recursive enumeration: the walk still visits
        /// every directory looking for matches, so a deep tree costs a full
        /// traversal every <see cref="CacheTtlSeconds"/> seconds however few
        /// files match. Both budgets are enforced in
        /// <see cref="ScanFolderAssets"/>.
        /// </summary>
        private const int MaxDirectoriesPerPlugin = 512;

        /// <summary>
        /// Extensions whose write time is folded into the generation alongside
        /// the plugin's assemblies: the files a browser actually executes or
        /// renders. Deliberately a short, fixed list — a plugin's runtime data
        /// files must not move the generation.
        /// </summary>
        private static readonly string[] ClientAssetExtensions =
        {
            ".js", ".mjs", ".css", ".map", ".html",
        };

        /// <summary>Jellyfin's plugin-configuration folder, a sibling of the plugin folders.</summary>
        private const string ConfigurationsFolderName = "configurations";

        /// <summary>
        /// How long a new configuration timestamp must stand still before it is
        /// allowed to move the generation. A settings page that writes three
        /// times as the admin clicks Save is one change, not three.
        /// </summary>
        private const int ConfigDebounceSeconds = 10;

        private static readonly Lazy<PluginGenerationProvider> _instance =
            new Lazy<PluginGenerationProvider>(() => new PluginGenerationProvider());

        private readonly object _lock = new object();
        private readonly Dictionary<string, ConfigSignal> _configSignals =
            new Dictionary<string, ConfigSignal>(StringComparer.Ordinal);

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

                var details = ScanPlugins(now);
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

        private IReadOnlyList<PluginFingerprint> ScanPlugins(DateTime now)
        {
            var pluginsPath = ResolvePluginsPath();
            if (string.IsNullOrEmpty(pluginsPath) || !Directory.Exists(pluginsPath))
            {
                return Array.Empty<PluginFingerprint>();
            }

            var configurationsPath = ResolveConfigurationsPath(pluginsPath);
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
                if (string.Equals(
                        Path.GetFileName(directory),
                        ConfigurationsFolderName,
                        StringComparison.OrdinalIgnoreCase))
                {
                    // "plugins/configurations" is not a plugin.
                    continue;
                }

                try
                {
                    results.Add(Fingerprint(directory, configurationsPath, now));
                }
                catch
                {
                    // One unreadable plugin folder must not blind the aggregate
                    // to the other twenty. Record its name so it still counts.
                    results.Add(new PluginFingerprint(
                        Path.GetFileName(directory), string.Empty, string.Empty, string.Empty, 0, 0));
                }
            }

            PruneConfigSignals(results);
            return results;
        }

        /// <summary>
        /// Drops debounce/cooldown state for plugins that no longer exist, so an
        /// uninstall-reinstall cycle starts clean and the dictionary cannot grow
        /// without bound over a long uptime.
        /// </summary>
        private void PruneConfigSignals(IReadOnlyList<PluginFingerprint> results)
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

        internal PluginFingerprint Fingerprint(string directory, string configurationsPath, DateTime now)
        {
            var folder = Path.GetFileName(directory);
            var id = string.Empty;
            var version = string.Empty;

            var status = string.Empty;

            var metaPath = Path.Combine(directory, "meta.json");
            if (File.Exists(metaPath))
            {
                try
                {
                    using var document = JsonDocument.Parse(File.ReadAllBytes(metaPath));
                    id = ReadStringProperty(document.RootElement, "guid");
                    version = ReadStringProperty(document.RootElement, "version");
                    status = ReadStringProperty(document.RootElement, "status");
                }
                catch
                {
                    // A hand-edited or truncated meta.json falls back to the
                    // folder name, which already encodes "Name_1.2.3.0".
                }
            }

            var assets = ScanFolderAssets(directory);
            var rawConfigTicks = ObserveConfigurationTicks(configurationsPath, assets.AssemblyNames);
            var publishedConfigTicks = PublishConfigTicks(folder, id, assets.AssemblyNames, rawConfigTicks, now);

            return new PluginFingerprint(folder, id, version, status, assets.NewestTicks, publishedConfigTicks);
        }

        /// <summary>
        /// One BOUNDED walk of a plugin folder, collecting the newest write time
        /// across the files that can change what a browser runs, plus the
        /// assembly names used to locate the plugin's configuration XML.
        ///
        /// <para>WHY THE WALK IS HAND-ROLLED</para>
        /// <para>
        /// <c>Directory.EnumerateFiles(dir, "*.dll", AllDirectories)</c> reads as
        /// if the file cap bounded it, but the cap can only count files the
        /// enumerator YIELDS — the traversal itself still descends every
        /// directory in the tree looking for matches. A plugin shipping a large
        /// asset tree (or caching into its own folder) therefore turned a scan
        /// that runs every <see cref="CacheTtlSeconds"/> seconds into a full-tree
        /// walk. Both the directories visited and the files examined are budgeted
        /// here, so the per-scan cost is bounded no matter what is on disk.
        /// </para>
        ///
        /// <para>WHY MORE THAN DLLs</para>
        /// <para>
        /// A plugin's client code is its <c>.js</c>/<c>.css</c>, and those change
        /// without the DLL changing — a developer editing a script in place, or a
        /// rebuild that preserves the binary's timestamp. Watching a small, fixed
        /// set of CLIENT-ASSET extensions alongside the binary costs nothing
        /// extra (it is the same traversal, a wider filter) and closes the case
        /// where the page changed but the generation did not. Data files a plugin
        /// writes at runtime (<c>.json</c>, <c>.db</c>, logs) are deliberately
        /// NOT included: they move on their own schedule with no user-visible
        /// change, which is exactly the noise this signal must not carry.
        /// </para>
        /// </summary>
        private static FolderAssets ScanFolderAssets(string directory)
        {
            long newestTicks = 0;
            var assemblyNames = new List<string>();
            var filesSeen = 0;
            var directoriesSeen = 0;

            var pending = new Stack<string>();
            pending.Push(directory);

            while (pending.Count > 0 && directoriesSeen < MaxDirectoriesPerPlugin && filesSeen < MaxFilesPerPlugin)
            {
                var current = pending.Pop();
                directoriesSeen++;

                try
                {
                    foreach (var file in Directory.EnumerateFiles(current))
                    {
                        if (++filesSeen > MaxFilesPerPlugin)
                        {
                            break;
                        }

                        var extension = Path.GetExtension(file);
                        var isAssembly = extension.Equals(".dll", StringComparison.OrdinalIgnoreCase);
                        if (isAssembly)
                        {
                            assemblyNames.Add(Path.GetFileNameWithoutExtension(file));
                        }
                        else if (!IsClientAsset(extension))
                        {
                            continue;
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

                    foreach (var subdirectory in Directory.EnumerateDirectories(current))
                    {
                        pending.Push(subdirectory);
                    }
                }
                catch
                {
                    // An unreadable subdirectory must not abandon the folder.
                }
            }

            return new FolderAssets(newestTicks, assemblyNames);
        }

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

        /// <summary>What one bounded folder walk found.</summary>
        private readonly struct FolderAssets
        {
            public FolderAssets(long newestTicks, List<string> assemblyNames)
            {
                NewestTicks = newestTicks;
                AssemblyNames = assemblyNames;
            }

            public long NewestTicks { get; }

            public List<string> AssemblyNames { get; }
        }

        /// <summary>
        /// Newest write time of the plugin-configuration files Jellyfin itself
        /// writes for the assemblies in one plugin folder:
        /// <c>&lt;assembly&gt;.xml</c>, and nothing else (see the class doc for
        /// why the private <c>&lt;assembly&gt;/</c> directory is excluded).
        /// </summary>
        private static long ObserveConfigurationTicks(string configurationsPath, IReadOnlyList<string> assemblyNames)
        {
            if (string.IsNullOrEmpty(configurationsPath) || assemblyNames.Count == 0)
            {
                return 0;
            }

            long newest = 0;
            foreach (var assemblyName in assemblyNames)
            {
                if (string.IsNullOrEmpty(assemblyName))
                {
                    continue;
                }

                try
                {
                    var file = Path.Combine(configurationsPath, assemblyName + ".xml");
                    if (File.Exists(file))
                    {
                        var ticks = File.GetLastWriteTimeUtc(file).Ticks;
                        if (ticks > newest)
                        {
                            newest = ticks;
                        }
                    }
                }
                catch
                {
                    // A config file being rewritten as we look at it is normal;
                    // the next scan (>= CacheTtlSeconds later) sees the result.
                }
            }

            return newest;
        }

        /// <summary>
        /// The debounce + cooldown gate between "the config file changed" and
        /// "every open client reloads".
        ///
        /// <list type="bullet">
        /// <item><description>FIRST OBSERVATION of a plugin adopts whatever is on
        /// disk silently — a server restart is not a settings change.</description></item>
        /// <item><description>DEBOUNCE: a new timestamp must stand still for
        /// <see cref="ConfigDebounceSeconds"/>; each further write restarts the
        /// clock, so a burst collapses into one bump.</description></item>
        /// <item><description>COOLDOWN, LEADING EDGE: a change that arrives while
        /// no cooldown window is open publishes IMMEDIATELY and opens a window of
        /// <see cref="Configuration.PluginConfiguration.ConfigCooldownMinutes"/>.
        /// Only changes that arrive *inside* that window are held; they coalesce
        /// into a single publish at the window's end, carrying the latest
        /// timestamp. Nothing is ever dropped.</description></item>
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
        private long PublishConfigTicks(
            string folder,
            string id,
            IReadOnlyList<string> assemblyNames,
            long rawTicks,
            DateTime now)
        {
            var configuration = ReadConfiguration();
            if (configuration?.EnableConfigWatching == false
                || IsConfigWatchExcluded(configuration, folder, id, assemblyNames))
            {
                // Excluded plugins fall back to version/DLL detection only.
                _configSignals.Remove(folder);
                return 0;
            }

            if (!_configSignals.TryGetValue(folder, out var signal))
            {
                signal = new ConfigSignal { PublishedTicks = rawTicks };
                _configSignals[folder] = signal;
                return signal.PublishedTicks;
            }

            if (rawTicks == signal.PublishedTicks)
            {
                signal.PendingTicks = 0;
                return signal.PublishedTicks;
            }

            if (rawTicks != signal.PendingTicks)
            {
                signal.PendingTicks = rawTicks;
                signal.PendingFirstSeenUtc = now;
                return signal.PublishedTicks;
            }

            if ((now - signal.PendingFirstSeenUtc).TotalSeconds < ConfigDebounceSeconds)
            {
                return signal.PublishedTicks;
            }

            var cooldownMinutes = Math.Clamp(configuration?.ConfigCooldownMinutes ?? 5, 0, 1440);
            var windowOpen = signal.CooldownUntilUtc != DateTime.MinValue;

            if (windowOpen && now < signal.CooldownUntilUtc)
            {
                // Inside the window: hold the newest timestamp until the window ends.
                return signal.PublishedTicks;
            }

            // A change first seen while the window was still running is a held
            // (trailing) publish; one first seen after the window had already
            // expired is a fresh leading edge.
            var wasHeldBack = windowOpen && signal.PendingFirstSeenUtc < signal.CooldownUntilUtc;

            signal.PublishedTicks = rawTicks;
            signal.PendingTicks = 0;
            signal.CooldownUntilUtc = wasHeldBack || cooldownMinutes <= 0
                ? DateTime.MinValue
                : now.AddMinutes(cooldownMinutes);
            return signal.PublishedTicks;
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
            public long PublishedTicks { get; set; }

            /// <summary>A newer value waiting out the debounce and/or cooldown.</summary>
            public long PendingTicks { get; set; }

            public DateTime PendingFirstSeenUtc { get; set; }

            /// <summary>
            /// End of the open cooldown window, or <see cref="DateTime.MinValue"/>
            /// when no window is open — in which case the next debounced change
            /// publishes on the leading edge.
            /// </summary>
            public DateTime CooldownUntilUtc { get; set; } = DateTime.MinValue;
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

    /// <summary>One installed plugin's contribution to the generation.</summary>
    public sealed class PluginFingerprint
    {
        public PluginFingerprint(
            string folder,
            string id,
            string version,
            string status,
            long newestDllTicks,
            long newestConfigTicks)
        {
            Folder = folder;
            Id = id;
            Version = version;
            Status = status;
            NewestDllTicks = newestDllTicks;
            NewestConfigTicks = newestConfigTicks;
        }

        public string Folder { get; }

        public string Id { get; }

        public string Version { get; }

        /// <summary>
        /// The plugin's <c>status</c> from <c>meta.json</c> ("Active",
        /// "Disabled", "Malfunctioned", …). Jellyfin rewrites this field in
        /// place when an admin enables or disables a plugin — nothing else on
        /// disk moves, so without it a disable is invisible to the generation.
        /// Verified on 10.11.11: disabling flips exactly this field and leaves
        /// the folder name, version and every binary timestamp untouched.
        /// </summary>
        public string Status { get; }

        /// <summary>
        /// Newest write time across this plugin's assemblies AND its client
        /// assets (.js/.mjs/.css/.map/.html) — see
        /// <see cref="PluginGenerationProvider"/> for why both.
        /// </summary>
        public long NewestDllTicks { get; }

        /// <summary>
        /// Newest write time of this plugin's configuration (settings saved from
        /// the dashboard). Zero when the plugin has never been configured.
        /// </summary>
        public long NewestConfigTicks { get; }

        internal string ToMaterial() => string.Format(
            CultureInfo.InvariantCulture,
            "{0}|{1}|{2}|{3}|{4}|{5}",
            Folder,
            Id,
            Version,
            Status,
            NewestDllTicks,
            NewestConfigTicks);
    }
}
