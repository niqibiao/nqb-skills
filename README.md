# nqb-skills

**English** · [简体中文](README.zh-CN.md)

A collection of [Agent Skills](https://docs.claude.com/en/docs/claude-code/skills) for
[Claude Code](https://claude.com/claude-code), packaged as a plugin marketplace so the whole set
can be added with a single command.

A *skill* is a folder of instructions (and optional scripts/resources) that teaches Claude how to
do a specialized task; Claude Code loads one on its own when the task matches.

## Skills

| Skill | What it does |
|---|---|
| [`cc-codex-discussion`](skills/cc-codex-discussion) | Runs a turn-by-turn adversarial discussion between Claude Code and the Codex CLI through a shared markdown file, converging on a high-confidence, evidence-backed conclusion. |

See each skill's own README for details, requirements, and usage.

## Install

Add the marketplace, then install the skills you want:

```
/plugin marketplace add niqibiao/nqb-skills
/plugin install cc-codex-discussion
```

(You can also add it from a local clone: `/plugin marketplace add /path/to/this/repo`.)

## Layout

```
.claude-plugin/marketplace.json   # makes this repo an installable plugin marketplace
skills/<name>/                    # one folder per skill (SKILL.md + optional scripts/resources)
```

## License

Each skill carries its own LICENSE (e.g. Apache-2.0 for `cc-codex-discussion`).
