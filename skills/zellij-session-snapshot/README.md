# zellij-session-snapshot

An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that snapshots a
[zellij](https://zellij.dev) session's tabs **together with the Claude Code conversation running
in each tab**, then restores every tab **and resumes its exact Claude session** after a
reboot/logout with one command.

Zellij's own resurrection re-runs `claude` fresh and loses which conversation each tab was on;
this skill closes that gap.

- **`save`** — snapshot the current session (run it from inside the session you want to keep).
  Writes a minimal restorable layout + manifest and installs it as a **named layout** in
  `~/.config/zellij/layouts/<name>.kdl`. Refuses to overwrite a good snapshot when run from an
  unhealthy session.
- **`restore` / `spawn`** — recreate the session from the named layout, created **detached in the
  background** so it works even from a non-tty cc pane after a reboot; then `zellij attach <name>`.
- **`show`** — print the saved manifest (tab, cwd, session id, source, args).

Each tab's Claude session is matched by its **cwd** (not its pane command, which zellij
mis-reports). See [`SKILL.md`](SKILL.md) for the full resolution mechanism and its honest
limitations.

Released under the Apache License 2.0 (see [`LICENSE.txt`](LICENSE.txt)).

## Requirements

- [Claude Code](https://claude.com/claude-code), writing its sessions under `~/.claude/`
- [`zellij`](https://zellij.dev) on `PATH`
- Python 3

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
