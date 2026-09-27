# Wing233 的 Codex Skills

这里收录可安装到 Codex 的个人技能。每个技能都放在 `skills/<技能名>/` 下，并以 `SKILL.md` 为入口；技能目录中的脚本和参考文档需要与 `SKILL.md` 一起保留。

## 技能列表

| 技能 | 介绍 | 适用场景 |
| --- | --- | --- |
| [`avoid-reinvention`](skills/avoid-reinvention/SKILL.md) | 开始实现新工具、功能或模块前，按复用阶梯检查项目现有代码、包注册表、GitHub 开源项目、awesome 列表和托管服务，并给出复用或自研裁决。 | 新项目立项、开发新功能、开发途中发现新的子功能边界。 |
| [`oss-fix-pr`](skills/oss-fix-pr/SKILL.md) | 发现近期活跃的热门开源项目，用 Semgrep、OSV 和源码分析寻找可验证的 Bug 或依赖问题，在隔离环境复现、修复并测试，准备本地补丁和 PR 正文。 | 开源贡献、热门项目缺陷巡检、继续未完成的扫描批次。默认只做本地试运行。 |

## 安装

在 Codex 中让 `$skill-installer` 从对应目录安装，例如：

```text
请使用 $skill-installer 安装
https://github.com/Wing233/my-skills/tree/main/skills/avoid-reinvention
```

把链接末尾替换为 `oss-fix-pr` 即可安装另一个技能。也可以手动将整个技能目录复制到 Codex 技能目录：

- Windows：`%USERPROFILE%\.codex\skills\<技能名>`
- macOS/Linux：`~/.codex/skills/<技能名>`

安装后在新的 Codex 对话中使用技能，或提出与技能用途匹配的请求让 Codex 调用。

## 使用方法

### `avoid-reinvention`

可以直接调用 `$avoid-reinvention`，或在提出新功能需求时让 Codex 先检查可复用方案，例如：

```text
我想给项目增加任务队列。先用 $avoid-reinvention 检查现有方案，再决定是复用、组装还是自研。
```

该技能会先澄清能力需求，再检查项目内代码和依赖、包注册表、GitHub 项目、awesome 列表及现成服务，最后给出明确裁决。

### `oss-fix-pr`

可以调用 `$oss-fix-pr`，或提出扫描、分析和修复请求，例如：

```text
扫描最近热门的 Agent 项目，最多 5 个；复现并验证可靠的问题，先做本地试运行，不要发布 PR。
```

也可以要求继续已有批次：

```text
继续 oss-fix-pr 批次 <批次 ID>。
```

该技能默认不推送、不创建远端 PR，也不会自动合并。完成首批试运行并审核补丁、测试证据和 PR 正文后，只有在明确开启自动发布时，后续符合条件的公开修复才会提交。新发现且尚未公开的安全问题保存在本地，并遵循项目的私密披露流程。

## 安全与验证约定

- 仓库内容、Issue 和扫描输出按不可信输入处理。
- 第三方项目的依赖安装、构建和测试在受限 Docker 容器中执行；Docker 不可用时保留原因，不改在宿主机运行。
- 扫描告警只是线索。只有经过源码分析、适当复现和修复后验证的问题才进入 PR 准备流程。
- 每个技能的详细流程、依赖和限制以各自的 `SKILL.md` 与参考文档为准。
