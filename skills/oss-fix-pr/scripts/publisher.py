"""Explicitly enabled, evidence-gated GitHub publisher; never merges a PR."""
from __future__ import annotations

import base64
from pathlib import Path
import time
from urllib.parse import quote, urlencode

from core import Stop, git, now, temp_dir
from pipeline import apply_patch, job_dir, snapshot, validate_evidence


def existing_pr(api, repo, owner, branch, marker):
    pulls = api.pages(f"repos/{repo}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}")
    matching = [p for p in pulls if marker in (p.get("body") or "")]
    if len(matching) > 1:
        raise Stop("找到多个同指纹 PR，停止自动操作")
    if pulls and not matching:
        raise Stop("相同分支已有其他 PR，不能重复提交")
    return matching[0] if matching else None


def preflight(state, finding, execute):
    if finding["sensitive"] or finding["spec"]["kind"] == "security":
        raise Stop("未公开安全问题不能创建公开 fork、分支或 PR；请走私密披露")
    validate_evidence(state, finding)
    if execute:
        cfg = state.config
        reviewed = state.get("batch", cfg.get("reviewed_batch"))
        if cfg["publish_mode"] != "auto" or not reviewed or not reviewed.get("trial"):
            raise Stop("仍处于试运行；首批质量审核后才可配置 publish_mode=auto 和 reviewed_batch")
        trial_findings = [f for f in state.all("finding") if state.require("job", f["job"])["batch"] == reviewed["id"]
                          and f["status"] in ("verified", "published") and not f["sensitive"]]
        if not trial_findings:
            raise Stop("被审核的试运行批次没有经过验证的公开修复")


