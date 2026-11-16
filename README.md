# Reference hooks for Claude Code

This repository contains five command hooks, their offline regression tests, two agent examples, and two rule examples.
The hooks use Python's standard library.
The tests require Python 3.9 or later and Git.
The settings file is an installation example.
The automated verification exercises hook payloads through subprocesses.
It does not verify an installed Claude Code session.

## What each hook checks

### compaction-reread-gate.py

A context summary can omit instructions that still apply to the task.
This hook pauses ordinary tool calls after a new parent transcript compaction boundary.
It permits reads of configured reference documents until their recorded coverage is complete.
It also permits configured feed reads, configured directory listings, tool discovery, and notifications.
A worker event cannot reset the parent's progress.
Repeated delivery of the same boundary does not reset progress.
Workspaces outside the configured roster are unaffected.
Missing documents are dropped from the roster and logged.
A roster with no existing documents does not arm.

The default release follows the last whole read.
An optional second phase requires a written check under `docs/session/`.
That block must name the compaction boundary, give each document its own applied or drifted line with an evidence pointer, and identify work since the read.
This phase checks the block's structure.
It cannot establish whether the reasoning is correct.
It releases before the writing tool runs, so a failed write can still release it.

### commit-red-witness.py

A passing test after a change does not establish that the test caught the original defect.
This hook checks commits that include files under `src/` or `tests/`.
It considers staged files and files that a visible add or commit all command could stage.
A Claude CLI subprocess looks for a failing test before the source change in the session or worker transcripts.
A boolean negative answer blocks the commit.
A boolean positive answer allows it.
Verdict words in the commit subject produce an advisory note.
The subject note does not block a commit.

The client uses the `opus` model alias with a 25 turn limit and a 240 second timeout.
It receives a restricted tool list and a request to inspect the evidence.
The permitted Python interpreter is not an operating system sandbox.
Installing this hook can consume model usage and transmit relevant context through the configured Claude client.
The self-test always uses a local canned stub.

### kill-witness-gate.py

A process may be working even when one observation looks idle.
This hook refuses recognized kills by name or filter.
A recognized literal PID kill requires two recorded samples at least 60 seconds apart.
The CPU totals must differ by less than 0.5 seconds.
Every literal PID in a recognized command needs a witness.
Processes started by the session receive the same check.
The hook inspects text and never executes the kill command.

### pr-text-gate.py

A pull request can expose internal review notes or ignore the repository's contribution instructions.
This hook checks visible titles and bodies of `gh pr create` and `gh pr edit` commands.
It refuses model identifiers and configured review vocabulary.
It also requires whole reads of existing `CONTRIBUTING.md` and `.github/pull_request_template.md` files.
Read evidence belongs to one session and caller.
The evidence is tied to the file's current hash with line endings normalized.
A change to the file requires another read.
The same bytes in a different checkout count for the same caller.

The default vocabulary includes review log paths, numbered seats and rounds, review verdict markers, and model review requests.
The records namespace defaults to `review-records`.
Set `PR_TEXT_RECORDS_PREFIX` to change that namespace.
The vocabulary is a policy example and lives in the three pattern declarations near the start of the hook.

### fold-cap-gate.py

Repeated narrow fixes can leave neighboring cases unchecked.
This hook checks commits whose subject starts with `fix(` or `fix:`.
After a prior fix on the branch, the next fix needs a `Walk: <path>` line in its message.
The named file must exist and its basename must start with `WALK-`.
Its modification time must be later than the latest prior fix commit.
Prior fixes come from `origin/main..HEAD`.
The first fix is allowed.
An unavailable `origin/main` causes the check to fail open.
The hook can read an inline message or a message file.
It checks the review file's existence and timestamp, not its contents.

## Install

