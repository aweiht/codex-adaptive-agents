# Codex Adaptive Agents validation

Evidence applies to a specific source revision and environment. Offline tests
and local readiness do not establish model access or quota on another machine.

## 2026-09-30: 0.8.0 root-independent source and local integration

The current, unreleased 0.8.0 source does not read or gate on root-model identity. `select`
uses the existing child-only Luna capability and policy path whether root
identity is absent, unknown, current, or a future model. `model_policy.json`
stays in the runtime layout solely to preserve exact install-receipt paths; its
unrestricted metadata is not read by selection. Live verification preserves
official root defaults and exact requested/effective checks for explicit
overrides. Missing or null public model fields remain unknown. Capability
snapshots are child-only schema 2 and continue to read valid legacy schema-1
snapshots without using their root field as eligibility. Install receipts keep
strict exact-layout validation for 0.6.0, 0.7.0, and 0.8.0 profiles.

The root-independent behavior snapshot below was checked offline and through
isolated fixtures before the release-boundary hardening recorded later in this
section.
No `verify --live` run or active `CODEX_HOME` installation was performed for
this root-independent revision; no Hook was added or installed. Model/provider
execution remains unverified for this source.

At that earlier root-independent source stage on macOS arm64 / Python 3.14.6,
the offline suite ran **123 tests:
122 passed and one Linux-only test was skipped**. This includes the release,
CLI, policy, transport, verification and strict install-layout tests. The
source CLI and a copy installed into a temporary isolated home both report
version `0.8.0`. An isolated `select --project ... --explain` run used a
synthetic child capability snapshot and local fallback inputs, returned
`adaptive_luna_xhigh`, and made no model or network calls.

At that same source stage, an isolated lifecycle test read and checksum-verified the earlier
gate-enabled 0.8.0 source ZIP, applied this same-version source update, and
confirmed six changed runtime files, preserved synthetic root settings
(`gpt-6.1-sol` / `high`), idempotent reapply, exact rollback to the previous
receipt and bytes, successful reapply, and uninstall. The test used a temporary
home and made no provider or network calls. These are offline compatibility
checks, not evidence that a model was run or that a particular root model is
supported by a provider.

Earlier on 2026-09-30, a pre-release 0.8.0 snapshot with a main-model gate ran
the full Python suite: **125 tests, 124 passed and one Linux-only test skipped**
on macOS arm64 / Python 3.14.6. It also tested isolated upgrades from actual
0.6.0 and 0.7.0 runtime layouts, preserving `gpt-6.1-sol` / `high`, unrelated
configuration, idempotence, exact rollback, and uninstall. Those results are
retained as evidence for that earlier snapshot only, not for the final source
described here. Historical entries below retain the facts observed at their
original dates.

### Final 0.8.0 offline integration on macOS arm64 / Python 3.14.6

The complete offline suite ran **141 tests: 140 passed and one was skipped**.
The skipped case requires Linux. The run explicitly added the repository root
to `sys.path` while keeping the interpreter isolated (`-I -B`), then discovered
all `tests_python/test_*.py` modules. This includes the current release, CLI,
install, policy, transport, and verification changes; the existing 0.6.0 and
0.7.0 receipt-layout and migration compatibility cases passed in that suite.
No test was skipped to hide a failure.

An isolated lifecycle used the checksum-pinned, pre-fix 0.8.0 ZIP
(`8137b80c99eb0332df4dd55749604a2ddc52301ca4e4e5d3d93df69c47f578d6`) as the
installed source, then applied the current 0.8.0 source. The same-version
update succeeded, repeat install returned `IDEMPOTENT_PASS`, exact rollback
returned `ROLLBACK_EXACT_PASS` and restored every captured managed and user
byte, reapply succeeded, its repeat was idempotent, and uninstall returned
`UNINSTALLED`. Synthetic root model/effort, later-added notification, MCP and
project settings, a custom role, and user instructions were preserved. The
home existed only in an isolated temporary directory. This exercises local
installer behavior and does not establish provider access or desktop behavior.