def publish(state, api, finding_id, execute=False):
    finding = state.require("finding", finding_id)
    preflight(state, finding, execute)
    job = state.require("job", finding["job"])
    root = state.root / "findings" / finding_id
    branch = "oss-fix-pr/" + finding_id
    marker = "<!-- oss-fix-pr:" + finding["fingerprint"] + " -->"
    if not execute:
        return {"mode": "dry-run", "finding": finding_id, "branch": branch, "remote_writes": 0,
                "body": str(root / "pr-body.md"), "note": "已验证补丁；未创建 fork、推送或 PR"}
    if not api.auth:
        raise Stop("自动发布需要 gh auth login；不会请求或打印你的 token")
    owner = api.get("user")["login"]
    repo = finding["repo"]
    fork = owner + "/" + repo.split("/")[1]
    if owner.lower() == repo.split("/")[0].lower():
        raise Stop("当前账号拥有目标仓库；首版不向上游直接写分支，请使用独立测试账号/仓库")
    found = existing_pr(api, repo, owner, branch, marker)
    if found:
        finding.update(status="published", published={"url": found["html_url"], "state": found["state"], "at": now()})
        state.put("finding", finding_id, finding)
        return dict(finding["published"], reused=True)
    upstream = api.get(f"repos/{repo}/commits/{quote(job['branch'], safe='')}")
    if upstream["sha"] != finding["commit"]:
        raise Stop("上游默认分支已变化；旧证据失效。新建批次在新 commit 上重新验证")
    metadata = api.get(f"repos/{repo}")
    if metadata.get("archived") or metadata.get("disabled") or metadata.get("private"):
        raise Stop("目标仓库状态已改变，停止公开提交")
    if metadata["default_branch"] != job["branch"]:
        raise Stop("上游默认分支已切换，需重新验证")
    q = f'repo:{repo} is:pr in:title "{finding["spec"]["title"]}"'
    duplicates = api.get("search/issues?" + urlencode({"q": q, "per_page": 100}))
    if duplicates.get("incomplete_results") or duplicates.get("total_count", 0):
        raise Stop("存在同标题/相关 PR 或搜索结果不完整，请先完成重复审查")
    # Persist intent before the first mutation, enabling timeout recovery.
    receipt = state.get("publication", finding_id) or {"finding": finding_id, "fork": fork, "branch": branch}
    if receipt["fork"] != fork:
        raise Stop("发布账号发生变化，不能接续旧的发布事务")
    state.put("publication", finding_id, receipt)
    try:
        fork_meta = api.get(f"repos/{fork}")
    except Stop as exc:
        # Only 404 is absence. Network/auth failures must not cause a fork retry.
        if "404" not in str(exc):
            raise
        receipt["stage"] = "fork_requested"
        state.put("publication", finding_id, receipt)
        try:
            api.mutation(f"repos/{repo}/forks", {"default_branch_only": True})
        except Stop:
            # The request may have succeeded; read once, never blindly reissue it.
            pass
        fork_meta = None
        for _ in range(6):
            try:
                fork_meta = api.get(f"repos/{fork}")
                break
            except Stop as exc:
                if "404" not in str(exc):
                    raise
                time.sleep(2)
        if not fork_meta:
            raise Stop("fork 尚未就绪，稍后重新 publish 会先查询状态")
    if not fork_meta.get("fork") or (fork_meta.get("parent") or {}).get("full_name", "").lower() != repo.lower():
        raise Stop("账号下同名仓库不是目标仓库的 fork，停止操作")
    # Reuse exact objects after an interrupted publish. No force-push or merge.
    if not receipt.get("commit"):
        tree_entries = []
        modes = {}
        listing = git("ls-tree", "-r", "-z", finding["commit"], cwd=job_dir(state, job) / "checkout")
        for record in listing.decode("utf-8").split("\0"):
            if record:
                meta, path = record.split("\t", 1)
                modes[path] = meta.split()[0]
        with temp_dir(job_dir(state, job), "publish-") as temp:
            snapshot(job_dir(state, job) / "checkout", finding["commit"], temp, state.config["max_source_mb"])
            apply_patch(temp, root / "fix.patch")
            if "regression_patch" in finding["spec"]:
                apply_patch(temp, root / "regression.patch")
            paths = sorted({p for group in finding["paths"].values() for p in group})
            for relative in paths:
                path = Path(temp) / relative
                if not path.exists():
                    tree_entries.append({"path": relative, "mode": "100644", "type": "blob", "sha": None})
                    continue
                if not path.is_file() or path.is_symlink():
                    raise Stop("发布补丁包含非普通文件")
                blob = api.mutation(f"repos/{fork}/git/blobs", {"content": base64.b64encode(path.read_bytes()).decode(), "encoding": "base64"})
                tree_entries.append({"path": relative, "mode": modes.get(relative, "100644"), "type": "blob", "sha": blob["sha"]})
        base_tree = upstream["commit"]["tree"]["sha"]
        tree = api.mutation(f"repos/{fork}/git/trees", {"base_tree": base_tree, "tree": tree_entries})
        commit = api.mutation(f"repos/{fork}/git/commits", {"message": finding["spec"]["title"], "tree": tree["sha"], "parents": [finding["commit"]]})
        receipt.update(commit=commit["sha"], tree=tree["sha"], stage="objects_created")
        state.put("publication", finding_id, receipt)
    ref_endpoint = f"repos/{fork}/git/ref/heads/{branch}"
    try:
        ref = api.get(ref_endpoint)
    except Stop as exc:
        if "404" not in str(exc):
            raise
        try:
            api.mutation(f"repos/{fork}/git/refs", {"ref": "refs/heads/" + branch, "sha": receipt["commit"]})
        except Stop:
            pass
        ref = api.get(ref_endpoint)
    if ref["object"]["sha"] != receipt["commit"]:
        raise Stop("远端分支已被其他操作修改；不会覆盖")
    # Recheck base immediately before creating the visible PR.
    if api.get(f"repos/{repo}/commits/{quote(job['branch'], safe='')}")["sha"] != finding["commit"]:
        raise Stop("上传期间上游变化；保留 fork 分支但不创建 PR")
    body = (root / "pr-body.md").read_text(encoding="utf-8") + "\n\n" + marker + "\n"
    receipt["stage"] = "pr_requested"
    state.put("publication", finding_id, receipt)
    try:
        pr = api.mutation(f"repos/{repo}/pulls", {"title": finding["spec"]["title"], "head": owner + ":" + branch,
                          "base": job["branch"], "body": body, "draft": False, "maintainer_can_modify": True})
    except Stop as exc:
        pr = existing_pr(api, repo, owner, branch, marker)
        if not pr:
            raise Stop("PR 请求结果不确定；已保留事务，下次先查远端，禁止盲目重复创建") from exc
    finding.update(status="published", published={"url": pr["html_url"], "state": pr.get("state", "open"), "at": now()})
    state.put("finding", finding_id, finding)
    receipt.update(stage="published", url=pr["html_url"])
    state.put("publication", finding_id, receipt)
    state.event("published", {"finding": finding_id, "url": pr["html_url"]})
    return dict(finding["published"], attach_artifact=True)
