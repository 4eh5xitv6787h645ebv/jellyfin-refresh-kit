using System;
using System.IO;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// The generation fingerprint, exercised against a real plugin folder on
    /// disk. The material a folder contributes is what the generation hashes, so
    /// asserting on <c>ToMaterial()</c> is asserting on the token.
    /// </summary>
    public sealed class PluginGenerationProviderTests : IDisposable
    {
        private readonly string _root;
        private readonly string _configurations;

        public PluginGenerationProviderTests()
        {
            _root = Path.Combine(Path.GetTempPath(), "rk-tests-" + Guid.NewGuid().ToString("N"));
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
                // A leftover temp folder is not a test failure.
            }
        }

        private string NewPluginFolder(string name, string status = "Active")
        {
            var folder = Path.Combine(_root, name);
            Directory.CreateDirectory(folder);
            WriteMeta(folder, status);
            var dll = Path.Combine(folder, "Jellyfin.Plugin.Demo.dll");
            File.WriteAllBytes(dll, new byte[] { 1, 2, 3 });

            // Backdate the binary. A DLL written "now" would be the newest file
            // in the folder and would mask every other timestamp under test —
            // which is also the real shape of the bug being covered: a marketplace
            // install preserves the package's own (old) binary timestamps, so a
            // later client-asset edit is genuinely the newest thing on disk.
            File.SetLastWriteTimeUtc(dll, new DateTime(2020, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            return folder;
        }

        private static void WriteMeta(string folder, string status)
        {
            File.WriteAllText(
                Path.Combine(folder, "meta.json"),
                "{\"guid\":\"9c4e63f1-031b-4f25-988b-4f7d78a8b53e\","
                + "\"name\":\"Demo\",\"version\":\"1.2.3.0\",\"status\":\"" + status + "\"}");
        }

        private string Material(string folder)
        {
            // A fresh provider each time: the debounce state is per-instance and
            // the first observation of a plugin adopts whatever is on disk.
            return new PluginGenerationProvider()
                .Fingerprint(folder, _configurations, DateTime.UtcNow)
                .ToMaterial();
        }

        // ---------------------------------------------------------------
        // Finding A — a disable must move the generation.
        // ---------------------------------------------------------------

        [Fact]
        public void DisablingAPlugin_ChangesTheMaterial()
        {
            // Reproduces what Jellyfin 10.11.11 does on disable: it rewrites
            // meta.json's "status" IN PLACE and touches nothing else — same
            // folder, same version, same binaries with the same timestamps.
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var before = Material(folder);

            WriteMeta(folder, "Disabled");
            var after = Material(folder);

            Assert.NotEqual(before, after);
            Assert.Contains("Active", before, StringComparison.Ordinal);
            Assert.Contains("Disabled", after, StringComparison.Ordinal);
        }

        [Fact]
        public void ReEnablingAPlugin_ReturnsToTheOriginalMaterial()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var active = Material(folder);

            WriteMeta(folder, "Disabled");
            var disabled = Material(folder);

            WriteMeta(folder, "Active");
            var reEnabled = Material(folder);

            Assert.NotEqual(active, disabled);
            Assert.Equal(active, reEnabled);
        }

        [Fact]
        public void MetaJsonStatus_IsCarriedOnTheFingerprint()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0", "Malfunctioned");

            var fingerprint = new PluginGenerationProvider()
                .Fingerprint(folder, _configurations, DateTime.UtcNow);

            Assert.Equal("Malfunctioned", fingerprint.Status);
            Assert.Equal("1.2.3.0", fingerprint.Version);
            Assert.Equal("9c4e63f1-031b-4f25-988b-4f7d78a8b53e", fingerprint.Id);
        }

        // ---------------------------------------------------------------
        // Finding D — client assets count, runtime data files do not.
        // ---------------------------------------------------------------

        [Theory]
        [InlineData("client.js")]
        [InlineData("client.mjs")]
        [InlineData("theme.css")]
        [InlineData("bundle.js.map")]
        [InlineData("page.html")]
        public void TouchingAClientAsset_ChangesTheMaterial(string fileName)
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var asset = Path.Combine(folder, fileName);
            File.WriteAllText(asset, "// v1");
            File.SetLastWriteTimeUtc(asset, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            var before = Material(folder);

            // A same-size in-place edit, exactly the developer case: only the
            // write time moves, and no DLL is involved.
            File.WriteAllText(asset, "// v2");
            File.SetLastWriteTimeUtc(asset, new DateTime(2026, 6, 1, 0, 0, 0, DateTimeKind.Utc));
            var after = Material(folder);

            Assert.NotEqual(before, after);
        }

        [Fact]
        public void ClientAssetInASubdirectory_ChangesTheMaterial()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var nested = Path.Combine(folder, "web", "assets");
            Directory.CreateDirectory(nested);
            var asset = Path.Combine(nested, "app.js");
            File.WriteAllText(asset, "// v1");
            File.SetLastWriteTimeUtc(asset, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            var before = Material(folder);

            File.SetLastWriteTimeUtc(asset, new DateTime(2026, 6, 1, 0, 0, 0, DateTimeKind.Utc));
            var after = Material(folder);

            Assert.NotEqual(before, after);
        }

        [Theory]
        [InlineData("tag-cache.json")]
        [InlineData("playback.db")]
        [InlineData("plugin.log")]
        public void TouchingARuntimeDataFile_DoesNotChangeTheMaterial(string fileName)
        {
            // The signal must stay clean: a plugin writing its own caches must
            // not reload every client on the server.
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var data = Path.Combine(folder, fileName);
            File.WriteAllText(data, "{}");
            File.SetLastWriteTimeUtc(data, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            var before = Material(folder);

            File.SetLastWriteTimeUtc(data, new DateTime(2026, 6, 1, 0, 0, 0, DateTimeKind.Utc));
            var after = Material(folder);

            Assert.Equal(before, after);
        }

        [Fact]
        public void InPlaceDllReplacement_ChangesTheMaterial()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");
            var dll = Path.Combine(folder, "Jellyfin.Plugin.Demo.dll");
            File.SetLastWriteTimeUtc(dll, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            var before = Material(folder);

            File.SetLastWriteTimeUtc(dll, new DateTime(2026, 6, 1, 0, 0, 0, DateTimeKind.Utc));
            var after = Material(folder);

            Assert.NotEqual(before, after);
        }

        [Fact]
        public void AStableFolder_ProducesAStableMaterial()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");

            Assert.Equal(Material(folder), Material(folder));
        }

        // ---------------------------------------------------------------
        // Finding E — the walk is bounded, and still finds what matters.
        // ---------------------------------------------------------------

        [Fact]
        public void ADeepAssetTree_IsScannedWithoutWalkingForever()
        {
            var folder = NewPluginFolder("Demo_1.2.3.0");

            // 600 sibling directories, past the 512-directory budget.
            for (var i = 0; i < 600; i++)
            {
                var child = Path.Combine(folder, "d" + i.ToString(System.Globalization.CultureInfo.InvariantCulture));
                Directory.CreateDirectory(child);
                File.WriteAllText(Path.Combine(child, "data.json"), "{}");
            }

            var started = DateTime.UtcNow;
            var material = Material(folder);
            var elapsed = DateTime.UtcNow - started;

            Assert.NotEqual(string.Empty, material);
            // Bounded work, not a wall-clock benchmark: a full walk of this tree
            // would still be fast, so this only guards against pathological
            // regressions (an unbounded walk of a real plugin's cache tree).
            Assert.True(elapsed < TimeSpan.FromSeconds(10), "scan took " + elapsed);
        }

        [Fact]
        public void UnreadableOrMissingMetaJson_StillProducesMaterial()
        {
            var folder = Path.Combine(_root, "Broken_9.9.9.9");
            Directory.CreateDirectory(folder);
            File.WriteAllText(Path.Combine(folder, "meta.json"), "{ this is not json");

            var fingerprint = new PluginGenerationProvider()
                .Fingerprint(folder, _configurations, DateTime.UtcNow);

            Assert.Equal("Broken_9.9.9.9", fingerprint.Folder);
            Assert.Equal(string.Empty, fingerprint.Status);
        }
    }
}
