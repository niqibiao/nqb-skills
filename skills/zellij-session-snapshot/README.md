# zellij-session-snapshot

An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that snapshots a
[zellij](https://zellij.dev) session's tabs **together with the Claude Code conversation running
in each tab**, then restores every tab **and resumes its exact Claude session** after a
reboot/logout with one command.

Zellij's own resurrection re-runs `claude` fresh and loses which conversation each tab was on;
this skill closes that gap.

- **`save`** — snapshot a session (default `$ZELLIJ_SESSION_NAME`, or `--session <name>` from
  anywhere). Writes a minimal restorable layout + manifest and installs it as a **named layout** in
  `~/.config/zellij/layouts/<name>.kdl`. Refuses to overwrite a good snapshot when run from an
  unhealthy session.
- **`restore`** — **prints a doctor + the exact commands to run by hand; it does not spawn
  anything.** Auto-spawning a zellij server from inside a claude pane makes restored panes inherit
  a non-persisting child session (transcripts stop saving), so restore is deliberately launched
  from a fresh terminal. (`spawn` is a deprecated alias that redirects to the doctor.)
- **`show`** — print the saved manifest (tab, cwd, session id, source, args).

Each tab's Claude session is resolved by asking the OS directly — no hook, no setup: Claude's
per-process runtime files (`~/.claude/sessions/<pid>.json`) are joined with zellij's pane table on
the **pane id** carried in each claude process's environment (read exactly via Darwin's
`KERN_PROCARGS2`), with the process identity-validated (start time, argv, descent from this
session's `zellij --server`). Same-cwd tabs stay distinct and the id is correct even after
`/clear`. Identity failures abort the save (fail closed). See [`SKILL.md`](SKILL.md) for the full
mechanism and its honest limitations.

Released under the Apache License 2.0 (see [`LICENSE.txt`](LICENSE.txt)).

## Requirements

- **macOS** (identity validation uses Darwin's libproc / `KERN_PROCARGS2`)
- [Claude Code](https://claude.com/claude-code), writing its sessions under `~/.claude/`
- [`zellij`](https://zellij.dev) on `PATH`
- Python 3 (stdlib only)

## Install

Uses the [`skills`](https://www.npmjs.com/package/skills) CLI — no plugin system involved:

```bash
# globally (→ ~/.claude/skills/)
npx skills add niqibiao/nqb-skills --skill zellij-session-snapshot --global

# …or into the current project only (→ .claude/skills/)
npx skills add niqibiao/nqb-skills --skill zellij-session-snapshot
```

Prefer no CLI? Clone and symlink it in — symlinks are
[officially supported](https://code.claude.com/docs/en/skills):

```bash
git clone https://github.com/niqibiao/nqb-skills.git
ln -s "$PWD/nqb-skills/skills/zellij-session-snapshot" ~/.claude/skills/zellij-session-snapshot
```
