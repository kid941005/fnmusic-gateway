#!/usr/bin/env python3
"""Remove non-release tags from the GHCR package for this repository.

Keeps only latest and full semantic versions (x.y.z).  The GitHub Packages
REST API addresses a package version directly, so tags that share a manifest
with a release tag are first detached by pushing a tiny scratch image under
that tag; the dummy version is then deleted as a whole.
"""
import json
import os
import re
import subprocess
import sys

OWNER = os.environ.get("GH_OWNER", "kid941005")
PACKAGE = os.environ.get("GH_PACKAGE", "fnmusic-gateway")
APPLY = os.environ.get("APPLY", "false").lower() == "true"


def gh(*args):
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    result = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        raise SystemExit(
            "gh api failed: " + " ".join(args) + "\n" + result.stderr[-2000:]
        )
    return result.stdout


def list_versions():
    raw = gh(
        "--paginate", "--slurp",
        f"/users/{OWNER}/packages/container/{PACKAGE}/versions?per_page=100",
    )
    pages = json.loads(raw or "[]")
    if pages and isinstance(pages[0], dict):
        pages = [pages]
    return [version for page in pages for version in page]


def version_tags(version):
    meta = version.get("metadata") or {}
    container = meta.get("container") or {}
    return list(container.get("tags") or [])


def is_release(tag):
    return tag == "latest" or re.fullmatch(r"\d+\.\d+\.\d+", tag) is not None


def delete_version(version_id, digest):
    gh("-X", "DELETE",
       f"/users/{OWNER}/packages/container/{PACKAGE}/versions/{version_id}")
    print(f"DELETE version {version_id} ({digest})", flush=True)


def push_dummy(tags):
    subprocess.run(
        ["docker", "build", "-q", "-t", "fnmusic-tag-dummy:latest", "-"],
        input=b"FROM scratch\n", check=True, capture_output=True,
    )
    for tag in tags:
        ref = f"ghcr.io/{OWNER}/{PACKAGE}:{tag}"
        subprocess.run(["docker", "tag", "fnmusic-tag-dummy:latest", ref],
                       check=True, capture_output=True)
        result = subprocess.run(["docker", "push", ref], check=True,
                                capture_output=True, text=True)
        sys.stdout.write(result.stdout)
    sys.stdout.flush()


def main():
    versions = list_versions()
    print(f"package versions: {len(versions)}", flush=True)

    tagged = [v for v in versions if version_tags(v)]
    whole, detach = [], []
    for version in tagged:
        tags = version_tags(version)
        wanted = [tag for tag in tags if is_release(tag)]
        unwanted = [tag for tag in tags if not is_release(tag)]
        if not unwanted:
            continue
        if wanted:
            detach.append((version.get("id"), version.get("name"), wanted, unwanted))
        else:
            whole.append((version.get("id"), version.get("name"), tags))

    print(f"delete whole versions: {len(whole)}", flush=True)
    for vid, digest, tags in whole:
        print(f"  {vid} {digest} {tags}", flush=True)
    detach_tags = [tag for _, _, _, unwanted in detach for tag in unwanted]
    print(f"detach tags first: {len(detach_tags)}", flush=True)
    for vid, digest, wanted, unwanted in detach:
        print(f"  {vid} {digest} keep={wanted} detach={unwanted}", flush=True)

    if not APPLY:
        print("DRY RUN: no changes made", flush=True)
        return 0

    if detach_tags:
        push_dummy(detach_tags)

    versions = list_versions()
    mixed = []
    for version in versions:
        tags = version_tags(version)
        if any(is_release(t) for t in tags) and any(not is_release(t) for t in tags):
            mixed.append((version.get("id"), tags))
    if mixed:
        print("MIXED versions remain (refusing to delete):", mixed, flush=True)
        return 1

    failed = []
    deleted = 0
    for version in versions:
        tags = version_tags(version)
        # Untagged versions are platform-specific child manifests of a
        # multi-arch index; deleting them would corrupt a release image.
        if not tags:
            continue
        if any(is_release(t) for t in tags):
            continue
        print(f"deleting version {version.get('id')} tags={tags}", flush=True)
        try:
            delete_version(version.get("id"), version.get("name"))
            deleted += 1
        except SystemExit as exc:
            failed.append((version.get("id"), tags, str(exc)))
    print(f"deleted versions: {deleted}", flush=True)

    versions = list_versions()
    remaining = sorted(t for version in versions for t in version_tags(version))
    print("remaining:", remaining, flush=True)
    bad = [tag for tag in remaining if not is_release(tag)]
    if failed or bad:
        print("FAILED delete versions:", failed, flush=True)
        print("unexpected remaining:", bad, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
