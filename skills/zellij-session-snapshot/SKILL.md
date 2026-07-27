---
name: zellij-session-snapshot
description: >
  Snapshot a zellij session's tabs together with the Claude Code (cc) session
  running in each tab, then restore every tab AND resume its exact Claude
  conversation after a reboot/logout with one command. Use this WHENEVER the
  user wants to save/record/back up a zellij session before rebooting or logging
  out, worries about losing their tabs or their running Claude conversations,
  asks to "记录 session / 保存 tab / 一键拉起 / 重启后恢复 claude / restore my
  zellij tabs / bring back my claude sessions", or has many tabs each running a
  Claude session they don't want to re-find by hand. Also use to inspect which
  Claude session id is currently active in each tab.
---

# Zellij session snapshot (+ Claude resume)

Zellij's own resurrection restores tab layout and re-runs each pane's command —
but it re-runs `claude` fresh, losing which conversation each tab was on. This
skill closes that gap: it records the **current** Claude session id for every
tab and regenerates a layout that resumes each one.

**Runs on macOS and Windows.** The pane→session mechanism is identical on both;
only the OS introspection differs — macOS uses Darwin's libproc / `KERN_PROCARGS2`,
Windows uses the Win32 process APIs (`ReadProcessMemory` over the PEB,
`GetProcessTimes`, CIM) plus a `conwrap.ps1` console shim. A single
`scripts/snapshot.py` dispatches to the right implementation by platform
(`snapshot_windows.py` on Windows). No setup, no extra dependencies beyond
python3 + zellij + claude (+ PowerShell, already present on Windows).

Everything is driven by `scripts/snapshot.py`. Run its subcommands directly.

## Commands

```bash
python3 ~/.claude/skills/zellij-session-snapshot/scripts/snapshot.py save    [--session <name>]
python3 ~/.claude/skills/zellij-session-snapshot/scripts/snapshot.py restore --session <name>
python3 ~/.claude/skills/zellij-session-snapshot/scripts/snapshot.py show    --session <name>
```

- **save** — snapshot a session (name from `$ZELLIJ_SESSION_NAME`, or pass
  `--session <name>` to save any live session from anywhere). Writes a
  **minimal** restorable layout + manifest, and installs the layout as a **named
  layout** in the zellij config dir (`~/.config/zellij/layouts/<name>.kdl` on
  macOS, `%APPDATA%\Zellij\config\layouts\<name>.kdl` on Windows).
- **restore** — **prints a doctor + the exact commands to run by hand; it does
  NOT spawn anything** (see *Restore is manual* below for why). It reports the
  layout path, the tabs and which will resume, whether your current shell is a
  safe launch context (not inside zellij / not a cc pane), and whether a stale
  same-name session needs deleting — then prints the `zellij --session <name>
  --new-session-with-layout <name>` line to run in a fresh terminal window.
- **spawn** *(Windows only)* — create the session **detached via WMI**
  (`Win32_Process.Create`), then just `zellij attach <name>`. This is the
  restore path **when you SSH into the Windows box** — see *Windows over SSH*
  below. It is safe by construction: the WMI-created server gets the user's
  clean default environment (no `SSH_*`, no `CLAUDE_CODE_CHILD_SESSION`), so
  the child-session trap that got the old auto-spawn removed cannot occur.
  (On macOS `spawn` remains deprecated and just redirects to the `restore`
  doctor.)
- **show** — print the saved manifest (tab, cwd, session id, source, args).

Output lives in `~/.claude/zellij-snapshots/<name>/` (`restore-layout.kdl` +
timestamped history + `manifest.json`) and the named-layout path above.

## How each tab's Claude session is resolved

`save` asks the OS directly — no side channel, no hook, no setup:

1. **`zellij action list-panes -aj`** (JSON) gives the structure: for every
   pane its `tab_name`, `tab_position`, `pane_cwd`, and stable `id`. The pane's
   `pane_command` is **never** used for identity — zellij can report the current
   *foreground child* there (an MCP server, `caffeinate`, …), not the launch
   command.
