# sync-agent-rules

An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that keeps your two
user-level agent-instruction files — `~/.claude/CLAUDE.md` (Claude Code's global instructions) and
`~/AGENTS.md` (the AGENTS-style instructions other agents read) — in sync with a git repo that
holds **one canonical copy**.

- **`pull`** — overwrite both local files with the repo's canonical copy, saving a timestamped
  `.bak` of anything it replaces (so edits stay recoverable).
- **`push`** — publish one local file up to the repo; it **refuses to silently clobber** remote
  changes, stopping with a diff on conflict so you decide.
- **`status`** — show the repo URL, canonical filename, and whether each local file is in sync
  (and whether the two local files disagree).
- **`init`** — first-time setup: records the repo URL and auto-detects the canonical filename.

First use asks for the repo address; on push conflicts it stops and asks how to proceed. See
[`SKILL.md`](SKILL.md) for the full flow, conflict handling, and exit codes.

Released under the Apache License 2.0 (see [`LICENSE.txt`](LICENSE.txt)).

## Requirements

- A git repo holding the canonical instruction file (any host — e.g. a Gitea/GitHub repo)
- `git` with credentials cached for that host (the tool never stores tokens)
- Python 3

## Install

Uses the [`skills`](https://www.npmjs.com/package/skills) CLI — no plugin system involved:

```bash
# globally (→ ~/.claude/skills/)
npx skills add niqibiao/nqb-skills --skill sync-agent-rules --global

# …or into the current project only (→ .claude/skills/)
npx skills add niqibiao/nqb-skills --skill sync-agent-rules
```

Prefer no CLI? Clone and symlink it in — symlinks are
[officially supported](https://code.claude.com/docs/en/skills):

```bash
git clone https://github.com/niqibiao/nqb-skills.git
ln -s "$PWD/nqb-skills/skills/sync-agent-rules" ~/.claude/skills/sync-agent-rules
```
