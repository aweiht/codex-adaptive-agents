# Codex Adaptive Agents public operator guide

**0.8.0 status: SOURCE_READY.** Codex Adaptive Agents uses the project
repository at <https://github.com/aweiht/codex-adaptive-agents>. The package
contains the Python source used at runtime. No new live model verification was
requested for this revision. Consult [VALIDATION.md](VALIDATION.md) for current
local evidence and the earlier published CI result tied to its exact commit.

## Downloaded package

The package contains one Python entry script, the codex_adaptive_agents modules and
public smoke assets, tests_python, public Markdown, LICENSE, NOTICE, VERSION,
release-manifest.json, the release script, and SHA256SUMS. Source equals
runtime under the checked-in allowlist.

The release builder rejects source links, uses no-follow opens relative to
checked directory handles on POSIX, and verifies each opened file descriptor
against the checked file identity before reading. It stages the captured bytes
and validates the ZIP against the same complete source snapshot:
missing or extra payloads and payload edits are rejected even if the ZIP's own
SHA256SUMS was recomputed. Explicit ZIP directories are limited to ancestors of
allowlisted files. The in-package checksum detects accidental changes; it is
not a signature or proof of publisher authenticity.

It requires Python 3.11 or newer from the standard library. It has no pip
dependency, Go requirement, or compiled binary. The package does not include
historical implementations, private development evidence, local state, or old
release archives.

The entry script accepts --codex-home PATH and --codex PATH before a subcommand.
The home defaults to CODEX_HOME and then the platform normal user Codex home.
The official client command defaults to `codex` resolved from the caller's normal
`PATH`; pass `--codex PATH` to choose an explicit executable. A path recorded in
an earlier capability snapshot is provenance only and is never run as an
implicit command.

## Installation state

Run the package entry script with Python:

~~~sh
python3 codex-adaptive-agents.py --codex-home "$HOME/.codex" install --dry-run
python3 codex-adaptive-agents.py --codex-home "$HOME/.codex" install --yes
python3 codex-adaptive-agents.py --codex-home "$HOME/.codex" doctor
~~~

On Windows PowerShell, use py -3 and PowerShell environment syntax:

~~~powershell
py -3 .\codex-adaptive-agents.py --codex-home "$env:USERPROFILE\.codex" install --dry-run
py -3 .\codex-adaptive-agents.py --codex-home "$env:USERPROFILE\.codex" install --yes
py -3 .\codex-adaptive-agents.py --codex-home "$env:USERPROFILE\.codex" doctor
~~~

The --yes flag applies a fresh managed plan. Add --plan-id HASH to bind a
previously reviewed preview. Before writing, the installer
checks the official CLI and GPT-6 Luna's five public child effort levels. Root
model identity and reasoning effort are not plugin eligibility gates. It hashes managed inputs,
creates a private backup, writes atomically, and verifies the result. It does
not read authentication files or claim that the account can complete a live
task.

After installation, the standalone entry is:

~~~text
$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py
~~~

Run it with Python after the original download directory has been moved or
removed:

~~~sh
codex_home="${CODEX_HOME:-$HOME/.codex}"
# If installation used --codex-home, use that same directory here.
python3 "$codex_home/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$codex_home" doctor
python3 "$codex_home/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$codex_home" uninstall --dry-run
~~~

On Windows PowerShell:

~~~powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
# If installation used --codex-home, set $CodexHome to that same directory.
$Entry = Join-Path $CodexHome "codex-adaptive-agents\runtime\codex-adaptive-agents.py"
py -3 $Entry --codex-home $CodexHome doctor
py -3 $Entry --codex-home $CodexHome uninstall --dry-run
~~~

A successful uninstall removes the owned runtime and managed installation
files while preserving unrelated user changes.

## Update an existing installation

Use the project repository at
<https://github.com/aweiht/codex-adaptive-agents> and read its `VERSION` from
the default branch. An update does not require a GitHub Release or tag.

