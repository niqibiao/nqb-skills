---
name: sync-agent-rules
description: >-
  Use this skill when the user wants to pull down or push up their personal global
  agent instruction files — ~/.claude/CLAUDE.md and ~/AGENTS.md — to/from the git repo
  that holds the canonical copy. This is the go-to for "同步 agents"、"拉一下 agents /
  agents 配置"、"推一下"、"更新成仓库最新版"、"把仓库内容覆盖到本地"、"把本地改的
  CLAUDE.md 推到仓库"、"另一台机器改了这台拉下来"、"sync/pull/push my agents",
  "update my global CLAUDE.md from the repo", "push my agent instructions to gitea".
  Any request to transfer these instruction files between this machine and the repo —
  in either direction — is this skill, even when the user names only one file, says
  just 拉 / 推 / 同步 agents, or vaguely references "my agent config/rules/instructions".
  On first use it asks for the repo address; on push conflicts it stops and asks how to
  proceed. Do NOT use it for editing or reviewing what these files say, configuring git
  tokens/credentials, or syncing anything else (project files, dotfiles, vim config,
  npm dependencies).
---

# sync-agent-rules

Keeps the user-level agent instructions in sync with a remote git repo that stores
**one canonical copy**. There is a single source of truth locally too:

- `~/AGENTS.md` — the full canonical instructions (the only file that is synced)
- `~/.claude/CLAUDE.md` — a one-line stub `@~/AGENTS.md` that imports it, so Claude
  Code loads the same rules without keeping a duplicate copy

The repo (any git repo, e.g. a Gitea/GitHub repo like `…/you/AGENTS`) holds a single file.
**Pull** overwrites `~/AGENTS.md` with it (backing it up first) and (re)writes the CLAUDE.md
stub. **Push** sends `~/AGENTS.md` up and refuses to silently clobber remote changes.

> The `@~/AGENTS.md` stub uses an **absolute** path on purpose: Claude Code resolves
> `@`-imports relative to the importing file, so a bare `@AGENTS.md` inside
> `~/.claude/CLAUDE.md` would look for `~/.claude/AGENTS.md` (wrong file). `init` and
> `pull` manage this stub for you — you don't edit it by hand.

All git plumbing lives in `scripts/sync_agent_rules.py`. Your job is to run the right
subcommand and handle the two moments that need a human: first-time setup and conflicts.

Set `SCRIPT="$HOME/.claude/skills/sync-agent-rules/scripts/sync_agent_rules.py"` and call
`python3 "$SCRIPT" <cmd>`.

## First use — get the repo address

If `~/.claude/agents-sync/config.json` does not exist, the tool isn't set up yet
(any command except `init` exits with code **2**). Ask the user for the repo address,
then initialize:

```
python3 "$SCRIPT" init --repo-url <URL>
```

The URL is a normal git clone URL (e.g. `https://your-git-host.example/you/AGENTS.git`, or an
SSH URL like `ssh://git@your-git-host.example:22/you/AGENTS.git`).
`init` clones it, auto-detects the canonical filename (`AGENTS.md`, else `CLAUDE.md`,
else `AGENTS.md` to be created on first push), records `~/AGENTS.md` as the local
target, and writes the `~/.claude/CLAUDE.md` stub (backing up anything already there).
**Don't guess the URL** — the user must supply it.

## Feature 1 — Pull (repo → local)

When the user wants the latest instructions:

```
python3 "$SCRIPT" pull
```

This resets the working copy to the remote, then overwrites `~/AGENTS.md` with the
canonical file and ensures the `~/.claude/CLAUDE.md` stub is in place, saving a timestamped
`.bak` of anything it replaces. Report which files were updated (and mention the backups so
the user knows edits are recoverable).

## Feature 2 — Push (local → repo)

When the user wants to publish their local edits:

```
python3 "$SCRIPT" push
```

It pushes `~/AGENTS.md` (the recorded `push_source`) — the single canonical file.
(`~/.claude/CLAUDE.md` is just a stub and is never pushed.)

**Handling conflicts (exit code 3).** If the remote changed since the last sync and its
content differs from what's being pushed, the tool prints `CONFLICT`, shows a diff, and
stops without touching the remote. Do NOT auto-resolve — surface the diff to the user and
let them choose:

- **Keep their local version** → re-run with `--overwrite-remote`:
  `python3 "$SCRIPT" push --overwrite-remote`
- **Take the remote version instead** → run `pull` (this backs up and overwrites local).
- **Merge by hand** → they can edit the file, then push again.

## Checking state

```
python3 "$SCRIPT" status
```

Shows the repo URL, canonical filename, whether `~/AGENTS.md` is in sync with the remote,
and whether the `~/.claude/CLAUDE.md` stub is in place.

## Authentication

The tool reuses the system's git credentials — it never stores tokens. If a command
exits with code **4** (AUTH/NETWORK), the repo is unreachable or the credential isn't
cached. Tell the user to set up a git credential helper for that host and authenticate
once, e.g.:

```
# pick the helper for the OS:
#   macOS:   git config --global credential.helper osxkeychain
#   Windows: git config --global credential.helper manager
#   Linux:   git config --global credential.helper store
git ls-remote <repo-url>   # prompts once, then caches
```

Then retry the command.

## Exit codes

`0` ok · `1` error · `2` not initialized (run `init`) · `3` conflict (ask the user) ·
`4` auth/network (fix credentials).
