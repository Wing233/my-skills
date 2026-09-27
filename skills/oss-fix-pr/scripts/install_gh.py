#!/usr/bin/env python3
"""Install a portable GitHub CLI into the state directory (no admin, no PATH edit).

Publishing needs `gh`; requiring a system-wide install would be a bigger step than
the skill needs. This downloads the official release archive, verifies the
publisher-provided SHA256 digest, and unpacks it under STATE/tools, which is the
location `core.executable("gh")` already searches. Login stays a human step:
run `gh auth login` afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import Stop  # noqa: E402

API = "https://api.github.com/repos/cli/cli/releases/latest"


def tools_root():
    """Same layout core.executable() searches for a portable gh."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        base = Path.home() / ".local" / "share"
    return base / "oss-fix-pr" / "tools"


def release_asset():
    """Resolve the latest release without spending API quota when possible.

    The releases API is rate limited for anonymous callers, which is easy to hit
    during discovery. The /releases/latest redirect needs no quota, and the
    release ships a checksums file, so the download stays verifiable either way.
    """
    suffix = "_windows_amd64.zip" if os.name == "nt" else "_linux_amd64.tar.gz"
    try:
        request = urllib.request.Request(API, headers={"User-Agent": "oss-fix-pr", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.load(response)
        asset = next((a for a in release.get("assets", []) if a["name"].endswith(suffix)), None)
        if asset:
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            return release["tag_name"], asset["name"], asset["browser_download_url"], digest
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError, StopIteration):
        pass
    request = urllib.request.Request("https://github.com/cli/cli/releases/latest", headers={"User-Agent": "oss-fix-pr"})
    with urllib.request.urlopen(request, timeout=60) as response:
        tag = response.url.rstrip("/").rsplit("/", 1)[-1]
    if not tag.startswith("v"):
        raise Stop("无法确定 GitHub CLI 最新版本")
    version = tag[1:]
    name = f"gh_{version}{suffix}"
    base = f"https://github.com/cli/cli/releases/download/{tag}/"
    checksums = download(base + f"gh_{version}_checksums.txt")
    for line in checksums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return tag, name, base + name, parts[0]
    raise Stop("校验文件中没有目标资产，放弃安装")


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "oss-fix-pr"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def unpack(data, name, dest):
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if any(n.startswith(("/", "\\")) or ".." in Path(n).parts for n in names):
                raise Stop("压缩包包含不安全路径")
            archive.extractall(dest)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if any(m.issym() or m.islnk() or not (m.isfile() or m.isdir()) for m in archive.getmembers()):
                raise Stop("压缩包包含符号链接或特殊文件")
            archive.extractall(dest, filter="data")


def main(argv=None):
    parser = argparse.ArgumentParser(description="把便携版 GitHub CLI 安装到本地工具目录（不需要管理员）")
    parser.add_argument("--dir", default=None, help="安装目录；默认 %LOCALAPPDATA%/oss-fix-pr/tools（或 ~/.local/share/…）")
    args = parser.parse_args(argv)
    tools = Path(args.dir).resolve() if args.dir else tools_root() / "gh"
    binary_name = "gh.exe" if os.name == "nt" else "gh"
    if any(tools.rglob(binary_name)):
        print(json.dumps({"status": "already-installed", "path": str(tools)}, ensure_ascii=False))
        return 0
    tag, name, url, expected = release_asset()
    data = download(url)
    if len(data) < 1024 ** 2:
        raise Stop("下载内容过小，判定失败")
    actual = hashlib.sha256(data).hexdigest()
    if not expected or expected != actual:
        raise Stop("SHA256 校验失败；已放弃安装")
    if tools.exists():
        shutil.rmtree(tools, ignore_errors=True)
    tools.mkdir(parents=True)
    unpack(data, name, tools)
    binary = next((p for p in tools.rglob(binary_name) if p.is_file()), None)
    if not binary:
        raise Stop("解包后没有找到 gh 可执行文件")
    print(json.dumps({"status": "installed", "release": tag, "binary": str(binary),
                      "sha256": actual, "next": "运行 gh auth login 后 publish --execute 可用"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Stop, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
