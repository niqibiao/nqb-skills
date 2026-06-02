# cc-codex-discussion

An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that makes Claude Code (CC)
and the Codex CLI debate a topic — a design, plan, bug, or decision — **turn by turn through a
shared markdown file**, each model playing to its strengths, until they converge on a
**high-confidence, evidence-backed conclusion** that you sign off.

- **CC is the sole writer + loop driver.** Codex runs read-only and returns its reply on
  stdout; CC appends it. No write races, zero blast radius.
- **Round 1 is independent:** both sides analyze the raw materials on their own — Codex does
  not see CC's position — so neither anchors the other. From round 2 they go adversarial, each
  fed the other's latest turn (`--resume-last`).
- The transcript lives in a **system-temp** scratch during the run (so Codex can't read CC's
  round-1 analysis), then is copied back to `cc-codex-discussion-history/` after sign-off.
- The blocking Codex call **is** the turn synchronization — no daemons or file-watching.
- A fail-closed transcript parser validates the debate log (alternating roles, increasing
  rounds, no duplicates).

Released under the Apache License 2.0 (see [`LICENSE.txt`](LICENSE.txt)).

## Disclaimer

Provided for demonstration and educational purposes. Running it invokes the Codex CLI, which
executes a separate model and may run read commands in your workspace — review what it does.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- The Codex CLI, installed and authenticated: `npm i -g @openai/codex`, then `/codex:setup`
- Python 3 and Node.js on `PATH`

## Install

From the marketplace:

```
/plugin marketplace add niqibiao/nqb-skills
/plugin install cc-codex-discussion
```

Or copy the skill directly:

```bash
# macOS / Linux
cp -r skills/cc-codex-discussion ~/.claude/skills/cc-codex-discussion

# Windows (PowerShell)
Copy-Item -Recurse skills\cc-codex-discussion "$env:USERPROFILE\.claude\skills\cc-codex-discussion"
```

## Usage

Ask Claude Code, in natural language, to debate something with Codex:

- `discuss this cache-invalidation plan with codex`
- `have codex debate this API design with you`
- `pull codex in to co-review the concurrency handling in this PR`
- `get a second opinion from codex by debating this schema`

The discussion is held in whatever language you write in. CC creates
`cc-codex-discussion-history/<timestamp>-<slug>.md`, runs the rounds (cap 6), then presents the
conclusion — with a stated confidence level and residual risks — for your sign-off.

## How the conclusion stays trustworthy

| | Strength | Role in the debate |
|---|---|---|
| **Codex** (GPT-5-codex) | runs commands, reads the real repo | round 1: independent grounded investigator; round 2+: adversary — every claim backed by `file:line` / output / repro |
| **CC** (Claude/Opus) | architecture, synthesis, judgment | architect + arbiter — verifies findings against the project's spec/decisions, writes the synthesis |

An **open-objections ledger** gates the conclusion: it can only be written as *agreed* when
every objection is resolved; otherwise it is written as "unresolved + crux + recommended
decision." Every conclusion ends with `Confidence: high/medium/low` and residual risks.

## Layout

```
SKILL.md            # the instructions Claude Code follows
LICENSE.txt         # Apache-2.0
scripts/discuss.py  # transcript helper: new / append / delta / check / last / codex-bin
```

### `discuss.py` commands

| command | purpose |
|---|---|
| `new <slug> --topic T` | create the discussion file, print its path |
| `append <file> --role cc\|codex --round N` | append stdin as one atomic block (body + marker) |
| `delta <file> [--role R --round N]` | print the last block's body, or one exact block |
| `check <file>` | validate the whole transcript (fail-closed) |
| `last <file>` | JSON `{role, round, blocks}` |
| `codex-bin` | resolve the bundled `codex-companion.mjs` path |
