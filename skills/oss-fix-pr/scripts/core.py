"""Local state and read-only GitHub discovery. Python 3.11+, standard library only."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULTS = {
    "min_stars": 1000, "active_days": 90, "batch_size": 5,
    "max_requests": 14, "seconds_per_repo": 1800, "max_attempts": 2,
    "max_prs": 3, "publish_mode": "dry-run", "reviewed_batch": None,
    "languages": ["Java", "Python", "JavaScript", "TypeScript", "Vue"],
    "topics": ["llm", "ai-agent", "rag", "mcp", "spring-boot", "vue"],
    "semgrep_image": "semgrep/semgrep:latest",
    "osv_image": "ghcr.io/google/osv-scanner:latest",
    "semgrep_rules_url": "https://semgrep.dev/c/p/ci",
    "scanner_timeout_s": 900, "scanner_jobs": 3,
    "memory": "4g", "cpus": "2", "work_size": "4g", "max_source_mb": 250,
}


def default_state_path():
    # Findings and scanner logs can contain unpublished details. Keep them outside
    # whichever untrusted repository the skill happens to be analyzing.
    workspace = os.path.normcase(str(Path.cwd().resolve()))
    key = digest(workspace.encode("utf-8"))[:20]
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        root = Path.home() / ".local" / "share"
    return root / "oss-fix-pr" / key


class Stop(Exception):
    """Expected, actionable stop; never interpreted as successful verification."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def stamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ident():
    return uuid.uuid4().hex[:12]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextlib.contextmanager
def temp_dir(parent, prefix="tmp-"):
    """Scratch directory that stays writable in sandboxed environments.

    ``tempfile.mkdtemp`` creates its directory with mode 0o700, and hosts that
    translate POSIX modes into access control (Windows sandboxed runs, hardened
    ACL setups) then deny writes inside it. That would break source snapshots and
    verification runs. Create the directory with the platform default mode, which
    the sandbox policy grants, and remove it afterwards.
    """
    path = Path(parent).resolve() / (prefix + ident())
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    if name == "gh" and os.name == "nt":
        candidates = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "GitHub CLI/gh.exe"]
        candidates += list((Path(os.environ.get("LOCALAPPDATA", ".")) / "oss-fix-pr/tools").glob("**/gh.exe"))
        return next((str(p) for p in candidates if p.is_file()), None)
    return None


