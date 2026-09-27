# 工作流、配置与证据

## 环境与状态

Python 3.11+；脚本仅使用标准库。Git 获取固定 commit。Linux Docker 引擎用于第三方安装和测试；GitHub CLI 登录仅在发布时必需，匿名发现受公共 API 额度限制。`doctor` 只读检测，不安装系统软件、不登录账号。

状态目录默认保存在 Windows `%LOCALAPPDATA%/oss-fix-pr/<工作区路径哈希>/` 或 macOS/Linux `~/.local/share/oss-fix-pr/<工作区路径哈希>/`，可用 `--state` 指定绝对路径。它包含 `config.json`、SQLite、`jobs/`、`findings/`、`reports/`、镜像与规则锁定记录。不要放进共享同步目录。进程锁防止同状态目录并发，异常退出后自动释放；`resume` 恢复中断任务。容器名以 `ossfix-` 开头，强制终止后由操作者核对名称再清理遗留容器。

默认 star ≥ 1000、90 天活跃，每批 5 项、最多 3 个公开修复，每仓库累计 1800 秒、每问题两轮，不自动扩大预算。单次扫描上限 900 秒（`scanner_timeout_s`），扫描并行 3 并给容器 4 CPU（`scanner_jobs`）；大型仓库仍需在预算内完成，超时按未完成记录。源码上限 250 MiB；测试容器 2 CPU、4 GiB 内存、256 PID。超限记录限制，不降低验证标准。

发现按主题和语言分轮搜索，去重并保存分页/star 分片游标，在“相关度、热度、总 star”排序上增加来源多样性。初次热度以活跃度和总 star 代替；相隔至少一天才显示实测日均 star 变化。默认 14 次搜索后选择一批，未访问游标保留；相同 star 区间超过搜索上限时报告覆盖缺口，不承诺全量扫描。

`prepare` 通过 Git 传输固定默认分支 SHA 并检查最近实际提交，保存安全政策、贡献规范和开放 Issue 前 100 条。Issue API 限流不阻止本地分析，但明确标记上下文缺失；该列表不是完整重复检查，提交前 Codex 须为具体问题继续搜索。

## 输入格式

在独立输入目录准备文件，不修改固定 `checkout`。在临时副本编辑并生成补丁：源码修复进入 `fix.patch`，回归测试进入 `regression.patch`；二者相对相同 commit 且不能修改同一文件。

Python 示例；命令须据实际项目替换：

```json
{
  "title": "Fix empty input handling in parser",
  "kind": "bug",
  "location": "src/parser.py:parse",
  "identity": "parse-empty-input-index-error",
  "description": "说明实际行为、预期依据、调用路径、影响范围，以及检查过的 Issue/PR。",
  "image": "python:3.12-slim",
  "setup": "python -m venv /home/runner/venv && /home/runner/venv/bin/pip install -e '.[test]'",
  "existing_tests": "/home/runner/venv/bin/python -m pytest tests/test_parser_existing.py -q",
  "regression_test": "/home/runner/venv/bin/python -m pytest tests/test_empty_input.py -q",
  "failure_marker": "test_empty_input",
  "fix_patch": "fix.patch",
  "regression_patch": "regression.patch",
  "pr_body": "pr-body.md"
}
```

```text
python SKILL/scripts/oss_fix_pr.py --state STATE finding --job JOB_ID --spec INPUT/finding.json
python SKILL/scripts/oss_fix_pr.py --state STATE verify --finding FINDING_ID
```

identity 按根因命名，不用随机数或时间；仓库、类型、位置、identity 构成跨 commit 指纹，具体 commit 生成验证实例。同一实例不能重新登记绕过两轮上限，新 commit 可新建实例但仍检查已有 PR。failure_marker 选择真实断言标识，不伪造失败。首版要求修复前退出码 1，其他框架先扩展适配与测试再使用。

Java 选择匹配项目 JDK 的 Maven/Gradle 官方镜像，setup 预取依赖，测试使用离线参数；JS/TS/Vue 选择对应 Node 镜像和项目锁文件/包管理器。每阶段独立执行 setup，shell 环境不跨命令保留，虚拟环境用绝对路径或显式激活。setup 只准备依赖，不能把测试挪入 setup 绕过断网。镜像需有 `/bin/sh`、`tar`，包管理器需支持非 root。需特权、系统安装、在线测试或多服务时记录限制，不提升权限。

验证依次执行：原始 commit 的已有测试通过；加回归测试后出现预期失败；加修复与回归测试后，回归及相关已有测试全部通过。环境失败、导入失败、未发现测试均不能算复现。

隔离器把源码写入容器 tmpfs，不挂载宿主工作区、用户目录、Docker socket 或凭据。依赖准备联网，测试前断开 bridge 并验证没有网络连接。无跨项目共享构建缓存。Git 获取禁用全局配置、hooks、凭据助手及外部传输。符号链接/特殊文件快照、二进制/权限变更补丁首版不自动执行/发布，保留限制。

## 依赖漏洞

kind 使用 `dependency`，增加字符串字段 `advisory`、`package`、`ecosystem`、`affected_version`、`fixed_version`。可省略回归补丁与命令，但兼容性测试不可省略。包名、生态、版本与 OSV 输出精确一致。

验证器自行运行前后 OSV 扫描：旧版本确有目标公告、新版本仍被枚举且目标告警消失，再通过兼容性测试。不能将依赖告警直接宣称为项目可利用漏洞。OSV 自动修复仍是实验性，首版由 Codex 选择最小兼容升级。

## 扫描与证据

Semgrep 使用官方 p/ci 规则，缓存并记录 SHA256；首次拉取镜像后锁定 registry digest，不隐式更新。CE 跨文件能力有限，解析失败标为部分覆盖。OSV 联网查询公开依赖数据，只运行可信扫描器，不执行项目脚本；只挂载不含 .git 的只读源码。源码树没有 OSV 可读清单时它以退出码 1 结束且不输出 JSON，这种情况记为零覆盖率，不算扫描失败，也不能据此声称依赖安全。

finding 保存补丁、正文、命令日志、退出码、镜像 digest、扫描元数据和哈希。验证后改正文也会使证据失效，应事先写好。PR 正文说明问题、触发、修复与测试，不添未经证实的严重性和 CVE。

`report` 只汇总状态，原始扫描留本地。无确认问题不等于没有问题。`close-job` 记录分析结束，不授权发布或改变验证状态。
