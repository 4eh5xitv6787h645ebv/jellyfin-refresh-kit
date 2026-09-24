using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace Jellyfin.Plugin.RefreshKit
{
    /// <summary>
    /// The built-in registry of plugin configuration elements that are
    /// bookkeeping rather than settings: values a plugin rewrites on its own
    /// (a last-run timestamp, a telemetry receipt) that would otherwise turn
    /// every rewrite into a server-wide tab reload.
    ///
    /// <para>
    /// HOW THE THREE LAYERS FIT TOGETHER. A plugin that ships a
    /// <c>RefreshKitIgnoredElements</c> element in its own configuration XML
    /// (see <see cref="PluginGenerationProvider.PluginDeclaredIgnoreElement"/>)
    /// needs no entry here: it declares its own bookkeeping and stays correct
    /// across its own releases without anyone touching this file. This registry
    /// exists for plugins that cannot or do not yet declare it themselves. The
    /// admin's <c>ConfigIgnoredElements</c> setting is seeded from this registry
    /// and is the per-server override for everything else.
    /// </para>
    ///
    /// <para>
    /// HOW TO ADD A PLUGIN. Append one <see cref="Entry"/>: the plugin's GUID
    /// (stable across renames; take it from its <c>meta.json</c> or
    /// <c>Plugin.Id</c>), its display name for readers, the exact top-level
    /// element names as the plugin's configuration class serializes them, the
    /// plugin version and date you verified them against, and WHY each element
    /// is bookkeeping (which code path rewrites it, and how often). Element
    /// names are matched case-insensitively against direct children of the
    /// document element only. Keep the list to elements that change nothing a
    /// browser renders; a value an admin edits on the plugin's settings page
    /// belongs in the generation. <c>KnownPluginConfigurationHintsTests</c>
    /// checks the shape of every entry, so a malformed addition fails the
    /// build's test gate rather than an installation.
    /// </para>
    /// </summary>
    internal static class KnownPluginConfigurationHints
    {
        /// <summary>One plugin's bookkeeping elements and the evidence behind them.</summary>
        internal sealed class Entry
        {
            public Entry(
                string pluginId,
                string name,
                string verifiedAgainst,
                string reason,
                params string[] ignoredElements)
            {
                PluginId = pluginId;
                Name = name;
                VerifiedAgainst = verifiedAgainst;
                Reason = reason;
                IgnoredElements = ignoredElements;
            }

            /// <summary>The plugin GUID in dashed form.</summary>
            public string PluginId { get; }

            /// <summary>The plugin's display name, for readers of this file and the settings page.</summary>
            public string Name { get; }

            /// <summary>The plugin version and date the element names were checked against.</summary>
            public string VerifiedAgainst { get; }

            /// <summary>Which code path rewrites these elements and how often.</summary>
            public string Reason { get; }

            /// <summary>Top-level configuration element names, as serialized.</summary>
            public IReadOnlyList<string> IgnoredElements { get; }
        }

        internal static readonly IReadOnlyList<Entry> Entries = new[]
        {
            new Entry(
                "f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b",
                "Jellyfin Enhanced",
                "12.8.0.0, source checked 2026-09-24",
                "ScheduledTasks/ClearTranslationCacheTask.cs has a StartupTrigger and calls SaveConfiguration() "
                + "with a fresh ClearTranslationCacheTimestamp at every server start; "
                + "Services/AnalyticsReportingService.cs rewrites the Analytics* receipts on registration and "
                + "every AnalyticsReportIntervalDays (7-30) days. None of them change what a browser renders.",
                "ClearTranslationCacheTimestamp",
                "AnalyticsInstallId",
                "AnalyticsInstallSecret",
                "AnalyticsLastReportedAt",
                "AnalyticsLastPayloadJson",
                "AnalyticsLastReportedPluginVersion",
                "AnalyticsLastReportedJellyfinTarget",
                "AnalyticsLastReportedJellyfinVersion",
                "AnalyticsForbiddenSinceLastSuccess"),
        };

        /// <summary>
        /// The registry rendered as admin-setting lines, one
        /// <c>&lt;guid&gt;:&lt;Element&gt;</c> per element. The GUID form is used
        /// because it survives a plugin renaming itself; the settings page
        /// explains the format.
        /// </summary>
        internal static string[] DefaultConfigIgnoredElements() =>
            Entries
                .SelectMany(entry => entry.IgnoredElements.Select(element =>
                    string.Format(CultureInfo.InvariantCulture, "{0}:{1}", entry.PluginId, element)))
                .ToArray();
    }
}
