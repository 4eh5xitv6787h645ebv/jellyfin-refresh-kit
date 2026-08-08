using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    public sealed class ActivePluginGenerationTests : IDisposable
    {
        private static readonly Guid DemoId = new Guid("9c4e63f1-031b-4f25-988b-4f7d78a8b53e");
        private readonly string _root;
        private readonly string _configurations;

        public ActivePluginGenerationTests()
        {
            _root = Path.Combine(Path.GetTempPath(), "rk-active-tests-" + Guid.NewGuid().ToString("N"));
            _configurations = Path.Combine(_root, "configurations");
            Directory.CreateDirectory(_configurations);
        }

        public void Dispose()
        {
            try
            {
                Directory.Delete(_root, recursive: true);
            }
            catch
            {
                // A leftover disposable fixture is not a product failure.
            }
        }

        [Fact]
        public void StagedUpgradeIsInvisibleUntilItsModuleIsLoadedAfterRestart()
        {
            var oldFolder = NewPluginFolder("Demo_1.0.0.0", "old");
            var newFolder = NewPluginFolder("Demo_2.0.0.0", "new");
            var oldPlugin = PluginRecord(oldFolder, "1.0.0.0", PluginStatus.Active);
            var newPlugin = PluginRecord(newFolder, "2.0.0.0", PluginStatus.Active);
            IReadOnlyList<LocalPlugin> records = new[] { oldPlugin };
            IReadOnlyList<LoadedModuleFingerprint> loaded = new[]
            {
                Module(oldPlugin.DllFiles[0], "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var provider = Provider(() => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            var beforeInstall = provider.Generation;

            // Jellyfin has written the new folder and persisted the old record as
            // Superseded, but the old assembly is still the one executing.
            oldPlugin.Manifest.Status = PluginStatus.Superseded;
            records = new[] { oldPlugin, newPlugin };
            provider.Invalidate();
            var staged = provider.Generation;

            Assert.Equal(beforeInstall, staged);
            Assert.Single(provider.Details);
            Assert.Equal("1.0.0.0", provider.Details[0].Version);

            // A restart loads v2 and no longer loads v1. This is the moment open
            // tabs need a new generation and a reload.
            loaded = new[]
            {
                Module(newPlugin.DllFiles[0], "2.0.0.0", "22222222-2222-2222-2222-222222222222"),
            };
            var restartedProvider = Provider(
                () => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            var activated = restartedProvider.Generation;

            Assert.NotEqual(staged, activated);
            Assert.Single(restartedProvider.Details);
            Assert.Equal("2.0.0.0", restartedProvider.Details[0].Version);
        }

        [Fact]
        public void PublicGenerationTokenDoesNotDisclosePluginCount()
        {
            var first = NodePlugin(
                _root,
                "First_1.0.0.0",
                DemoId,
                "11111111-1111-1111-1111-111111111111");
            var second = NodePlugin(
                _root,
                "Second_1.0.0.0",
                Guid.NewGuid(),
                "22222222-2222-2222-2222-222222222222");
            var provider = Provider(() => new[] { first, second });

            Assert.Matches("^g-[0-9a-f]{16}$", provider.Generation);
            Assert.Equal(2, provider.Details.Count);
        }

        [Fact]
        public void DisableAndEnableMoveOnlyWhenLoadedStateMoves()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "active");
            var plugin = PluginRecord(folder, "1.0.0.0", PluginStatus.Active);
            IReadOnlyList<LocalPlugin> records = new[] { plugin };
            IReadOnlyList<LoadedModuleFingerprint> loaded = new[]
            {
                Module(plugin.DllFiles[0], "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var provider = Provider(() => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            var loadedGeneration = provider.Generation;

            plugin.Manifest.Status = PluginStatus.Disabled;
            provider.Invalidate();
            Assert.Equal(loadedGeneration, provider.Generation);

            loaded = Array.Empty<LoadedModuleFingerprint>();
            var disabledProvider = Provider(
                () => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            var disabledAfterRestart = disabledProvider.Generation;
            Assert.NotEqual(loadedGeneration, disabledAfterRestart);
            Assert.Empty(disabledProvider.Details);

            plugin.Manifest.Status = PluginStatus.Active;
            disabledProvider.Invalidate();
            Assert.Equal(disabledAfterRestart, disabledProvider.Generation);

            loaded = new[]
            {
                Module(plugin.DllFiles[0], "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var enabledProvider = Provider(
                () => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            Assert.Equal(loadedGeneration, enabledProvider.Generation);
        }

        [Fact]
        public void LoadedPluginWhoseFolderIsUnlinkedKeepsItsLastGoodAssetsUntilRestart()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "active asset");
            var configurationFile = Path.Combine(_configurations, "Demo.xml");
            File.WriteAllText(configurationFile, "active configuration");
            IReadOnlyList<ActivePluginDescriptor> active = new[]
            {
                Descriptor(folder, "11111111-1111-1111-1111-111111111111", "Demo.xml"),
            };
            var provider = Provider(() => active);
            var beforeUninstall = provider.Generation;
            var beforeDetail = Assert.Single(provider.Details);

            Directory.Delete(folder, recursive: true);
            File.Delete(configurationFile);
            active = Array.Empty<ActivePluginDescriptor>();
            provider.Invalidate();

            Assert.Equal(beforeUninstall, provider.Generation);
            var stillLoaded = Assert.Single(provider.Details);
            Assert.True(stillLoaded.UsingLastGoodAssets);
            Assert.True(stillLoaded.UsingLastGoodConfiguration);
            Assert.True(stillLoaded.UsingLastKnownPluginRecord);
            Assert.Equal(beforeDetail.AssetIdentity, stillLoaded.AssetIdentity);
            Assert.Equal(beforeDetail.ConfigurationIdentity, stillLoaded.ConfigurationIdentity);

            var restartedProvider = Provider(() => active);
            Assert.NotEqual(beforeUninstall, restartedProvider.Generation);
            Assert.Empty(restartedProvider.Details);
        }

        [Fact]
        public void SameVersionBinaryReplacementWithPreservedTimestampMovesAfterRestartByMvid()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "active");
            var plugin = PluginRecord(folder, "1.0.0.0", PluginStatus.Active);
            var dll = plugin.DllFiles[0];
            var timestamp = File.GetLastWriteTimeUtc(dll);
            IReadOnlyList<LoadedModuleFingerprint> loaded = new[]
            {
                Module(dll, "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var provider = Provider(() => PluginGenerationProvider.DiscoverActivePlugins(new[] { plugin }, loaded));
            var oldLoadedCode = provider.Generation;

            File.WriteAllText(dll, "replacement with a different payload");
            File.SetLastWriteTimeUtc(dll, timestamp);
            provider.Invalidate();
            Assert.Equal(oldLoadedCode, provider.Generation);

            loaded = new[]
            {
                Module(dll, "1.0.0.0", "22222222-2222-2222-2222-222222222222"),
            };
            var restartedProvider = Provider(
                () => PluginGenerationProvider.DiscoverActivePlugins(new[] { plugin }, loaded));
            Assert.NotEqual(oldLoadedCode, restartedProvider.Generation);
        }

        [Fact]
        public void OnlyAssetsBelongingToLoadedPluginsAffectTheGeneration()
        {
            var activeFolder = NewPluginFolder("Active_1.0.0.0", "active v1");
            var stagedFolder = NewPluginFolder("Staged_1.0.0.0", "staged v1");
            var activePlugin = PluginRecord(activeFolder, "1.0.0.0", PluginStatus.Active);
            var stagedPlugin = PluginRecord(stagedFolder, "1.0.0.0", PluginStatus.Active, Guid.NewGuid());
            IReadOnlyList<LocalPlugin> records = new[] { activePlugin, stagedPlugin };
            var loaded = new[]
            {
                Module(activePlugin.DllFiles[0], "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var provider = Provider(() => PluginGenerationProvider.DiscoverActivePlugins(records, loaded));
            var baseline = provider.Generation;

            RewriteAsset(stagedFolder, "staged v2", new DateTime(2026, 2, 1, 0, 0, 0, DateTimeKind.Utc));
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);

            RewriteAsset(activeFolder, "active v2", new DateTime(2026, 3, 1, 0, 0, 0, DateTimeKind.Utc));
            provider.Invalidate();
            Assert.NotEqual(baseline, provider.Generation);
        }

        [Fact]
        public void ActiveAssetInventoryDetectsNonNewestAddDeleteAndPreservedMtimeContentChanges()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "newest");
            var plugin = PluginRecord(folder, "1.0.0.0", PluginStatus.Active);
            var loaded = new[]
            {
                Module(plugin.DllFiles[0], "1.0.0.0", "11111111-1111-1111-1111-111111111111"),
            };
            var provider = Provider(
                () => PluginGenerationProvider.DiscoverActivePlugins(new[] { plugin }, loaded));
            var baseline = provider.Generation;

            var olderAsset = Path.Combine(folder, "web", "older.css");
            var preservedTimestamp = new DateTime(2020, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            File.WriteAllText(olderAsset, "one");
            File.SetLastWriteTimeUtc(olderAsset, preservedTimestamp);
            provider.Invalidate();
            var afterAdd = provider.Generation;
            Assert.NotEqual(baseline, afterAdd);
            Assert.Equal(2, Assert.Single(provider.Details).AssetFileCount);

            File.WriteAllText(olderAsset, "two");
            File.SetLastWriteTimeUtc(olderAsset, preservedTimestamp);
            provider.Invalidate();
            var afterSameSizeContentChange = provider.Generation;
            Assert.NotEqual(afterAdd, afterSameSizeContentChange);

            File.Delete(olderAsset);
            provider.Invalidate();
            var afterDelete = provider.Generation;
            Assert.NotEqual(afterSameSizeContentChange, afterDelete);
            Assert.Equal(1, Assert.Single(provider.Details).AssetFileCount);
        }

        [Fact]
        public void SourceMapsAndRuntimeDataDoNotAddRecurringContentIoOrMoveGeneration()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "active");
            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            var provider = Provider(() => new[] { descriptor });
            var baseline = provider.Generation;
            var baselineDetail = Assert.Single(provider.Details);

            File.WriteAllText(Path.Combine(folder, "web", "client.js.map"), "large debug-only source map");
            File.WriteAllText(Path.Combine(folder, "runtime-cache.json"), "runtime data");
            provider.Invalidate();

            Assert.Equal(baseline, provider.Generation);
            var after = Assert.Single(provider.Details);
            Assert.Equal(baselineDetail.AssetFileCount, after.AssetFileCount);
            Assert.Equal(baselineDetail.AssetBytesHashed, after.AssetBytesHashed);
        }

        [Fact]
        public void ActiveConfigurationUsesContentIdentityAndKeepsDebounce()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "asset");
            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111", "Demo.xml");
            var configurationFile = Path.Combine(_configurations, "Demo.xml");
            var unrelatedFile = Path.Combine(_configurations, "Unrelated.xml");
            var preservedTimestamp = new DateTime(2020, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            File.WriteAllText(configurationFile, "one");
            File.SetLastWriteTimeUtc(configurationFile, preservedTimestamp);
            File.WriteAllText(unrelatedFile, "ignored");

            var now = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var provider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                () => now);
            var baseline = provider.Generation;
            var baselineDetail = Assert.Single(provider.Details);

            File.WriteAllText(unrelatedFile, "still ignored");
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);

            File.WriteAllText(configurationFile, "two");
            File.SetLastWriteTimeUtc(configurationFile, preservedTimestamp);
            now = now.AddSeconds(1);
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);

            now = now.AddSeconds(11);
            provider.Invalidate();
            var published = provider.Generation;
            var publishedDetail = Assert.Single(provider.Details);
            Assert.NotEqual(baseline, published);
            Assert.NotEqual(baselineDetail.ConfigurationIdentity, publishedDetail.ConfigurationIdentity);
            Assert.Equal(baselineDetail.NewestConfigTicks, publishedDetail.NewestConfigTicks);
            Assert.Equal(1, publishedDetail.ConfigurationFileCount);
        }

        [Fact]
        public void IdenticalActiveConfigurationContentAgreesAcrossPathsAndTimestamps()
        {
            var firstRoot = Path.Combine(_root, "config-node-a");
            var secondRoot = Path.Combine(_root, "config-node-b");
            var firstConfigurations = Path.Combine(firstRoot, "configurations");
            var secondConfigurations = Path.Combine(secondRoot, "configurations");
            Directory.CreateDirectory(firstConfigurations);
            Directory.CreateDirectory(secondConfigurations);
            var firstPlugin = NodePlugin(
                firstRoot,
                "Demo-A",
                DemoId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Demo.xml");
            var secondPlugin = NodePlugin(
                secondRoot,
                "Demo-B",
                DemoId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Demo.xml");
            var firstFile = Path.Combine(firstConfigurations, "Demo.xml");
            var secondFile = Path.Combine(secondConfigurations, "Demo.xml");
            File.WriteAllText(firstFile, "identical settings");
            File.WriteAllText(secondFile, "identical settings");
            File.SetLastWriteTimeUtc(firstFile, new DateTime(2020, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            File.SetLastWriteTimeUtc(secondFile, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

            var firstProvider = new PluginGenerationProvider(() => new[] { firstPlugin }, firstConfigurations);
            var secondProvider = new PluginGenerationProvider(() => new[] { secondPlugin }, secondConfigurations);

            Assert.Equal(firstProvider.Generation, secondProvider.Generation);
            Assert.Equal(
                Assert.Single(firstProvider.Details).ConfigurationIdentity,
                Assert.Single(secondProvider.Details).ConfigurationIdentity);
        }

        [Fact]
        public void OversizedDirectoryInventoryStopsAtBudgetAndUsesDeterministicSentinel()
        {
            var firstRoot = Path.Combine(_root, "large-node-a");
            var secondRoot = Path.Combine(_root, "large-node-b");
            var firstPlugin = NodePlugin(
                firstRoot,
                "Demo-A",
                DemoId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var secondPlugin = NodePlugin(
                secondRoot,
                "Demo-B",
                DemoId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            File.WriteAllText(Path.Combine(secondPlugin.DirectoryPath, "web", "client.js"), "different bytes");

            for (var index = 0; index < PluginGenerationProvider.MaxDirectoriesPerPlugin; index++)
            {
                Directory.CreateDirectory(Path.Combine(firstPlugin.DirectoryPath, "a" + index.ToString(CultureInfo.InvariantCulture)));
                Directory.CreateDirectory(Path.Combine(secondPlugin.DirectoryPath, "z" + index.ToString(CultureInfo.InvariantCulture)));
            }

            var firstProvider = Provider(() => new[] { firstPlugin });
            var secondProvider = Provider(() => new[] { secondPlugin });
            var firstDetail = Assert.Single(firstProvider.Details);
            var secondDetail = Assert.Single(secondProvider.Details);

            Assert.True(firstDetail.AssetScanTruncated);
            Assert.True(secondDetail.AssetScanTruncated);
            Assert.Equal(1, firstDetail.AssetDirectoriesScanned);
            Assert.Equal(1, secondDetail.AssetDirectoriesScanned);
            Assert.Equal(firstDetail.AssetIdentity, secondDetail.AssetIdentity);
            Assert.Equal(firstProvider.Generation, secondProvider.Generation);
        }

        [Fact]
        public void WideAndDeepDirectoryInventoryReservesPendingTraversalBudget()
        {
            var folder = Path.Combine(_root, "wide-deep");
            Directory.CreateDirectory(folder);
            var branch = Path.Combine(folder, "a-branch");
            Directory.CreateDirectory(branch);
            for (var index = 1; index < 256; index++)
            {
                Directory.CreateDirectory(Path.Combine(
                    folder,
                    "b" + index.ToString("D3", CultureInfo.InvariantCulture)));
            }

            for (var index = 0; index < 256; index++)
            {
                Directory.CreateDirectory(Path.Combine(
                    branch,
                    "c" + index.ToString("D3", CultureInfo.InvariantCulture)));
            }

            var provider = Provider(() => new[]
            {
                Descriptor(folder, "11111111-1111-1111-1111-111111111111"),
            });
            var detail = Assert.Single(provider.Details);

            Assert.True(detail.AssetScanTruncated);
            Assert.Equal(2, detail.AssetDirectoriesScanned);
            Assert.Equal(0, detail.AssetFileCount);
            Assert.Equal(0, detail.AssetBytesHashed);
        }

        [Fact]
        public void CacheTtlStartsWhenSlowScanCompletes()
        {
            var folder = NewPluginFolder("slow-scan", "asset");
            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            var now = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var scans = 0;
            var provider = new PluginGenerationProvider(
                () =>
                {
                    scans++;
                    now = now.AddSeconds(PluginGenerationProvider.CacheTtlSeconds + 1);
                    return new[] { descriptor };
                },
                _configurations,
                () => now);

            var first = provider.Generation;
            var immediateSecond = provider.Generation;

            Assert.Equal(first, immediateSecond);
            Assert.Equal(1, scans);
        }

        [Fact]
        public void ActiveAssetContentIoIsHardCappedPerPlugin()
        {
            var folder = Path.Combine(_root, "byte-budget");
            var web = Path.Combine(folder, "web");
            Directory.CreateDirectory(web);
            var asset = Path.Combine(web, "large.js");
            using (var stream = new FileStream(asset, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                stream.SetLength(PluginGenerationProvider.MaxAssetBytesPerPlugin);
            }

            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            var provider = Provider(() => new[] { descriptor });
            var atLimit = Assert.Single(provider.Details);
            Assert.False(atLimit.AssetScanTruncated);
            Assert.Equal(PluginGenerationProvider.MaxAssetBytesPerPlugin, atLimit.AssetBytesHashed);

            using (var stream = new FileStream(asset, FileMode.Open, FileAccess.Write, FileShare.None))
            {
                stream.SetLength(PluginGenerationProvider.MaxAssetBytesPerPlugin + 1);
            }

            provider.Invalidate();
            var overLimit = Assert.Single(provider.Details);
            Assert.True(overLimit.AssetScanTruncated);
            Assert.Equal(0, overLimit.AssetBytesHashed);
        }

        [Fact]
        public void GenerationIsIndependentOfPluginOrderAndAbsoluteInstallPath()
        {
            var firstRoot = Path.Combine(_root, "node-a");
            var secondRoot = Path.Combine(_root, "node-b");
            Directory.CreateDirectory(firstRoot);
            Directory.CreateDirectory(secondRoot);
            var a1 = NodePlugin(firstRoot, "Alpha", DemoId, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var b1 = NodePlugin(firstRoot, "Beta", Guid.Parse("22222222-2222-2222-2222-222222222222"), "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var a2 = NodePlugin(secondRoot, "Renamed-Alpha-Folder", DemoId, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var b2 = NodePlugin(secondRoot, "Renamed-Beta-Folder", Guid.Parse("22222222-2222-2222-2222-222222222222"), "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            File.SetLastWriteTimeUtc(
                Path.Combine(a2.DirectoryPath, "web", "client.js"),
                new DateTime(2026, 5, 1, 0, 0, 0, DateTimeKind.Utc));
            File.SetLastWriteTimeUtc(
                Path.Combine(b2.DirectoryPath, "web", "client.js"),
                new DateTime(2026, 6, 1, 0, 0, 0, DateTimeKind.Utc));
            IReadOnlyList<ActivePluginDescriptor> state = new[] { a1, b1 };
            var provider = Provider(() => state);
            var nodeA = provider.Generation;

            state = new[] { b2, a2 };
            provider.Invalidate();
            var nodeB = provider.Generation;

            Assert.Equal(nodeA, nodeB);
        }

        [Fact]
        public void LoadedHostIdentityIsRestartStableAndMovesOnJellyfinUpdate()
        {
            var folder = NewPluginFolder("Demo_1.0.0.0", "asset");
            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            HostFingerprint Host(string path, string mvid) =>
                PluginGenerationProvider.DiscoverHostFingerprint(new[]
                {
                    new LoadedModuleFingerprint(
                        path,
                        "jellyfin",
                        "12.0.0.0",
                        Guid.Parse(mvid)),
                    new LoadedModuleFingerprint(
                        Path.Combine(Path.GetDirectoryName(path)!, "Jellyfin.Api.dll"),
                        "Jellyfin.Api",
                        "12.0.0.0",
                        Guid.Parse("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")),
                });

            var firstHost = Host(
                Path.Combine(_root, "node-a", "jellyfin.dll"),
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var sameHostAtAnotherPath = Host(
                Path.Combine(_root, "node-b", "jellyfin.dll"),
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var updatedHost = Host(
                Path.Combine(_root, "node-b", "jellyfin.dll"),
                "cccccccc-cccc-cccc-cccc-cccccccccccc");
            var firstProvider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                hostFingerprintProvider: () => firstHost);
            var stableRestartProvider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                hostFingerprintProvider: () => sameHostAtAnotherPath);
            var updatedProvider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                hostFingerprintProvider: () => updatedHost);

            Assert.Equal(firstHost.Identity, sameHostAtAnotherPath.Identity);
            Assert.Equal(firstProvider.Generation, stableRestartProvider.Generation);
            Assert.NotEqual(firstProvider.Generation, updatedProvider.Generation);
            Assert.Equal(2, updatedProvider.Host.Modules.Count);
            Assert.DoesNotContain(_root, string.Join('|', updatedProvider.Host.Modules), StringComparison.Ordinal);
        }

        private PluginGenerationProvider Provider(Func<IReadOnlyList<ActivePluginDescriptor>> source) =>
            new PluginGenerationProvider(source, _configurations);

        private string NewPluginFolder(string folderName, string assetBody)
        {
            var folder = Path.Combine(_root, folderName);
            Directory.CreateDirectory(folder);
            File.WriteAllText(Path.Combine(folder, "Jellyfin.Plugin.Demo.dll"), "fixture");
            RewriteAsset(folder, assetBody, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            return folder;
        }

        private static void RewriteAsset(string folder, string body, DateTime timestamp)
        {
            var asset = Path.Combine(folder, "web", "client.js");
            Directory.CreateDirectory(Path.GetDirectoryName(asset)!);
            File.WriteAllText(asset, body);
            File.SetLastWriteTimeUtc(asset, timestamp);
        }

        private static LocalPlugin PluginRecord(
            string folder,
            string version,
            PluginStatus status,
            Guid? id = null)
        {
            var plugin = new LocalPlugin(
                folder,
                true,
                new PluginManifest
                {
                    Id = id ?? DemoId,
                    Name = Path.GetFileName(folder),
                    Version = version,
                    Status = status,
                })
            {
                DllFiles = new[] { Path.Combine(folder, "Jellyfin.Plugin.Demo.dll") },
            };
            return plugin;
        }

        private static LoadedModuleFingerprint Module(
            string path,
            string version,
            string mvid) =>
            new LoadedModuleFingerprint(
                path,
                "Jellyfin.Plugin.Demo",
                version,
                Guid.Parse(mvid));

        private static ActivePluginDescriptor Descriptor(
            string folder,
            string mvid,
            params string[] configurationFileNames) =>
            new ActivePluginDescriptor(
                folder,
                DemoId.ToString("D", CultureInfo.InvariantCulture),
                "1.0.0.0",
                PluginStatus.Active.ToString(),
                new[]
                {
                    new LoadedModuleFingerprint(
                        Path.Combine(folder, "Jellyfin.Plugin.Demo.dll"),
                        "Jellyfin.Plugin.Demo",
                        "1.0.0.0",
                        Guid.Parse(mvid)),
                },
                configurationFileNames);

        private static ActivePluginDescriptor NodePlugin(
            string root,
            string folderName,
            Guid id,
            string mvid,
            string? configurationFileName = null)
        {
            var folder = Path.Combine(root, folderName);
            Directory.CreateDirectory(Path.Combine(folder, "web"));
            var asset = Path.Combine(folder, "web", "client.js");
            File.WriteAllText(asset, "same bytes");
            File.SetLastWriteTimeUtc(asset, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            return new ActivePluginDescriptor(
                folder,
                id.ToString("D", CultureInfo.InvariantCulture),
                "1.0.0.0",
                PluginStatus.Active.ToString(),
                new[]
                {
                    new LoadedModuleFingerprint(
                        Path.Combine(folder, "Jellyfin.Plugin.Demo.dll"),
                        "Jellyfin.Plugin.Demo",
                        "1.0.0.0",
                        Guid.Parse(mvid)),
                },
                configurationFileName == null
                    ? Array.Empty<string>()
                    : new[] { configurationFileName });
        }
    }
}