The final release ZIP was built into a private output directory from the
current 45-file source allowlist, checked against the same captured source
bytes, and inspected for exact payload and checksum coverage. The checked-in
root `SHA256SUMS` was refreshed for those 45 source files. 0.8.0 remains
unreleased (`SOURCE_READY`); the ZIP was not published.

This final validation was local and offline. No actual Codex home, Hook,
`verify --live`, provider, network service, Windows runner, Linux runner, or
CI job was used. Earlier root-independent, pre-release-gate, and historical
entries below retain their original test counts and describe only their stated
source snapshots.

### 2026-09-30 release-boundary hardening (focused local stage)

The release builder now captures allowlisted files through checked opens,
checks the opened descriptor identity, and carries those captured bytes through
staging and ZIP validation. POSIX uses no-follow directory-relative opens;
Windows rejects reparse points and verifies the opened file identity before
reading. ZIP validation requires the exact current manifest
payload set and compares each file to the captured source hash; recomputing an
archive's own SHA256SUMS does not make missing, extra, or changed payloads pass.
Archive and external checksum targets are created exclusively and checked
against the created file identity, preventing a raced target symlink from
being followed.

Before the final integration, on macOS arm64 / Python 3.14.6,
`tests_python/test_release.py` ran **11 tests, all passed**. Coverage includes the normal allowlisted build and workflow check,
source-file and parent-directory symlink swaps, a raced external checksum
target, and missing, extra, or self-checksummed changed archive payloads. This
is focused local evidence for those attack cases. The suite and lifecycle
results above cover the subsequent final integration. Earlier suite and ZIP
digests identify their earlier source snapshots, not the final package.

POSIX uses directory-relative no-follow file operations where available and
binds bytes to `fstat` identities. Windows Python's standard library lacks
equivalent `dir_fd` operations; its fallback checks for reparse points and
compares file and parent identities around path-based access, but cannot prove
safety against a same-user parent-directory swap-away-and-back during the
operation. This has not yet been checked on Windows or Linux CI.

## 2026-09-24: compact process output and execution handoffs

The complete offline suite ran **106 tests: 105 passed, one Linux-only test
skipped**, on macOS arm64 / Python 3.14.6. The focused process and CLI suite
passed 22 tests, including default-output compatibility, nonzero exits,
timeouts, output overflow, partial log writes, unsafe log paths, and rejecting
`--summary` for other commands.

The installed runtime was exercised with the same real command in full and
summary modes. Each command produced 30 samples over at least 15 seconds,
with 245,760 stdout bytes and 16 stderr bytes. Completion JSON measured
246,076 bytes in full mode and 812 bytes in summary mode; saved log bytes
matched the full output exactly. Each invocation emitted one startup event
and one completion JSON, with no agent reads of intermediate progress.
These are output-size measurements, not model-token savings or evidence of
a changed daily Astra/Luna usage ratio.

An isolated upgrade, exact rollback, reapply, repeat installation, and
uninstall preserved user settings and instructions. The reviewed local
installation changed no configuration keys; unrelated roles and instructions
outside the managed block were preserved. All 16 runtime files match source,
a repeat installation plan has no changes, and `doctor` reports
**READY_FALLBACK**. Two native Luna/xhigh children handled implementation and
documentation; no separate `verify --live` test was run. This new revision
has not yet run on Windows or Linux CI.

## 0.7.0: GPT-6 Luna and xhigh fallback

On 2026-09-23, the complete offline suite ran **101 tests: 100 passed, one
Linux-only test skipped**, on macOS arm64 / Python 3.14.6. Tests cover missing
scores, old-model radar rows, exact-model cache identity, same-day migration,
failed capability probes, and preserving rating-based choices. Installation
coverage includes 0.6.0 roles using `gpt-5.6-luna`, upgrade to `gpt-6-luna`,
root setting preservation, strict child configuration conflicts, rollback, and
uninstall. Public-event validation rejects the previous child model while
accepting explicitly selected supported reasoning levels.

