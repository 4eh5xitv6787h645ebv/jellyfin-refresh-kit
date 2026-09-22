# Enhanced adoption readiness: Refresh Kit 1.1.0.3

This closes the findings in the 2026-09-22 review at `e2e/jellyfin/artifacts/readiness-review/review.md` against Enhanced 12.8.0.0. Runtime 2.5.0 protects Enhanced review forms after blur, dirty admin settings, and saves in progress. Other application-owned drafts and saves use `registerReloadGuard`; every registered guard must explicitly permit a reload. Guards compose across instances and retain their release handles during manager upgrades. Successful saving or intentional discarding allows reloads again.

The three existing fixes were reviewed and preserved: clock-rollback reload-budget accounting, retaining the last good configuration snapshot after read/access failures, and disabled-fieldset editor detection with first-legend exceptions.

Enhanced should keep its independent script injector and emit the kit before its existing entry tag, following [the embedding example](../examples/enhanced/README.md). Replacing Enhanced's injector with another stacked `AddRefreshKit` helper is not the supported path. The adoption fixtures install the official Enhanced binaries unmodified alongside the standalone kit and another helper, and verify both middleware orders, injection disable/reenable, and duplicate runtime copies. No Enhanced upstream files were changed.

## Exact candidate and release receipts

Published [v1.1.0.3](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases/tag/v1.1.0.3) contains exactly the two ZIPs retained by the successful validation run. `main` was fast-forwarded to the validated manifest child after publication.

- Source: `4a09fc32a59d4abf4f1f1ede3d330e04d735854d`.
- Manifest child: `25da87f09fa48d1e16b16413cdcf9531e1c10c5f`.
- Source tree SHA-256: `5d0cfd8e7cdc6e5413738eca421b6a033298d0377f239e81966ecdc6408f7ba5`.
- net9 ZIP SHA-256: `10128af5b9109d65cbc1a0dfbc7a80afaf4d4b551fea0702a043ecc63c0f5396`.
- net10 ZIP SHA-256: `ee4fe32a3339cbf1c9b42f8b4385d44bdfbe08b7b62212aa6c85da0d017a910a`.
- [Candidate validation](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35762138092): **PASS**, including the final independent rebuild and 479-file retained bundle.
- [Post-release correspondence](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35766149293): **PASS**. Published asset bytes, release/tag identity, manifest child, retained validation, and main promotion were verified read-only.

| Evidence | Result |
| --- | --- |
| .NET regression suites | 399 passed on net9; 399 passed on net10; none skipped |
| Chromium regression suite | 101 passed; no failures |
| Static/negative release-tool checks, package verification, two-path reproducibility and concurrent-build locking | Passed |
| NuGet and npm security audits | Passed |
| Full real-server and proxy integration | Passed: ABI-floor smoke; installation/update/disable/enable/uninstall/reinstall; real two-version rollback lifecycle; playback and three-tab restart safety; ordinary proxies, cache controls/remedies, and `/jellyfin` subpath |
| Locked compatibility campaign | All 44 locked archives verified; 14 runtime matrices complete: 12 passed, 2 passed with the documented GetAvatar outer-owner stamping limitation |
| Four Enhanced adoption cases | Passed all ten assertions in each host/order case; exact runtime identity, entry preservation, duplicates, draft/save protection, and disable/reenable verified |

## Investigated integration failures

The historical scheduled failure had no retained request-level artifact, so its exact cause cannot be established retrospectively. The new failure retention and focused diagnostics reproduced an intermittent pre-restart `Generation` request cancellation on both host lines with the runner's system browser.

A controlled ten-trial comparison on source `6ffb563` found:

| Browser | Trials | Transport audit failures | Observed application behavior |
| --- | ---: | ---: | --- |
| System Chrome 152.0.7977.0 | 10 | 5 | Every canceled generation request had HTTP 200 and a successfully parsed complete generation/epoch payload; no navigation or JavaScript abort coincided with it. |
| Locked Chrome 152.0.7977.54 | 10 | 0 | All restart, authentication, same-document, three-tab and websocket assertions passed. |

The [system-browser trace](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35761621249) and [pinned-browser comparison](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35761717367) retain redacted network and parsing evidence. For example, system trial 4 recorded response 200 at elapsed 12,539 ms and `ERR_ABORTED` at 12,540 ms; the runtime's JSON parser had already returned the expected generation and epoch. This supports a browser transport-reporting difference for the reproduced event, not an incomplete version response. It does not identify the underlying Chromium implementation defect.

The harness fix awaits Puppeteer 25's asynchronous executable lookup, uses its locked browser by default, and records browser versions. It does not allowlist the cancellations or change the error audit. The optional parsing observer is reproducible with `RK_BROWSER_DIAGNOSTICS=1` and an explicit `RK_BROWSER_EXECUTABLE`; instrumented runs are rejected as release evidence. See [lab instructions](../e2e/jellyfin/README.md).

An independent package-identity investigation ([comparison run](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35756760536), [pinned compiler confirmation](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/actions/runs/35757228624)) found that an exact SDK could still run its compiler on a newer installed runtime patch. Roslyn embedded that runtime identity in its PDBs, changing DLL and ZIP hashes. The package builder now disables compiler-host roll-forward and shared compilation, and the reproducibility check deliberately varies the caller's roll-forward setting. Deployed Jellyfin runtime selection is unchanged.

## Scope and remaining limitations

Automated browser evidence is Chromium-based. Enhanced adoption uses official 12.8 binaries on stable Jellyfin 10.11.11 and 12.1; the separate full lifecycle/ecosystem lab pins 10.11.11 and 12 RC4. The in-place host-upgrade browser leg remains quarantined for its previously documented harness limitations and is not counted as passed.

Connected empty Enhanced review forms conservatively block until closed. The lost-draft reproduction uses Enhanced's real form builder with controlled save callbacks; it does not certify Enhanced's review database API. Other application-owned state needs the documented guard wiring. Per-user settings synchronization remains outside this work.

Stacked `AddRefreshKit` helpers retain their documented stand-down behavior; an adopter must preserve an independent path for its required entry scripts. The existing outer-owner script-versioning limitation remains explicit in the compatibility contract; the supported Enhanced embedding path preserves its entry scripts in both tested orders. No existing release assets were overwritten. Test resources were disposable and cleaned up.
