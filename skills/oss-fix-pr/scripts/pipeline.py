"""Pinned source, bounded Docker execution, scanner adapters and verification gates."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import time
import urllib.request

from core import Stop, digest, doctor, git, ident, now, read_json, run, stamp, temp_dir, write_json


def job_dir(state, job):
    return state.root / "jobs" / job["id"]


class Budget:
    def __init__(self, state, job):
        self.state, self.job = state, job
        self.start = time.monotonic()
        self.remaining = state.config["seconds_per_repo"] - job.get("spent", 0)

    def left(self, cap=None):
        left = self.remaining - (time.monotonic() - self.start)
        if left <= 0:
            raise Stop("该仓库执行预算已耗尽")
        return min(left, cap) if cap else left

    def save(self):
        self.job["spent"] = round(self.job.get("spent", 0) + time.monotonic() - self.start, 2)
        self.state.put("job", self.job["id"], self.job)


def snapshot(checkout, commit, dest, max_mb=250):
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    listing = git("ls-tree", "-rl", "-z", commit, cwd=checkout, timeout=30)
    total = 0
    for record in listing.decode("utf-8").split("\0"):
        if record:
            metadata = record.split("\t", 1)[0].split()
            if metadata[1] != "blob":
                raise Stop("源码包含子模块；首版不执行不完整快照")
            total += int(metadata[3])
    if total > max_mb * 1024 ** 2:
        raise Stop("源码超出快照大小上限")
    data = git("archive", "--format=tar", commit, cwd=checkout, timeout=60)
    if len(data) > max_mb * 1024 ** 2:
        raise Stop("源码超出快照大小上限")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = tar.getmembers()
        if any(m.issym() or m.islnk() or not (m.isfile() or m.isdir()) for m in members):
            raise Stop("源码快照包含符号链接或特殊文件；首版只分析，不执行")
        tar.extractall(dest, filter="data")


def source_tar(source, max_mb):
    stream = io.BytesIO()
    root = Path(source).resolve()
    size = 0
    with tarfile.open(fileobj=stream, mode="w") as out:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if ".git" in rel.parts:
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise Stop("执行快照不得包含符号链接或目录联接")
            if not path.is_file():
                continue
            size += path.stat().st_size
            if size > max_mb * 1024 ** 2:
                raise Stop("源码超出执行快照大小上限")
            item = out.gettarinfo(str(path), arcname=str(rel).replace("\\", "/"))
            item.uid = item.gid = 65534
            item.uname = item.gname = ""
            item.mode = 0o755 if path.suffix in (".sh",) or path.name in ("gradlew", "mvnw") else 0o644
            with path.open("rb") as f:
                out.addfile(item, f)
    return stream.getvalue()


def pinned_image(state, image, budget):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]+", image):
        raise Stop("无效 Docker 镜像标识")
    lock = state.get("image", image)
    ref = lock["digest"] if lock else image
    p = run(["docker", "image", "inspect", ref], check=False, timeout=budget.left(15))
    if p.returncode:
        run(["docker", "pull", ref], timeout=budget.left(300))
        p = run(["docker", "image", "inspect", ref], timeout=budget.left(15))
    details = json.loads(p.stdout)[0]
    digests = details.get("RepoDigests") or []
    if not digests:
        raise Stop("镜像没有 registry digest；请使用已发布的工具镜像")
    result = next((d for d in digests if d == ref), digests[0])
    state.put("image", image, {"digest": result, "id": details["Id"], "locked": now()})
    return result


def docker_limits(state, name, cpus=None):
    c = state.config
    return ["--name", name, "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory", c["memory"], "--cpus", str(cpus or c["cpus"]), "--pids-limit", "256",
            "--read-only", "--user", "65534:65534", "--log-driver=none",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,uid=65534,gid=65534"]


def scanner(state, source, engine, output, budget):
    """Trusted scanner only; read-only mount. No project build/setup commands here."""
    image = pinned_image(state, state.config[f"{engine}_image"], budget)
    name = "ossfix-scan-" + ident()
    jobs = state.config["scanner_jobs"]
    args = ["docker", "run", "--rm", *docker_limits(state, name, cpus=jobs + 1), "--network", "bridge",
            "--mount", f"type=bind,source={Path(source).resolve()},target=/src,readonly",
            "--workdir", "/src", "--env", "HOME=/tmp"]
    info = {"image": image, "engine": engine, "started": now()}
    coverage = None
    if engine == "semgrep":
        rules_path = state.root / "tools" / "semgrep-rules.yaml"
        if not rules_path.exists():
            req = urllib.request.Request(state.config["semgrep_rules_url"], headers={"User-Agent": "oss-fix-pr", "Accept": "application/x-yaml"})
            with urllib.request.urlopen(req, timeout=min(30, budget.left())) as response:
                content = response.read(5 * 1024 ** 2 + 1)
            if len(content) > 5 * 1024 ** 2 or b"rules:" not in content:
                raise Stop("Semgrep 规则下载失败或格式不正确")
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            rules_path.write_bytes(content)
        info["rules_sha256"] = digest(rules_path.read_bytes())
        args += ["--mount", f"type=bind,source={rules_path},target=/rules.yaml,readonly", image,
                 "semgrep", "scan", "--config", "/rules.yaml", "--json", "--metrics=off",
                 "--disable-version-check", "--oss-only", "--jobs", str(jobs), "--timeout", "5",
                 "--max-memory", "1536", "--no-git-ignore", "/src"]
    else:
        args += [image, "scan", "source", "--recursive", "--all-packages", "--format=json", "/src"]
    try:
        p = run(args, check=False, timeout=budget.left(state.config["scanner_timeout_s"]))
    finally:
        run(["docker", "rm", "-f", name], check=False, timeout=15)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(p.stdout)
    output.with_suffix(".stderr.log").write_bytes(p.stderr)
    try:
        parsed = json.loads(p.stdout)
    except ValueError as exc:
        if engine == "osv" and b"No package sources found" in p.stderr:
            # OSV exits 1 with empty stdout when the tree has no manifest it can
            # read. That is zero coverage, not a failed scan; reporting it as a
            # failure would misrepresent the repository as unverified.
            parsed, coverage = {"results": []}, "none"
        else:
            raise Stop(f"{engine} 未返回有效 JSON；不能判定扫描通过") from exc
    if p.returncode not in ((0,) if engine == "semgrep" else (0, 1)):
        raise Stop(f"{engine} 执行失败：{p.returncode}")
    if coverage is None:
        coverage = "partial" if engine == "semgrep" and parsed.get("errors") else "scanner-reported"
    info["coverage"] = coverage
    version = parsed.get("version")
    if not version:
        cached = state.get("scanner-version", image)
        if cached:
            version = cached["version"]
        else:
            version_name = "ossfix-version-" + ident()
            try:
                v = run(["docker", "run", "--rm", *docker_limits(state, version_name), "--network", "none",
                         image, "--version"], timeout=budget.left(30))
                version = v.stdout.decode("utf-8", "replace").strip()
                state.put("scanner-version", image, {"version": version})
            finally:
                run(["docker", "rm", "-f", version_name], check=False, timeout=15)
    info.update(exit_code=p.returncode, version=version, sha256=digest(p.stdout), output=str(output))
    write_json(output.with_suffix(".meta.json"), info)
    return info


def prepare(state, api, job_id):
    job = state.require("job", job_id)
    if job["status"] in ("complete", "skipped"):
        return job
    if job["status"] == "needs_analysis" and job["scans"] and all(s.get("status") == "complete" for s in job["scans"].values()):
        return job
    budget = Budget(state, job)
    root = job_dir(state, job)
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / "checkout"
    job.update(status="preparing", error=None)
    state.put("job", job_id, job)
    try:
        if not job.get("commit"):
            # Git transport pins HEAD without spending scarce REST API quota.
            refs = git("ls-remote", "--symref", f"https://github.com/{job['repo']}.git", "HEAD", timeout=budget.left(60)).decode()
            branch = re.search(r"^ref: refs/heads/([^\t\r\n]+)\tHEAD$", refs, re.M)
            commit = re.search(r"^([0-9a-f]{40,64})\tHEAD$", refs, re.M)
            if not branch or not commit:
                raise Stop("无法确定远端默认分支及 commit")
            job.update(commit=commit.group(1), branch=branch.group(1))
            state.put("job", job_id, job)
        if not (checkout / ".git").exists():
            checkout.mkdir(parents=True, exist_ok=True)
            git("init", str(checkout), timeout=budget.left(20))
            git("remote", "add", "origin", f"https://github.com/{job['repo']}.git", cwd=checkout)
        if git("remote", "get-url", "origin", cwd=checkout).decode().strip() != f"https://github.com/{job['repo']}.git":
            raise Stop("checkout 远端不匹配")
        try:
            head = git("rev-parse", "HEAD", cwd=checkout).decode().strip()
        except Stop:
            head = None
        if head != job["commit"]:
            if head is not None:
                raise Stop("已有 checkout 的 commit 不一致；保留现场，不覆盖")
            git("fetch", "--depth=1", "origin", job["commit"], cwd=checkout, timeout=budget.left(180))
            git("checkout", "--detach", job["commit"], cwd=checkout, timeout=budget.left(60))
        last = git("show", "-s", "--format=%cI", job["commit"], cwd=checkout).decode().strip()
        if stamp(last) < time.time() - state.config["active_days"] * 86400:
            job.update(status="skipped", error="默认分支最近没有提交")
            return job
        policy = []
        for relative in ("SECURITY.md", ".github/SECURITY.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "AGENTS.md"):
            path = checkout / relative
            if path.is_file() and not path.is_symlink():
                policy.append({"path": relative, "text": path.read_text(encoding="utf-8", errors="replace")[:60000]})
        write_json(root / "policies.json", {"untrusted": True, "documents": policy})
        # First page is context, never a claim of exhaustive duplicate checking.
        try:
            issues = api.get(f"repos/{job['repo']}/issues?state=open&per_page=100")
            write_json(root / "open-issues.json", {"coverage": "first-100", "items": issues})
            job["issue_context"] = "first-100"
        except Stop as exc:
            job["issue_context"] = "unavailable"
            write_json(root / "open-issues.json", {"coverage": "unavailable", "error": str(exc), "items": []})
        if not doctor()["docker_ready"]:
            raise Stop("Docker Linux 引擎不可用；源码已固定，可阅读分析，不能执行验证")
        with temp_dir(root, "scan-") as temp:
            snapshot(checkout, job["commit"], temp, state.config["max_source_mb"])
            for engine in ("semgrep", "osv"):
                if job["scans"].get(engine, {}).get("status") == "complete":
                    continue
                try:
                    result = scanner(state, temp, engine, root / f"{engine}.json", budget)
                    job["scans"][engine] = dict(result, status="complete")
                except Exception as exc:
                    job["scans"][engine] = {"status": "failed", "error": str(exc)}
                state.put("job", job_id, job)
        job["status"] = "needs_analysis"
        write_json(root / "handoff.json", {"job": job, "instruction": "由当前 Codex 会话阅读真实源码和告警，复现问题后编写 finding.json 与两个独立补丁。不得把告警直接当作漏洞。", "schema": "references/workflow.md"})
    except Exception as exc:
        job.update(status="blocked", error=str(exc))
    finally:
        budget.save()
    return job


def patch_paths(data):
    if any(marker in data for marker in (b"GIT binary patch", b"old mode ", b"new mode ", b"new file mode 100755", b"new file mode 120000")):
        raise Stop("首版不自动发布二进制、符号链接或文件权限变更")
    # Git prefixes paths relative to the current subdirectory of a worktree.
    # Parse from its root so stored paths remain relative to the target repository.
    try:
        root = Path(git("rev-parse", "--show-toplevel").decode("utf-8").strip())
    except Stop:
        root = None
    # Git itself parses patch paths; no shell interpretation or path-regex patch parsing.
    p = run(["git", "apply", "--numstat", "-z", "-"], cwd=root, input=data)
    paths = []
    for record in p.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        parts = record.split("\t", 2)
        if len(parts) != 3:
            raise Stop("不支持的补丁路径格式")
        path = parts[2]
        if not path or path.startswith(("/", "\\")) or any(p in ("..", ".git") for p in Path(path).parts) or ":" in path or "\\" in path:
            raise Stop("补丁路径不安全")
        paths.append(path)
    if not paths:
        raise Stop("补丁为空")
    return paths


def add_finding(state, job_id, spec_path):
    job = state.require("job", job_id)
    if not job.get("commit"):
        raise Stop("先 prepare 固定源码 commit")
    spec_path = Path(spec_path).resolve()
    spec = read_json(spec_path)
    for key in ("title", "kind", "location", "identity", "description", "image", "setup", "existing_tests", "fix_patch", "pr_body"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise Stop(f"finding 缺少非空字段 {key}")
    if spec["kind"] not in ("bug", "security", "dependency"):
        raise Stop("kind 必须是 bug / security / dependency")
    if spec["kind"] != "dependency":
        for key in ("regression_patch", "regression_test", "failure_marker"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise Stop(f"finding 缺少 {key}")
    else:
        for key in ("advisory", "package", "ecosystem", "affected_version", "fixed_version"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise Stop(f"依赖 finding 缺少 {key}")
    if len(spec["title"]) > 200 or "\n" in spec["title"]:
        raise Stop("标题过长或包含换行")
    fingerprint = digest((job["repo"].lower() + "\0" + spec["kind"] + "\0" + spec["location"] + "\0" + spec["identity"]).encode())
    finding_id = digest((fingerprint + job["commit"]).encode())[:20]
    existing = state.get("finding", finding_id)
    if existing and (existing["job"] != job_id or existing.get("published") or existing.get("attempts", 0) >= state.config["max_attempts"]):
        raise Stop("该问题已存在、已发布或已达到验证上限")
    root = state.root / "findings" / finding_id
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for field in ("fix_patch", "regression_patch", "pr_body"):
        if field not in spec:
            continue
        source = (spec_path.parent / spec[field]).resolve()
        if not source.is_relative_to(spec_path.parent) or source.is_symlink():
            raise Stop("输入文件必须在 finding.json 同目录或子目录中")
        data = source.read_bytes()
        if field.endswith("patch"):
            files[field] = patch_paths(data)
        dest = root / {"fix_patch": "fix.patch", "regression_patch": "regression.patch", "pr_body": "pr-body.md"}[field]
        dest.write_bytes(data)
        spec[field] = dest.name
    if spec["kind"] != "dependency":
        if set(files["fix_patch"]) & set(files["regression_patch"]):
            raise Stop("修复补丁与回归测试补丁不得修改同一文件；请拆分源码修复与测试")
        for path in files["regression_patch"]:
            if not re.search(r"(^|/)(tests?|__tests__|specs?)(/|$)|(^|/)(test_[^/]+|[^/]+(_test|\.test|\.spec)\.[^/]+)$", path, re.I):
                raise Stop("回归补丁只能修改测试文件；其他命名需先在技能中审查后扩展识别")
    record = {"id": finding_id, "fingerprint": fingerprint, "job": job_id, "repo": job["repo"], "commit": job["commit"],
              "spec": spec, "status": "candidate", "attempts": (existing or {}).get("attempts", 0),
              "sensitive": spec["kind"] == "security", "created": now(), "paths": files}
    write_json(root / "finding.json", spec)
    state.put("finding", finding_id, record)
    return record


def apply_patch(source, path):
    run(["git", "apply", "--check", str(path)], cwd=source)
    run(["git", "apply", str(path)], cwd=source)


class DockerTests:
    def __init__(self, state, budget, image):
        self.state, self.budget = state, budget
        self.image = pinned_image(state, image, budget)

    def phase(self, source, setup, commands, output):
        """Source and caches live only in tmpfs. Tests execute after network disconnect."""
        name = "ossfix-test-" + ident()
        c = self.state.config
        args = ["docker", "create", *docker_limits(self.state, name), "--network", "bridge",
                "--tmpfs", f"/work:rw,nosuid,nodev,size={c['work_size']},uid=65534,gid=65534",
                "--tmpfs", "/home/runner:rw,nosuid,nodev,size=2g,uid=65534,gid=65534",
                "--env", "HOME=/home/runner", "--env", "CI=true", "--workdir", "/work",
                "--entrypoint", "/bin/sh", self.image, "-c", "while :; do sleep 3600; done"]
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        records = []
        try:
            run(args, timeout=self.budget.left(30))
            run(["docker", "start", name], timeout=self.budget.left(20))
            payload = source_tar(source, c["max_source_mb"])
            run(["docker", "exec", "-i", name, "sh", "-c", "mkdir -p /work/repo && tar -xf - -C /work/repo"],
                input=payload, timeout=self.budget.left(60))
            commands = [("setup", setup)] + commands
            for index, (label, command) in enumerate(commands):
                if index == 1:
                    run(["docker", "network", "disconnect", "bridge", name], timeout=self.budget.left(20))
                    networks = json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name], timeout=self.budget.left(15)).stdout)
                    if networks:
                        raise Stop("验证容器仍有网络连接")
                p = run(["docker", "exec", "--workdir", "/work/repo", name, "sh", "-lc", command],
                        check=False, timeout=self.budget.left())
                log = p.stdout + b"\n--- stderr ---\n" + p.stderr
                (output / f"{label}.log").write_bytes(log)
                record = {"label": label, "command": command, "exit": p.returncode,
                          "network": "bridge" if label == "setup" else "disconnected",
                          "log_sha256": digest(log), "log": str(output / f"{label}.log")}
                records.append(record)
                if label == "setup" and p.returncode != 0:
                    raise Stop("依赖安装失败；不把安装失败计为复现成功")
        finally:
            run(["docker", "rm", "-f", name], check=False, timeout=15)
            write_json(output / "commands.json", records)
        return records


def osv_packages(path):
    data = read_json(path)
    if "results" not in data or not isinstance(data["results"], list):
        raise Stop("OSV 输出结构不受支持")
    result = []
    for group in data["results"]:
        for package in group.get("packages", []):
            info = package.get("package", {})
            vulnerabilities = set()
            for v in package.get("vulnerabilities", []):
                vulnerabilities.add(v["id"])
                vulnerabilities.update(v.get("aliases", []))
            result.append((info.get("name"), info.get("ecosystem"), info.get("version"), vulnerabilities))
    return result


def dependency_gate(before, after, spec):
    prior = [p for p in osv_packages(before) if p[:3] == (spec["package"], spec["ecosystem"], spec["affected_version"])]
    fixed = [p for p in osv_packages(after) if p[:3] == (spec["package"], spec["ecosystem"], spec["fixed_version"])]
    if not prior or not any(spec["advisory"] in p[3] for p in prior):
        raise Stop("依赖版本或漏洞公告未在修复前扫描中确认")
    if not fixed or any(spec["advisory"] in p[3] for p in osv_packages(after)):
        raise Stop("修复后未确认目标依赖版本，或目标告警仍存在")


def artifact_hashes(root):
    return {str(p.relative_to(root)).replace("\\", "/"): digest(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file() and p.name not in ("verification.json", "private-report.md")}


def verify(state, finding_id, runner_factory=DockerTests, scan=scanner):
    finding = state.require("finding", finding_id)
    job = state.require("job", finding["job"])
    if finding.get("published"):
        raise Stop("已发布的问题不能重新验证")
    if finding["attempts"] >= state.config["max_attempts"]:
        raise Stop("该问题已达到两轮验证上限")
    ready = [f for f in state.all("finding") if f["id"] != finding_id and f["status"] in ("verified", "published")
             and state.require("job", f["job"])["batch"] == job["batch"]]
    if len(ready) >= state.config["max_prs"]:
        raise Stop("本批次已达到 PR 准备上限")
    budget = Budget(state, job)
    budget.left()
    finding.update(status="verifying", attempts=finding["attempts"] + 1, verification=None)
    state.put("finding", finding_id, finding)
    root = state.root / "findings" / finding_id
    evidence = root / f"attempt-{finding['attempts']}"
    evidence.mkdir(parents=True, exist_ok=True)
    spec = finding["spec"]
    result = {"commit": finding["commit"], "started": now(), "passed": False, "phases": {}}
    try:
        runner = runner_factory(state, budget, spec["image"])
        result["image"] = runner.image
        with temp_dir(job_dir(state, job), "verify-") as temp:
            before, fixed = temp / "before", temp / "fixed"
            snapshot(job_dir(state, job) / "checkout", finding["commit"], before, state.config["max_source_mb"])
            snapshot(job_dir(state, job) / "checkout", finding["commit"], fixed, state.config["max_source_mb"])
            baseline = runner.phase(before, spec["setup"], [("existing", spec["existing_tests"])], evidence / "baseline")
            result["phases"]["baseline"] = baseline
            if baseline[-1]["exit"] != 0:
                raise Stop("基线测试失败，不能提交")
            apply_patch(fixed, root / "fix.patch")
            if spec["kind"] == "dependency":
                result["osv_before"] = scan(state, before, "osv", evidence / "osv-before.json", budget)
                result["osv_after"] = scan(state, fixed, "osv", evidence / "osv-after.json", budget)
                dependency_gate(evidence / "osv-before.json", evidence / "osv-after.json", spec)
                commands = [("existing", spec["existing_tests"])]
            else:
                apply_patch(before, root / "regression.patch")
                apply_patch(fixed, root / "regression.patch")
                broken = runner.phase(before, spec["setup"], [("regression", spec["regression_test"])], evidence / "before")
                result["phases"]["before"] = broken
                log = Path(broken[-1]["log"]).read_text(encoding="utf-8", errors="replace")
                if broken[-1]["exit"] != 1 or spec["failure_marker"] not in log:
                    raise Stop("修复前未出现预期断言失败（要求退出码 1 及 failure_marker）")
                if re.search(r"ModuleNotFoundError|ImportError|command not found|No tests (ran|found)", log):
                    raise Stop("回归失败包含环境/测试发现错误，不能作为漏洞复现证据")
                commands = [("regression", spec["regression_test"]), ("existing", spec["existing_tests"])]
            passed = runner.phase(fixed, spec["setup"], commands, evidence / "after")
            result["phases"]["after"] = passed
            if any(p["exit"] != 0 for p in passed):
                raise Stop("修复后测试未全部通过")
        result.update(passed=True, finished=now())
        result["artifacts"] = artifact_hashes(root)
        result["spec_sha256"] = digest(json.dumps(spec, sort_keys=True).encode())
        finding.update(status="private_ready" if finding["sensitive"] else "verified", verification=result, error=None)
        if finding["sensitive"]:
            (root / "private-report.md").write_text(f"# 私密安全报告（尚未发送）\n\n项目：{finding['repo']}\ncommit：{finding['commit']}\n\n{spec['title']}\n\n{spec['description']}\n\n位置：{spec['location']}\n\n验证详情见 verification.json；请按项目 SECURITY.md 选择私密渠道。未经单独授权不得发送。\n", encoding="utf-8")
    except Exception as exc:
        result["error"] = str(exc)
        finding.update(status="rejected", error=str(exc), verification=None)
    finally:
        write_json(root / "verification.json", result)
        state.put("finding", finding_id, finding)
        budget.save()
    return finding


def validate_evidence(state, finding):
    root = state.root / "findings" / finding["id"]
    evidence = finding.get("verification")
    if finding["status"] not in ("verified", "published") or not evidence or not evidence.get("passed"):
        raise Stop("缺少脚本生成的通过证据")
    if digest(json.dumps(finding["spec"], sort_keys=True).encode()) != evidence["spec_sha256"]:
        raise Stop("验证后的 finding 配置发生变化")
    if artifact_hashes(root) != evidence["artifacts"]:
        raise Stop("验证后的补丁、正文或测试证据发生变化，必须重新验证")
    if evidence["commit"] != finding["commit"]:
        raise Stop("验证 commit 不匹配")
