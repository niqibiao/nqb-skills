# nqb-skills

[English](README.md) · **简体中文**

一组面向 [Claude Code](https://claude.com/claude-code) 及其他兼容 skill 的 agent 的
[Agent Skills](https://docs.claude.com/en/docs/claude-code/skills)。这些是**纯 skill —— 不走
plugin、不走 marketplace**,用 [`skills`](https://www.npmjs.com/package/skills) CLI 直接安装。

所谓 *skill*,就是一个由指令(以及可选的脚本/资源)组成的文件夹,用来教 agent 完成某项专门
任务;当任务匹配时,agent 会自行加载相应的 skill。

## Skills

| Skill | 作用 |
|---|---|
| [`cc-codex-discussion`](skills/cc-codex-discussion) | 让 Claude Code 与 Codex CLI 通过一个共享 markdown 文件逐回合对抗式讨论,收敛出一个高置信度、有证据支撑的结论。 |
| [`sync-agent-rules`](skills/sync-agent-rules) | 把你的全局 agent 指令文件(`~/.claude/CLAUDE.md` 和 `~/AGENTS.md`)与保存规范副本的 git 仓库互相 pull/push —— pull 覆盖前先备份,push 遇冲突会停下询问。 |
| [`zellij-session-snapshot`](skills/zellij-session-snapshot) | 把 zellij 会话的各个 tab 连同其中运行的 Claude Code 会话一起快照,重启/登出后一条命令即可恢复所有 tab 并接续每个 tab 原本的 Claude 对话。 |

各 skill 的详细说明、前置要求与用法,见其各自文件夹。

## 安装

用 `skills` CLI(`npx skills`),不涉及任何 plugin 机制:

```bash
# 安装本仓库全部 skill,全局(用户级 → ~/.claude/skills/)
npx skills add niqibiao/nqb-skills --global

# …或只装进当前项目(→ .claude/skills/)
npx skills add niqibiao/nqb-skills

# 只装某一个 skill
npx skills add niqibiao/nqb-skills --skill cc-codex-discussion

# 复制文件而不是软链
npx skills add niqibiao/nqb-skills --copy
```

之后用 `npx skills list`、`npx skills update`、`npx skills remove` 管理。

不想用 CLI?自己克隆后把 skill 软链进 skills 目录即可 —— 软链是
[官方支持的](https://code.claude.com/docs/en/skills):

```bash
git clone https://github.com/niqibiao/nqb-skills.git
ln -s "$PWD/nqb-skills/skills/cc-codex-discussion" ~/.claude/skills/cc-codex-discussion
```

## 目录结构

```
skills/<name>/          # 每个 skill 一个文件夹
  └─ SKILL.md           # + 可选的 scripts/、references/
```

没有 `.claude-plugin/`、没有 `marketplace.json` —— `skills` CLI(以及手动软链)直接读
`skills/` 文件夹。

## 许可证

每个 skill 自带各自的 LICENSE(例如 `cc-codex-discussion` 为 Apache-2.0)。
