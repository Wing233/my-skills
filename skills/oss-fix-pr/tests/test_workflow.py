"""Regression tests. Only self-authored fixtures execute on the host; never target repos."""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import (DEFAULTS, GitHub, State, Stop, default_state_path, digest, discover, eligible, git, now, rank_repo,
                  run, temp_dir, write_json)
from pipeline import (Budget, DockerTests, add_finding, apply_patch, artifact_hashes, dependency_gate,
                      docker_limits, patch_paths, prepare, scanner, source_tar, validate_evidence, verify)
from publisher import publish
from oss_fix_pr import report


def candidate(name="org/repo", stars=2000, language="Python", topics=None):
    return {"full_name": name, "stargazers_count": stars, "pushed_at": now(), "language": language,
            "description": "Useful tools", "topics": topics or ["ai-agent"], "license": {"spdx_id": "MIT"},
            "default_branch": "main", "fork": False, "archived": False, "private": False}


class SearchAPI:
    def __init__(self, items, count=None):
        self.items, self.count, self.calls = items, count, []

    def get(self, endpoint):
        self.calls.append(endpoint)
        return {"items": self.items, "total_count": self.count if self.count is not None else len(self.items), "incomplete_results": False}


class HostFixtureRunner:
    """Injected test double, unavailable through CLI; executes only these tiny authored fixtures."""
    def __init__(self, state, budget, image):
        self.image = "fixture@sha256:test"

    def phase(self, source, setup, commands, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        results = [{"label": "setup", "exit": 0, "network": "test-double"}]
        for label, command in commands:
            args = shlex.split(command)
            assert args[0] == "python"
            p = run([sys.executable, *args[1:]], cwd=source, check=False)
            log = p.stdout + p.stderr
            path = output / (label + ".log")
            path.write_bytes(log)
            results.append({"label": label, "exit": p.returncode, "command": command, "log": str(path), "log_sha256": digest(log)})
        write_json(output / "commands.json", results)
        return results


class RemoteAPI:
    auth = True
    def __init__(self, finding, job):
        self.finding, self.job = finding, job
        self.writes, self.prs, self.ref = [], [], None
        self.fork = False
        self.timeout_pr = False
        self.duplicate = False
        self.changed = False

    def get(self, endpoint):
        if endpoint == "user":
            return {"login": "contributor"}
        if endpoint.startswith("search/issues?"):
            return {"total_count": int(self.duplicate), "items": []}
        if endpoint.endswith("/commits/main"):
            return {"sha": "different" if self.changed else self.finding["commit"], "commit": {"tree": {"sha": "base-tree"}}}
        if "/git/ref/" in endpoint:
            if self.ref is None:
                raise Stop("HTTP 404")
            return {"object": {"sha": self.ref}}
        if endpoint == "repos/contributor/repo":
            if not self.fork:
                raise Stop("HTTP 404")
            return {"fork": True, "parent": {"full_name": "org/repo"}}
        if endpoint == "repos/org/repo":
            return {"default_branch": "main", "private": False, "archived": False}
        raise AssertionError(endpoint)

    def pages(self, endpoint, limit=10):
        return self.prs

    def mutation(self, endpoint, payload):
        self.writes.append((endpoint, payload))
        if endpoint.endswith("/forks"):
            self.fork = True
            return {}
        if endpoint.endswith("/git/blobs"):
            return {"sha": digest(payload["content"].encode())}
        if endpoint.endswith("/git/trees"):
            return {"sha": "new-tree"}
        if endpoint.endswith("/git/commits"):
            return {"sha": "new-commit"}
        if endpoint.endswith("/git/refs"):
            self.ref = payload["sha"]
            return {}
        if endpoint.endswith("/pulls"):
            pr = {"html_url": "https://github.com/org/repo/pull/1", "body": payload["body"], "state": "open"}
            self.prs.append(pr)
            if self.timeout_pr:
                raise Stop("request timeout")
            return pr
        raise AssertionError(endpoint)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # tempfile.mkdtemp uses mode 0o700, which sandboxed hosts refuse to write
        # into; the suite must therefore use the same scratch helper as the skill.
        self.stack = ExitStack()
        self.root = self.stack.enter_context(temp_dir(Path(tempfile.gettempdir()), "ossfix-tests-"))
        self.state = State(self.root / "state")

    def tearDown(self):
        self.state.db.close()
        self.stack.close()

    def fixture(self, kind="bug"):
        job = {"id": "job1", "batch": "batch1", "repo": "org/repo", "branch": "main", "spent": 0,
               "status": "needs_analysis", "created": now(), "scans": {}, "metadata": dict(candidate(), heat_info={"kind": "proxy"})}
        checkout = self.state.root / "jobs/job1/checkout"
        checkout.mkdir(parents=True)
        git("init", str(checkout))
        (checkout / "tests").mkdir()
        (checkout / "helper.py").write_text("def first(values):\n    return values[0]\n", encoding="utf-8")
        prefix = "import unittest\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nfrom helper import first\n"
        (checkout / "tests/test_basic.py").write_text(prefix + "class Basic(unittest.TestCase):\n    def test_nonempty(self):\n        self.assertEqual(first([1]), 1)\n", encoding="utf-8")
        git("add", ".", cwd=checkout)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture", cwd=checkout)
        job["commit"] = git("rev-parse", "HEAD", cwd=checkout).decode().strip()
        self.state.put("job", job["id"], job)
        self.state.put("batch", "batch1", {"id": "batch1", "created": now(), "status": "selected", "trial": True, "jobs": ["job1"], "candidates": {}, "queue": [], "notes": []})
        inputs = self.root / "inputs"
        inputs.mkdir()
        original = (checkout / "helper.py").read_text()
        fixed = original.replace("return values[0]", "return values[0] if values else None")
        patch_text = "".join(difflib.unified_diff(original.splitlines(True), fixed.splitlines(True), "a/helper.py", "b/helper.py"))
        (inputs / "fix.patch").write_text(patch_text, encoding="utf-8")
        regression = prefix + "class Regression(unittest.TestCase):\n    def test_empty(self):\n        self.assertIsNone(first([]))\n"
        regression_patch = "".join(difflib.unified_diff([], regression.splitlines(True), "/dev/null", "b/tests/test_regression.py"))
        (inputs / "regression.patch").write_text(regression_patch, encoding="utf-8")
        (inputs / "pr-body.md").write_text("Handle empty lists. Regression and existing tests pass after the fix.\n", encoding="utf-8")
        spec = {"title": "Fix empty input handling", "kind": kind, "location": "helper.py:first", "identity": "empty-input",
                "description": "Self-authored fixture; no remote target.", "image": "python:3.12-slim", "setup": "true",
                "existing_tests": "python -m unittest discover -s tests -p test_basic.py -v",
                "regression_test": "python -m unittest discover -s tests -p test_regression.py -v", "failure_marker": "test_empty",
                "fix_patch": "fix.patch", "regression_patch": "regression.patch", "pr_body": "pr-body.md"}
        write_json(inputs / "finding.json", spec)
        finding = add_finding(self.state, job["id"], inputs / "finding.json")
        return job, finding

    def verified(self, kind="bug"):
        job, finding = self.fixture(kind)
        finding = verify(self.state, finding["id"], runner_factory=HostFixtureRunner)
        self.assertEqual(finding["status"], "private_ready" if kind == "security" else "verified", finding.get("error"))
        return job, finding

    def allow_publish(self):
        self.state.config.update(publish_mode="auto", reviewed_batch="batch1")

    def test_filter_excludes_archived_private_fork_and_no_license(self):
        good = candidate()
        self.assertTrue(eligible(good, DEFAULTS))
        for field in ("archived", "private", "fork", "disabled"):
            self.assertFalse(eligible(dict(good, **{field: True}), DEFAULTS))
        for license in (None, {"spdx_id": "NOASSERTION"}):
            self.assertFalse(eligible(dict(good, license=license), DEFAULTS))
        self.assertFalse(eligible(dict(good, stargazers_count=999), DEFAULTS))
        self.assertFalse(eligible(dict(good, pushed_at="2020-01-01T00:00:00Z"), DEFAULTS))

    def test_default_state_is_workspace_scoped_and_outside_checkout(self):
        path = default_state_path()
        self.assertNotEqual(path, Path.cwd() / ".oss-fix-pr")
        self.assertFalse(path.is_relative_to(Path.cwd()))
        self.assertEqual(len(path.name), 20)

    def test_heat_is_proxy_until_actual_history_exists(self):
        at = now()
        first = rank_repo(self.state, candidate(), at)
        self.assertEqual(first["heat_info"]["kind"], "proxy")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat(timespec="seconds")
        self.state.db.execute("INSERT INTO snapshots VALUES(?,?,?)", ("org/repo", old, 1800))
        self.state.db.commit()
        second = rank_repo(self.state, candidate(), at)
        self.assertEqual(second["heat_info"]["kind"], "measured")
        self.assertAlmostEqual(second["heat_info"]["stars_per_day"], 100, delta=1)

    def test_discovery_deduplicates_languages_and_topics(self):
        api = SearchAPI([candidate("o/" + str(i)) for i in range(8)])
        batch = discover(self.state, api)
        self.assertEqual(len(batch["jobs"]), 5)
        self.assertEqual(len(batch["candidates"]), 8)
        batch["status"] = "discovering"
        self.state.put("batch", batch["id"], batch)
        again = discover(self.state, api, batch["id"])
        self.assertEqual(len(again["jobs"]), 5)

    def test_splits_large_search_and_keeps_cursor(self):
        self.state.config.update(max_requests=1, topics=[], languages=[])
        batch = discover(self.state, SearchAPI([candidate(stars=10000)], count=2500))
        self.assertEqual(len(batch["queue"]), 2)
        self.assertEqual(batch["queue"][0]["lo"], 500_000_501)
        self.assertEqual(batch["queue"][0]["hi"], 1_000_000_000)
        self.assertEqual(batch["queue"][1]["hi"], 500_000_500)

    def test_search_pagination_cursor(self):
        self.state.config.update(max_requests=1, topics=[], languages=[])
        batch = discover(self.state, SearchAPI([candidate()], count=150))
        self.assertEqual(batch["queue"][0]["page"], 2)

    def test_rate_limit_preserves_current_page_for_resume(self):
        class Limited:
            def get(self, endpoint):
                raise Stop("rate limit")
        batch = discover(self.state, Limited())
        self.assertEqual(batch["status"], "discovery_paused")
        cursor = batch["queue"][0].copy()
        self.state.config["max_requests"] = 1
        api = SearchAPI([candidate()])
        done = discover(self.state, api, batch["id"])
        q = parse_qs(urlparse(api.calls[0]).query)["q"][0]
        self.assertIn(cursor["lane"], q)
        self.assertEqual(done["status"], "selected")

    def test_incomplete_search_is_not_accepted(self):
        api = SearchAPI([])
        api.get = lambda _: {"incomplete_results": True, "items": [], "total_count": 0}
        batch = discover(self.state, api)
        self.assertEqual(batch["status"], "discovery_paused")
        self.assertGreater(len(batch["queue"]), 0)

    def test_real_fixture_red_green_and_baseline(self):
        _, f = self.verified()
        phases = f["verification"]["phases"]
        self.assertEqual(phases["baseline"][-1]["exit"], 0)
        self.assertEqual(phases["before"][-1]["exit"], 1)
        self.assertEqual(phases["after"][-1]["exit"], 0)
        validate_evidence(self.state, f)

    def test_false_positive_cannot_pass(self):
        _, f = self.fixture()
        f["spec"]["regression_test"] = f["spec"]["existing_tests"]
        self.state.put("finding", f["id"], f)
        result = verify(self.state, f["id"], runner_factory=HostFixtureRunner)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("预期断言", result["error"])

    def test_baseline_failure_rejected(self):
        _, f = self.fixture()
        f["spec"]["existing_tests"] = "python missing_test.py"
        self.state.put("finding", f["id"], f)
        result = verify(self.state, f["id"], runner_factory=HostFixtureRunner)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("基线", result["error"])

    def test_missing_toolchain_rejected_not_verified(self):
        _, f = self.fixture()
        def bad(*args):
            raise Stop("unsupported runtime")
        result = verify(self.state, f["id"], runner_factory=bad)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["attempts"], 1)

    def test_budget_and_attempts_are_enforced(self):
        job, f = self.fixture()
        job["spent"] = 1800
        self.state.put("job", job["id"], job)
        with self.assertRaisesRegex(Stop, "预算"):
            verify(self.state, f["id"], runner_factory=HostFixtureRunner)
        job["spent"] = 0
        self.state.put("job", job["id"], job)
        f["attempts"] = 2
        self.state.put("finding", f["id"], f)
        with self.assertRaisesRegex(Stop, "两轮"):
            verify(self.state, f["id"], runner_factory=HostFixtureRunner)
        with self.assertRaises(Stop):
            add_finding(self.state, job["id"], self.root / "inputs/finding.json")

    def test_modified_patch_or_logs_invalidates_evidence(self):
        _, f = self.verified()
        root = self.state.root / "findings" / f["id"]
        (root / "fix.patch").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(Stop, "发生变化"):
            validate_evidence(self.state, f)

    def test_dry_run_has_no_remote_calls(self):
        job, f = self.verified()
        api = RemoteAPI(f, job)
        api.get = lambda _: self.fail("dry-run must not need remote API")
        result = publish(self.state, api, f["id"])
        self.assertEqual(result["remote_writes"], 0)
        self.assertEqual(api.writes, [])

    def test_execute_requires_reviewed_trial(self):
        job, f = self.verified()
        with self.assertRaisesRegex(Stop, "试运行"):
            publish(self.state, RemoteAPI(f, job), f["id"], execute=True)

    def test_private_finding_not_published_or_leaked_to_report(self):
        job, f = self.verified("security")
        self.allow_publish()
        with self.assertRaisesRegex(Stop, "安全"):
            publish(self.state, RemoteAPI(f, job), f["id"], execute=True)
        output = report(self.state, "batch1")
        text = Path(output["report"]).read_text(encoding="utf-8")
        self.assertNotIn(f["spec"]["title"], text)
        self.assertNotIn(f["spec"]["description"], text)
        self.assertIn("私密安全候选", text)

    def test_publisher_happy_path_and_duplicate_reuse(self):
        job, f = self.verified()
        api = RemoteAPI(f, job)
        self.allow_publish()
        result = publish(self.state, api, f["id"], execute=True)
        self.assertEqual(result["url"], "https://github.com/org/repo/pull/1")
        count = len(api.writes)
        again = publish(self.state, api, f["id"], execute=True)
        self.assertTrue(again["reused"])
        self.assertEqual(len(api.writes), count)
        tree = next(body for endpoint, body in api.writes if endpoint.endswith("/git/trees"))
        self.assertEqual({e["path"] for e in tree["tree"]}, {"helper.py", "tests/test_regression.py"})

    def test_publish_timeout_queries_remote_before_retry(self):
        job, f = self.verified()
        api = RemoteAPI(f, job)
        api.timeout_pr = True
        self.allow_publish()
        result = publish(self.state, api, f["id"], execute=True)
        self.assertIn("url", result)
        self.assertEqual(sum(e.endswith("/pulls") for e, _ in api.writes), 1)

    def test_changed_upstream_and_existing_pr_stop_before_mutation(self):
        job, f = self.verified()
        api = RemoteAPI(f, job)
        self.allow_publish()
        api.changed = True
        with self.assertRaisesRegex(Stop, "上游"):
            publish(self.state, api, f["id"], execute=True)
        self.assertFalse(api.writes)
        api.changed, api.duplicate = False, True
        with self.assertRaisesRegex(Stop, "PR"):
            publish(self.state, api, f["id"], execute=True)
        self.assertFalse(api.writes)

    def test_dependency_gate_requires_actual_fixed_package(self):
        def osv(version, vulnerable):
            return {"results": [{"packages": [{"package": {"name": "foo", "ecosystem": "PyPI", "version": version},
                     "vulnerabilities": [{"id": "GHSA-fixture"}] if vulnerable else []}]}]}
        before, after = self.root / "before.json", self.root / "after.json"
        write_json(before, osv("1.0", True))
        write_json(after, {"results": []})
        spec = {"package": "foo", "ecosystem": "PyPI", "affected_version": "1.0", "fixed_version": "1.1", "advisory": "GHSA-fixture"}
        with self.assertRaises(Stop):
            dependency_gate(before, after, spec)
        write_json(after, osv("1.1", False))
        dependency_gate(before, after, spec)
        write_json(after, osv("1.1", True))
        with self.assertRaises(Stop):
            dependency_gate(before, after, spec)

    def test_container_is_nonroot_and_does_not_mount_host(self):
        job, _ = self.fixture()
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            stdout = b"{}" if "inspect" in args else b""
            return subprocess.CompletedProcess(args, 0, stdout, b"")
        with patch("pipeline.pinned_image", return_value="python@sha256:fixture"), patch("pipeline.run", side_effect=fake_run):
            runner = DockerTests(self.state, Budget(self.state, job), "python:3.12-slim")
            runner.phase(self.state.root / "jobs/job1/checkout", "true", [("existing", "python tests/test_basic.py")], self.root / "logs")
        create = calls[0]
        self.assertIn("65534:65534", create)
        self.assertIn("--cap-drop=ALL", create)
        self.assertNotIn("--mount", create)
        self.assertNotIn("--privileged", create)
        disconnect = next(i for i, c in enumerate(calls) if "disconnect" in c)
        test = next(i for i, c in enumerate(calls) if c[-1] == "python tests/test_basic.py")
        self.assertLess(disconnect, test)
        self.assertFalse(any("TOKEN" in str(c) for c in calls))

    def test_path_traversal_and_mode_changes_rejected(self):
        with self.assertRaises(Stop):
            patch_paths(b"old mode 100644\nnew mode 100755\n")
        with self.assertRaises(Stop):
            patch_paths(b"--- a/../../evil\n+++ b/../../evil\n@@ -1 +1 @@\n-a\n+b\n")

    def test_command_timeout_and_output_limit(self):
        with self.assertRaisesRegex(Stop, "超时"):
            run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
        with self.assertRaisesRegex(Stop, "输出"):
            run([sys.executable, "-c", "print('x' * 10000)"], output_limit=100)

    def test_scratch_dir_stays_writable_and_is_removed(self):
        with temp_dir(self.root, "scratch-") as scratch:
            (scratch / "nested").mkdir()
            (scratch / "nested" / "file.txt").write_text("snapshot", encoding="utf-8")
            self.assertEqual((scratch / "nested" / "file.txt").read_text(encoding="utf-8"), "snapshot")
            # 0o700 is what tempfile.mkdtemp uses; sandboxed hosts deny writes there.
            self.assertNotEqual(scratch.stat().st_mode & 0o777, 0o700)
            path = scratch
        self.assertFalse(path.exists())

    def test_scanner_timeout_is_configurable_and_validated(self):
        self.assertEqual(DEFAULTS["scanner_timeout_s"], 900)
        bad = self.root / "bad-config"
        write_json(bad / "config.json", dict(DEFAULTS, scanner_timeout_s=0))
        with self.assertRaisesRegex(Stop, "scanner_timeout_s"):
            State(bad)

    def test_scanner_uses_configured_jobs_and_timeout(self):
        job, _ = self.fixture()
        rules = self.state.root / "tools/semgrep-rules.yaml"
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text("rules: []\n", encoding="utf-8")
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            stdout = b'{"results": [], "errors": []}' if str(args[-1]).endswith("/src") else b"{}"
            return subprocess.CompletedProcess(args, 0, stdout, b"")

        with patch("pipeline.pinned_image", return_value="semgrep@sha256:fixture"), patch("pipeline.run", side_effect=fake_run):
            info = scanner(self.state, self.state.root / "jobs/job1/checkout", "semgrep", self.root / "semgrep.json", Budget(self.state, job))
        self.assertEqual(info["coverage"], "scanner-reported")
        scan, kwargs = next((c, k) for c, k in calls if str(c[-1]).endswith("/src"))
        jobs = self.state.config["scanner_jobs"]
        self.assertEqual(scan[scan.index("--jobs") + 1], str(jobs))
        self.assertEqual(scan[scan.index("--cpus") + 1], str(jobs + 1))
        self.assertLessEqual(kwargs["timeout"], self.state.config["scanner_timeout_s"])

    def test_osv_without_package_sources_is_zero_coverage_not_failure(self):
        job, _ = self.fixture()

        def fake_run(args, **kwargs):
            if str(args[-1]).endswith("/src"):
                return subprocess.CompletedProcess(args, 1, b"", b"No package sources found, --help for usage information.\n")
            return subprocess.CompletedProcess(args, 0, b"osv-scanner version: 2.6.0\n", b"")

        with patch("pipeline.pinned_image", return_value="osv@sha256:fixture"), patch("pipeline.run", side_effect=fake_run):
            info = scanner(self.state, self.state.root / "jobs/job1/checkout", "osv", self.root / "osv.json", Budget(self.state, job))
        self.assertEqual(info["coverage"], "none")
        self.assertEqual(info["exit_code"], 1)

    def test_gh_installer_verifies_checksum(self):
        import io
        import zipfile
        import install_gh
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            # The installer rejects truncated downloads, so the fixture must clear
            # that size guard before the checksum gate is exercised.
            archive.writestr("gh_1.0.0_windows_amd64/bin/gh.exe", b"x" * (1024 ** 2 + 1))
        data = buffer.getvalue()
        archive_name = "gh_1.0.0_windows_amd64.zip"
        target = self.root / "portable-gh"
        with patch.object(install_gh, "release_asset", return_value=("v1.0.0", archive_name, "https://example.invalid/a.zip", "0" * 64)), \
             patch.object(install_gh, "download", return_value=data):
            with self.assertRaisesRegex(Stop, "SHA256"):
                install_gh.main(["--dir", str(target)])
        self.assertFalse(target.exists())
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(install_gh, "release_asset", return_value=("v1.0.0", archive_name, "https://example.invalid/a.zip", digest)), \
             patch.object(install_gh, "download", return_value=data):
            self.assertEqual(install_gh.main(["--dir", str(target)]), 0)
        self.assertTrue(any(target.rglob("gh.exe" if os.name == "nt" else "gh")))

    def test_second_process_cannot_acquire_running_state(self):
        script_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        code = "import sys;sys.path.insert(0,sys.argv[1]);from core import State;s=State(sys.argv[2]);\nwith s.lock(): print('unexpected')"
        with self.state.lock():
            result = run([sys.executable, "-X", "utf8", "-c", code, script_dir, str(self.state.root)], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已有任务运行", result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
