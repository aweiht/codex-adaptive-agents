# Changelog

## 0.7.0 — Unreleased

- Keep long tests, polling, sampling, and ordinary failure repair inside one
  Luna work package; use script or native-wait monitoring and review compact
  summaries, relevant diffs, and targeted artifacts. Document summary-mode
  process output with private logs, while preserving root risk and final checks.
- Clarify verification ownership: the package owner handles relevant scripts,
  builds, tests, package checks, and ordinary repair; the root sets criteria,
  reviews stage evidence, and decides acceptance without automatically taking
  over validation execution. Independent review is optional and bounded; this
  rule change does not claim measured token savings or add forced scheduling.
- Use `gpt-6-luna` for the default child model and all five native roles.
- Default to `xhigh` when usable scores for this exact model are unavailable;
  invalidate old-model policy and capability caches. Model capability checks
  still apply before selection.
- Preserve the user's main model and reasoning level during installation and
  upgrade. Migrate 0.6.0 receipts without changing their historical profiles;
  keep child configuration and managed-file conflict checks strict.
- Update verification, installation and policy regression coverage, bilingual
  documentation, and the captured local command illustration.

## 0.6.0 — Prior source snapshot

- Revise the trial delegation policy: after a minimal boundary check, a safe
  substantive package with explicit scope, ownership, artifact, and acceptance
  is dispatched to Luna before execution when the host permits it.
- Keep complete feature and UI diagnosis work with one child through local tests
  and ordinary failure repair; escalate only public-interface or major-design
  changes, repeated unresolved failures, or capability and permission limits.
- Keep Astra responsible for decisions, public interfaces, coordination, and
  final and high-risk integration acceptance; serialize shared resources and
  preserve user-specified worker counts or parallel arrangements.
- Remove cost, elapsed-time, token-saving, and fixed-ratio delegation gates.
  This trial adds no usage-collection system and makes no promise that token
  use or completion time will decrease in real tasks.
- Set the public project identity to Codex Adaptive Agents and use the
  `codex-adaptive-agents.py` entry point, `codex_adaptive_agents` package,
  `codex-adaptive-agents` resource directory, `CODEX_ADAPTIVE_AGENTS` marker,
  and `.codex-adaptive-agents/` project cache.
- Keep the `gpt-6-astra` root model, `gpt-5.6-luna` child model, and
  `adaptive_luna_*` roles unchanged.
- Add self-contained installation, verification, update, and optional live
  prompts with the canonical repository URL in both READMEs.
- Document normal future-version updates through a reviewed plan, backup, and
  local `--version`/`doctor` checks. This entry does not claim remote CI or live
  model evidence for 0.6.0.

## 0.5.0 — Prior source snapshot

- Restrict `recover` to interrupted transactions and require an explicit backup
  for rollback; reject unsupported `--dry-run` requests before any command runs.
- Resolve process-ledger case aliases by directory identity while rejecting
  copied ledgers and redirected ledger metadata.
- Validate backup ancestry before directory creation and reject malformed
  receipt fields with structured errors.
- Record ownership of newly created configuration files and empty tables so
  uninstall removes only those without subsequent user content.
- Tie validation claims to the tested commit and separate published CI evidence
  from later local regression checks.

- Keep a fresh capability snapshot when the shared probe deadline expires,
  without extending its age; make `refresh --project` retry that project's
  same-day failure without changing other project caches.
- Retain sanitized public child model/effort metadata and reject contradictory
  spawn requests; missing public evidence remains unknown.
- Supervise POSIX commands with identity-checked descendant cleanup and an
  explicit completion acknowledgement; document the macOS tracking boundary.
- Add per-work-package `process-init`, `process-run`, `process-check`, and
  `process-cleanup` bookkeeping for bounded long commands; keep root cleanup
  separate from leaf reporting and make explicitly retained work visible.
- Clarify that leaves report requests for new Agents to the root instead of
  starting further delegation themselves.
- Accept case aliases of the same installation directory without accepting
  another home or a symbolic-link alias.
- Run CI on every push and pull request, including translated documentation,
  images and ignore rules; honor custom homes in Windows standalone examples.

- Document the earlier delegation gate for substantive work with explicit
  ownership and acceptance; honor an explicit user request to delegate when the
  host permits it.
- Clarify direct-completion cases, parallel versus serial packages, the five
  child limit, re-evaluation triggers, and evidence-based selector, role, and
  tool failure reporting.
- Synchronize the English and Simplified Chinese usage guidance without adding
  dependencies or third-party notice entries.

## Codex-assisted setup and implementation scope — 2026-09-20

- Put copyable installation and post-installation verification prompts near
  the top of both READMEs; keep real model verification explicitly optional.
- Clarify the project's own implementation and actual runtime/data dependencies.
- Reduce NOTICE to this distribution's license and runtime statement.

## Documentation and execution summary — 2026-09-19

- Add English and Simplified Chinese quick starts covering installation,
  everyday Codex use, local readiness and optional live verification.
- Add workflow illustrations and a dated CLI output image with public source
  data and explicit validation scope; include the assets in the source ZIP.
- Publish the compact execution summary: root model name, actual child roles,
  effort levels, counts and observed concurrency. The summary does not claim to
  read the root window's reasoning setting.
- Correct the platform evidence links and explain model-catalogue prerequisites.

## 0.4.0 — 2026-09-15 (source published)

This transition defines a Python-only source and runtime ZIP using Python 3.11+
and the standard library.

- The entry script is codex-adaptive-agents.py; the source and runtime payload use the
  same explicit allowlist.
- The package has no pip dependency, compiled binary, daemon, MCP server,
  lifecycle Hook, or background service.
- Installation keeps the standalone entry at
  codex-adaptive-agents/runtime/codex-adaptive-agents.py under the selected Codex home.
- The package builder writes an in-package and an external SHA256 checksum set,
  rejects unsafe links and symlinks, and refuses to overwrite existing output.
- The workflow defines Python 3.11 and 3.13 checks on macOS, Linux, and
  Windows. A workflow definition is not a completed CI run.
- Windows, Linux and macOS CI passed source, isolated installation lifecycle
  and ZIP checks under Python 3.11 and 3.13. Real native task evidence remains
  macOS only; see [validation](docs/VALIDATION.md).
- Fixed Windows batch command quote handling. UTC needs no IANA database;
  the default local calendar day follows the host operating system.
- Lifecycle validation now matches every revision turn by its unique ID and
  measures overlap only inside active turn intervals. It accepts either
  ordering of completion and final idle notifications while still requiring
  successful completion and a later idle metadata read.
- Daily selection uses the system local calendar day. An expired public
  capability snapshot can still locate the client for a fresh catalogue
  probe; a failed probe never renews an old snapshot's timestamp.
- Installation previews report the current change, retaining original values
  separately in the receipt for uninstall and recovery.


The current 0.7.0 contract and package contents are defined by README.md,
docs/PUBLIC_GUIDE.md, and release-manifest.json.
