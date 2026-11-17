#!/usr/bin/env python3
"""Keep the two newest successful-release Pages records in the public repository."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

REPOSITORY = "chanchanman2/chanchanman2.github.io"
KEEP = 2
PAGES_WORKFLOW = "dynamic/pages/pages-build-deployment"
RETENTION_WORKFLOW = ".github/workflows/retain-deployments.yml"


class Superseded(Exception):
    """A newer release must perform the cleanup instead."""


class GitHub:
    def request(self, path, method="GET", body=None, missing_ok=False):
        command = ["gh", "api", "--method", method, f"repos/{REPOSITORY}/{path}"]
        if body is not None:
            command += ["--input", "-"]
        result = subprocess.run(
            command, input=json.dumps(body) if body is not None else None,
            text=True, capture_output=True, timeout=60,
        )
        if result.returncode:
            if missing_ok and "HTTP 404" in result.stderr:
                return None
            raise RuntimeError(f"{method} {path}: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def items(self, path, key=None):
        result = subprocess.run(
            ["gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/{path}"],
            text=True, capture_output=True, timeout=60,
        )
        if result.returncode:
            raise RuntimeError(f"GET {path}: {result.stderr.strip()}")
        pages = json.loads(result.stdout)
        return [item for page in pages for item in (page[key] if key else page)]


def newest(items):
    return sorted(items, key=lambda item: (item["created_at"], item["id"]), reverse=True)


def deployments(api):
    return newest([
        item for item in api.items("deployments?per_page=100")
        if item["environment"] == "github-pages"
    ])


def states(api, deployment):
    return api.items(f"deployments/{deployment['id']}/statuses?per_page=100")


def current_release(api, expected_sha):
    if api.request("git/ref/heads/main")["object"]["sha"] != expected_sha:
        raise Superseded("Public main changed; leave cleanup to the newer release.")
    records = deployments(api)
    if not records or records[0]["sha"] != expected_sha:
        raise RuntimeError("The current public main has no latest Pages deployment yet.")
    latest = states(api, records[0])
    if not latest or latest[0]["state"] != "success":
        raise RuntimeError("The latest Pages deployment has not succeeded; no cleanup performed.")
    return records


def run_ids(statuses):
    result = set()
    for status in statuses:
        url = urlsplit(status.get("log_url") or "")
        match = re.fullmatch(rf"/{re.escape(REPOSITORY)}/actions/runs/(\d+)(?:/job/\d+)?/?", url.path)
        if url.scheme == "https" and url.netloc == "github.com" and match:
            result.add(int(match[1]))
    return result


def ensure_same_release(api, expected_sha, latest_id):
    current = current_release(api, expected_sha)
    if current[0]["id"] != latest_id:
        raise Superseded("A newer Pages deployment appeared; leave it and its records intact.")


def prune(api, expected_sha, own_run_id=None):
    records = current_release(api, expected_sha)
    kept, old = records[:KEEP], records[KEEP:]
    protected_runs = set()
    for record in kept:
        protected_runs.update(run_ids(states(api, record)))
    if own_run_id:
        protected_runs.add(own_run_id)
    removed = []
    for record in old:
        status = states(api, record)
        if not status or status[0]["state"] not in {"success", "failure", "error", "inactive"}:
            continue  # Let an unfinished build complete before removing its records.
        runs = []
        for run_id in sorted(run_ids(status) - protected_runs):
            run = api.request(f"actions/runs/{run_id}", missing_ok=True)
            if run and (
                run["path"] == PAGES_WORKFLOW
                and run["head_branch"] == "main"
                and run["head_sha"] == record["sha"]
                and run["status"] == "completed"
            ):
                runs.append(run_id)
        ensure_same_release(api, expected_sha, kept[0]["id"])
        api.request(
            f"deployments/{record['id']}/statuses", "POST",
            {"state": "inactive", "auto_inactive": False,
             "description": "Retaining the two newest Pages deployments."},
        )
        for run_id in runs:
            ensure_same_release(api, expected_sha, kept[0]["id"])
            api.request(f"actions/runs/{run_id}", "DELETE")
        # Keep the deployment's historical log URLs until its builds are gone,
        # so an interrupted cleanup can discover and retry the same association.
        ensure_same_release(api, expected_sha, kept[0]["id"])
        api.request(f"deployments/{record['id']}", "DELETE")
        removed.append(record["id"])
        print(f"Removed old deployment {record['id']} and {len(runs)} associated Pages run(s).", flush=True)

    ensure_same_release(api, expected_sha, kept[0]["id"])
    # These runs expose their checkout revision in GitHub's logs too. Retain two
    # cleanup runs, without deleting the running job or unrelated workflows.
    cleanup_runs = newest([
        run for run in api.items("actions/runs?per_page=100", "workflow_runs")
        if run["path"] == RETENTION_WORKFLOW and run["head_branch"] == "main"
    ])
    for run in cleanup_runs[KEEP:]:
        if run["id"] != own_run_id and run["status"] == "completed":
            ensure_same_release(api, expected_sha, kept[0]["id"])
            api.request(f"actions/runs/{run['id']}", "DELETE")
    remaining = current_release(api, expected_sha)
    if len(remaining) > KEEP:
        raise RuntimeError("Older unfinished deployments remain; retry after their builds complete.")
    return {"kept_deployments": [item["id"] for item in remaining], "deleted_deployments": removed}


def verify(api, expected_sha):
    records = current_release(api, expected_sha)
    if len(records) > KEEP:
        raise RuntimeError(f"Public Pages still has {len(records)} deployments; expected at most {KEEP}.")
    return {"kept_deployments": [item["id"] for item in records]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay", type=float, default=10)
    args = parser.parse_args()
    if args.attempts < 1 or args.delay < 0:
        parser.error("attempts must be positive and delay must be nonnegative")
    if not args.verify_only and os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        parser.error("Cleanup must run in the public repository using its own GITHUB_TOKEN.")
    api = GitHub()
    try:
        for attempt in range(args.attempts):
            try:
                if args.verify_only:
                    result = verify(api, args.expected_sha)
                else:
                    run_id = int(os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else None
                    result = prune(api, args.expected_sha, run_id)
                break
            except RuntimeError as error:
                if not args.verify_only or attempt + 1 == args.attempts:
                    raise
                print(f"Waiting for Pages history retention: {error}", flush=True)
                time.sleep(args.delay)
        summary = "PAGES RETENTION OK " + json.dumps(result)
        print(summary, flush=True)
        if not args.verify_only and os.environ.get("GITHUB_STEP_SUMMARY"):
            Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(summary + "\n")
        return 0
    except Superseded as error:
        print(str(error), flush=True)
        return 1 if args.verify_only else 0
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"PAGES RETENTION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
