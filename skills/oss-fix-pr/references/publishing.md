# 发布与披露

## 试运行与授权

discover、prepare、finding、verify、resume 不写 GitHub。publish 不带 `--execute` 也不写远端。不能因用户最初说想自动提交，跳过其已选择的首批审核。

用户审核具体补丁、证据、正文并明确开启后，更新本地配置 `publish_mode: auto`、`reviewed_batch: 批次ID`。已有授权范围不用逐 PR 询问；授权撤回立即恢复 dry-run。

发布使用 GitHub CLI，用户通过 `gh auth login` 登录 github.com，不索取或输出 token，不读取其他项目凭据。认证环境不传给测试容器。

机器上没有 gh 时，不要为此安装不需要的软件或要求管理员权限；先运行安装便携版：

```text
python SKILL/scripts/install_gh.py
gh auth login
```

`install_gh.py` 从官方发布下载并校验 SHA256 后解包到 `%LOCALAPPDATA%/oss-fix-pr/tools`（或 `~/.local/share/oss-fix-pr/tools`），`doctor` 会自动识别；登录始终由本人完成。

## 门槛与恢复

- 普通 Bug 或适合公开的已知依赖修复；Codex 核对贡献政策、具体重复问题。
- 脚本验证通过，补丁、正文、日志哈希与证据一致。
- 上游默认分支及 SHA 不变，仓库公开、未归档。
- 同分支/同指纹 PR 返回已有链接；其他同分支或相关标题 PR 停止并交会话审查。
- fork 属于当前账号且 parent 匹配目标；不覆盖同名其他仓库。

通过 `gh api` 的 Git 数据 API 上传验证文件、保留文件模式、建立 commit 和分支，最后创建非草稿 PR。不强推、不合并、不删除上游内容。发布阶段写 SQLite，超时先读远端；结果不确定则停止，下次先查状态。Git 数据对象重复上传不会创建第二个 PR。

上游变化时关闭旧 job，在新批次重新固定验证；不得手改证据或数据库 commit。脚本首版拒绝向当前账号拥有的上游直接写分支，真实链路测试需使用可 fork 的自有测试组织仓库或独立测试账号，不能借公共第三方仓库做无意义 PR 测试。

返回 PR URL 后，当前 Codex 会话调用 `attach_artifact`：

```json
{"artifact_type":"pull_request","url":"实际返回的 PR URL"}
```

工具不可用则告知已创建但未附加，并保留链接，不能再建 PR。脚本没有聊天 ID，不假装已经附加。

## 未公开安全问题

源码安全缺陷用 `kind=security`，通过后状态为 private_ready，本地生成 private-report.md。不上传扫描日志、补丁、详情或公开 fork；普通报告只列私密候选数量。

按 SECURITY.md 渠道或 GitHub private vulnerability reporting 准备私密报告。未启用则用维护者指定渠道，不能通过公开 Issue 披露。发送前须有针对具体报告和渠道的单独用户授权；首版不自动发送。

协调披露后若要公开修复，另行审查维护者指示和公开范围。首版不自动解除 security 拦截，不改 kind 绕过；已公开依赖公告不意味着新的利用路径可以公开。

参考：[GitHub 私密报告](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)、[Git 数据 API](https://docs.github.com/en/rest/git)、[搜索 API](https://docs.github.com/en/rest/search/search)。