For a Git checkout with local edits, preserve the edits and use a separate
fresh checkout when necessary. Set the project remote or clone a new checkout:

~~~sh
git remote set-url origin https://github.com/aweiht/codex-adaptive-agents.git
git pull --ff-only
python3 codex-adaptive-agents.py --version
~~~

ZIP users should fetch and extract a fresh archive from that repository. The
previous download directory is not an update source. Updating a checkout with
`git pull` alone does not update the copied runtime under `CODEX_HOME`.

Use the existing `CODEX_HOME` and installed runtime path. From the new source
checkout, obtain and review a plan, then apply exactly that plan:

~~~sh
python3 codex-adaptive-agents.py --codex-home "$CODEX_HOME" install --dry-run
python3 codex-adaptive-agents.py --codex-home "$CODEX_HOME" install --yes --plan-id <plan_id>
python3 codex-adaptive-agents.py --version
python3 "$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$CODEX_HOME" --version
python3 "$CODEX_HOME/codex-adaptive-agents/runtime/codex-adaptive-agents.py" --codex-home "$CODEX_HOME" doctor
~~~

The installer makes its normal backup and preserves unrelated settings and
roles. A managed conflict requires attention; do not overwrite it. There is no
need to uninstall or reinstall, and there is no automatic or self-update
command. Refresh only when the capability snapshot or policy cache is stale,
then run `doctor` again. Accept the update only when the source and installed
`--version` outputs match the expected version and `doctor` returns `READY` or
`READY_FALLBACK`. New model support arrives through maintainer releases;
arbitrary future models are not promised to work automatically. Do not run
`verify --live` or a model call as part of this update check.

## Offline commands and policy

doctor is an after-install, offline check. A clean home reports NOT_READY
because no installation exists. After installation it checks managed hashes,
the standalone entry, the official CLI version, and local capability and policy
state without a model call or network request.

The other local commands are:

~~~sh
python3 codex-adaptive-agents.py select --project /path/to/project
python3 codex-adaptive-agents.py select --project /path/to/project --explain
python3 codex-adaptive-agents.py refresh
~~~

### Main-model independence

`select` does not read, require, or gate on the main model. Missing, unknown,
current, and future model slugs all use the same Luna child capability and
selection policy. `--root-model` is accepted by `select` only for command-line
compatibility and is ignored there. The file
`codex_adaptive_agents/model_policy.json` remains in the runtime layout so
existing install receipts keep their exact path contract; it now contains only
unrestricted metadata and is not read by the selector. The plugin does not
change the user's main model or reasoning effort.

The root effort has no plugin whitelist or forced `max`; the official client
chooses what it supports. `refresh` and `doctor` maintain/check child
capabilities without consulting root identity.

Each project keeps its policy cache in .codex-adaptive-agents/state; projects should
ignore .codex-adaptive-agents/. A next-day delegation attempt can refresh an expired
project policy. Use refresh --project /path/to/project once to update policy inputs
after a same-day selection failure. An ordinary `select` that finds malformed
JSON or a semantically invalid policy cache returns `blocked` and leaves that
cache unchanged. A normal next-day selection can still use its one daily refresh
attempt for an expired but structurally valid cache. The explicit `refresh`
command refreshes the public capability snapshot, then rechecks policy-cache
state under the refresh lock before rebuilding; it does not silently treat a
damaged previous result as valid. Without `--project`, refresh updates home-level
policy state. Both forms leave unrelated project caches intact. They do not
retry a native child dispatch or establish quota recovery; if refresh fails,
keep the actual error and do not loop.

The child model is `gpt-6-luna`. If public sources have no usable scores for
this exact model, the selector retains a still-valid verified cache or falls
back to `xhigh` when the local capability catalogue supports it. Older Luna
model scores and caches cannot select a GPT-6 Luna role. Missing model access
or stale capabilities remain readiness failures, not scoring fallbacks. The
result reports why fallback was used and does not claim a dynamic update succeeded.

