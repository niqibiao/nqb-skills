# nqb-skills

[English](README.md) · **简体中文**

一组面向 [Claude Code](https://claude.com/claude-code) 的
[Agent Skills](https://docs.claude.com/en/docs/claude-code/skills)，按插件市场（plugin
marketplace）的结构打包，因此可以一条命令把整套技能添加进来。

所谓 *skill*，就是一个由指令（以及可选的脚本/资源）组成的文件夹，用来教 Claude 完成某项专门
任务；当任务匹配时，Claude Code 会自行加载相应的 skill。

## Skills

| Skill | 作用 |
|---|---|
| [`cc-codex-discussion`](skills/cc-codex-discussion) | 让 Claude Code 与 Codex CLI 通过一个共享 markdown 文件逐回合对抗式讨论，收敛出一个高置信度、有证据支撑的结论。 |

各 skill 的详细说明、前置要求与用法，见其各自文件夹内的 README。

## 安装

先添加市场，再安装你需要的 skill：

```
/plugin marketplace add niqibiao/nqb-skills
/plugin install cc-codex-discussion
```

（也可以从本地克隆添加：`/plugin marketplace add /path/to/this/repo`。）

## 目录结构

```
.claude-plugin/marketplace.json   # 使本仓库成为可安装的插件市场
skills/<name>/                    # 每个 skill 一个文件夹（SKILL.md + 可选脚本/资源）
```

## 许可证

每个 skill 自带各自的 LICENSE（例如 `cc-codex-discussion` 为 Apache-2.0）。
