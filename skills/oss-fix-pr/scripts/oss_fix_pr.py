#!/usr/bin/env python3
"""Codex skill helper. Analysis and patch authoring remain in the current Codex chat."""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import sys

from core import GitHub, State, Stop, default_state_path, discover, doctor, now
from pipeline import add_finding, prepare, verify
from publisher import publish


def report(state, batch_id):
    batch = state.require("batch", batch_id)
    lines = [f"# 开源项目试运行报告：{batch_id}", "", f"生成时间：{now()}", "",
             f"批次状态：{batch['status']}；候选数：{len(batch['candidates'])}；待续搜索分片：{len(batch['queue'])}", "",
             "说明：报告只反映本次可验证的覆盖范围，不代表仓库没有缺陷。", ""]
    if batch.get("error"):
        lines += [f"发现阶段：{batch['error']}", ""]
    lines += ["| 项目 | Star | 热度依据 | 状态 | 执行秒数 |", "|---|---:|---|---|---:|"]
    for job_id in batch["jobs"]:
        job = state.require("job", job_id)
        meta = job["metadata"]
        heat = meta["heat_info"]
        metric = f"实测 {heat['stars_per_day']} star/日" if heat["kind"] == "measured" else "活跃度 + 总 star（非增长量）"
        lines.append(f"| {job['repo']} | {meta['stargazers_count']} | {metric} | {job['status']} | {job['spent']} |")
    for job_id in batch["jobs"]:
        job = state.require("job", job_id)
        lines += ["", f"## {job['repo']}", "", f"固定 commit：{job.get('commit', '尚未获取')}"]
        if job.get("error"):
            # Operational failures only; raw scanner content never enters this report.
            lines += ["", f"执行限制：{job['error']}"]
        for engine, info in job["scans"].items():
            lines += ["", f"{engine}：{info['status']}；覆盖标记：{info.get('coverage', '未完成')}。"]
        fs = [f for f in state.all("finding") if f["job"] == job_id]
        private = [f for f in fs if f["sensitive"]]
        if private:
            lines += ["", f"私密安全候选：{len(private)} 项，详情仅保存在本地私密记录，不进入公开报告。"]
        for f in fs:
            if f["sensitive"]:
                continue
            lines += ["", f"- {f['spec']['title']}：{f['status']}（{f['id']}）"]
            if f.get("published"):
                lines.append(f"  PR：{f['published']['url']}")
        if not fs:
            lines += ["", "尚无经复现确认的问题；不得据此生成 PR。"]
    lines += ["", *batch.get("notes", [])]
    path = state.root / "reports" / (batch_id + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"report": str(path), "batch": batch_id}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="热门开源项目扫描与修复：默认零远端写入")
    parser.add_argument("--state", default=str(default_state_path()), help="运行记录与配置目录；默认保存在用户本地数据目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("discover")
    for name in ("prepare", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--batch", required=True)
        if name == "resume":
            p.add_argument("--discovery-only", action="store_true", help="续跑保留的搜索游标，暂不准备项目")
    p = sub.add_parser("finding")
    p.add_argument("--job", required=True)
    p.add_argument("--spec", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--finding", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--finding", required=True)
    p.add_argument("--execute", action="store_true", help="仍要求配置 auto 且记录已审核试运行批次")
    p = sub.add_parser("report")
    p.add_argument("--batch", required=True)
    p = sub.add_parser("close-job")
    p.add_argument("--job", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("status")
    p.add_argument("--batch")
    args = parser.parse_args(argv)
    state = State(args.state)
    try:
        with contextlib.nullcontext() if args.command in ("doctor", "status") else state.lock():
            if args.command == "doctor":
                result = doctor()
                result["state_dir"] = str(state.root)
            elif args.command == "discover":
                result = discover(state, GitHub())
                report(state, result["id"])
            elif args.command in ("prepare", "resume"):
                api = GitHub()
                batch = state.require("batch", args.batch)
                if args.command == "resume" and args.discovery_only and batch["queue"]:
                    batch["status"] = "discovering"
                    state.put("batch", args.batch, batch)
                if batch["status"] in ("discovering", "discovery_paused"):
                    batch = discover(state, api, args.batch)
                if args.command == "resume" and args.discovery_only:
                    result = {"batch": batch["id"], "status": batch["status"], "remaining": len(batch["queue"])}
                elif batch["status"] in ("discovering", "discovery_paused"):
                    result = {"batch": batch["id"], "status": batch["status"], "error": batch.get("error")}
                else:
                    result = [prepare(state, api, j) for j in batch["jobs"]]
                report(state, args.batch)
            elif args.command == "finding":
                result = add_finding(state, args.job, args.spec)
            elif args.command == "verify":
                result = verify(state, args.finding)
            elif args.command == "publish":
                result = publish(state, GitHub(), args.finding, args.execute)
            elif args.command == "report":
                result = report(state, args.batch)
            elif args.command == "close-job":
                result = state.require("job", args.job)
                result.update(status="complete", conclusion=args.reason)
                state.put("job", args.job, result)
            else:
                result = {"batches": [{k: b[k] for k in ("id", "status", "created", "jobs")} for b in state.all("batch")],
                          "jobs": [j for j in state.all("job") if not args.batch or j["batch"] == args.batch],
                          "findings": [{"id": f["id"], "job": f["job"], "status": f["status"], "sensitive": f["sensitive"]} for f in state.all("finding")]}
        # Large candidate pools remain in SQLite; terminal output contains only useful status.
        if isinstance(result, dict) and "candidates" in result:
            result = {k: v for k, v in result.items() if k not in ("candidates", "queue")}
        if isinstance(result, dict) and result.get("sensitive"):
            result = {k: result[k] for k in ("id", "job", "status", "sensitive")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Stop, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        state.db.close()


if __name__ == "__main__":
    sys.exit(main())
