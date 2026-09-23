#!/usr/bin/env python3
"""Public-exposure audit for the Des Moines data monitor.

A reusable pre-commit / pre-deploy scanner. It flags anything that would leak
to the public if committed to the (public) GitHub repo or shipped in the
frontend bundle:

  * credentials embedded in the git remote URL,
  * hardcoded secrets in tracked or about-to-ship files,
  * review/write internals on public-facing surfaces,
  * storage-layer endpoint names on public-facing surfaces,
  * unsafe backend review-write guardrails,
  * browser CORS that exposes writes beyond the public access-request form,
  * account cost exposed without an explicit internal-use guard.

Run from the repo root:

    python scripts/quality/audit_public_exposure.py

Git-ignored files are skipped (they never reach the public repo). Every finding
is tagged TRACKED (already public on GitHub) or UNTRACKED (public only if
committed) so already-shipped leaks are distinguished from not-yet-committed
ones. Exit status is non-zero when any CRITICAL or HIGH finding is present, so
it can gate CI or a deploy script. Secret values are never printed.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

FRONTEND_SURFACE = ["frontend/src", "frontend/dist"]
DOCS_SURFACE = [
    "README.md",
    "PROJECT_HANDOVER.md",
    "CLAUDE.md",
    "AGENTS.md",
    "CODEX.md",
    "GEMINI.md",
    "docs",
]
SKIP_PARTS = {"node_modules", ".git", ".vercel", "__pycache__"}

SECRET_PATTERNS = [
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{60,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
]

# The functional client legitimately calls the write endpoints, so bare endpoint
# references are only flagged in docs; the other strings are high-signal because
# public read code never needs them.
SURFACE_CHECKS = [
    ("review key env name exposed", re.compile(r"AQ_REVIEW_API_KEY|REVIEW_API_KEY"), "HIGH", "both"),
    ("api_key/review_key passed in URL", re.compile(r"(?:\?|&)api_key=|(?:\?|&)review_key="), "HIGH", "both"),
    ("key-store internals on public surface", re.compile(r"DynamoDB|Lambda authorizer", re.I), "HIGH", "both"),
    ("write scopes documented publicly", re.compile(r"review:write"), "MEDIUM", "both"),
    ("write endpoint documented in docs", re.compile(r"/record-(?:flags|corrections)"), "MEDIUM", "docs"),
    ("storage-layer endpoint on public surface", re.compile(r"/silver-(?:download|records)"), "HIGH", "both"),
]

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
findings = []
TRACKED = set()
IGNORED = set()


def add(severity, category, location, detail):
    findings.append((severity, category, location, detail))


def run(*args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True).stdout


def read(path):
    try:
        return Path(path).read_text(errors="replace")
    except Exception:
        return ""


def surface_files():
    files = []
    for root in FRONTEND_SURFACE + DOCS_SURFACE:
        base = REPO / root
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files += [p for p in base.rglob("*")
                      if p.is_file() and not SKIP_PARTS & set(p.parts)]
    return files


def load_git_state():
    TRACKED.update((REPO / p).resolve() for p in run("git", "ls-files").splitlines() if p)
    candidates = surface_files()
    if candidates:
        proc = subprocess.run(["git", "check-ignore", "--stdin"], cwd=REPO,
                              input="\n".join(str(p) for p in candidates),
                              capture_output=True, text=True)
        IGNORED.update(Path(line).resolve() for line in proc.stdout.splitlines() if line)


def status_of(path):
    return "TRACKED - public on GitHub now" if Path(path).resolve() in TRACKED \
        else "UNTRACKED - public only if committed"


def scope_of(path):
    return "frontend" if Path(path).relative_to(REPO).parts[0] == "frontend" else "docs"


def check_git_remote():
    for line in run("git", "remote", "-v").splitlines():
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                add("CRITICAL", "credential-in-git-remote", "git remote URL",
                    f"{name} embedded in a remote URL. Revoke it and use SSH or a credential helper.")
        if re.search(r"https://[^/@:\s]+:[^/@\s]+@", line):
            add("CRITICAL", "credential-in-git-remote", "git remote URL",
                "user:password credentials embedded in a remote URL.")


def check_repo_visibility():
    if '"PUBLIC"' in run("gh", "repo", "view", "--json", "visibility,nameWithOwner"):
        add("INFO", "repo-visibility", "GitHub",
            "Repository is PUBLIC: every committed file is world-readable.")


def scan_secrets():
    seen = set()
    for path in TRACKED | {p.resolve() for p in surface_files()}:
        if path in IGNORED:
            continue
        text = read(path)
        for name, pattern in SECRET_PATTERNS:
            rel = str(path.relative_to(REPO))
            if pattern.search(text) and (name, rel) not in seen:
                seen.add((name, rel))
                add("CRITICAL", "hardcoded-secret", f"{rel}  [{status_of(path)}]", f"{name} found.")


def scan_surfaces():
    for path in surface_files():
        if path.resolve() in IGNORED:
            continue
        scope, text, rel = scope_of(path), read(path), str(path.relative_to(REPO))
        for name, pattern, severity, applies in SURFACE_CHECKS:
            if applies in ("both", scope) and pattern.search(text):
                add(severity, "write-internals-on-public-surface",
                    f"{rel}  [{status_of(path)}]", name)


def check_backend_guardrails():
    api = REPO / "lambda_api.py"
    api_text = read(api)
    if not api.exists():
        return

    if re.search(r'params\.get\(["\'](?:api_key|review_key)["\']\)', api_text):
        add("HIGH", "unsafe-review-auth", "lambda_api.py",
            "Review auth accepts keys from URL query parameters.")
    has_review_write = "def write_review_item(" in api_text
    has_team_claim_guard = (
        "def require_team_role(" in api_text
        and 'get("authorizer", {})' in api_text
        and 'get("jwt", {})' in api_text
        and 'INTERNAL_API_ROUTES["flags"]' in api_text
    )
    if has_review_write and not has_team_claim_guard:
        add("HIGH", "unsafe-review-auth", "lambda_api.py",
            "Review writes do not appear to require API Gateway-verified team identity and roles.")
    if "PUBLIC_API_KEY_REQUIRED" in api_text:
        if "API_KEY_HASH_PEPPER" not in api_text or "key_hash(" not in api_text:
            add("HIGH", "unsafe-api-key-auth", "lambda_api.py",
                "Public API key support does not appear to use a server-side hash pepper.")
        if re.search(r'params\.get\(["\'](?:api_key|key)["\']\)', api_text):
            add("HIGH", "unsafe-api-key-auth", "lambda_api.py",
                "Public API key support accepts keys from URL query parameters.")
    has_cost_code = re.search(r"mtdCost|AWS account MTD|get_cost_and_usage|Cost Explorer", api_text)
    has_cost_guard = (
        "ENABLE_COST_KPI" in api_text
        and 'os.environ.get("ENABLE_COST_KPI") == "1"' in api_text
    )
    if has_cost_code and not has_cost_guard:
        add("HIGH", "cost-exposure", "lambda_api.py",
            "Cost code is present without the explicit ENABLE_COST_KPI opt-in guard.")


def check_deploy_guardrails():
    deploy = REPO / "scripts" / "aws" / "deploy_backend.py"
    text = read(deploy)
    if not deploy.exists():
        return

    if "ce:GetCostAndUsage" in text and "ENABLE_COST_KPI" not in text:
        add("HIGH", "cost-exposure", "scripts/aws/deploy_backend.py",
            "Deploy policy grants billing read access without the explicit ENABLE_COST_KPI opt-in guard.")
    allows_post = re.search(r'"AllowMethods"\s*:\s*\[[^\]]*"POST"', text, re.S)
    has_access_request_route = "POST /air-quality/v1/access-requests" in text
    if allows_post and not has_access_request_route:
        add("HIGH", "browser-write-cors", "scripts/aws/deploy_backend.py",
            "API Gateway CORS allows browser POST calls without a narrowly scoped access-request route.")
    credential_headers = re.search(
        r'"AllowHeaders"\s*:\s*\[[^\]]*(x-api-key|authorization)', text, re.S | re.I
    )
    wildcard_origin = re.search(r'"AllowOrigins"\s*:\s*\[[^\]]*"\*"', text, re.S)
    if credential_headers and wildcard_origin:
        add("HIGH", "browser-write-cors", "scripts/aws/deploy_backend.py",
            "API Gateway allows browser credential headers from every origin.")


def main():
    load_git_state()
    checks = (
        check_git_remote,
        check_repo_visibility,
        scan_secrets,
        scan_surfaces,
        check_backend_guardrails,
        check_deploy_guardrails,
    )
    for check in checks:
        check()

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f[0], 9), f[1], f[2]))
    counts = {}
    for severity, *_ in findings:
        counts[severity] = counts.get(severity, 0) + 1

    print("Public-exposure audit\n" + "=" * 64)
    if not findings:
        print("No findings.")
        return 0
    for severity, category, location, detail in findings:
        print(f"[{severity:8}] {category}\n    {location}\n    {detail}")
    print("=" * 64)
    print("summary: " + ", ".join(f"{sev}={counts[sev]}" for sev in
                                   sorted(counts, key=lambda s: SEVERITY_ORDER.get(s, 9))))
    return 1 if counts.get("CRITICAL", 0) + counts.get("HIGH", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
