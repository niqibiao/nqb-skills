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

**macOS-only** (pane→session identity uses Darwin's libproc / `KERN_PROCARGS2`).
No setup, no extra dependencies: python3 + zellij + claude.

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
  layout** in `~/.config/zellij/layouts/<name>.kdl`.
- **restore** — **prints a doctor + the exact commands to run by hand; it does
  NOT spawn anything** (see *Restore is manual* below for why). It reports the
  layout path, the tabs and which will resume, whether your current shell is a
  safe launch context (not inside zellij / not a cc pane), and whether a stale
  same-name session needs deleting — then prints the `zellij --session <name>
  --new-session-with-layout <name>` line to run in a fresh terminal. (zellij
  ≥ 0.44 changed `--session --layout` to mean "append tabs to an EXISTING
  session", which silently no-ops when it doesn't exist — hence `-n`.)
- **show** — print the saved manifest (tab, cwd, session id, source, args).

  (`spawn` still exists but is deprecated — it now just redirects to the
  `restore` doctor, since background-spawning was the very thing that produced
  non-persisting sessions.)

Output lives in `~/.claude/zellij-snapshots/<name>/` (`restore-layout.kdl` +
timestamped history + `manifest.json`) and `~/.config/zellij/layouts/<name>.kdl`.

## How each tab's Claude session is resolved

`save` asks the OS directly — no side channel, no hook, no setup:

1. **`zellij action list-panes -aj`** (JSON) gives the structure: for every
   pane its `tab_name`, `tab_position`, `pane_cwd`, and stable `id`. The
   pane's `pane_command` is **never** used for identity — zellij reports the
   current *foreground child* there (an MCP server, `caffeinate`, …), not the
   launch command; in a live 7-pane session 4 panes were mis-reported.
2. **`~/.claude/sessions/<pid>.json`** — Claude Code's per-process runtime
   files, enumerated directly (not via `pgrep`, which has been observed to
   miss live claudes). Each records the process's **current** `sessionId` —
   correct even after `/clear` and for launches **without** `--resume`.
3. Each pid is **identity-validated**: alive, kernel start time
   (`proc_pidinfo`) matches the file's `procStart` (fixed-format UTC parse)
   within 2s (defeats pid reuse), `argv[0]` is `claude`, and the process is a
   **descendant of this session's `zellij --server`** (rejects orphans from a
   dead same-name session; restored panes chain claude → zsh wrapper →
   server, so this is an ancestor walk, not a bare ppid check).
4. The process's **exact argv + env** are read via `sysctl KERN_PROCARGS2`
   (NUL boundaries — no `ps` string-splitting). The env's
   `ZELLIJ_SESSION_NAME` + `ZELLIJ_PANE_ID` are the join key: two tabs in the
   **same cwd** stay distinct.

Per-pane outcome:

- **`live ✓`** — a validated claude process owns the pane; its current
  sessionId is recorded. The restore cwd is chosen by probing candidate cwds
  (the process's cwd, then the pane's cwd) for the one whose Claude project
  dir actually holds the transcript — so a session created in `repo/X` then
  `cd`'d into a **worktree** still restores in `repo/X` where `--resume` can
  find it.
- **`✗ failed`** — no live claude owns the pane (exited claude, or a
  brand-new still-empty session with no transcript on disk). The tab restores
  as a fresh claude. If the pane's command string carries a `--resume <id>`,
  that id is recorded as a **stale hint** and printed as a manual
  `claude --resume <id>` command — it is **never auto-restored**, because it
  may predate a `/clear` (observed in practice).
- A plain shell / non-claude pane is skipped entirely.

**Flag replay is allowlisted.** Only flags that are safe to re-apply to a
resumed session are replayed (`--dangerously-skip-permissions`, `--model`,
`--add-dir`, `--permission-mode`). Session-selection flags are dropped —
e.g. replaying `--fork-session` next to the new `--resume <id>` would fork yet
another session instead of continuing it. Anything unrecognized is recorded in
the manifest as `unreplayed_flags` and reported loudly, never passed through.

**Failure model (fail closed).** Identity infrastructure errors — unreadable
runtime JSON, unreadable argv/env of a live pid, an ambiguous `zellij
--server` match, two claudes claiming one pane — **abort the whole save**
without writing anything. And if 0 resumable tabs resolve, save refuses to
overwrite the existing good snapshot. All snapshot files are written
atomically (tempfile + rename).

## Why a named layout, not `zellij attach`

After a reboot the zellij **server** is gone, and with it the live session.
Zellij's own resurrection (`attach` on a dead session) re-runs each pane's
*original* command — a fresh `claude` with no `--resume`. Its cache layouts
(`~/.cache/zellij/*/session_info/<name>/session-layout.kdl`) are overwritten
every few seconds by the running server, so editing those is futile.

So the restore path uses a file zellij only ever **reads**: a **named layout**
in `~/.config/zellij/layouts/<name>.kdl` (config, never overwritten). `save`
installs it. It's a **minimal** layout — one tab per entry, each a single pane
running claude under a zsh wrapper (`zsh -c 'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1
claude --resume …; exec zsh'`, so exiting claude drops into a normal shell; the
env prefix is a persistence fallback, see below) with cwd + `--resume` args,
`start_suspended true`, and
**no** captured `swap_tiled_layout`/pane sizes. Omitting sizes is what makes
zellij lay panes out for the **current** terminal (a full `dump-layout` bakes in
old geometry and restores at the wrong size). It **does** include a
`default_tab_template` with the stock `zellij:tab-bar`/`zellij:status-bar`
plugins — starting a new session with `--layout` uses this layout verbatim and
ignores the user's default layout, so without the template the restored session comes up with
**no tab bar**: all tabs exist but are invisible and un-switchable. The plugins
carry no geometry, so terminal-fit still holds. Tab names, cwds and args are
KDL/shell-escaped.

## Restore is manual — and why (the child-session persistence trap)

Restore does **not** launch zellij for you, on purpose. When a zellij **server**
is started from inside a claude pane, it inherits that pane's
`CLAUDE_CODE_CHILD_SESSION` marker in its environment. Every pane the server
forks then inherits it too, so each resumed `claude` decides it is a *child
session* and **turns transcript saving off** — the conversation lives only in
process memory and **evaporates on the next reboot / `delete-session`**. This
silently destroyed real sessions (confirmed via `/status`:
`⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker`).

The robust fix is to **launch the restore from a fresh terminal window** — its
ancestor is launchd, so the environment is clean and the marker is absent. That
designs the failure away. As a second line of defence, every pane command in the
layout is prefixed with `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, which forces
saving even from a dirty shell. `restore` therefore just prints a **doctor**
(launch-context health check + the exact commands) instead of spawning anything.

## The intended flow

```
# 1) In ANY cc pane inside the session you want to keep:
/zellij-session-snapshot save

# 2) reboot / log out ...

# 3) Open a BRAND-NEW terminal window (Terminal.app / iTerm — NOT inside zellij,
#    NOT a cc pane) and get the commands:
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

- **macOS-only.** Identity validation uses Darwin's `proc_pidinfo` and
  `KERN_PROCARGS2` (via python stdlib `ctypes`). `KERN_PROCARGS2` is marked
  `__APPLE_API_UNSTABLE` in the SDK, and `~/.claude/sessions/*.json` is a
  Claude Code internal (already relied on by earlier versions of this skill) —
  neither is a stable public contract. Everything fails **closed**: if either
  breaks, save aborts loudly instead of writing a wrong snapshot. Validated
  on claude 2.1.217 / zellij 0.44.3 / macOS 15.
- A **brand-new, still-empty** session has no transcript on disk yet and can't
  be resumed — it snapshots as `✗ failed` until it has real content.
- A tab whose claude has **exited** has no process to interrogate: it snapshots
  as `✗ failed`, with at most a stale manual-resume hint.
- Flags outside the replay allowlist are **not** re-applied on restore (listed
  per-tab at save time as `⚠ NOT replayed`).
- Plain `zellij attach <name>` alone is **not** a restore path (see above); use
  the restore doctor's printed commands to bootstrap after a reboot.
