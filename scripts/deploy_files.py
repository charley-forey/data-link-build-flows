"""Sync the code the notebooks import into the lakehouse Files area.

The notebooks hold no logic of their own worth speaking of - they import the
library and run the .sql folders. That code lives in git and has to be pushed
to OneLake before a run, or the pipeline executes whatever was uploaded last
time. This is the one command that makes "what is in Fabric" equal "what is in
the repo".

    python scripts/deploy_files.py            list what would be uploaded
    python scripts/deploy_files.py --apply    upload it

Auth comes from the Azure CLI, so no secret is read or printed here. Nothing in
the mapping below is a secret: it is library code, SQL and endpoint config.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKSPACE_ID = "2ac993c2-c72f-48d1-9933-93ac189f25bf"
LAKEHOUSE_ID = "d7527d0d-f3e1-4dad-bd55-1a165fe29d93"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"

# local directory -> path under Files/. Mirrors what the notebooks import.
# The `rename` entry matters: every source keeps its config as endpoints.yml or
# entities.yml inside its own folder, but they all land in one flat Files/config
# and would collide, so they are prefixed on the way up. The notebooks read the
# PREFIXED names - uploading the bare name silently adds a file nobody reads and
# leaves the real config stale.
MAPPING = [
    (ROOT / "platform" / "lib", "lib", "*.py", None),
    (ROOT / "transformation" / "sql" / "silver", "sql/silver", "*.sql", None),
    (ROOT / "transformation" / "sql" / "gold", "sql/gold", "*.sql", None),
    (ROOT / "transformation" / "sql" / "meta", "sql/meta", "*.sql", None),
    (ROOT / "transformation" / "dq", "dq", "*.py", None),
    (ROOT / "ingestion" / "procore" / "config", "config", "endpoints.yml",
     "procore_endpoints.yml"),
    (ROOT / "ingestion" / "qbo" / "config", "config", "entities.yml",
     "qbo_entities.yml"),
    (ROOT / "ingestion" / "hubspot" / "config", "config", "objects.yml",
     "hubspot_objects.yml"),
]


def token() -> str:
    exe = shutil.which("az")
    if not exe:
        sys.exit("azure CLI not found on PATH")
    out = subprocess.run(
        [exe, "account", "get-access-token", "--resource", "https://storage.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("could not get a storage token - run `az login` first")
    return out.stdout.strip()


def request(method: str, url: str, tok: str, data: bytes | None = None) -> int:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:300]
        sys.exit(f"{method} {url.split('?')[0]} -> {error.code} {detail}")


def upload(local: pathlib.Path, remote: str, tok: str) -> None:
    """OneLake DFS upload is three calls: create, append, flush."""
    base = f"{ONELAKE}/{WORKSPACE_ID}/{LAKEHOUSE_ID}/Files/{remote}"
    body = local.read_bytes()
    request("PUT", f"{base}?resource=file", tok)
    if body:
        request("PATCH", f"{base}?action=append&position=0", tok, body)
    request("PATCH", f"{base}?action=flush&position={len(body)}", tok)


def collect() -> list[tuple[pathlib.Path, str]]:
    out: list[tuple[pathlib.Path, str]] = []
    for folder, remote_dir, pattern, rename in MAPPING:
        if not folder.exists():
            continue
        for path in sorted(folder.glob(pattern)):
            if path.name.startswith("_") and path.name != "__init__.py":
                continue
            out.append((path, f"{remote_dir}/{rename or path.name}"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    files = collect()
    total = sum(p.stat().st_size for p, _ in files)
    for path, remote in files:
        print(f"  {path.relative_to(ROOT)}  ->  Files/{remote}")
    print(f"\n{len(files)} files, {total / 1024:.0f} KB")

    if not args.apply:
        print("dry run - nothing uploaded. re-run with --apply.")
        return 0

    tok = token()
    for path, remote in files:
        upload(path, remote, tok)
    print(f"uploaded {len(files)} files to the lakehouse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