The existing local installation was upgraded with a reviewed plan and backup.
All five role files now specify `gpt-6-luna`; the default child effort is
`xhigh`. The root Astra/xhigh settings, unrelated configuration, other roles,
and instructions outside the managed block were preserved. All 16 installed
runtime files match source, and a repeat installation plan has zero changes.
The official CLI 0.155.1 public catalogue exposes GPT-6 Luna with all five
configured reasoning levels. Actual `doctor` returned **READY_FALLBACK** and
`select` returned **adaptive_luna_xhigh**, because comparable radar evidence
was insufficient.

These checks establish local installation and selection readiness. Two native
children were requested as GPT-6 Luna/xhigh for development work; no separate
`verify --live` acceptance was run. This revision has not been validated by a
new Windows/Linux CI run or published release. Earlier results below remain
historical evidence for their stated revisions.

## 2026-09-22 delegation-policy trial

The parent and leaf templates plus the English and Simplified Chinese public
guidance were reviewed together for simple questions, one-off small changes,
complete feature or UI diagnosis, serial shared resources, ordinary failure
repair, cross-module integration, and high-risk permission, data, or process
acceptance. The current policy requires a safe package with explicit goal,
boundary, ownership, and checkable acceptance to be dispatched before its work
starts when the host permits it. It has no cost, elapsed-time, token-saving, or
fixed-ratio gate and adds no usage-collection system. This is a routing trial;
real-task token or completion-time changes remain unverified.

No wording-only tests were added. The existing offline suite ran with
`python3 -m unittest discover -s tests_python -p 'test_*.py' -v`: **91 tests,
90 passed, one Linux-only test skipped** on macOS arm64 / Python 3.14.6. The
existing release builder also produced `codex-adaptive-agents-0.6.0-python.zip`
with `source_equals_runtime: true` and validated the public Markdown links and
checksums. No additional `verify --live` model smoke test was run for this
instruction update; ordinary development delegation is separate from that test.

## 0.6.0: one project identity

On 2026-09-20, the complete local suite ran **91 tests: 90 passed, one Linux-only
test skipped**, on macOS arm64 / Python 3.14.6. This includes 24 installation
tests and separate-process CLI lifecycle checks with simulated public catalogue
inputs. Removed tests covered only the discarded cross-project migration paths.

The actual local installation now runs the new entry at version **0.6.0**.
`doctor` returned **READY_FALLBACK** and `select` returned `adaptive_luna_max`.
All 16 installed runtime files match source; a repeat installation plan has no
changes. Configuration, six existing role files and instructions outside the
managed block were preserved. These checks started no model tasks.

The source entry is `codex-adaptive-agents.py`, the Python package is
`codex_adaptive_agents`, installed resources are under
`CODEX_HOME/codex-adaptive-agents`, and project state is under
`.codex-adaptive-agents`. The distribution contains one installation contract;
it does not include a migration layer for another project identity.

The supported lifecycle is fresh install, repeat install, source-directory move,
local readiness, selection, version upgrade, backup rollback, and uninstall.
Tests preserve unrelated configuration, instructions and custom roles. Future
model migrations use synthetic test profiles, not claims of model availability.

Public commands and installation receipts reject unsupported versions,
conflicting managed edits and unsafe paths. `READY_FALLBACK` is a successful
local installation check with a conservative supported role. `select` does not
start a model task. See [the operating guide](PUBLIC_GUIDE.md) for commands.

No new live model verification was requested for this revision. Check its own
published CI run for Linux and Windows results; an earlier run does not validate
changed source. The cooperative process registry only checks registered
processes and is not an operating-system security sandbox. See
[process cleanup boundaries](PUBLIC_GUIDE.md#process-cleanup-boundary).

## Earlier published CI

[Run 35509779531](https://github.com/aweiht/codex-adaptive-agents/actions/runs/35509779531)
passed six jobs for `e9024b9e7f49a5edd60f84ad01ac8fd721928749` on macOS 14,
Ubuntu 24.04 and Windows Server 2022 with Python 3.11 and 3.13. The jobs run the
offline suite, isolated installation lifecycle and source ZIP validation. This
is evidence for that commit only.

## Documentation images

The workflow SVGs illustrate use of the project. The CLI illustration displays
selected actual command output with private paths omitted; its public input and
capture scope are described in [the image notes](images/README.md).