## Delegation gate

The root agent owns requirements, key decisions, public interfaces,
coordination, and final acceptance. Before substantive work starts, it does
only enough exploration to identify the boundary. A safe work package with an
explicit goal, boundary, artifact or problem ownership, and checkable acceptance
must be handed to a native Luna child when the host permits it, before that
package starts. This is a trial routing policy: it has no total-cost, elapsed-time, or
token-saving threshold, no 2:8 (or other) delegation ratio, and no usage-
collection system. An explicit user request to delegate is honored within host
permissions; a verbal split or a child added after the work is complete is not
delegation.

A complete feature or UI diagnosis stays with one child through implementation,
relevant verification scripts, build, test, package checks, and ordinary failure
repair. Entering validation does not transfer execution to the root; return a
normal failure to the package owner. Escalate only for a public-interface change
or major design decision, a failure that remains unresolved after reasonable
repair, or a capability or permission limit. The root defines acceptance
criteria and risk boundaries, reviews real diffs, key evidence, and boundaries
at stage handoffs, and makes the acceptance decision. This does not require the
root to run every validation command or script. When independent execution
review is needed, the root may assign a bounded Luna review package with no
write overlap, while retaining the decision; an extra reviewer is not required
by default. The root independently checks high-risk permission, real-data, and
process-cleanup behavior within existing authorization boundaries, confirms
required authorization is already in place, and does not infer or expand it. A
child's report or exit code is not a substitute for that check.

Long tests, polling, sampling, and ordinary failure handling remain in that
same Luna package. Use a script or native wait for ongoing monitoring. Review
stage deliveries together: the root reads the handoff summary, relevant diff,
and specific artifacts without taking over execution or repeating the same full
exploration or tests. Expand checks only for a clear evidence conflict, missing
key coverage, a pending decision, or a risk signal.

Direct completion is appropriate for a simple question, a genuinely one-off
small change, a root-only decision, a tightly coupled dependency chain with no
safe independent boundary, or an explicit no-delegation request. Do not split a
substantive batch into small edits to avoid this assessment. Independent
packages may run in parallel; dependent packages run serially. Shared browser,
database, GUI, port, or build-output resources have one owner and are serialized
when needed. One task uses no more than five direct Luna children, and agents
are not started merely to satisfy a ratio or create duplicate work.

| Example | Expected route |
| --- | --- |
| Simple question, one small edit, or a root-only decision | Complete directly |
| Complete feature or UI diagnosis with clear ownership and acceptance | Delegate implementation, relevant verification scripts, build/test/package checks, and ordinary repairs to one child |
| Independent work packages with separate resources | Delegate; parallelize when safe |
| Dependent packages or shared GUI, port, or build directory | Delegate; use serial order with one resource owner |
| Cross-module integration or high-risk permission, real-data, or process behavior | Child performs the package; root owns the acceptance judgment and independently checks high-risk behavior within existing authorization boundaries; a separate bounded reviewer is optional |
| Public-interface change, major design decision, or a block that remains after bounded recovery or needs new authorization | Escalate to the root; continue independent work |
| The user explicitly asks for no delegation | Complete directly |

If the user specifies a worker count or parallel arrangement that cannot be met
safely, explain the constraint and ask to adjust it; do not silently change the
request. Reassess unfinished work when scope expands, the original plan stops
fitting, troubleshooting repeats, or a new implementation or verification
phase begins. For each actual selector, role, or native-dispatch failure, add
one concise line to the existing task status entry with the safe original
error, category, affected package, time, any `retry-after`/quota reset or
checkable recovery condition, cumulative attempts, whether that recovery event
is consumed, current owner, and handoff point. An old failure is not evidence
about a new batch; the count for the same
unresolved failure persists across turns and packages. If a response may mean a
child already started, check for that active owner before retrying. A successful
selection is not a dispatch or recovery result, and these instructions cannot
force host scheduling.