Keep this checkout at a stable local path.
Run the self-tests before wiring the hooks.
Make a copy of `settings.example.json` outside this repository.
Replace every `__HOOKS_DIR__` with the absolute path of this checkout's `hooks` directory.
Use forward slashes in that JSON path, including on Windows.
Use an absolute Python executable path if `python` is not available to the hook process.
Merge the `hooks` and `env` entries into your Claude Code settings.
Preserve existing entries when merging.
The user settings location is `~/.claude/settings.json`.
The project settings location is `.claude/settings.json`.
Restart the client after editing settings and inspect the hook logs during a disposable session.

The shell matchers cover `Bash`, `PowerShell`, and MCP tools ending in `__shell`.
The kill matcher also covers MCP tools ending in `__process_kill`.
The read matchers cover `Read` and MCP tools ending in `__read` or `__read_lines`.
A different tool name or response schema needs an adapter and a matching test.
For MCP readers, the expected response fields are visible in the self-tests.
The compaction hook is registered on every tool because its read phase must block unrelated calls.

Copy the two agent files into `~/.claude/agents/` or `.claude/agents/` to use them.
Copy the rule files into the corresponding `rules/` directory if you want those instructions.
Both agent definitions omit the native Agent and Workflow tools.
The worker also forbids starting children through a shell.
The closeout agent's read only behavior is an instruction, not a shell sandbox.
The example settings include the spawn depth environment setting used by the source setup.
Check support for that setting in your installed client.
The tool allowlist remains the explicit agent boundary.

## Configure compaction reads

Copy `examples/compaction-rosters.json` to `~/.claude/compaction-rosters.json` and edit its paths.
Alternatively set `COMPACTION_GATE_CONFIG` to another configuration file.
Without a configuration file, the hook gates no workspace.
Each key under `rosters` names one workspace policy.
The first matching policy wins.

| Field | Meaning | Default when omitted |
| --- | --- | --- |
| `roots` | Workspace paths including their descendants | Empty list |
| `docs` | Files that must be read whole | Empty list |
| `feeds` | Paths whose descendants may be read in any range | Empty list |
| `lists` | Paths whose descendants may be listed | Empty list |
| `source_roots` | Parent directories holding named checkouts | Empty list |
| `source_names` | Checkout names, also matching a dash or underscore suffix | Empty list |
| `source_parents` | Directory segment names that may contain named checkouts | Empty list |
| `drive_prefixes` | Prefixes for checkout names at a Windows drive root | Empty list |
| `label` | Text describing the document set | Configured reference documents |
| `canon` | Text naming the controlling instructions | The configured workspace rules |
| `after` | Reminder shown after the reads | Check the work and fix drift |

Paths expand the home directory and environment variables.
Relative paths resolve from the configuration file's folder.
Set `HOOK_GATE_CHECK_ROSTERS` to a comma separated list of roster keys to enable the written check phase.
Its default is empty.
Set `CHECK_RECORD_SUBDIR` to change the permitted record directory segment.
Its default is `docs/session`.
The marker expires after three hours.
A refusal names the marker file that can be removed to release a stuck gate.
The separate boundary receipt remains so the same event cannot arm the gate again.

## Record a process sample

The supplied `stall-sample.ps1` helper reads a Windows process and its current descendants.
It writes one JSON line to the witness directory.
It never stops a process.
Run it twice with at least 60 seconds between samples.
Use the actual target PID in place of 1234.

```powershell
pwsh -NoProfile -File ./hooks/stall-sample.ps1 -Pid 1234
```

Set `KILL_WITNESS_DIR` in both the sampling shell and the hook environment if you change the witness directory.
Set `KILL_SAMPLE_HELPER` to change the helper path printed in refusals.
The default helper path is beside the hook.
On other operating systems, supply a collector that appends the following shape to `<PID>.jsonl` in that directory.
The collector must record real observations.

```json
{"at":"2000-01-01T00:00:00Z","pid":1234,"cpu_total_s":5.0}
```

The hook accepts numeric epoch seconds or ISO timestamps for `at`.
It ignores additional fields.
It checks any qualifying pair in the file.
It does not enforce sample recency or compare process start times.
Remove a stale witness file by its exact path before sampling a reused PID.
A changing process tree can make aggregate CPU samples misleading.
The Windows helper records a null CPU value if a member cannot be read.
A null value does not count as a sample.

