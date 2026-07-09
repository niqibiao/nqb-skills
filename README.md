# nqb-skills

**English** · [简体中文](README.zh-CN.md)

A collection of [Agent Skills](https://docs.claude.com/en/docs/claude-code/skills) for
[Claude Code](https://claude.com/claude-code) and other skills-compatible agents. These are plain
skills — **no plugin, no marketplace** — installed directly with the [`skills`](https://www.npmjs.com/package/skills) CLI.

A *skill* is a folder of instructions (and optional scripts/resources) that teaches the agent how
to do a specialized task; the agent loads one on its own when the task matches.

## Skills

| Skill | What it does |
|---|---|
| [`cc-codex-discussion`](skills/cc-codex-discussion) | Runs a turn-by-turn adversarial discussion between Claude Code and the Codex CLI through a shared markdown file, converging on a high-confidence, evidence-backed conclusion. |
| [`sync-agent-rules`](skills/sync-agent-rules) | Pull/push your global agent instruction files (`~/.claude/CLAUDE.md` and `~/AGENTS.md`) to/from a git repo that holds the canonical copy — backs up before overwriting on pull, stops and asks on push conflicts. |

See each skill's own folder for details, requirements, and usage.

## Install

Uses the `skills` CLI (`npx skills`) — no plugin system involved:

```bash
# Every skill in this repo, globally (user-level → ~/.claude/skills/)
npx skills add niqibiao/nqb-skills --global

# …or into the current project only (→ .claude/skills/)
npx skills add niqibiao/nqb-skills

# Just one skill
npx skills add niqibiao/nqb-skills --skill cc-codex-discussion

# Copy the files instead of symlinking them in
npx skills add niqibiao/nqb-skills --copy
```

Manage them later with `npx skills list`, `npx skills update`, `npx skills remove`.

Prefer no CLI? Clone and symlink a skill into your skills dir yourself — symlinks are
[officially supported](https://code.claude.com/docs/en/skills):

```bash
git clone https://github.com/niqibiao/nqb-skills.git
ln -s "$PWD/nqb-skills/skills/cc-codex-discussion" ~/.claude/skills/cc-codex-discussion
```

## Layout

```
skills/<name>/          # one folder per skill
  └─ SKILL.md           # + optional scripts/ and references/
```

No `.claude-plugin/`, no `marketplace.json` — the `skills` CLI (and manual symlinks) read the
`skills/` folder directly.

## License

Each skill carries its own LICENSE (e.g. Apache-2.0 for `cc-codex-discussion`).