Classify before acting. Distinguish quota, concurrency, parameter/role,
permission, temporary network/service, and host-capability errors; mark an
uncertain category `unknown`, never as Luna quota by default. Explicit host
non-support is a capability limit, not a transient failure; retry only after
host support demonstrably changes. When concurrency is full, reuse a suitable Luna who has
stopped writing, or wait natively for current work to finish; never interrupt
another child to free a slot. A failed new child request does not show that an
existing child cannot continue, and without a close operation do not claim a
slot was released. For temporary rate, network, or service errors, honor an
explicit `retry-after`; when none is given, wait 30 seconds, 2 minutes, then 5
minutes. In either case, allow at most three extra attempts for the same fault;
an explicit delay changes timing, not the limit. Use native waits, not frequent
probes. Once exhausted, do not start another backoff sequence. Allow one real
retry only for a new, unconsumed recovery event or checkable condition change,
and mark it consumed. If it fails, the same past reset time or status change
cannot trigger another retry in a later turn; retain the cumulative record and
wait for a new recovery event.

For explicit quota exhaustion, wait for a stated reset and check once after it;
do not probe before it, and mark that reset event consumed. If no reset is
given, do not guess a time or retry blindly: resume only after a new checkable
state change or user action. The same past reset time or consumed state change
cannot trigger another attempt. A reset time does not prove recovery. For
authentication, permission, parameter, or role
errors, diagnose and actually correct the cause before a bounded retry; do not
change account or permission settings without authorization. The root may take
one small, pre-scoped package within current authorization while recovery is
unresolved, and must stop at its stated close point. Do not keep renaming
dependent packages as temporary takeovers: without new recovery evidence,
leave later Luna-dependent work pending with its recovery condition, and ask
the user before expanding root execution. Independent work may continue. Once
real native-dispatch recovery is evidenced, hand the remaining package back at
a safe boundary; the root stops writing to that scope and records the evidence,
remaining work, and new owner. Ordinary package test failures stay with the
original owner. These are agent instructions, not a host-enforced guarantee of
delegation.

Each work package states its full goal, boundary, artifact or problem
ownership, acceptance checks, and necessary local context. Prefer bounded,
history-free handoffs without unrelated history. The execution owner reports
startup once, delivers once, and reports exceptions, thresholds, or decisions
when they arise. If an interim user update is required, use known state; do not
reread unchanged logs or snapshots to produce it. A native wait timeout ends
only that wait interval and does not remove a host turn. The child returns five
concise points: completed work, file or artifact locations, verification
evidence, unresolved issues, and pending decisions. Keep detailed logs at the
agreed local path for on-demand reading; do not omit failures or risks. The
root waits on the same native task and works only on different,
non-overlapping work while it runs. Record the delegation or direct-completion
reason in one sentence in the plan or progress update and keep the compact
execution footer. Do not create a separate delegation report file. After
declaring work stopped or complete, the child stops writing; further repair
starts only after the root reassigns it.

## Temporary process and long command runs

Ordinary short commands do not need a process run. Replace angle-bracket
placeholders with the values passed by the root. If a work package needs a
temporary service or a bounded long command, the root creates one isolated run
and passes its `project`, `run_id`, entry point, owner, purpose, and resource or
port boundary to each child:

~~~sh
codex_home="${CODEX_HOME:-$HOME/.codex}"
entry="$codex_home/codex-adaptive-agents/runtime/codex-adaptive-agents.py"
python3 "$entry" --codex-home "$codex_home" process-init --project /path/to/project
python3 "$entry" --codex-home "$codex_home" process-run \
  --project /path/to/project --run-id <run_id> --owner <owner> \
  --purpose "long test" --summary --timeout 3600 -- python3 -m unittest