2. **`~/.claude/sessions/<pid>.json`** — Claude Code's per-process runtime
   files, enumerated directly (not via `pgrep`, which has been observed to miss
   live claudes). A record's `sessionId` is **not** authoritative on its own —
   it is written at startup and is **not** rewritten when the conversation moves
   on, so every id is confirmed against a transcript on disk before it is
   snapshotted. macOS additionally reads `kind=bg` records (daemon-hosted
   sessions, see below); Windows currently reads only `kind=interactive`.
3. Each pid is **identity-validated**: alive, kernel start time matches the
   file's start time within a tolerance (defeats pid reuse), the image is
   `claude`, and the process is a **descendant of this session's
   `zellij --server`** (rejects orphans from a dead same-name session; restored
   panes chain claude → shell wrapper → server, so this is an ancestor walk).
   - **macOS**: `proc_pidinfo` start-epoch vs the file's `procStart`
     (fixed-format UTC parse); ppid walk via `proc_pidinfo`; the image is
     `argv[0]`'s **leading token** — claude rewrites argv[0] into a process
     title for helper processes (`claude bg-spare`), one argv element with a
     space in it.
   - **Windows**: `GetProcessTimes` vs the file's `startedAt` (unix-ms;
     `procStart` there is .NET ticks rendered in a machine-local timezone, so it
     is ambiguous and unused); ancestor walk over the CIM `ParentProcessId`
     snapshot; image checked as `claude.exe`.
4. The process's **exact environment** is read — macOS via `sysctl
   KERN_PROCARGS2`, Windows via `ReadProcessMemory` over the PEB
   (`RTL_USER_PROCESS_PARAMETERS.Environment`). The env's `ZELLIJ_SESSION_NAME` +
   `ZELLIJ_PANE_ID` are the join key: **two tabs in the same cwd stay distinct**
   because the join is on pane id, not cwd. Launch flags come from the process's
   argv (`KERN_PROCARGS2` / `CommandLineToArgvW`).

Per-pane outcome:

- **`live`** — a validated claude process owns the pane; its current sessionId
  is recorded. The restore cwd is chosen by probing candidate cwds (the
  process's cwd, then the pane's cwd) for the one whose Claude project dir
  actually holds the transcript — so a session created in `repo/X` then `cd`'d
  into a **worktree** still restores in `repo/X` where `--resume` can find it.
- **`live (daemon-hosted)`** (macOS only) — the pane's conversation is *parked*
  into a daemon-spawned background process (`claude bg-spare`, runtime
  `kind=bg`) and the pane is only its client. The pane process then still
  advertises the `sessionId` it started with, which typically has no transcript
  at all — so a `kind=bg` session that joins on the same `ZELLIJ_PANE_ID`
  (inherited through the daemon) **and** has a transcript on disk wins over it.
  Flags are still replayed from the **pane's** argv, never the bg process's
  (`bg-spare …`). Two such sessions on one pane → identity error, whole save
  aborts. Windows has no equivalent yet: such a tab snapshots as `x failed`.
- **`x failed`** — no live claude owns the pane (exited claude; a brand-new
  still-empty session; or a **non-persisting child session** that never wrote a
  runtime file — see the trap below). The tab restores as a fresh claude. If the
  pane's command string carries a `--resume <id>`, that id is recorded as a
  **stale hint** and printed as a manual `claude --resume <id>` command — it is
  **never auto-restored**, because it may predate a `/clear`.
- A plain shell / non-claude pane is skipped entirely.

**Flag replay is allowlisted.** Only flags safe to re-apply to a resumed session
are replayed (`--dangerously-skip-permissions`, `--model`, `--add-dir`,
`--permission-mode`). Session-selection flags are dropped — e.g. replaying
`--fork-session` next to the new `--resume <id>` would fork yet another session
instead of continuing it. Anything unrecognized is recorded in the manifest as
`unreplayed_flags` and reported loudly, never passed through silently.

**Failure model (fail closed).** Identity infrastructure errors — unreadable
runtime JSON, unreadable argv/env of a live claude pid, an ambiguous `zellij
--server` match, two claudes claiming one pane — **abort the whole save**
without writing anything. And if 0 resumable tabs resolve, save refuses to
overwrite the existing good snapshot. All snapshot files are written atomically
(tempfile + rename).

## Why a named layout, not `zellij attach`

After a reboot the zellij **server** is gone, and with it the live session.
Zellij's own resurrection re-runs each pane's *original* command — a fresh
`claude` with no `--resume` — and it re-serializes the session from live process
state, overwriting anything you try to inject into its cache. Fighting that
cache is futile.

So the restore path uses a file zellij only ever **reads**: a **named layout**
in the config dir (never overwritten by serialization). `save` installs it. It's
a **minimal** layout — one tab per entry, each a single pane running claude
(under a `zsh`/conwrap wrapper so exiting claude drops into a normal shell) with
cwd + `--resume` args, `start_suspended true`, and **no** captured
`swap_tiled_layout`/pane sizes. Omitting sizes is what makes zellij lay panes out
for the **current** terminal (a full `dump-layout` bakes in old geometry and
restores at the wrong size). It **does** include a `default_tab_template` with
the stock `zellij:tab-bar`/`zellij:status-bar` plugins — `--new-session-with-layout`
uses this layout verbatim and ignores the user's default layout, so without the
template the restored session comes up with **no tab bar**: all tabs exist but
are invisible and un-switchable. The plugins carry no geometry, so terminal-fit
still holds. Tab names, cwds and args are shell/KDL-escaped (Windows backslashes
are doubled, or zellij rejects the layout).

## conwrap.ps1 — why claude is launched through a wrapper (Windows only)

On Windows each pane launches claude through `scripts/conwrap.ps1` instead of
directly. zellij-on-Windows command panes hand the child PIPE std handles even
though a ConPTY console is attached (and is the only thing the pane renders), so
a directly-launched claude sees no TTY, drops into headless mode, and `--resume`
exits immediately with "Provide a prompt to continue the conversation". The
wrapper reopens `CONIN$`/`CONOUT$` read-write (a `< CON > CON` redirect is not
enough — `GetConsoleMode` needs read access) and hands them to claude via
`STARTF_USESTDHANDLES`, restoring interactive mode. It also (a) sets
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` and clears any inherited
`CLAUDE_CODE_CHILD_SESSION` before launching claude — the persistence fallback
below — and (b) drops into an interactive shell after claude exits, so the pane
stays usable instead of dying. (macOS achieves the equivalent with a plain
`zsh -c '… ; exec zsh'` wrapper in the layout.)

