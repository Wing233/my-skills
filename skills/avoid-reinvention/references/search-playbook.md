# 搜索手册（search-playbook）

各层具体搜法。按当前层需要选择，不必每层全跑。

## 环境判断

- 机器有已认证的 `gh`：优先 `gh search repos`（结构化、能按星排序）
- 没有 gh / 未认证：用 `web_search` 工具配 `site:github.com` 语法
- 检测方法：跑 `gh auth status`，失败就走 web_search 路线

## L0 澄清能力需求

把用户的点子翻译成能力清单，写进报告：
"我需要一个能做 X、Y、Z 的东西"。
搜之前先想：这个能力在生态里通常叫什么名字（例：拖拽排序 = drag-and-drop
sorting / sortable；图表 = charting library）。

## L1 项目内自查

- 读 `package.json` 的 dependencies（别重复装已装的包）
- 在代码库里 grep 相关关键词（已有类似模块吗？）
- 检查项目里是否已有相关工具脚本

## L2 包注册表

- npm：`npm search <关键词>`，或 https://www.npmjs.com 按 downloads 排序
- 注意质量信号：周下载量、最近发布、star、是否弃坑
- 查完对照 L3：包的名字往往直接对应 GitHub 仓库，顺手查它的仓库活跃度

## L3 GitHub 项目

有 gh 认证：
```powershell
gh search repos <关键词> --sort stars --limit 10
gh repo view <owner>/<repo>    # 看星数/最近活动/license