python3 "$entry" --codex-home "$codex_home" process-check \
  --project /path/to/project --run-id <run_id>
python3 "$entry" --codex-home "$codex_home" process-cleanup \
  --project /path/to/project --run-id <run_id> [--retain <entry_id>,<entry_id>]
~~~

`process-init` prints the `run_id`. `process-run` is a foreground wrapper with
a default 600-second timeout; it records the real process identity and purpose
before starting, then flushes a startup event to stderr. It reclaims the command
when it exits or times out and does not create a resident service. When the
command completes, stdout contains one JSON object. `process-run` is
non-interactive: stdin is closed, combined stdout and stderr capture is capped
at 2 MiB, and `--timeout` accepts at most 86400 seconds. Exceeding the output
cap fails the command and can leave logs incomplete. Use `--summary` for long
tests and other high-output commands, with an explicit `--timeout` within the
task's authorized duration. The summary returns status, exit result, registered
run/entry identifiers and PID, captured byte counts, and private log paths,
without stdout/stderr text. Without `--summary`, the original completion JSON
with output text remains compatible. Captured logs stay in the private run
directory; inspect only the diagnostic portions needed. If `output_complete`
is false, treat the logs as partial evidence and do not call them complete.

The command or a native wait performs ongoing monitoring. Do not reread an
unchanged log or repeat `process-check` without a state change; a targeted
check is appropriate for a clear exception, process identity doubt, or lost
supervision. The root still performs its independent final `process-check` and
cleanup sequence. A native wait timeout means only that the wait interval
ended, not that the work failed. Do not turn an interim user update into
another status read. The execution owner reports the startup `entry_id` and
PID once, then delivers results at completion or reports a clear exception,
threshold, or pending decision. If a task explicitly authorizes a retained
service, report it as `RETAINED` with its purpose; a run with retained entries
is not `CLEAN`.

After every participant has stopped starting commands, the root performs the
independent final `process-check` and `process-cleanup`, then runs a second
`process-check` to confirm the post-cleanup state. Cleanup closes the run and
rejects new commands, so a child must not clean up a shared run. A retained
entry may be checked or cleaned again later in the same run, but that does not
permit a new command. If a child needs to stop its own entry early, stop its
execution wrapper or report it to the root; do not teach it to kill arbitrary
PIDs. This ledger is scoped to the work package. It does not scan every process
on the machine and is not a host-enforced prompt policy.

Pass the same `--retain` list to the final check when keeping a service.
`CLEAN` confirms that registered processes have stopped; it does not prove the
command or work package succeeded. `DIRTY` means a registered process remains
active, and `UNVERIFIED` means cleanup could not be confirmed. Check and cleanup
return a nonzero exit code for either of those states. An explicitly retained
service remains subject to its original command timeout.

On Windows PowerShell, use the same Codex home and replace `python3` with
`py -3`:

~~~powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$Entry = Join-Path $CodexHome "codex-adaptive-agents\runtime\codex-adaptive-agents.py"
py -3 $Entry --codex-home $CodexHome process-init --project C:\path\to\project
py -3 $Entry --codex-home $CodexHome process-run --project C:\path\to\project --run-id <run_id> --owner <owner> --purpose "long test" --summary --timeout 3600 -- py -3 -m unittest
py -3 $Entry --codex-home $CodexHome process-check --project C:\path\to\project --run-id <run_id>
py -3 $Entry --codex-home $CodexHome process-cleanup --project C:\path\to\project --run-id <run_id> [--retain <entry_id>,<entry_id>]
~~~

## Live verification

verify --live is explicit, uses real Codex allowance, and starts at most two
direct native Luna children with the selected role. It is bounded at 600
seconds. By default it sends no root model or effort override and does not
gate on root identity. Optional `--root-model` and `--root-effort` values are
explicit request overrides and must exactly match the public effective values
before a turn starts. Missing or null public root fields remain unknown; no
plugin effort allowlist applies.