## Restore is manual — and why (the child-session persistence trap)

Restore does **not** launch zellij for you, on purpose. When a zellij **server**
is started from inside a claude pane, it inherits that pane's
`CLAUDE_CODE_CHILD_SESSION` marker in its environment. Every pane the server
forks then inherits it too, so each resumed `claude` decides it is a *child
session* and **turns transcript saving off** — the conversation lives only in
process memory, no runtime file is written, and it **evaporates on the next
reboot / `delete-session`**. This is observable: such panes have no
`~/.claude/sessions/<pid>.json`, their `.jsonl` stops growing, and `/status`
shows `⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker`.

The robust fix is to **launch the restore from a fresh terminal window** — a new
Terminal.app / iTerm window on macOS, a new Windows Terminal / PowerShell window
on Windows — whose ancestor is the shell, not a cc pane, so the environment is
clean and the marker is absent. That designs the failure away. As a second line
of defence the layout forces persistence per-pane (`conwrap.ps1` on Windows, a
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` prefix on macOS). `restore` therefore
just prints a **doctor** (launch-context health check + the exact commands)
instead of spawning anything.

## Windows over SSH: use `spawn` (the SSH-disconnect trap)

If you SSH into the Windows box, the fresh-window advice above is
**unattainable**: every window you can open is still a descendant of the SSH
connection, and Windows tears that whole process tree down when the connection
drops — zellij has no Unix-style daemonize escape on Windows, so the server
(and every claude in it) dies with the disconnect.

`spawn` fixes this by creating the session **detached via WMI**
(`Win32_Process.Create`): the server is parented to the WMI provider service —
outside every SSH job and console, unreachable by the disconnect teardown —
and gets the user's clean **default** environment: correct `TEMP`/`APPDATA`
(so the socket and named layout resolve for your SSH shells), and no `SSH_*` /
`CLAUDE_CODE_CHILD_SESSION` (so the persistence trap above cannot occur — the
env is clean by construction, not by cleanup). The SSH window then only ever
runs a disposable `zellij attach`:

```
python3 ~/.claude/skills/zellij-session-snapshot/scripts/snapshot.py spawn --session work
zellij attach work
# ... SSH drops ... reconnect, then simply:
zellij attach work
```

The `restore` doctor detects an SSH context (`SSH_CONNECTION`/`SSH_CLIENT`/
`SSH_TTY`) and prints this flow instead of the fresh-window one. After the
spawn, `spawn` verifies the new server's env is actually clean and warns
loudly if not.

**Stay in one login session.** A WMI-created server lands in the login session
of the WMI provider (**session 0** — the same session an SSH login runs in), so
from SSH you can see and manage it normally. But a process in **another** login
session — the local desktop is session 1 — or at a different elevation is
opaque to CIM: its command line reads back empty, so `session_state` cannot
tell whether it is a stale same-name server. `restore` and `spawn` therefore
**warn** when any such uninspectable `zellij.exe` exists (`tasklist /FI
"IMAGENAME eq zellij.exe"` to inspect it from the owning context) — they can't
kill or attribute it for you across the boundary. Practical rule: manage a
given session from the same place you spawned it (spawn over SSH → attach over
SSH; kill a stale one from a shell in its own login session, or an elevated
one).

## The intended flow

```
# 1) In ANY cc pane inside the session you want to keep:
/zellij-session-snapshot save

# 2) reboot / log out ...

# 3) Open a BRAND-NEW terminal window (Terminal.app / iTerm on macOS, Windows
#    Terminal / pwsh on Windows — NOT inside zellij, NOT a cc pane) and get the
#    commands:
python3 ~/.claude/skills/zellij-session-snapshot/scripts/snapshot.py restore --session work

# 4) Run the printed commands in that fresh terminal, e.g.:
zellij --session work --new-session-with-layout work
#    (prefix with `zellij delete-session work --force` if the doctor flags a
#    stale same-name session)
```

Panes come up **suspended**: switch to a tab, press Enter to wake its
`claude --resume` (deliberate — avoids launching many claudes at once, and lets
each start cleanly).

Re-run `save` whenever the tabs or their conversations changed — it re-installs
the named layout and keeps timestamped history.

## Limitations to be honest about

- **OS internals, not public contracts.** Identity validation relies on process
  introspection that is not a stable public API — macOS `KERN_PROCARGS2`
  (`__APPLE_API_UNSTABLE`) + `proc_pidinfo`; Windows x64 PEB offsets (stable for
  Win10/11) + CIM — and on `~/.claude/sessions/*.json`, a Claude Code internal.
  Everything fails **closed**: if any of it breaks, save aborts loudly instead of
  writing a wrong snapshot. Validated on claude 2.1.217 / zellij 0.44.3 (macOS 15,
  Windows 11 x64).
- A tab whose claude is a **non-persisting child session** (see the trap above)
  has no runtime file, so it snapshots as `x failed` with at most a stale
  manual-resume hint. Restoring from a fresh terminal keeps future sessions
  healthy so this doesn't recur.
- A **brand-new, still-empty** session has no transcript on disk yet and can't
  be resumed — it snapshots as `x failed` until it has real content.
- A tab whose claude has **exited** snapshots as `x failed`, with at most a
  stale manual-resume hint.
- Flags outside the replay allowlist (notably `--worktree`) are **not**
  re-applied on restore (listed per-tab at save time as `NOT replayed`).
- **Windows: use a native `claude.exe`.** The restored pane launches claude via
  `CreateProcessW`, which cannot execute a batch-file shim — so an npm global
  install (`claude.cmd` on `PATH`) won't start in a restored pane. Install the
  native Windows binary (`claude.exe`) instead.
- **Windows: run `save` at the same elevation as your claudes.** A claude
  started from an elevated terminal can't be introspected from a non-elevated
  one (UIPI), so that tab snapshots as `x failed` (with a stale hint) rather
  than resuming.
- Plain `zellij attach <name>` alone is **not** a restore path (see above); use
  the restore doctor's printed commands to bootstrap after a reboot.