## Environment and state

Set hook environment variables in the client environment or in the settings `env` object.
A variable assignment inside the proposed shell command does not configure the hook that inspects it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMPACTION_GATE_STATE_DIR` | Temporary directory plus `claude-compaction-reread` | Markers, boundary receipts, and log |
| `COMMIT_RED_WITNESS_STATE_DIR` | Temporary directory plus `claude-commit-red-witness` | Commit decisions |
| `KILL_WITNESS_DIR` | Temporary directory plus `claude-kill-witness` | PID samples and gate log |
| `PR_TEXT_GATE_STATE_DIR` | Temporary directory plus `claude-pr-text-gate` | Rule read evidence and decisions |
| `FOLD_CAP_STATE_DIR` | Temporary directory plus `claude-fold-cap` | Surface review decisions |
| `HOOK_CLAUDE_BIN` | `claude` on PATH | Witness client executable or Python stub |
| `HOOK_RED_WITNESS_TIMEOUT_S` | `240` | Witness client timeout |
| `HOOK_RED_WITNESS_SKIP` | Unset | Set to `1` to skip the commit hook |
| `HOOK_PR_TEXT_SKIP` | Unset | Set to `1` to skip both PR events |
| `HOOK_FOLD_CAP_SKIP` | Unset | Set to `1` to skip the fold hook |
| `HOOK_COMPACTION_AUDITOR` | Unset | Set to `1` to skip commit, PR, and fold hooks in the witness client |

The last variable is a recursion guard.
It does not disable the compaction or kill hook.
The commit hook sets it for its own client subprocess.
Decision logs can contain command text and local paths.
Keep runtime state outside a public repository.

## Run the self-tests

Run these commands from the checkout root.
No test uses a real model client or runs a pull request command.
The kill tests do not kill target processes.
The commit timeout test terminates its own sleeping stub.
Git fixtures use local repositories and a local reference named `origin/main`.
They create no remote and contact no server.
The compaction test generates its own reference files and configuration.

```text
python -B hooks/compaction-reread-gate.selftest.py
python -B hooks/commit-red-witness.selftest.py
python -B hooks/kill-witness-gate.selftest.py
python -B hooks/pr-text-gate.selftest.py
python -B hooks/fold-cap-gate.selftest.py
```

Exit code zero means that the suite's assertions passed.
The tests retain their scratch directories for inspection.
They print the retained locations.
Set `TEMP` and `TMP` before starting Python to control those locations on Windows.
Set `TMPDIR` on systems that use it.
The commit and kill tests also accept `HOOK_SELFTEST_TMP`.
Delete individual scratch files by exact path after inspecting them.
No test deletes a directory tree.

For a mutation test, point the self-test at a separate hook copy.
The commit and kill tests take `--script <path>`.
The other three take the hook path as a positional argument.
A refusal changed to success must make the relevant refusal assertion fail.
Restoring the original copy must make it pass.

## Limits

These hooks are workflow checks.
They are not a security boundary.
Errors generally allow the operation and are recorded when logging is possible.
The command parsers recognize a bounded set of shell forms.
They do not inspect scripts invoked by a command.
The kill parser allows variable PIDs and shell job identifiers.
Corrupt witness files also allow the kill.
The commit hook inspects the first recognized commit for its model check.
An absent transcript, a timeout, or an unusable model answer allows that commit.

The PR hook allows unseen variables and body text supplied through stdin.
It scans at most 65,536 characters of each visible field.
Its repository read check uses the command's local working directory even when a remote repository flag is present.
Files over eight MiB are outside that read check.
A read still counts after context compaction.
The read witnesses trust tool metadata and the file as it exists when the hook runs.
The compaction hook retains legacy whole read heuristics for older response shapes.
Paths are compared without case distinctions.
The fold hook relies on file modification times and does not require the cited file to be tracked.

MIT License. See `LICENSE`.
