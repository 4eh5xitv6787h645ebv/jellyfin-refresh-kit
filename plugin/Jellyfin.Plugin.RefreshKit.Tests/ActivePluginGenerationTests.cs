using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    public sealed class ActivePluginGenerationTests : IDisposable
    {
        private static readonly Guid DemoId = new Guid("9c4e63f1-031b-4f25-988b-4f7d78a8b53e");
        // This is only a deadlock escape for deterministic coordination, not a
        // performance assertion. Dedicated reader threads avoid thread-pool
        // starvation while the first reader owns the provider's scan lock.
        private static readonly TimeSpan ConcurrencyDeadlockGuard = TimeSpan.FromSeconds(30);
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
        public void AggregateAssetFileCapIsSharedInStablePluginOrder()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "file-cap-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "file-cap-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var limits = new PluginScanLimits(maxTotalFilesPerScan: 1);
            var reverseProvider = new PluginGenerationProvider(
                () => new[] { second, first },
                _configurations,
                scanLimits: limits);
            var forwardProvider = new PluginGenerationProvider(
                () => new[] { first, second },
                _configurations,
                scanLimits: limits);

            Assert.Equal(reverseProvider.Generation, forwardProvider.Generation);
            var firstDetail = Assert.Single(
                reverseProvider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var secondDetail = Assert.Single(
                reverseProvider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(firstDetail.AssetScanTruncated);
            Assert.Equal(1, firstDetail.AssetFileCount);
            Assert.True(secondDetail.AssetScanTruncated);
            Assert.Equal(0, secondDetail.AssetFileCount);
        }

        [Fact]
        public void AggregateAssetDirectoryCapIsSharedInStablePluginOrder()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "directory-cap-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "directory-cap-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var limits = new PluginScanLimits(maxTotalDirectoriesPerScan: 3);
            var reverseProvider = new PluginGenerationProvider(
                () => new[] { second, first },
                _configurations,
                scanLimits: limits);
            var forwardProvider = new PluginGenerationProvider(
                () => new[] { first, second },
                _configurations,
                scanLimits: limits);

            Assert.Equal(reverseProvider.Generation, forwardProvider.Generation);
            var firstDetail = Assert.Single(
                reverseProvider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var secondDetail = Assert.Single(
                reverseProvider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(firstDetail.AssetScanTruncated);
            Assert.Equal(2, firstDetail.AssetDirectoriesScanned);
            Assert.True(secondDetail.AssetScanTruncated);
            Assert.Equal(1, secondDetail.AssetDirectoriesScanned);
        }

        [Fact]
        public void RetainedPluginKeepsAggregateAssetByteBudgetReservedUntilRestart()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "retained-byte-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "retained-byte-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var assetLength = new FileInfo(
                Path.Combine(first.DirectoryPath, "web", "client.js")).Length;
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var limits = new PluginScanLimits(maxTotalAssetBytesPerScan: assetLength);
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: limits);
            var beforeRemoval = provider.Generation;

            var firstBeforeRemoval = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var secondBeforeRemoval = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(firstBeforeRemoval.AssetScanTruncated);
            Assert.True(secondBeforeRemoval.AssetScanTruncated);

            active = new[] { second };
            provider.Invalidate();

            Assert.Equal(beforeRemoval, provider.Generation);
            var retained = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var stillTruncated = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(retained.UsingLastKnownPluginRecord);
            Assert.True(stillTruncated.AssetScanTruncated);

            var restartedProvider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: limits);
            Assert.NotEqual(beforeRemoval, restartedProvider.Generation);
            Assert.False(Assert.Single(restartedProvider.Details).AssetScanTruncated);
        }

        [Fact]
        public void RetainedPluginKeepsAggregateAssetFileBudgetReserved()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "retained-file-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "retained-file-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            File.Move(
                Path.Combine(first.DirectoryPath, "web", "client.js"),
                Path.Combine(first.DirectoryPath, "client.js"));
            Directory.Delete(Path.Combine(first.DirectoryPath, "web"));
            File.WriteAllText(Path.Combine(first.DirectoryPath, "ignored.txt"), "not a client asset");
            File.Move(
                Path.Combine(second.DirectoryPath, "web", "client.js"),
                Path.Combine(second.DirectoryPath, "client.js"));
            Directory.Delete(Path.Combine(second.DirectoryPath, "web"));
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalFilesPerScan: 2));
            var beforeRemoval = provider.Generation;

            var firstBeforeRemoval = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            Assert.False(firstBeforeRemoval.AssetScanTruncated);
            Assert.Equal(1, firstBeforeRemoval.AssetFileCount);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .AssetScanTruncated);

            active = new[] { second };
            provider.Invalidate();

            Assert.Equal(beforeRemoval, provider.Generation);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .AssetScanTruncated);
        }

        [Fact]
        public void RetainedPluginKeepsAggregateAssetDirectoryBudgetReserved()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "retained-directory-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "retained-directory-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var firstWeb = Path.Combine(first.DirectoryPath, "web");
            var firstBranch = Path.Combine(first.DirectoryPath, "a");
            Directory.Move(firstWeb, firstBranch);
            var firstAsset = Path.Combine(firstBranch, "client.js");
            var failFirstRead = false;
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalDirectoriesPerScan: 4),
                beforeContentRead: path =>
                {
                    if (failFirstRead && path.Equals(firstAsset, StringComparison.Ordinal))
                    {
                        throw new IOException("Deterministic asset read failure.");
                    }
                });
            _ = provider.Generation;

            Directory.CreateDirectory(Path.Combine(first.DirectoryPath, "b"));
            failFirstRead = true;
            provider.Invalidate();
            var beforeRemoval = provider.Generation;

            var firstBeforeRemoval = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var secondBeforeRemoval = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(firstBeforeRemoval.UsingLastGoodAssets);
            Assert.Equal(2, firstBeforeRemoval.AssetDirectoriesScanned);
            Assert.True(secondBeforeRemoval.AssetScanTruncated);
            Assert.Equal(1, secondBeforeRemoval.AssetDirectoriesScanned);

            active = new[] { second };
            provider.Invalidate();

            Assert.Equal(beforeRemoval, provider.Generation);
            var stillTruncated = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(stillTruncated.AssetScanTruncated);
            Assert.Equal(1, stillTruncated.AssetDirectoriesScanned);
        }

        [Fact]
        public void RetainedPluginKeepsAggregateConfigurationBudgetReserved()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "retained-configuration-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "First.xml");
            var second = NodePlugin(
                _root,
                "retained-configuration-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "Second.xml");
            File.WriteAllText(Path.Combine(_configurations, "First.xml"), "aa");
            File.WriteAllText(Path.Combine(_configurations, "Second.xml"), "bb");
            var now = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var limits = new PluginScanLimits(maxTotalConfigurationBytesPerScan: 2);
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                () => now,
                scanLimits: limits);
            var beforeRemoval = provider.Generation;

            Assert.False(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);

            active = new[] { second };
            now = now.AddSeconds(1);
            provider.Invalidate();
            _ = provider.Generation;
            now = now.AddSeconds(11);
            provider.Invalidate();

            Assert.Equal(beforeRemoval, provider.Generation);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);

            var restartedProvider = new PluginGenerationProvider(
                () => active,
                _configurations,
                () => now,
                scanLimits: limits);
            Assert.NotEqual(beforeRemoval, restartedProvider.Generation);
            Assert.False(Assert.Single(restartedProvider.Details).ConfigurationScanTruncated);
        }

        [Fact]
        public void FailedAssetReadKeepsLastGoodSnapshotAndChargesAggregateBytes()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "asset-failure-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "asset-failure-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var firstAsset = Path.Combine(first.DirectoryPath, "web", "client.js");
            var secondAsset = Path.Combine(second.DirectoryPath, "web", "client.js");
            File.WriteAllText(firstAsset, "aa");
            File.WriteAllText(secondAsset, "bb");
            var failFirstRead = false;
            var injectedFailures = 0;
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalAssetBytesPerScan: 4),
                beforeContentRead: path =>
                {
                    if (failFirstRead && path.Equals(firstAsset, StringComparison.Ordinal))
                    {
                        injectedFailures++;
                        throw new IOException("Deterministic asset read failure.");
                    }
                });
            var baselineFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var baselineSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(baselineFirst.AssetScanTruncated);
            Assert.False(baselineSecond.AssetScanTruncated);

            File.WriteAllText(firstAsset, "fail");
            failFirstRead = true;
            provider.Invalidate();
            var failedFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var exhaustedSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));

            Assert.Equal(1, injectedFailures);
            Assert.True(failedFirst.UsingLastGoodAssets);
            Assert.True(failedFirst.AssetScanUnavailable);
            Assert.Equal(baselineFirst.AssetIdentity, failedFirst.AssetIdentity);
            Assert.True(exhaustedSecond.AssetScanTruncated);
            Assert.Equal(0, exhaustedSecond.AssetBytesHashed);

            var generationWithFailedReservation = provider.Generation;
            active = new[] { second };
            provider.Invalidate();

            Assert.Equal(generationWithFailedReservation, provider.Generation);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .AssetScanTruncated);
        }

        [Fact]
        public void EarlyAssetFailureKeepsPriorChargeForDownstreamAndRetainedPlugins()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "early-asset-failure-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "early-asset-failure-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            File.WriteAllText(Path.Combine(first.DirectoryPath, "web", "client.js"), "aa");
            File.WriteAllText(Path.Combine(second.DirectoryPath, "web", "client.js"), "bb");
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var limits = new PluginScanLimits(maxTotalAssetBytesPerScan: 2);
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: limits);
            var baseline = provider.Generation;
            var baselineFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var baselineSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(baselineFirst.AssetScanTruncated);
            Assert.True(baselineSecond.AssetScanTruncated);

            var unavailableDirectory = first.DirectoryPath + ".temporarily-unavailable";
            Directory.Move(first.DirectoryPath, unavailableDirectory);
            provider.Invalidate();

            Assert.Equal(baseline, provider.Generation);
            var failedFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var stillExhaustedSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(failedFirst.UsingLastGoodAssets);
            Assert.True(failedFirst.AssetScanUnavailable);
            Assert.Equal(baselineFirst.AssetIdentity, failedFirst.AssetIdentity);
            Assert.True(stillExhaustedSecond.AssetScanTruncated);
            Assert.Equal(0, stillExhaustedSecond.AssetBytesHashed);

            Directory.Move(unavailableDirectory, first.DirectoryPath);
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);

            active = new[] { second };
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .AssetScanTruncated);

            var restartedProvider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: limits);
            Assert.NotEqual(baseline, restartedProvider.Generation);
            Assert.False(Assert.Single(restartedProvider.Details).AssetScanTruncated);
        }

        [Fact]
        public void AssetFallbackDoesNotRetainReleasedConfigurationBudget()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "mixed-fallback-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "FirstMixed.xml");
            var second = NodePlugin(
                _root,
                "mixed-fallback-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "SecondMixed.xml");
            var firstConfiguration = Path.Combine(_configurations, "FirstMixed.xml");
            var secondConfiguration = Path.Combine(_configurations, "SecondMixed.xml");
            File.WriteAllText(Path.Combine(first.DirectoryPath, "web", "client.js"), "aa");
            File.WriteAllText(Path.Combine(second.DirectoryPath, "web", "client.js"), "bb");
            File.WriteAllText(firstConfiguration, "aa");
            File.WriteAllText(secondConfiguration, "bb");
            var provider = new PluginGenerationProvider(
                () => new[] { second, first },
                _configurations,
                scanLimits: new PluginScanLimits(
                    maxTotalAssetBytesPerScan: 2,
                    maxTotalConfigurationBytesPerScan: 2));
            _ = provider.Generation;
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);

            var unavailableDirectory = first.DirectoryPath + ".temporarily-unavailable";
            Directory.Move(first.DirectoryPath, unavailableDirectory);
            File.WriteAllText(firstConfiguration, string.Empty);
            provider.Invalidate();

            var failedFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var secondAfterRelease = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(failedFirst.UsingLastGoodAssets);
            Assert.True(failedFirst.AssetScanUnavailable);
            Assert.False(failedFirst.UsingLastGoodConfiguration);
            Assert.False(secondAfterRelease.ConfigurationScanTruncated);
            Assert.Equal(2, secondAfterRelease.ConfigurationBytesHashed);
            Assert.True(secondAfterRelease.AssetScanTruncated);

            Directory.Move(unavailableDirectory, first.DirectoryPath);
        }

        [Fact]
        public void FailedConfigurationReadKeepsLastGoodSnapshotAndChargesAggregateBytes()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "configuration-failure-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "First.xml");
            var second = NodePlugin(
                _root,
                "configuration-failure-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "Second.xml");
            var firstConfiguration = Path.Combine(_configurations, "First.xml");
            var secondConfiguration = Path.Combine(_configurations, "Second.xml");
            File.WriteAllText(firstConfiguration, "aa");
            File.WriteAllText(secondConfiguration, "bb");
            var failFirstRead = false;
            var injectedFailures = 0;
            IReadOnlyList<ActivePluginDescriptor> active = new[] { second, first };
            var provider = new PluginGenerationProvider(
                () => active,
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalConfigurationBytesPerScan: 4),
                beforeContentRead: path =>
                {
                    if (failFirstRead && path.Equals(firstConfiguration, StringComparison.Ordinal))
                    {
                        injectedFailures++;
                        throw new IOException("Deterministic configuration read failure.");
                    }
                });
            var baselineFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var baselineSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.False(baselineFirst.ConfigurationScanTruncated);
            Assert.False(baselineSecond.ConfigurationScanTruncated);

            File.WriteAllText(firstConfiguration, "fail");
            failFirstRead = true;
            provider.Invalidate();
            var failedFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var exhaustedSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));

            Assert.Equal(1, injectedFailures);
            Assert.True(failedFirst.UsingLastGoodConfiguration);
            Assert.True(failedFirst.ConfigurationScanUnavailable);
            Assert.Equal(baselineFirst.ConfigurationIdentity, failedFirst.ConfigurationIdentity);
            Assert.True(exhaustedSecond.ConfigurationScanTruncated);
            Assert.Equal(0, exhaustedSecond.ConfigurationBytesHashed);

            active = new[] { second };
            provider.Invalidate();

            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);
        }

        [Fact]
        public void ConfigurationFallbackReservesTheUnattemptedPriorByteDeficit()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "configuration-deficit-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "FirstDeficit.xml");
            var second = NodePlugin(
                _root,
                "configuration-deficit-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "SecondDeficit.xml");
            var firstConfiguration = Path.Combine(_configurations, "FirstDeficit.xml");
            var secondConfiguration = Path.Combine(_configurations, "SecondDeficit.xml");
            File.WriteAllText(firstConfiguration, "aaaa");
            File.WriteAllText(secondConfiguration, "bb");
            var failFirstRead = false;
            var provider = new PluginGenerationProvider(
                () => new[] { second, first },
                _configurations,
                scanLimits: new PluginScanLimits(maxTotalConfigurationBytesPerScan: 4),
                beforeContentRead: path =>
                {
                    if (failFirstRead && path.Equals(firstConfiguration, StringComparison.Ordinal))
                    {
                        throw new IOException("Deterministic early configuration failure.");
                    }
                });
            var baselineFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            Assert.Equal(4, baselineFirst.ConfigurationBytesHashed);
            Assert.True(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);

            // The failing attempt reserves only two bytes. Reusing the prior
            // four-byte snapshot must reserve the missing two before B scans.
            File.WriteAllText(firstConfiguration, "aa");
            failFirstRead = true;
            provider.Invalidate();

            var failedFirst = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var stillExhaustedSecond = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));
            Assert.True(failedFirst.UsingLastGoodConfiguration);
            Assert.True(failedFirst.ConfigurationScanUnavailable);
            Assert.Equal(baselineFirst.ConfigurationIdentity, failedFirst.ConfigurationIdentity);
            Assert.True(stillExhaustedSecond.ConfigurationScanTruncated);
            Assert.Equal(0, stillExhaustedSecond.ConfigurationBytesHashed);
        }

        [Fact]
        public void ExcludedConfigurationConsumesNoIoOrAggregateBudget()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "excluded-configuration-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Excluded.xml");
            var second = NodePlugin(
                _root,
                "watched-configuration-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "Watched.xml");
            var excludedPath = Path.Combine(_configurations, "Excluded.xml");
            var watchedPath = Path.Combine(_configurations, "Watched.xml");
            File.WriteAllText(excludedPath, "aa");
            File.WriteAllText(watchedPath, "bb");
            var now = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var configurationReads = new List<string>();
            var provider = new PluginGenerationProvider(
                () => new[] { second, first },
                _configurations,
                () => now,
                scanLimits: new PluginScanLimits(maxTotalConfigurationBytesPerScan: 2),
                beforeContentRead: path =>
                {
                    if (Path.GetExtension(path).Equals(".xml", StringComparison.OrdinalIgnoreCase))
                    {
                        configurationReads.Add(path);
                    }
                },
                configurationProvider: () => new Configuration.PluginConfiguration
                {
                    ConfigWatchExclusions = new[] { firstId.ToString("D") },
                });
            var baseline = provider.Generation;
            var excluded = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(firstId.ToString("D"), StringComparison.Ordinal));
            var watched = Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal));

            Assert.Equal(string.Empty, excluded.ConfigurationIdentity);
            Assert.Equal(0, excluded.ConfigurationFileCount);
            Assert.False(watched.ConfigurationScanTruncated);
            Assert.Equal(2, watched.ConfigurationBytesHashed);
            Assert.DoesNotContain(excludedPath, configurationReads);
            Assert.Contains(watchedPath, configurationReads);

            File.WriteAllText(excludedPath, "changed-and-larger");
            now = now.AddSeconds(1);
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);
            now = now.AddSeconds(11);
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);
            Assert.DoesNotContain(excludedPath, configurationReads);
            Assert.False(Assert.Single(
                provider.Details,
                detail => detail.Id.Equals(secondId.ToString("D"), StringComparison.Ordinal))
                .ConfigurationScanTruncated);
        }

        [Fact]
        public void DisabledConfigurationWatchingPerformsNoConfigurationIo()
        {
            var descriptor = NodePlugin(
                _root,
                "disabled-configuration-watching",
                Guid.Parse("11111111-1111-1111-1111-111111111111"),
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Disabled.xml");
            var configurationPath = Path.Combine(_configurations, "Disabled.xml");
            File.WriteAllText(configurationPath, "before");
            var configurationReads = 0;
            var provider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                beforeContentRead: path =>
                {
                    if (path.Equals(configurationPath, StringComparison.Ordinal))
                    {
                        configurationReads++;
                    }
                },
                configurationProvider: () => new Configuration.PluginConfiguration
                {
                    EnableConfigWatching = false,
                });
            var baseline = provider.Generation;
            var detail = Assert.Single(provider.Details);

            Assert.Equal(0, configurationReads);
            Assert.Equal(string.Empty, detail.ConfigurationIdentity);
            Assert.Equal(0, detail.ConfigurationFileCount);

            File.WriteAllText(configurationPath, "after");
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);
            Assert.Equal(0, configurationReads);
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
        public void BackwardClockStepExpiresCacheAndRestartsPendingConfigDebounce()
        {
            var folder = NewPluginFolder("clock-rollback", "asset-one");
            var descriptor = Descriptor(
                folder,
                "11111111-1111-1111-1111-111111111111",
                "Clock.xml");
            var asset = Path.Combine(folder, "web", "client.js");
            var configuration = Path.Combine(_configurations, "Clock.xml");
            File.WriteAllText(configuration, "config-one");
            var now = new DateTime(2026, 1, 1, 1, 0, 0, DateTimeKind.Utc);
            var scans = 0;
            var provider = new PluginGenerationProvider(
                () =>
                {
                    scans++;
                    return new[] { descriptor };
                },
                _configurations,
                () => now);
            var baseline = provider.Generation;

            File.WriteAllText(asset, "asset-two");
            File.WriteAllText(configuration, "config-two");
            now = now.AddHours(-1);

            // No explicit invalidation: a negative cache age itself must force
            // a rescan. The changed asset publishes immediately; configuration
            // begins a fresh debounce on the corrected clock timeline.
            var afterRollback = provider.Generation;
            Assert.NotEqual(baseline, afterRollback);
            Assert.Equal(2, scans);

            provider.Invalidate();
            Assert.Equal(afterRollback, provider.Generation);
            now = now.AddSeconds(11);
            provider.Invalidate();

            var afterDebounce = provider.Generation;
            Assert.NotEqual(afterRollback, afterDebounce);
            Assert.Equal(4, scans);
        }

        [Fact]
        public void BackwardClockStepRestartsExistingPendingDebounceAndClosesOldCooldown()
        {
            var folder = NewPluginFolder("clock-rollback-pending", "asset");
            var descriptor = Descriptor(
                folder,
                "11111111-1111-1111-1111-111111111111",
                "ClockPending.xml");
            var configuration = Path.Combine(_configurations, "ClockPending.xml");
            File.WriteAllText(configuration, "config-one");
            var now = new DateTime(2026, 1, 1, 1, 0, 0, DateTimeKind.Utc);
            var provider = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                () => now);
            var baseline = provider.Generation;

            // Publish one ordinary leading edge, which opens the default five-
            // minute cooldown.
            File.WriteAllText(configuration, "config-two");
            now = now.AddSeconds(1);
            provider.Invalidate();
            Assert.Equal(baseline, provider.Generation);
            now = now.AddSeconds(11);
            provider.Invalidate();
            var leadingEdge = provider.Generation;
            Assert.NotEqual(baseline, leadingEdge);

            // A second identity matures while that cooldown is open and is held.
            File.WriteAllText(configuration, "config-three");
            now = now.AddSeconds(1);
            provider.Invalidate();
            Assert.Equal(leadingEdge, provider.Generation);
            now = now.AddSeconds(11);
            provider.Invalidate();
            Assert.Equal(leadingEdge, provider.Generation);

            // Moving to an earlier clock timeline must neither publish instantly
            // nor wait for the stale absolute cooldown/debounce timestamps.
            now = now.AddHours(-1);
            provider.Invalidate();
            Assert.Equal(leadingEdge, provider.Generation);
            now = now.AddSeconds(9);
            provider.Invalidate();
            Assert.Equal(leadingEdge, provider.Generation);
            now = now.AddSeconds(2);
            provider.Invalidate();
            var afterFreshDebounce = provider.Generation;
            Assert.NotEqual(leadingEdge, afterFreshDebounce);

            // The post-rollback publish is a new leading edge and opens a fresh
            // cooldown on the corrected timeline.
            File.WriteAllText(configuration, "config-four");
            now = now.AddSeconds(1);
            provider.Invalidate();
            Assert.Equal(afterFreshDebounce, provider.Generation);
            now = now.AddSeconds(11);
            provider.Invalidate();
            Assert.Equal(afterFreshDebounce, provider.Generation);

            now = now.AddMinutes(5);
            provider.Invalidate();
            var trailingPublish = provider.Generation;
            Assert.NotEqual(afterFreshDebounce, trailingPublish);
        }

        [Theory]
        [InlineData(10)]
        [InlineData(50)]
        [InlineData(100)]
        public async Task ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(
            int readerCount)
        {
            var descriptor = NodePlugin(
                _root,
                "concurrent-polling",
                DemoId,
                "11111111-1111-1111-1111-111111111111");
            var asset = Path.Combine(descriptor.DirectoryPath, "web", "client.js");
            var originalTimestamp = File.GetLastWriteTimeUtc(asset);
            var scanCount = 0;
            var contentReadCount = 0;
            Task? activeReaderEntryBarrier = null;
            var provider = new PluginGenerationProvider(
                () =>
                {
                    Interlocked.Increment(ref scanCount);
                    var barrier = activeReaderEntryBarrier;
                    if (barrier != null && !barrier.Wait(ConcurrencyDeadlockGuard))
                    {
                        throw new TimeoutException(
                            $"The scan began before all {readerCount} generation readers entered.");
                    }

                    return new[] { descriptor };
                },
                _configurations,
                beforeContentRead: _ => Interlocked.Increment(ref contentReadCount));

            async Task<string[]> ReadWaveAsync()
            {
                var enteredCount = 0;
                var allReadersEntered = new TaskCompletionSource<bool>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                activeReaderEntryBarrier = allReadersEntered.Task;
                try
                {
                    return await RunSynchronizedReadersAsync(
                        readerCount,
                        () =>
                        {
                            if (Interlocked.Increment(ref enteredCount) == readerCount)
                            {
                                allReadersEntered.TrySetResult(true);
                            }

                            return provider.Generation;
                        });
                }
                finally
                {
                    activeReaderEntryBarrier = null;
                }
            }

            string AssertCoherentWave(string[] results)
            {
                Assert.Equal(readerCount, results.Length);
                return Assert.Single(results.Distinct(StringComparer.Ordinal));
            }

            var coldResults = await ReadWaveAsync();
            var coldGeneration = AssertCoherentWave(coldResults);
            Assert.Equal(1, Volatile.Read(ref scanCount));
            Assert.Equal(1, Volatile.Read(ref contentReadCount));

            var immediateResults = await ReadWaveAsync();
            Assert.Equal(coldGeneration, AssertCoherentWave(immediateResults));
            Assert.Equal(1, Volatile.Read(ref scanCount));
            Assert.Equal(1, Volatile.Read(ref contentReadCount));

            // Same path, length and timestamp but different bytes is the cache-
            // invalidation case Refresh Kit exists to detect.
            File.WriteAllText(asset, "next bytes");
            File.SetLastWriteTimeUtc(asset, originalTimestamp);
            provider.Invalidate();

            var invalidatedResults = await ReadWaveAsync();
            var invalidatedGeneration = AssertCoherentWave(invalidatedResults);
            Assert.NotEqual(coldGeneration, invalidatedGeneration);
            Assert.Equal(2, Volatile.Read(ref scanCount));
            Assert.Equal(2, Volatile.Read(ref contentReadCount));

            var postInvalidationResults = await ReadWaveAsync();
            Assert.Equal(invalidatedGeneration, AssertCoherentWave(postInvalidationResults));
            Assert.Equal(2, Volatile.Read(ref scanCount));
            Assert.Equal(2, Volatile.Read(ref contentReadCount));
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
        public void NativeEntryEnumerationStopsAtFirstDirectoryOverflowRegardlessOfOrder()
        {
            var folder = NewPluginFolder("native-entry-budget", "asset");
            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            var children = Enumerable.Range(0, 10)
                .Select(index => Directory.CreateDirectory(
                    Path.Combine(folder, $"branch-{index:D2}"))).Select(info => info.FullName)
                .ToArray();
            var forwardMoves = 0;
            var reverseMoves = 0;

            IEnumerable<string> CountedEntries(
                string current,
                IReadOnlyList<string> rootEntries,
                Action onYield)
            {
                var entries = current.Equals(folder, StringComparison.Ordinal)
                    ? rootEntries
                    : Directory.EnumerateFileSystemEntries(current).ToArray();
                foreach (var entry in entries)
                {
                    onYield();
                    yield return entry;
                }
            }

            var limits = new PluginScanLimits(
                maxDirectoriesPerPlugin: 1,
                maxTotalDirectoriesPerScan: 1);
            var forward = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => CountedEntries(
                    current,
                    children,
                    () => forwardMoves++));
            var reverse = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => CountedEntries(
                    current,
                    children.AsEnumerable().Reverse().ToArray(),
                    () => reverseMoves++));

            var forwardGeneration = forward.Generation;
            var reverseGeneration = reverse.Generation;
            var forwardDetail = Assert.Single(forward.Details);
            var reverseDetail = Assert.Single(reverse.Details);

            Assert.Equal(1, forwardMoves);
            Assert.Equal(1, reverseMoves);
            Assert.Equal(forwardGeneration, reverseGeneration);
            Assert.True(forwardDetail.AssetScanTruncated);
            Assert.True(reverseDetail.AssetScanTruncated);
            Assert.Equal(1, forwardDetail.AssetDirectoriesScanned);
            Assert.Equal(1, reverseDetail.AssetDirectoriesScanned);
            Assert.Equal(0, forwardDetail.AssetFileCount);
            Assert.Equal(0, reverseDetail.AssetFileCount);
        }

        [Fact]
        public void NativeEntryEnumerationStopsAtFirstFileOverflowRegardlessOfOrder()
        {
            var folder = Path.Combine(_root, "native-file-budget");
            Directory.CreateDirectory(folder);
            var files = Enumerable.Range(0, 10)
                .Select(index => Path.Combine(folder, $"entry-{index:D2}.txt"))
                .ToArray();
            foreach (var file in files)
            {
                File.WriteAllText(file, "not a client asset");
            }

            var descriptor = Descriptor(folder, "11111111-1111-1111-1111-111111111111");
            var forwardMoves = 0;
            var reverseMoves = 0;

            IEnumerable<string> CountedEntries(
                string current,
                IReadOnlyList<string> rootEntries,
                Action onYield)
            {
                var entries = current.Equals(folder, StringComparison.Ordinal)
                    ? rootEntries
                    : Directory.EnumerateFileSystemEntries(current).ToArray();
                foreach (var entry in entries)
                {
                    onYield();
                    yield return entry;
                }
            }

            var limits = new PluginScanLimits(
                maxFilesPerPlugin: 1,
                maxTotalFilesPerScan: 1);
            var forward = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => CountedEntries(
                    current,
                    files,
                    () => forwardMoves++));
            var reverse = new PluginGenerationProvider(
                () => new[] { descriptor },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => CountedEntries(
                    current,
                    files.AsEnumerable().Reverse().ToArray(),
                    () => reverseMoves++));

            var forwardGeneration = forward.Generation;
            var reverseGeneration = reverse.Generation;
            var forwardDetail = Assert.Single(forward.Details);
            var reverseDetail = Assert.Single(reverse.Details);

            Assert.Equal(2, forwardMoves);
            Assert.Equal(2, reverseMoves);
            Assert.Equal(forwardGeneration, reverseGeneration);
            Assert.True(forwardDetail.AssetScanTruncated);
            Assert.True(reverseDetail.AssetScanTruncated);
            Assert.Equal(1, forwardDetail.AssetDirectoriesScanned);
            Assert.Equal(1, reverseDetail.AssetDirectoriesScanned);
            Assert.Equal(0, forwardDetail.AssetFileCount);
            Assert.Equal(0, reverseDetail.AssetFileCount);
        }

        [Fact]
        public void MixedEntryOverflowNormalizesAggregateChargeRegardlessOfNativeOrder()
        {
            var firstId = Guid.Parse("11111111-1111-1111-1111-111111111111");
            var secondId = Guid.Parse("22222222-2222-2222-2222-222222222222");
            var first = NodePlugin(
                _root,
                "mixed-order-first",
                firstId,
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
            var second = NodePlugin(
                _root,
                "mixed-order-second",
                secondId,
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");
            var firstWeb = Path.Combine(first.DirectoryPath, "web");
            var firstFile = Path.Combine(first.DirectoryPath, "first.txt");
            var secondFile = Path.Combine(first.DirectoryPath, "second.txt");
            File.WriteAllText(firstFile, "irrelevant");
            File.WriteAllText(secondFile, "irrelevant");
            var fileFirst = new[] { firstFile, secondFile, firstWeb };
            var directoryFirst = new[] { firstWeb, firstFile, secondFile };

            IEnumerable<string> Entries(string current, IReadOnlyList<string> firstRootEntries) =>
                current.Equals(first.DirectoryPath, StringComparison.Ordinal)
                    ? firstRootEntries
                    : Directory.EnumerateFileSystemEntries(current);

            var limits = new PluginScanLimits(
                maxFilesPerPlugin: 1,
                maxTotalFilesPerScan: 2,
                maxDirectoriesPerPlugin: 2,
                maxTotalDirectoriesPerScan: 3);
            var fileFirstProvider = new PluginGenerationProvider(
                () => new[] { first, second },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => Entries(current, fileFirst));
            var directoryFirstProvider = new PluginGenerationProvider(
                () => new[] { first, second },
                _configurations,
                scanLimits: limits,
                fileSystemEntriesProvider: current => Entries(current, directoryFirst));

            var fileFirstGeneration = fileFirstProvider.Generation;
            var directoryFirstGeneration = directoryFirstProvider.Generation;
            var fileFirstDetails = fileFirstProvider.Details;
            var directoryFirstDetails = directoryFirstProvider.Details;

            Assert.Equal(fileFirstGeneration, directoryFirstGeneration);
            Assert.Equal(2, fileFirstDetails.Count);
            Assert.Equal(2, directoryFirstDetails.Count);
            Assert.All(fileFirstDetails, detail => Assert.True(detail.AssetScanTruncated));
            Assert.All(directoryFirstDetails, detail => Assert.True(detail.AssetScanTruncated));
            Assert.Equal(
                fileFirstDetails.Select(detail => detail.AssetIdentity),
                directoryFirstDetails.Select(detail => detail.AssetIdentity));
            Assert.Equal(
                fileFirstDetails.Select(detail => detail.AssetDirectoriesScanned),
                directoryFirstDetails.Select(detail => detail.AssetDirectoriesScanned));
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

        private static async Task<T[]> RunSynchronizedReadersAsync<T>(
            int readerCount,
            Func<T> read)
        {
            var readyCount = 0;
            var allReadersReady = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var start = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            var readers = Enumerable.Range(0, readerCount)
                .Select(_ => Task.Factory.StartNew(
                    () =>
                    {
                        if (Interlocked.Increment(ref readyCount) == readerCount)
                        {
                            allReadersReady.TrySetResult(true);
                        }

                        start.Task.GetAwaiter().GetResult();
                        return read();
                    },
                    CancellationToken.None,
                    TaskCreationOptions.LongRunning | TaskCreationOptions.DenyChildAttach,
                    TaskScheduler.Default))
                .ToArray();

            try
            {
                await allReadersReady.Task.WaitAsync(ConcurrencyDeadlockGuard);
                start.TrySetResult(true);
                return await Task.WhenAll(readers).WaitAsync(ConcurrencyDeadlockGuard);
            }
            finally
            {
                start.TrySetResult(true);
            }
        }

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
