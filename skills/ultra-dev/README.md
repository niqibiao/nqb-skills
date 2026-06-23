# ultra-dev

A project-agnostic [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that drives
a feature **slice end-to-end** — `spec → review → plan → review → implement → review → docs →
report → merge` — with adversarial review gates and human stops, then converges on a
mergeable PR you sign off.

- **Main-loop driven, not a Workflow.** Claude Code drives each stage and can stop to ask you
  where it matters — a Workflow (a background fan-out) runs to completion and can't stop for
  input. Only the post-impl parallel review may *call* a Workflow as a sub-step.
- **Risk-tiered gates.** Each slice is classed **light / medium / heavy** (touches a core
  invariant? changes a public contract? needs a decision record?) and only runs the review
  weight that tier warrants — no review fatigue on a doc tweak, full adversarial depth on a
  contract change.
- **Right tool per gate.** spec/plan → `codex:adversarial-review` (challenges design, not code);
  post-impl (medium/heavy) → `cc-codex-discussion` (its round-1 independent investigation is the
  deep defect pass), light or fallback → `codex:review`; heavy specs escalate to a full discussion.
- **Two human stops only:** any stop-and-ask threshold (core invariant / decision / public
  contract / unfounded design fork — also where a spec/plan review gate halts, if it surfaces
  one), and the final merge — which is always yours to confirm. The spec and plan gates
  otherwise **announce and continue** — no routine sign-off pause.
- **Project bindings, discovered at runtime.** ultra-dev stays generic; in a given repo it
  finds and delegates to that project's own PR-flow / decision-log / verification / doc-sync /
  domain-review skills, falling back to generic equivalents where none exist.
- **Durable trail.** A live dispute ledger (survives compaction) is assembled into a committed
  slice report under `docs/superpowers/reports/`.

## Requires

ultra-dev orchestrates external tools rather than reimplementing them. For the review gates and
sub-stages it expects, in the host environment:

- **superpowers** skills — `brainstorming`, `writing-plans`, `subagent-driven-development`,
  `finishing-a-development-branch` (generic VCS fallback).
- **codex** plugin — `codex:adversarial-review`, `codex:review`.
- *(optional)* **cc-codex-discussion** (sibling skill in this marketplace) — used for the deep
  post-impl gate on medium/heavy slices and the heavy spec gate; if absent, degrade to
  `codex:review` (post-impl) or `codex:adversarial-review` (heavy spec).

It has **no dependency on any specific project repo**; project-specific actions are delegated to
whatever skills the target project provides (see "Project bindings" in `SKILL.md`).
