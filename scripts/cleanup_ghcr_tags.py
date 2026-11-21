#!/usr/bin/env python3
"""Remove non-release tags from the GHCR package for this repository.

Keeps only latest and full semantic versions (x.y.z).  GHCR deletes a
manifest together with every tag that points at it, so tags that share a
digest with a release tag are first detached by pushing a tiny scratch image
under that tag, then the dummy manifest is removed.
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OWNER = os.environ.get("GH_OWNER", "kid941005")
PACKAGE = os.environ.get("GH_PACKAGE", "fnmusic-gateway")
USER = os.environ["GH_USER"]
TOKEN = os.environ["GH_TOKEN"]
APPLY = os.environ.get("APPLY", "false").lower() == "true"
REGISTRY = "https://ghcr.io"
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except Exception as exc:  # noqa: BLE001
        return "ERR", repr(exc).encode(), {}


def registry_token():
    basic = base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
    scope = urllib.parse.quote(f"repository:{OWNER}/{PACKAGE}:pull,push,delete", safe="")
    status, body, _ = http(
        "GET",
        f"{REGISTRY}/token?service=ghcr.io&scope={scope}",
        {"Authorization": "Basic " + basic},
    )
    if status != 200:
        raise SystemExit(f"token request failed: {status} {body[:300]!r}")
    return json.loads(body)["token"]


def tag_list(auth):
    tags, url = [], f"{REGISTRY}/v2/{OWNER}/{PACKAGE}/tags/list?n=1000"
    while url:
        status, body, headers = http("GET", url, auth)
        if status != 200:
            raise SystemExit(f"tag list failed: {status} {body[:300]!r}")
        tags.extend(json.loads(body).get("tags") or [])
        link = headers.get("Link") or headers.get("link")
        match = re.search(r"<([^>]+)>", link) if link else None
        url = match.group(1) if match else None
    return sorted(set(tags))


def manifest_digest(tag, auth):
    status, body, headers = http(
        "HEAD",
        f"{REGISTRY}/v2/{OWNER}/{PACKAGE}/manifests/{tag}",
        {**auth, "Accept": ACCEPT},
    )
    digest = headers.get("Docker-Content-Digest") or headers.get("docker-content-digest")
    if status != 200 or not digest:
        raise SystemExit(f"HEAD {tag} failed: {status} {body[:160]!r} {digest!r}")
    return tag, digest


def is_release(tag):
    return tag == "latest" or re.fullmatch(r"\d+\.\d+\.\d+", tag) is not None


def delete_manifest(digest, auth):
    encoded = urllib.parse.quote(digest, safe="")
    return http("DELETE", f"{REGISTRY}/v2/{OWNER}/{PACKAGE}/manifests/{encoded}", auth)


def push_dummy(tags):
    subprocess.run(
        ["docker", "build", "-q", "-t", "fnmusic-tag-dummy:latest", "-"],
        input=b"FROM scratch\n",
        check=True,
        capture_output=True,
    )
    digest = None
    for tag in tags:
        ref = f"ghcr.io/{OWNER}/{PACKAGE}:{tag}"
        subprocess.run(["docker", "tag", "fnmusic-tag-dummy:latest", ref], check=True,
                       capture_output=True)
        result = subprocess.run(["docker", "push", ref], check=True,
                                capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        match = re.search(r"digest:\s*(sha256:[0-9a-f]+)", result.stdout)
        if match:
            digest = match.group(1)
    if not digest:
        raise SystemExit("could not read dummy manifest digest from docker push")
    return digest


def main():
    auth = {"Authorization": "Bearer " + registry_token()}
    tags = tag_list(auth)
    print(f"tags: {len(tags)}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        pairs = list(pool.map(lambda tag: manifest_digest(tag, auth), tags))

    groups = {}
    for tag, digest in pairs:
        groups.setdefault(digest, []).append(tag)

    whole, detach = [], []
    for digest, group_tags in groups.items():
        wanted = [tag for tag in group_tags if is_release(tag)]
        unwanted = [tag for tag in group_tags if not is_release(tag)]
        if not unwanted:
            continue
        if wanted:
            detach.append((digest, wanted, unwanted))
        else:
            whole.append((digest, group_tags))

    detach_tags = [tag for _, _, unwanted in detach for tag in unwanted]
    print(f"delete whole manifests: {len(whole)}")
    for digest, group_tags in whole:
        print(f"  {digest} {group_tags}")
    print(f"detach tags first: {len(detach_tags)}")
    for digest, wanted, unwanted in detach:
        print(f"  {digest} keep={wanted} detach={unwanted}")

    if not APPLY:
        print("DRY RUN: no changes made")
        return 0

    failed = []
    for digest, group_tags in whole:
        status, body, _ = delete_manifest(digest, auth)
        print(f"DELETE whole {digest}: {status} {body[:100]!r}")
        if status not in (200, 202, 204):
            failed.append((digest, group_tags, status))

    if detach_tags:
        dummy_digest = push_dummy(detach_tags)
        status, body, _ = delete_manifest(dummy_digest, auth)
        print(f"DELETE dummy {dummy_digest}: {status} {body[:100]!r}")
        if status not in (200, 202, 204):
            failed.append((dummy_digest, detach_tags, status))

    remaining = tag_list(auth)
    print("remaining:", remaining)
    bad = [tag for tag in remaining if not is_release(tag)]
    if failed or bad:
        print("FAILED delete groups:", failed)
        print("unexpected remaining:", bad)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