~~~sh
python3 codex-adaptive-agents.py verify --live \
  --project /path/to/smoke-project \
  --output /path/to/verification-output
~~~

The CLI uses the official client's own defaults; it does not read a separate
desktop Codex task's active model.

The command creates a temporary official stdio app-server test channel. That
channel exists only for the check; it is not an installed MCP server, daemon,
Hook, scheduler, or resident process. Live output keeps requested,
client-effective, and server-reported model metadata separate when the official
client exposes those values. The independent verifier checks child roles,
scope, hashes, and smoke contracts; a successful process exit alone does not
prove the task result.

## Process cleanup boundary

The transient client channel is bounded. Windows assigns descendants to a Job
Object. Linux uses a short-lived Python subreaper per command so detached
orphans are still cleaned up. macOS tracks process birth and original-parent
identifiers and cleans up observed descendants even after `setsid` or a parent
exit. A separate cleanup acknowledgement is required before success is reported;
a timeout or supervision failure is not a successful verification.

The original-parent-version field is used only after a live parent/child
capability check. Older macOS versions fall back to birth identities and the
command process group; a detached child whose parent exits before observation
may be missed. On every macOS version, a short-lived intermediary that forks
and is reaped between snapshots can hide deeper lineage. This is not
kernel-enforced containment.
Use the official Codex sandbox for untrusted task code; this process helper is
not a replacement sandbox. It is created only for a command and exits with it,
not an installed service. Current platform-specific test evidence is recorded
in [VALIDATION.md](VALIDATION.md).

## Recovery and uninstall

Use the transaction commands with the same Codex home:

~~~sh
python3 codex-adaptive-agents.py uninstall --dry-run
python3 codex-adaptive-agents.py uninstall --yes
python3 codex-adaptive-agents.py recover --yes
python3 codex-adaptive-agents.py rollback --backup PATH --yes
~~~

`--dry-run` is accepted only by `install` and `uninstall`. Other commands reject
it before execution; it cannot be used to preview `refresh` or `verify --live`.

`recover --yes` restores only an interrupted journal marked `applying`. It
refuses a committed journal and does not accept `--backup`. To reverse a
completed transaction, `rollback --backup PATH --yes` requires its recorded
backup path. Both operations refuse to overwrite an unrelated user change.

Uninstall removes files and empty configuration tables created by the installer
only when their creation was recorded and they contain no later user additions.
Backup history is preserved.

## Package inspection

The checked-in release manifest is the exact allowlist. Build a private
package with:

~~~sh
python3 scripts/release_python.py --output-dir /tmp/codex-adaptive-agents-release
~~~

The in-package SHA256SUMS covers each regular payload file except itself. The
external checksum file covers each archive produced in that invocation. The
builder rejects source symlinks, unsafe archive members, private Markdown
paths, broken local Markdown links, and overwriting an existing archive. Source
files are opened relative to no-follow directory handles where the platform
supports them, then read through the same descriptor whose `fstat` identity was
checked. Archive and checksum outputs use exclusive creation and verify the
created file identity; a raced destination symlink or pre-existing destination
is rejected without writing through it. Newly created files are visible while
being written, so a process interruption can leave a partial file that must be
removed manually before retrying.

Windows Python's standard library does not expose the same directory-relative
open operations. There, the builder rejects reparse points and checks the
opened file identity and parent directory chain before and after path-based
access. This catches file and persistent parent-link substitutions, but cannot
prove safety against a same-user actor that swaps a parent directory away and
back during the path-based operation. POSIX directory handles prevent raced
symlink redirection; concurrent renames of an already-open directory remain an
OS namespace race outside this check.

Source tests and package inspection are offline checks. They do not establish
model access, current quota, live delegation, or publication. See LICENSE,
NOTICE, UPSTREAM.md, and VALIDATION.md for the supporting public material.
