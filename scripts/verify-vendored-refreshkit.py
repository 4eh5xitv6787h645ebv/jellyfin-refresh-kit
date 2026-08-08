#!/usr/bin/env python3
"""Prove the plugin's vendored RefreshKit differs by only three adaptations."""

from __future__ import annotations

import difflib
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT_SOURCE = ROOT / "RefreshKit.cs"
VENDORED_SOURCE = ROOT / "plugin" / "Jellyfin.Plugin.RefreshKit" / "RefreshKit.cs"

VENDOR_NOTE = r"""//  VENDORED COPY — DO NOT EDIT CASUALLY
//  ===================================
//  This is the repository-root RefreshKit.cs, vendored into the standalone
//  plugin. It differs from the root file in exactly three places, all marked
//  "STANDALONE-PLUGIN ADAPTATION":
//    1. the namespace is Jellyfin.Plugin.RefreshKit (root: JellyfinRefreshKit);
//    2. RefreshKitOptions gains HtmlPostProcess, HtmlPostProcessWithTags,
//       and ApplyHtmlPostProcess;
//    3. the middleware calls ApplyHtmlPostProcess right after
//       ReplaceOwnedScriptTags.
//  To re-sync after the root file changes:
//    sed 's/^namespace JellyfinRefreshKit$/namespace Jellyfin.Plugin.RefreshKit/' \
//        RefreshKit.cs > plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs
//  and re-apply (2) and (3). The file is vendored rather than
//  <Compile Link>ed because those hooks do not exist upstream and the root file
//  is owned by the single-file adoption path.
//
"""

OPTIONS_ADAPTATION = """        /// <summary>
        /// OPTIONAL — STANDALONE-PLUGIN ADAPTATION (not in the upstream
        /// RefreshKit.cs at the repository root). A last-pass rewrite of the
        /// injected HTML, applied AFTER this plugin's own tags have been
        /// scrubbed-and-reinserted and BEFORE the representation is encoded,
        /// cached and ETagged. The standalone plugin uses it to stamp
        /// <c>?rkv=</c> onto OTHER plugins' unversioned script/link tags.
        /// <para>
        /// The delegate must be PURE with respect to the current generation:
        /// the representation cache is keyed on the injected tag block (which
        /// carries the generation), so the same input HTML plus the same
        /// generation must always produce the same output bytes.
        /// </para>
        /// Exceptions are swallowed and the un-post-processed HTML is used.
        /// This released one-argument surface is retained for binary and source
        /// compatibility; the standalone plugin itself uses the internal
        /// contextual hook below so it can consume the exact tag identity.
        /// </summary>
        public Func<string, string>? HtmlPostProcess { get; set; }

        /// <summary>
        /// Contextual standalone hook. Its second argument is the exact tag
        /// block that keys this representation.
        /// </summary>
        internal Func<string, string, string>? HtmlPostProcessWithTags { get; set; }

        /// <summary>
        /// Applies <see cref="HtmlPostProcess"/> with the exact tag block that
        /// keyed this representation; never throws, never returns null/empty.
        /// </summary>
        internal string ApplyHtmlPostProcess(string html, string currentTags)
        {
            if (string.IsNullOrEmpty(html))
            {
                return html;
            }

            var processed = html;
            try
            {
                var legacyResult = HtmlPostProcess?.Invoke(processed);
                if (!string.IsNullOrEmpty(legacyResult))
                {
                    processed = legacyResult;
                }
            }
            catch
            {
                // Preserve the input to the failing hook.
            }

            try
            {
                var contextualResult = HtmlPostProcessWithTags?.Invoke(processed, currentTags);
                return string.IsNullOrEmpty(contextualResult) ? processed : contextualResult;
            }
            catch
            {
                return processed;
            }
        }

"""

MIDDLEWARE_ADAPTATION = """                            // STANDALONE-PLUGIN ADAPTATION: third-party tag
                            // stamping runs here, inside the same transform,
                            // so its output is what gets encoded, ETagged and
                            // cached — one representation, one validator.
                            html = _options.ApplyHtmlPostProcess(html, scriptTags);

"""


def replace_exactly_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    root = ROOT_SOURCE.read_text(encoding="utf-8")
    vendored = VENDORED_SOURCE.read_text(encoding="utf-8")
    normalized = replace_exactly_once(vendored, VENDOR_NOTE, "", "vendor notice")
    normalized = replace_exactly_once(
        normalized,
        "namespace Jellyfin.Plugin.RefreshKit",
        "namespace JellyfinRefreshKit",
        "namespace adaptation",
    )
    normalized = replace_exactly_once(
        normalized, OPTIONS_ADAPTATION, "", "HtmlPostProcess adaptation"
    )
    normalized = replace_exactly_once(
        normalized, MIDDLEWARE_ADAPTATION, "", "middleware adaptation"
    )
    if normalized != root:
        diff = "".join(
            difflib.unified_diff(
                root.splitlines(keepends=True),
                normalized.splitlines(keepends=True),
                fromfile=str(ROOT_SOURCE.relative_to(ROOT)),
                tofile=f"normalized-{VENDORED_SOURCE.relative_to(ROOT)}",
                n=3,
            )
        )
        raise RuntimeError("vendored source has an unapproved difference:\n" + diff)
    print("==> Root/vendored RefreshKit parity passed (three explicit adaptations)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
