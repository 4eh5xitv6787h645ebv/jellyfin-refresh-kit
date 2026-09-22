using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Jellyfin.Plugin.RefreshKit.Controllers;
using MediaBrowser.Model.Plugins;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// Acceptance criteria for how the generation provider charges budget and
    /// folds identity when a plugin's asset walk fails partially or entirely,
    /// and for how the settings cooldown follows its setting. Ported from the
    /// bug-hunt fixtures; each test names the contract clause it protects.
    /// </summary>
    public sealed class ProviderFailureHandlingTests : IDisposable
    {
        private static readonly DateTime FixedTimestamp = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        private static readonly string AaaId = "00000000-0000-0000-0000-000000000001";
        private static readonly string EeeId = "ffffffff-ffff-ffff-ffff-ffffffffffff";
        private readonly string _root;
        private readonly string _configurations;
        private readonly List<string> _lockedPaths = new List<string>();

        public ProviderFailureHandlingTests()
        {
            _root = Path.Combine(Path.GetTempPath(), "rk-failure-tests-" + Guid.NewGuid().ToString("N"));
            _configurations = Path.Combine(_root, "configurations");
            Directory.CreateDirectory(_configurations);
        }

        public void Dispose()
        {
            try
            {
                if (!OperatingSystem.IsWindows())
                {
                    foreach (var locked in _lockedPaths)
                    {
                        try
                        {
                            File.SetUnixFileMode(
                                locked,
                                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
                        }
                        catch
                        {
                            // Best effort: a leftover fixture is not a product failure.
                        }
                    }
                }

                Directory.Delete(_root, recursive: true);
            }
            catch
            {
                // A leftover disposable fixture is not a product failure.
            }
        }

        // ---------------------------------------------------------------
        // F1: a transient failure reserves the last-good snapshot's charge,
        // never the per-plugin ceiling, so the NEXT plugin in stable-identity
        // order keeps the capacity it had and the generation does not flap.
        // ---------------------------------------------------------------

        [Fact]
        public void TransientRootEnumerationFailureReservesLastGoodChargeInsteadOfCeiling()
        {
            var a = Plugin("Aaa", AaaId, "a body");
            var e = Plugin("Eee", EeeId, "e body");
            var failA = false;
            var provider = new PluginGenerationProvider(
                () => new[] { a, e },
                _configurations,
                scanLimits: TightEntryLimits(),
                fileSystemEntriesProvider: path =>
                {
                    if (failA && path.Equals(a.DirectoryPath, StringComparison.Ordinal))
                    {
                        throw new IOException("transient");
                    }

                    return Directory.EnumerateFileSystemEntries(path);
                });

            var g1 = provider.Snapshot;
            failA = true;
            provider.Invalidate();
            var g2 = provider.Snapshot;
            failA = false;
            provider.Invalidate();
            var g3 = provider.Snapshot;

            var aRow2 = Row(g2, "Aaa");
            var eRow2 = Row(g2, "Eee");
            Assert.True(aRow2.UsingLastGoodAssets);
            Assert.True(aRow2.AssetScanUnavailable);
            Assert.Equal(Row(g1, "Aaa").AssetIdentity, aRow2.AssetIdentity);
            // E did not change on disk; A's failure must not push it over the cap.
            Assert.False(eRow2.AssetScanTruncated);
            Assert.Equal(Row(g1, "Eee").AssetIdentity, eRow2.AssetIdentity);
            Assert.Equal(g1.Generation, g2.Generation);
            Assert.Equal(g1.Generation, g3.Generation);
        }

        [Fact]
        public void EntryUnlinkedAfterReaddirRetainsLastGoodWithoutTruncatingUnrelatedPlugin()
        {
            // readdir yielded an entry that was unlinked before it could be
            // stat'ed — an uninstall's recursive delete racing the scan. No
            // injected throw: File.GetAttributes fails on its own.
            var a = Plugin("Aaa", AaaId, "a body");
            var e = Plugin("Eee", EeeId, "e body");
            var race = false;
            var provider = new PluginGenerationProvider(
                () => new[] { a, e },
                _configurations,
                scanLimits: TightEntryLimits(),
                fileSystemEntriesProvider: path =>
                {
                    var entries = Directory.EnumerateFileSystemEntries(path).ToList();
                    if (race && path.Equals(a.DirectoryPath, StringComparison.Ordinal))
                    {
                        entries.Add(Path.Combine(path, "vanished.tmp"));
                    }

                    return entries;
                });

            var g1 = provider.Snapshot;
            race = true;
            provider.Invalidate();
            var g2 = provider.Snapshot;
            race = false;
            provider.Invalidate();
            var g3 = provider.Snapshot;

            Assert.True(Row(g2, "Aaa").UsingLastGoodAssets);
            Assert.False(Row(g2, "Eee").AssetScanTruncated);
            Assert.Equal(g1.Generation, g2.Generation);
            Assert.Equal(g1.Generation, g3.Generation);
        }

        [Fact]
        public void WalkFailureWithoutLastGoodSnapshotStillReservesTheCeiling()
        {
            // With nothing coherent to fall back on, the charge must not depend
            // on how far the native enumerator got before failing, so the
            // per-plugin ceiling is the only deterministic reservation.
            var a = Plugin("Aaa", AaaId, "a body");
            var e = Plugin("Eee", EeeId, "e body");
            var failA = true;
            var provider = new PluginGenerationProvider(
                () => new[] { a, e },
                _configurations,
                scanLimits: TightEntryLimits(),
                fileSystemEntriesProvider: path =>
                {
                    if (failA && path.Equals(a.DirectoryPath, StringComparison.Ordinal))
                    {
                        throw new IOException("transient");
                    }

                    return Directory.EnumerateFileSystemEntries(path);
                });

            var first = provider.Snapshot;
            Assert.True(Row(first, "Aaa").AssetScanUnavailable);
            Assert.False(Row(first, "Aaa").UsingLastGoodAssets);
            Assert.True(Row(first, "Eee").AssetScanTruncated);

            failA = false;
            provider.Invalidate();
            var second = provider.Snapshot;
            Assert.False(Row(second, "Aaa").AssetScanUnavailable);
            Assert.False(Row(second, "Eee").AssetScanTruncated);
        }

        [Fact]
        public void ContentRaceWithLastGoodSnapshotKeepsDownstreamCapacityOfThatSnapshot()
        {
            // A read that races a writer reserves what the failed attempt cost,
            // topped up to the last-good charge — and nothing beyond it.
            var a = Plugin("Aaa", AaaId, "aa");
            var e = Plugin("Eee", EeeId, "ee");
            var failRead = false;
            var provider = new PluginGenerationProvider(
                () => new[] { a, e },
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalAssetBytesPerScan: 4),
                beforeContentRead: path =>
                {
                    if (failRead && path.StartsWith(a.DirectoryPath, StringComparison.Ordinal))
                    {
                        throw new IOException("racing writer");
                    }
                });

            var g1 = provider.Snapshot;
            Assert.False(Row(g1, "Eee").AssetScanTruncated);

            failRead = true;
            provider.Invalidate();
            var g2 = provider.Snapshot;
            Assert.True(Row(g2, "Aaa").UsingLastGoodAssets);
            Assert.False(Row(g2, "Eee").AssetScanTruncated);
            Assert.Equal(g1.Generation, g2.Generation);
        }

        // ---------------------------------------------------------------
        // F2: one unreadable entry skips that entry, not the plugin.
        // ---------------------------------------------------------------

        [Fact]
        public void UnreadableSubdirectoryIsSkippedCountedAndFoldedAsStableSentinel()
        {
            if (!CanTestPermissionDenied())
            {
                return;
            }

            var perm = Plugin("Perm", AaaId, "real body");
            var locked = Path.Combine(perm.DirectoryPath, "web", "locked");
            Directory.CreateDirectory(locked);
            Lock(locked);
            var e = Plugin("Zzz", EeeId, "e body");
            var provider = new PluginGenerationProvider(
                () => new[] { perm, e },
                _configurations,
                scanLimits: TightEntryLimits());

            var first = provider.Snapshot;
            var permRow = Row(first, "Perm");
            Assert.False(permRow.AssetScanUnavailable);
            Assert.False(permRow.UsingLastGoodAssets);
            Assert.Equal(1, permRow.AssetFileCount);
            Assert.Equal(1, permRow.AssetEntriesUnreadable);
            Assert.False(Row(first, "Zzz").AssetScanTruncated);

            // Stable: the same unreadable set folds to the same identity.
            provider.Invalidate();
            var second = provider.Snapshot;
            Assert.Equal(first.Generation, second.Generation);
            Assert.Equal(permRow.AssetIdentity, Row(second, "Perm").AssetIdentity);

            // The sentinel is part of the identity: a node whose tree is readable
            // (same bytes, no locked directory) folds differently ...
            var cleanRoot = Path.Combine(_root, "clean-node");
            var clean = Plugin("Perm", AaaId, "real body", cleanRoot);
            var cleanProvider = new PluginGenerationProvider(() => new[] { clean }, Path.Combine(cleanRoot, "configurations"));
            var cleanIdentity = Assert.Single(cleanProvider.Details).AssetIdentity;
            Assert.NotEqual(cleanIdentity, permRow.AssetIdentity);

            // ... and once the directory becomes readable (and is empty), the
            // identity converges on the clean one: it moved only because the set
            // of unreadable entries changed.
            Unlock(locked);
            provider.Invalidate();
            var third = provider.Snapshot;
            Assert.Equal(0, Row(third, "Perm").AssetEntriesUnreadable);
            Assert.Equal(cleanIdentity, Row(third, "Perm").AssetIdentity);
        }

        [Fact]
        public void UnreadableAssetFileIsSkippedAndTheRestOfTheTreeStillFolds()
        {
            if (!CanTestPermissionDenied())
            {
                return;
            }

            var perm = Plugin("Perm", AaaId, "real body");
            var lockedFile = Path.Combine(perm.DirectoryPath, "web", "locked.js");
            File.WriteAllText(lockedFile, "cannot read me");
            Lock(lockedFile);
            var provider = new PluginGenerationProvider(() => new[] { perm }, _configurations);

            var row = Assert.Single(provider.Details);
            Assert.False(row.AssetScanUnavailable);
            Assert.Equal(1, row.AssetFileCount);
            Assert.Equal(1, row.AssetEntriesUnreadable);
            Assert.Equal("real body".Length, row.AssetBytesHashed);

            // The readable asset is still folded: changing it moves the generation.
            var before = provider.Generation;
            File.WriteAllText(Path.Combine(perm.DirectoryPath, "web", "client.js"), "changed body");
            provider.Invalidate();
            Assert.NotEqual(before, provider.Generation);
        }

        [Fact]
        public void InaccessibleConfigurationDirectoryRetainsLastGoodInsteadOfPublishingDeletion()
        {
            if (!CanTestPermissionDenied())
            {
                return;
            }

            const string configName = "Jellyfin.Plugin.Demo.xml";
            var configFile = Path.Combine(_configurations, configName);
            File.WriteAllText(configFile, "<settings/>");
            var plugin = Plugin("ConfigPermissions", AaaId, "body", configNames: new[] { configName });
            var now = FixedTimestamp;
            var provider = new PluginGenerationProvider(
                () => new[] { plugin },
                _configurations,
                utcNow: () => now,
                configurationProvider: () => new Configuration.PluginConfiguration { ConfigCooldownMinutes = 0 });
            var before = provider.Snapshot;

            Lock(_configurations);
            Assert.Equal(before.Generation, Settle(provider, ref now));
            var unavailable = Assert.Single(provider.Details);
            Assert.True(unavailable.ConfigurationScanUnavailable);
            Assert.True(unavailable.UsingLastGoodConfiguration);
            Assert.Equal(1, unavailable.ConfigurationFileCount);

            Unlock(_configurations);
            Assert.Equal(before.Generation, Settle(provider, ref now));
            Assert.False(Assert.Single(provider.Details).ConfigurationScanUnavailable);

            // A real deletion remains observable once access is restored.
            File.Delete(configFile);
            Assert.NotEqual(before.Generation, Settle(provider, ref now));
            Assert.Equal(0, Assert.Single(provider.Details).ConfigurationFileCount);
        }

        [Fact]
        public void RootThatCannotBeListedIsStillUnavailable()
        {
            if (!CanTestPermissionDenied())
            {
                return;
            }

            var perm = Plugin("Perm", AaaId, "real body");
            Lock(perm.DirectoryPath);
            var provider = new PluginGenerationProvider(() => new[] { perm }, _configurations);

            var row = Assert.Single(provider.Details);
            Assert.True(row.AssetScanUnavailable);
            Assert.Equal(0, row.AssetEntriesUnreadable);
        }

        [Fact]
        public void TransientIoErrorListingASubdirectoryRetainsLastGoodInsteadOfFoldingASentinel()
        {
            // An EIO while listing web/ during one scan is not a permission
            // problem: folding it as an unreadable sentinel would move the
            // identity now and move it back on the next scan — two reloads for
            // an error that never touched the assets.
            var a = Plugin("Aaa", AaaId, "a body");
            var web = Path.Combine(a.DirectoryPath, "web");
            var failWeb = false;
            var provider = new PluginGenerationProvider(
                () => new[] { a },
                _configurations,
                fileSystemEntriesProvider: path =>
                {
                    if (failWeb && path.Equals(web, StringComparison.Ordinal))
                    {
                        throw new IOException("EIO");
                    }

                    return Directory.EnumerateFileSystemEntries(path);
                });

            var g1 = provider.Snapshot;
            failWeb = true;
            provider.Invalidate();
            var g2 = provider.Snapshot;
            failWeb = false;
            provider.Invalidate();
            var g3 = provider.Snapshot;

            var row2 = Row(g2, "Aaa");
            Assert.True(row2.UsingLastGoodAssets);
            Assert.Equal(0, row2.AssetEntriesUnreadable);
            Assert.Equal(Row(g1, "Aaa").AssetIdentity, row2.AssetIdentity);
            Assert.Equal(g1.Generation, g2.Generation);
            Assert.Equal(g1.Generation, g3.Generation);
            Assert.False(Row(g3, "Aaa").UsingLastGoodAssets);
        }

        [Fact]
        public void PermissionDeniedListingASubdirectoryStillFoldsTheSentinel()
        {
            // The injected counterpart of the mode-000 fixture above: a listing
            // the process is not allowed to make is stable across scans, so it
            // is skipped, counted and folded rather than retained as last-good.
            var a = Plugin("Aaa", AaaId, "a body");
            var web = Path.Combine(a.DirectoryPath, "web");
            var denyWeb = false;
            var provider = new PluginGenerationProvider(
                () => new[] { a },
                _configurations,
                fileSystemEntriesProvider: path =>
                {
                    if (denyWeb && path.Equals(web, StringComparison.Ordinal))
                    {
                        throw new UnauthorizedAccessException("EACCES");
                    }

                    return Directory.EnumerateFileSystemEntries(path);
                });

            var readable = provider.Snapshot;
            denyWeb = true;
            provider.Invalidate();
            var denied = provider.Snapshot;
            provider.Invalidate();
            var deniedAgain = provider.Snapshot;

            var row = Row(denied, "Aaa");
            Assert.False(row.UsingLastGoodAssets);
            Assert.False(row.AssetScanUnavailable);
            Assert.Equal(1, row.AssetEntriesUnreadable);
            Assert.Equal(0, row.AssetFileCount);
            Assert.NotEqual(Row(readable, "Aaa").AssetIdentity, row.AssetIdentity);
            Assert.NotEqual(readable.Generation, denied.Generation);
            // Deterministic: the same unreadable set folds to the same identity.
            Assert.Equal(denied.Generation, deniedAgain.Generation);
        }

        // ---------------------------------------------------------------
        // F3: symlinked asset files and directories are not followed, and
        // that is reported instead of silent.
        // ---------------------------------------------------------------

        [Fact]
        public void SymlinkedAssetFileIsNotFollowedAndIsReported()
        {
            var q = Plugin("Nix", AaaId, "body");
            var link = Path.Combine(q.DirectoryPath, "web", "client.js");
            var outside = Path.Combine(_root, "nix-store", "client.js");
            Directory.CreateDirectory(Path.GetDirectoryName(outside)!);
            File.Move(link, outside);
            File.CreateSymbolicLink(link, outside);
            var provider = new PluginGenerationProvider(() => new[] { q }, _configurations);

            var row = Assert.Single(provider.Details);
            Assert.Equal(0, row.AssetFileCount);
            Assert.Equal(1, row.AssetReparsePointsSkipped);
            Assert.Equal(0, row.AssetEntriesUnreadable);
            Assert.False(row.AssetScanUnavailable);
            Assert.False(row.AssetScanTruncated);

            // Not followed: the link target is invisible to the generation.
            var before = provider.Generation;
            File.WriteAllText(outside, "changed body");
            provider.Invalidate();
            Assert.Equal(before, provider.Generation);
        }

        [Fact]
        public void SymlinkedAssetSubdirectoryIsNotFollowedAndIsReported()
        {
            var q = Plugin("NixDir", AaaId, "body");
            var web = Path.Combine(q.DirectoryPath, "web");
            var store = Path.Combine(_root, "store", "web");
            Directory.CreateDirectory(Path.GetDirectoryName(store)!);
            Directory.Move(web, store);
            Directory.CreateSymbolicLink(web, store);
            var provider = new PluginGenerationProvider(() => new[] { q }, _configurations);

            var row = Assert.Single(provider.Details);
            Assert.Equal(0, row.AssetFileCount);
            Assert.Equal(1, row.AssetReparsePointsSkipped);
            Assert.False(row.AssetScanUnavailable);
            Assert.False(row.AssetScanTruncated);

            var before = provider.Generation;
            File.WriteAllText(Path.Combine(store, "client.js"), "changed");
            provider.Invalidate();
            Assert.Equal(before, provider.Generation);
        }

        [Fact]
        public void DanglingSymlinkDoesNotHideEveryOtherAsset()
        {
            var p = Plugin("Dangling", AaaId, "real body");
            File.CreateSymbolicLink(
                Path.Combine(p.DirectoryPath, "web", "broken.js"),
                Path.Combine(_root, "nowhere.js"));
            var provider = new PluginGenerationProvider(() => new[] { p }, _configurations);

            var row = Assert.Single(provider.Details);
            Assert.False(row.AssetScanUnavailable);
            Assert.Equal(1, row.AssetFileCount);
            Assert.Equal(1, row.AssetReparsePointsSkipped);
        }

        [Fact]
        public void DiagnosticsPayloadProjectsAssetReparseAndUnreadableCounters()
        {
            var q = Plugin("Nix", AaaId, "body");
            var link = Path.Combine(q.DirectoryPath, "web", "client.js");
            var outside = Path.Combine(_root, "nix-store", "client.js");
            Directory.CreateDirectory(Path.GetDirectoryName(outside)!);
            File.Move(link, outside);
            File.CreateSymbolicLink(link, outside);
            var provider = new PluginGenerationProvider(() => new[] { q }, _configurations);
            var controller = new RefreshKitController(provider)
            {
                ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() },
            };

            var result = Assert.IsType<OkObjectResult>(controller.GetDiagnostics().Result);
            var payload = result.Value;
            Assert.NotNull(payload);
            var plugins = payload.GetType().GetProperty("Plugins")?.GetValue(payload) as IEnumerable;
            Assert.NotNull(plugins);
            var row = plugins.Cast<object>().Single();

            object? Read(string name)
            {
                var property = row.GetType().GetProperty(name);
                Assert.NotNull(property);
                return property.GetValue(row);
            }

            Assert.Equal(1, Read("AssetReparsePointsSkipped"));
            Assert.Equal(0, Read("AssetEntriesUnreadable"));
            Assert.Equal(0, Read("ConfigurationReparsePointsSkipped"));
        }

        // ---------------------------------------------------------------
        // F7: the cooldown window's end follows the CURRENT setting.
        // ---------------------------------------------------------------

        [Fact]
        public void LoweringCooldownReleasesAHeldChangeOnTheNextScan()
        {
            var cfg = Path.Combine(_configurations, "Jellyfin.Plugin.Demo.xml");
            File.WriteAllText(cfg, "<a/>");
            var p = Plugin("Cool", AaaId, "body", configNames: new[] { "Jellyfin.Plugin.Demo.xml" });
            var now = FixedTimestamp;
            var cooldown = 60;
            var provider = new PluginGenerationProvider(
                () => new[] { p },
                _configurations,
                utcNow: () => now,
                configurationProvider: () => new Configuration.PluginConfiguration { ConfigCooldownMinutes = cooldown });
            var g0 = provider.Generation;

            // Leading edge: publishes after debounce and opens a 60-minute window.
            File.WriteAllText(cfg, "<b/>");
            var g1 = Settle(provider, ref now);
            Assert.NotEqual(g0, g1);

            // Inside the window: held.
            File.WriteAllText(cfg, "<c/>");
            var held = Settle(provider, ref now);
            Assert.Equal(g1, held);

            // The admin turns the cooldown off. The next scan must release the
            // held change without waiting for the window that was sized under
            // the old setting.
            cooldown = 0;
            now = now.AddSeconds(30);
            provider.Invalidate();
            Assert.NotEqual(g1, provider.Generation);
        }

        [Fact]
        public void RaisingCooldownExtendsAnOpenWindow()
        {
            var cfg = Path.Combine(_configurations, "Jellyfin.Plugin.Demo.xml");
            File.WriteAllText(cfg, "<a/>");
            var p = Plugin("Cool", AaaId, "body", configNames: new[] { "Jellyfin.Plugin.Demo.xml" });
            var now = FixedTimestamp;
            var cooldown = 1;
            var provider = new PluginGenerationProvider(
                () => new[] { p },
                _configurations,
                utcNow: () => now,
                configurationProvider: () => new Configuration.PluginConfiguration { ConfigCooldownMinutes = cooldown });
            _ = provider.Generation;

            File.WriteAllText(cfg, "<b/>");
            var opened = Settle(provider, ref now);
            var openedAt = now;

            File.WriteAllText(cfg, "<c/>");
            var held = Settle(provider, ref now);
            Assert.Equal(opened, held);

            // Raise the cooldown to 60 minutes while the 1-minute window is open.
            // Past the OLD end, the change is still held ...
            cooldown = 60;
            now = openedAt.AddMinutes(2);
            provider.Invalidate();
            Assert.Equal(opened, provider.Generation);

            // ... and it is released once the NEW end is reached.
            now = openedAt.AddMinutes(60);
            provider.Invalidate();
            Assert.NotEqual(opened, provider.Generation);
        }

        [Fact]
        public void RaisingCooldownDoesNotResurrectAWindowThatAlreadyExpired()
        {
            var cfg = Path.Combine(_configurations, "Jellyfin.Plugin.Demo.xml");
            File.WriteAllText(cfg, "<a/>");
            var p = Plugin("Cool", AaaId, "body", configNames: new[] { "Jellyfin.Plugin.Demo.xml" });
            var now = FixedTimestamp;
            var cooldown = 1;
            var provider = new PluginGenerationProvider(
                () => new[] { p },
                _configurations,
                utcNow: () => now,
                configurationProvider: () => new Configuration.PluginConfiguration { ConfigCooldownMinutes = cooldown });
            _ = provider.Generation;

            // Leading edge at T: publishes and opens a 1-minute window.
            File.WriteAllText(cfg, "<b/>");
            var opened = Settle(provider, ref now);
            var openedAt = now;

            // The window expires quietly. The regular scan cadence observes the
            // plugin (unchanged) well after that, with the setting still at 1.
            now = openedAt.AddMinutes(30);
            provider.Invalidate();
            Assert.Equal(opened, provider.Generation);

            // The admin now raises the cooldown to a day. That must size the NEXT
            // window, not revive the one that closed 29 minutes ago: the save a
            // minute later is a leading edge and publishes, instead of being
            // held until T + 24 h.
            cooldown = 1440;
            now = openedAt.AddMinutes(31);
            File.WriteAllText(cfg, "<c/>");
            var afterRaise = Settle(provider, ref now);
            Assert.NotEqual(opened, afterRaise);
        }

        [Fact]
        public void LoweringCooldownReleasesAHeldChangeAsAHeldPublishNotALeadingEdge()
        {
            var cfg = Path.Combine(_configurations, "Jellyfin.Plugin.Demo.xml");
            File.WriteAllText(cfg, "<a/>");
            var p = Plugin("Cool", AaaId, "body", configNames: new[] { "Jellyfin.Plugin.Demo.xml" });
            var now = FixedTimestamp;
            var cooldown = 60;
            var provider = new PluginGenerationProvider(
                () => new[] { p },
                _configurations,
                utcNow: () => now,
                configurationProvider: () => new Configuration.PluginConfiguration { ConfigCooldownMinutes = cooldown });
            _ = provider.Generation;

            // Leading edge at T: opens a 60-minute window.
            File.WriteAllText(cfg, "<b/>");
            var opened = Settle(provider, ref now);
            var openedAt = now;

            // A change at T+5 is held inside it.
            now = openedAt.AddMinutes(5);
            File.WriteAllText(cfg, "<c/>");
            var held = Settle(provider, ref now);
            Assert.Equal(opened, held);

            // The admin lowers the cooldown to 1 minute at T+5:17. The held
            // change is released on the next scan ...
            cooldown = 1;
            now = now.AddSeconds(5);
            provider.Invalidate();
            var released = provider.Generation;
            Assert.NotEqual(held, released);

            // ... as the HELD publish it is, which closes the window rather than
            // arming a fresh 1-minute one: a save made in that same minute must
            // publish after the debounce instead of being held again.
            File.WriteAllText(cfg, "<d/>");
            var nextSave = Settle(provider, ref now);
            Assert.NotEqual(released, nextSave);
        }

        // ---------------------------------------------------------------
        // Helpers.
        // ---------------------------------------------------------------

        /// <summary>
        /// Advances past the debounce with two observations, the way the 5 s
        /// scan cadence would, and returns the generation after the second.
        /// </summary>
        private static string Settle(PluginGenerationProvider provider, ref DateTime now)
        {
            now = now.AddSeconds(6);
            provider.Invalidate();
            _ = provider.Generation;
            now = now.AddSeconds(11);
            provider.Invalidate();
            return provider.Generation;
        }

        private static PluginScanLimits TightEntryLimits() =>
            new PluginScanLimits(
                maxFilesPerPlugin: 10,
                maxTotalFilesPerScan: 10,
                maxDirectoriesPerPlugin: 4,
                maxTotalDirectoriesPerScan: 16);

        private static PluginFingerprint Row(GenerationSnapshot snapshot, string folder) =>
            snapshot.Details.Single(row => row.Folder.Equals(folder, StringComparison.Ordinal));

        private static bool CanTestPermissionDenied() =>
            !OperatingSystem.IsWindows() && !Environment.IsPrivilegedProcess;

        private void Lock(string path)
        {
            if (OperatingSystem.IsWindows())
            {
                return;
            }

            _lockedPaths.Add(path);
            File.SetUnixFileMode(path, UnixFileMode.None);
        }

        private static void Unlock(string path)
        {
            if (OperatingSystem.IsWindows())
            {
                return;
            }

            File.SetUnixFileMode(
                path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        private ActivePluginDescriptor Plugin(
            string folderName,
            string id,
            string body,
            string? root = null,
            params string[] configNames)
        {
            var folder = Path.Combine(root ?? _root, folderName);
            Directory.CreateDirectory(Path.Combine(folder, "web"));
            var asset = Path.Combine(folder, "web", "client.js");
            File.WriteAllText(asset, body);
            File.SetLastWriteTimeUtc(asset, FixedTimestamp);
            return new ActivePluginDescriptor(
                folder,
                id,
                "1.0.0.0",
                PluginStatus.Active.ToString(),
                new[]
                {
                    new LoadedModuleFingerprint(
                        Path.Combine(folder, "Jellyfin.Plugin.Demo.dll"),
                        "Jellyfin.Plugin.Demo",
                        "1.0.0.0",
                        Guid.Parse("11111111-1111-1111-1111-111111111111")),
                },
                configNames);
        }
    }
}
