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

Everything is driven by `scripts/snapshot.py`. Let `SK` = `<this skill dir>/scripts/snapshot.py`. Run its subcommands directly.

## Commands

```bash
python3 $SK save
python3 $SK spawn   --session <name>
python3 $SK restore --session <name>
python3 $SK show    --session <name>
```

- **save** — snapshot the current session (name from `$ZELLIJ_SESSION_NAME`, so
  run it from inside the session you want to save; or pass `--session`). Writes a
  **minimal** restorable layout + manifest, and installs the layout as a **named
  layout** in `~/.config/zellij/layouts/<name>.kdl`. **Only run save from a
  healthy session where every tab is actually running `claude`** — if a tab's
  claude has exited or the session is a broken rebuild (panes running something
  else), save captures nothing for it.
- **restore** — recreate the session from the named layout. Run it from a
  **plain terminal OR a cc pane** that is **not inside zellij** (the normal
  post-reboot state): it creates the session **detached in the background**
  (`zellij --new-session-with-layout <name> attach --create-background <name>`),
  then you just `zellij attach <name>`. Background create is what lets this work
  from a non-tty cc pane (a cold-start exec would fight the terminal). If run
  from inside a *different* live zellij session, it injects the tabs there via
  `zellij action` instead.
- **spawn** — alias for restore's background-create path (same effect); kept as a
  separate verb for discoverability.
- **show** — print the saved manifest (tab, cwd, session id, source, args).

Output lives in `~/.claude/zellij-snapshots/<name>/` (`restore-layout.kdl` +
timestamped history + `manifest.json`) and `~/.config/zellij/layouts/<name>.kdl`.

## How each tab's Claude session is resolved

The session id shown per tab comes from one of two sources — prefer the first:

`save` reads each tab's **name + cwd** from `zellij action dump-layout` — never
the pane's command. That's deliberate: zellij's command discovery mis-reports a
claude pane as whatever child it spawned (e.g. `npm exec @playwright/mcp`), so
matching on `command="claude"` would find nothing. The **cwd stays correct**, and
that's the key. Each tab's cwd is then matched to a session:

1. **`live ✓` (precise)** — a running claude process whose runtime file
   `~/.claude/sessions/<pid>.json` has the same `cwd`. That file records the
   process's *current* `sessionId` (correct even when launched **without**
   `--resume`, and survives `/clear`). Each live process is claimed once, so two
   tabs sharing a cwd resolve to two different sessions instead of colliding. The
   process's launch flags (`--dangerously-skip-permissions`, `--model`, …) are
   read from its cmdline and preserved.

2. **`inferred`** — fallback when a tab's claude has already exited (no live
   process). Picks the most recently modified `*.jsonl` under
   `~/.claude/projects/<cwd-with-slashes-as-dashes>/`, excluding claimed ids.
   Less certain — verify before trusting. Because the original process is gone,
   its launch flags are unknown, so the tab restores with **just `--resume` and
   no extra flags** — it does **not** silently gain `--dangerously-skip-permissions`.

Why not `lsof`? Claude doesn't hold the session `.jsonl` open, so the handle
can't be read from the process. The per-pid runtime file is the reliable source.

**Safety guard:** if `save` resolves **0** resumable tabs (an unhealthy session —
e.g. a broken rebuild where no live claude matches any tab's cwd), it **refuses
to write** and leaves the existing good snapshot untouched.

## Why a named layout, not `zellij attach`

Plain `zellij attach <name>` after a reboot does **not** bring back the resumed
sessions, and you can't fix that by pre-writing zellij's resurrection cache:
**zellij re-serializes the session from live process state at logout, which
overwrites anything you put there** (and its command-discovery captures claude's
child processes like an MCP server instead of `claude`). Fighting that cache is
futile.

So the restore path uses a file zellij only ever **reads**: a **named layout**
in `~/.config/zellij/layouts/<name>.kdl` (config, never overwritten). `save`
installs it. It's a **minimal** layout — one tab per entry, each a single
`pane command="claude"` with cwd + `--resume` args, `start_suspended true`, and
**no** captured `swap_tiled_layout`/pane sizes. Omitting sizes is what makes
zellij lay panes out for the **current** terminal (a full `dump-layout` bakes in
old geometry and restores at the wrong size). It **does** include a
`default_tab_template` with the stock `zellij:tab-bar`/`zellij:status-bar`
plugins — `--new-session-with-layout` uses this layout verbatim and ignores the
user's default layout, so without the template the restored session comes up with
**no tab bar**: all tabs exist but are invisible and un-switchable. The plugins
carry no geometry, so terminal-fit still holds. Every non-`--resume` flag on the
pane (`--dangerously-skip-permissions`, `--model`, …) is preserved.

**Windows**: each pane launches claude through `scripts/conwrap.ps1` instead of
directly. zellij-on-Windows command panes hand the child PIPE std handles even
though a ConPTY console is attached (and is the only thing the pane renders), so
a directly-launched claude sees no TTY, drops into headless mode, and `--resume`
exits immediately with "Provide a prompt to continue the conversation". The
wrapper reopens CONIN$/CONOUT$ read-write (a `< CON > CON` redirect is not
enough — GetConsoleMode needs read access) and hands them to claude via
STARTF_USESTDHANDLES, restoring interactive mode.

## The intended flow

```
# 1) In ANY cc pane inside the session you want to keep:
/zellij-session-snapshot save

# 2) reboot / log out ...

# 3) In ANY cc pane (or plain shell) that is NOT inside zellij:
/zellij-session-snapshot restore          # creates 'work' in the background

# 4) Connect:
zellij attach work
```

`restore` in step 3 works from a cc pane because it creates the session
**detached in the background** (`--create-background`) rather than trying to take
over the terminal. After a reboot the zellij server is gone; step 3 bootstraps
it, and from then on within the same power-on you can detach (`Ctrl-o d`) and
`zellij attach work` reconnects to the live session freely. After the next reboot,
run `restore` again.

Panes come up **suspended**: switch to a tab, press Enter to wake its
`claude --resume` (deliberate — avoids launching many claudes at once).

Re-run `save` whenever the tabs or their conversations changed — it re-installs
the named layout and keeps timestamped history. (`spawn` is an alias for the
`restore` background path.)

## Limitations to be honest about

- **Only save from a healthy session.** Every tab must be actually running
  `claude`. Saving from a broken rebuild (panes running an MCP child or a shell)
  captures nothing for those tabs. If unsure, check the printed tab count.
- A tab whose Claude has **exited** falls back to `inferred` — double-check it.
- Two tabs in the **same cwd both launched without `--resume`** and both already
  exited can't be told apart; while running they're matched precisely by pid.
- Cross-session save isn't supported — run `save` from inside the target session.
- Plain `zellij attach <name>` alone is **not** a restore path (see above); use
  spawn or cold start to bootstrap after a reboot.
