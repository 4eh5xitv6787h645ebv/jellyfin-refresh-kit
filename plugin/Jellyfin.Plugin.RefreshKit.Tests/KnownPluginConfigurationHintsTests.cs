using System;
using System.Linq;
using System.Text;
using Xunit;

namespace Jellyfin.Plugin.RefreshKit.Tests
{
    /// <summary>
    /// Shape checks for the built-in bookkeeping registry, so a malformed
    /// addition fails here rather than on an installation, plus the plugin
    /// self-declaration reader that makes the registry optional.
    /// </summary>
    public sealed class KnownPluginConfigurationHintsTests
    {
        [Fact]
        public void EveryRegistryEntryIsWellFormed()
        {
            Assert.NotEmpty(KnownPluginConfigurationHints.Entries);
            var ids = KnownPluginConfigurationHints.Entries.Select(entry => entry.PluginId).ToList();
            Assert.Equal(ids.Count, ids.Distinct(StringComparer.OrdinalIgnoreCase).Count());

            foreach (var entry in KnownPluginConfigurationHints.Entries)
            {
                Assert.True(Guid.TryParseExact(entry.PluginId, "D", out var parsed), entry.PluginId);
                Assert.NotEqual(Guid.Empty, parsed);
                Assert.False(string.IsNullOrWhiteSpace(entry.Name));
                Assert.False(string.IsNullOrWhiteSpace(entry.VerifiedAgainst));
                Assert.True(entry.Reason.Length >= 40, "explain which code path rewrites the elements and how often");
                Assert.NotEmpty(entry.IgnoredElements);
                Assert.Equal(
                    entry.IgnoredElements.Count,
                    entry.IgnoredElements.Distinct(StringComparer.OrdinalIgnoreCase).Count());
                foreach (var element in entry.IgnoredElements)
                {
                    Assert.Matches("^[A-Za-z_][A-Za-z0-9_.-]*$", element);
                    Assert.NotEqual(PluginGenerationProvider.PluginDeclaredIgnoreElement, element);
                }
            }
        }

        [Fact]
        public void DefaultSettingLinesAreGuidScopedAndResolveOnlyForThatPlugin()
        {
            var lines = Configuration.PluginConfiguration.DefaultConfigIgnoredElements();
            Assert.Equal(
                KnownPluginConfigurationHints.Entries.Sum(entry => entry.IgnoredElements.Count),
                lines.Length);
            foreach (var line in lines)
            {
                var separator = line.LastIndexOf(':');
                Assert.True(separator > 0, line);
                Assert.True(Guid.TryParseExact(line.Substring(0, separator), "D", out _), line);
            }

            var enhanced = new ActivePluginDescriptor(
                "/plugins/JellyfinEnhanced_12.8.0.0",
                "f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b",
                "12.8.0.0",
                "Active",
                Array.Empty<LoadedModuleFingerprint>(),
                new[] { "Jellyfin.Plugin.JellyfinEnhanced.xml" },
                "Jellyfin Enhanced");
            var other = new ActivePluginDescriptor(
                "/plugins/Other_1.0.0.0",
                Guid.NewGuid().ToString("D"),
                "1.0.0.0",
                "Active",
                Array.Empty<LoadedModuleFingerprint>(),
                new[] { "Other.xml" },
                "Other");
            var configuration = new Configuration.PluginConfiguration();
            Assert.Contains(
                "ClearTranslationCacheTimestamp",
                PluginGenerationProvider.ResolveIgnoredConfigurationElements(configuration, enhanced));
            Assert.Empty(PluginGenerationProvider.ResolveIgnoredConfigurationElements(configuration, other));
        }

        private static byte[] Bytes(string text) => Encoding.UTF8.GetBytes(text);

        [Fact]
        public void PluginDeclarationIsReadInBothSerializedAndTextForms()
        {
            var serialized = Bytes("<PluginConfiguration><A>1</A>"
                + "<RefreshKitIgnoredElements><string>LastRunUtc</string><string>Receipt</string></RefreshKitIgnoredElements>"
                + "</PluginConfiguration>");
            var declared = PluginGenerationProvider.ReadDeclaredIgnoredElements(serialized, serialized.Length);
            Assert.Equal(
                new[] { "LastRunUtc", "Receipt", "RefreshKitIgnoredElements" },
                declared.OrderBy(name => name, StringComparer.Ordinal));

            var text = Bytes("<PluginConfiguration><RefreshKitIgnoredElements> LastRunUtc, Receipt;Other\n</RefreshKitIgnoredElements></PluginConfiguration>");
            Assert.Equal(
                new[] { "LastRunUtc", "Other", "Receipt", "RefreshKitIgnoredElements" },
                PluginGenerationProvider.ReadDeclaredIgnoredElements(text, text.Length).OrderBy(name => name, StringComparer.Ordinal));

            var empty = Bytes("<PluginConfiguration><RefreshKitIgnoredElements /></PluginConfiguration>");
            Assert.Equal(
                new[] { "RefreshKitIgnoredElements" },
                PluginGenerationProvider.ReadDeclaredIgnoredElements(empty, empty.Length));
        }

        [Fact]
        public void PluginDeclarationIgnoresNestedMalformedAndInvalidNames()
        {
            var nested = Bytes("<PluginConfiguration><Group><RefreshKitIgnoredElements><string>Hidden</string></RefreshKitIgnoredElements></Group></PluginConfiguration>");
            Assert.Empty(PluginGenerationProvider.ReadDeclaredIgnoredElements(nested, nested.Length));

            var invalid = Bytes("<PluginConfiguration><RefreshKitIgnoredElements><string>bad name</string><string>ok_1</string><string>RefreshKitIgnoredElements</string></RefreshKitIgnoredElements></PluginConfiguration>");
            Assert.Equal(
                new[] { "RefreshKitIgnoredElements", "ok_1" },
                PluginGenerationProvider.ReadDeclaredIgnoredElements(invalid, invalid.Length).OrderBy(name => name, StringComparer.Ordinal));

            var malformed = Bytes("<PluginConfiguration><RefreshKitIgnoredElements><string>X</string>");
            Assert.Empty(PluginGenerationProvider.ReadDeclaredIgnoredElements(malformed, malformed.Length));

            var dtd = Bytes("<!DOCTYPE x [<!ENTITY e \"y\">]><PluginConfiguration><RefreshKitIgnoredElements>&e;</RefreshKitIgnoredElements></PluginConfiguration>");
            Assert.Empty(PluginGenerationProvider.ReadDeclaredIgnoredElements(dtd, dtd.Length));

            var absent = Bytes("<PluginConfiguration><A>1</A></PluginConfiguration>");
            Assert.Empty(PluginGenerationProvider.EffectiveIgnoredElements(absent, absent.Length, null));
        }
    }
}