def run(args, *, cwd=None, timeout=60, check=True, input=None, env=None, output_limit=32 * 1024 ** 2):
    try:
        # Bound output without keeping an untrusted project's unbounded logs in RAM.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as data:
            if input is not None:
                data.write(input)
                data.seek(0)
            process = subprocess.Popen([str(a) for a in args], cwd=cwd,
                                       stdin=data if input is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, env=env)
            deadline = time.monotonic() + max(0.1, timeout)
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise Stop(f"命令超时：{args[0]}")
                    if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > output_limit:
                        raise Stop(f"命令输出超过限制：{args[0]}")
                    time.sleep(0.05)
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > output_limit:
                    raise Stop(f"命令输出超过限制：{args[0]}")
                out.seek(0)
                err.seek(0)
                p = subprocess.CompletedProcess(args, process.returncode, out.read(), err.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(f"命令未完成：{args[0]}：{type(exc).__name__}") from exc
    if check and p.returncode:
        raise Stop(f"命令失败（{p.returncode}）：{args[0]}\n" + p.stderr.decode("utf-8", "replace")[-2000:])
    return p


def git(*args, cwd=None, timeout=60):
    env = os.environ.copy()
    # Ignore machine/user Git hooks, filters, URL rewrites and credential helpers.
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return run(["git", "-c", "core.hooksPath=" + os.devnull,
                "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
                *args], cwd=cwd, timeout=timeout, env=env,
               output_limit=384 * 1024 ** 2 if args and args[0] == "archive" else 32 * 1024 ** 2).stdout


def repo_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise Stop("仓库标识必须是 owner/repo")
    return name


class State:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS docs(kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS snapshots(repo TEXT, at TEXT, stars INTEGER, PRIMARY KEY(repo,at));
        CREATE TABLE IF NOT EXISTS events(at TEXT, kind TEXT, data TEXT);
        """)
        path = self.root / "config.json"
        if not path.exists():
            write_json(path, DEFAULTS)
        self.config = DEFAULTS | read_json(path)
        c = self.config
        for k in ("min_stars", "active_days", "batch_size", "max_requests", "seconds_per_repo",
                  "max_attempts", "max_prs", "max_source_mb", "scanner_timeout_s", "scanner_jobs"):
            if type(c[k]) is not int or c[k] < 1:
                raise Stop(f"配置 {k} 必须是正整数")
        if c["batch_size"] > 5 or c["max_prs"] > 3 or c["max_attempts"] > 2:
            raise Stop("首版上限：每批 5 个仓库、3 个 PR，每问题 2 轮验证")
        if c["publish_mode"] not in ("dry-run", "auto"):
            raise Stop("publish_mode 必须是 dry-run 或 auto")

    def get(self, kind, key):
        row = self.db.execute("SELECT data FROM docs WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def require(self, kind, key):
        value = self.get(kind, key)
        if value is None:
            raise Stop(f"找不到 {kind}: {key}")
        return value

    def put(self, kind, key, value):
        self.db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?)", (kind, key, json.dumps(value)))
        self.db.commit()

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM docs WHERE kind=? ORDER BY rowid", (kind,))]

    def event(self, kind, data):
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (now(), kind, json.dumps(data)))
        self.db.commit()

    @contextlib.contextmanager
    def lock(self):
        with (self.root / "run.lock").open("a+b") as f:
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Stop("该状态目录已有任务运行；不要并发发布") from exc
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)


class GitHub:
    def __init__(self):
        self.gh = executable("gh")
        self.auth = bool(self.gh and run([self.gh, "auth", "status", "--hostname", "github.com"],
                                        check=False, timeout=15).returncode == 0)

    def get(self, endpoint):
        if self.auth:
            return json.loads(run([self.gh, "api", "--hostname", "github.com", endpoint], timeout=45).stdout)
        url = "https://api.github.com/" + endpoint.lstrip("/")
        req = urllib.request.Request(url, headers={"User-Agent": "oss-fix-pr", "Accept": "application/vnd.github+json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (403, 429, 502, 503):
                    raise Stop(f"GitHub HTTP {exc.code}") from exc
                retry = exc.headers.get("Retry-After", "")
                reset = exc.headers.get("X-RateLimit-Reset", "0")
                wait = int(retry) if retry.isdigit() else max(2 ** attempt, int(reset or 0) - int(time.time()))
                if wait > 30 or attempt == 2:
                    raise Stop(f"GitHub 限流或暂时不可用；游标已保留，稍后 resume（HTTP {exc.code}）") from exc
                time.sleep(max(1, wait))
            except (urllib.error.URLError, TimeoutError) as exc:
                raise Stop("GitHub 网络不可用；可稍后 resume") from exc

    def pages(self, endpoint, limit=10):
        result = []
        for page in range(1, limit + 1):
            sep = "&" if "?" in endpoint else "?"
            rows = self.get(f"{endpoint}{sep}per_page=100&page={page}")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Stop("远端结果过多，无法完成重复检查；停止发布")

    def mutation(self, endpoint, payload):
        if not self.auth:
            raise Stop("发布需要安装 GitHub CLI 并执行 gh auth login")
        return json.loads(run([self.gh, "api", "--hostname", "github.com", "--method", "POST", endpoint,
                               "--input", "-"], input=json.dumps(payload).encode(), timeout=60).stdout)


def eligible(repo, cfg, current=None):
    current = current or time.time()
    lic = (repo.get("license") or {}).get("spdx_id")
    return (not any(repo.get(k) for k in ("private", "archived", "fork", "disabled"))
            and lic not in (None, "NOASSERTION", "NONE")
            and repo.get("stargazers_count", 0) >= cfg["min_stars"]
            and stamp(repo["pushed_at"]) >= current - cfg["active_days"] * 86400)


def rank_repo(state, repo, at):
    cfg = state.config
    text = " ".join([repo["full_name"], repo.get("description") or "", *repo.get("topics", [])]).lower()
    hits = sum(bool(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text)) for t in cfg["topics"])
    relevance = min(3, hits) * 2 + int(repo.get("language") in cfg["languages"])
    past = state.db.execute("SELECT at,stars FROM snapshots WHERE repo=? AND at<=? ORDER BY at DESC LIMIT 1",
                            (repo["full_name"], dt.datetime.fromtimestamp(stamp(at) - 86400, dt.timezone.utc).isoformat(timespec="seconds"))).fetchone()
    if past:
        growth = (repo["stargazers_count"] - past["stars"]) / ((stamp(at) - stamp(past["at"])) / 86400)
        heat = growth / (abs(growth) + 100)
        heat_info = {"kind": "measured", "stars_per_day": round(growth, 3), "since": past["at"]}
    else:
        age = max(0, (stamp(at) - stamp(repo["pushed_at"])) / 86400)
        heat = 0.5 / (1 + age) + min(0.49, math.log10(max(1, repo["stargazers_count"])) / 20)
        heat_info = {"kind": "proxy", "note": "首次/间隔不足一天；以活跃度和总 star 代替，非增长量"}
    repo = dict(repo, relevance=relevance, heat=heat, heat_info=heat_info)
    state.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?)", (repo["full_name"], at, repo["stargazers_count"]))
    state.db.commit()
    return repo


def discover(state, api, batch_id=None):
    cfg = state.config
    batch = state.get("batch", batch_id) if batch_id else None
    if batch_id and not batch:
        raise Stop("找不到批次")
    if batch and batch["status"] not in ("discovering", "discovery_paused"):
        return batch
    if not batch:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg["active_days"])).date()
        lanes = [f"topic:{t}" for t in cfg["topics"]] + [f"language:{lang}" for lang in cfg["languages"]] + [""]
        batch = {"id": ident(), "created": now(), "status": "discovering", "jobs": [], "candidates": {},
                 "queue": [{"lane": lane, "lo": cfg["min_stars"], "hi": None, "page": 1} for lane in lanes],
                 "cutoff": str(cutoff), "notes": [], "trial": cfg["publish_mode"] == "dry-run"}
    state.put("batch", batch["id"], batch)
    try:
        for _ in range(cfg["max_requests"]):
            if not batch["queue"]:
                break
            cursor = batch["queue"][0]
            stars = f'{cursor["lo"]}..{cursor["hi"]}' if cursor["hi"] is not None else f'>={cursor["lo"]}'
            q = f'is:public archived:false fork:false stars:{stars} pushed:>={batch["cutoff"]} {cursor["lane"]}'
            response = api.get("search/repositories?" + urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": cursor["page"]}))
            if response.get("incomplete_results"):
                raise Stop("GitHub 返回不完整结果；保留当前页等待重试")
            items = response.get("items", [])
            total = response.get("total_count", 0)
            batch["queue"].pop(0)
            if total > 1000:
                # The first result page is star-descending; its maximum does not
                # bound later, even more popular repositories. Use GitHub's
                # practical star ceiling so bisection cannot silently drop them.
                hi = cursor["hi"] if cursor["hi"] is not None else 1_000_000_000
                if hi > cursor["lo"]:
                    mid = (hi + cursor["lo"]) // 2
                    batch["queue"].extend([dict(cursor, lo=mid + 1, hi=hi, page=1), dict(cursor, hi=mid, page=1)])
                else:
                    batch["notes"].append(f"同 star 区间超过 1000 条，仅保留可见结果：{cursor['lane']} / {hi}")
            elif cursor["page"] * 100 < total:
                batch["queue"].append(dict(cursor, page=cursor["page"] + 1))
            # Save already-seen candidates even when a range is split; repo key deduplicates.
            for repo in items:
                if eligible(repo, cfg):
                    name = repo_name(repo["full_name"])
                    if name not in batch["candidates"]:
                        selected = {k: repo.get(k) for k in ("full_name", "stargazers_count", "pushed_at", "language", "description", "topics", "license", "default_branch")}
                        selected["lane"] = cursor["lane"] or "general"
                        batch["candidates"][name] = rank_repo(state, selected, batch["created"])
            state.put("batch", batch["id"], batch)
    except Stop as exc:
        batch.update(status="discovery_paused", error=str(exc))
        state.put("batch", batch["id"], batch)
        return batch
    ordered = sorted(batch["candidates"].values(), key=lambda r: (r["relevance"], r["heat"], r["stargazers_count"]), reverse=True)
    selected, lanes = [], set()
    previous = {j["repo"] for j in state.all("job") if j.get("status") not in ("complete", "skipped")}
    for diversity in (True, False):
        for repo in ordered:
            if len(selected) + len(batch["jobs"]) >= cfg["batch_size"]:
                break
            if repo in selected or repo["full_name"] in previous or (diversity and repo["lane"] in lanes):
                continue
            selected.append(repo)
            lanes.add(repo["lane"])
    for repo in selected:
        job_id = digest((batch["id"] + repo["full_name"]).encode())[:12]
        job = {"id": job_id, "batch": batch["id"], "repo": repo["full_name"], "status": "queued", "spent": 0,
               "metadata": repo, "created": now(), "scans": {}}
        state.put("job", job_id, job)
        if job_id not in batch["jobs"]:
            batch["jobs"].append(job_id)
        state.put("batch", batch["id"], batch)
    batch.update(status="selected", error=None)
    # Unvisited cursor persists; explicit resume-discovery can extend discovery later.
    state.put("batch", batch["id"], batch)
    return batch


def doctor():
    result = {"python": os.sys.version.split()[0], "git": bool(executable("git")),
              "gh": executable("gh"), "docker": executable("docker")}
    api = GitHub()
    result["github_authenticated"] = api.auth
    if result["docker"]:
        p = run([result["docker"], "info", "--format", "{{.OSType}}"], check=False, timeout=15)
        result["docker_ready"] = p.returncode == 0 and p.stdout.strip() == b"linux"
    else:
        result["docker_ready"] = False
    result["note"] = "发现项目可匿名读取；验证需要 Linux Docker 引擎，发布还需要 gh 登录。"
    return result
