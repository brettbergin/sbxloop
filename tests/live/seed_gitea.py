"""Seed the live Gitea with the shape the field questions and the
conformance suite need. Idempotent: a second run reuses what the first
made and repairs what is missing.

What it builds, all under ``acme/widgets``:

- ``sbx-admin`` (site admin) and an API token minted for it with basic
  auth on ``POST /users/{name}/tokens``;
- ``dev-alice`` (write collaborator, the reduced-permission token the
  field questions read with), ``rev-bob`` (a second human who reviews) and
  ``ci-bot`` (created with ``--user-type bot``, the closest Gitea has to a
  bot account);
- ``main`` with a README, ``feature/one`` with one commit, an open pull
  request from it with a review comment, a labelled open issue and a
  closed issue;
- branch protection on ``main``: one approval and the status checks
  ``ci`` and ``lint``;
- statuses ``ci`` and ``lint`` green on ``main``'s head, and ``ci`` alone
  on the pull request's head, so "required and present" and "required and
  missing" are both on the forge.

Passwords are generated here or by ``gitea admin user create
--random-password``, tokens by the forge; both land in ``.state/live.env``
and nowhere else. Run: ``python -m tests.live.seed_gitea``.
"""

from __future__ import annotations

import base64
import re
import secrets
import subprocess  # nosec B404 - docker exec against the harness container
import sys
import time
from typing import Any

from tests.live._http import Client, read_env_file, write_env_file
from tests.live.harness import CONTAINERS, LIVE_ENV

WEB = "https://localhost:3000"
API = f"{WEB}/api/v1"
ORG, REPO = "acme", "widgets"
SLUG = f"{ORG}/{REPO}"
ADMIN, DEVELOPER, REVIEWER, BOT = "sbx-admin", "dev-alice", "rev-bob", "ci-bot"
BRANCH = "feature/one"
REQUIRED = ["ci", "lint"]
HUMAN_SCOPES = ["write:repository", "write:issue", "read:user", "read:organization"]


def gitea_cli(*args: str) -> str:
    """``gitea`` inside the container, as the ``git`` user it insists on."""
    result = subprocess.run(  # nosec B603 B607 - fixed argv, no secret in it
        ["docker", "exec", "-u", "git", CONTAINERS["gitea"], "gitea", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gitea {args[:3]} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def wait_for_api(timeout_s: float = 300) -> str:
    anon = Client(API, {}, "anonymous")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            response = anon.get("/version", check=False)
            if response.ok:
                return str(response.data["version"])
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("Gitea did not answer /api/v1/version")
        time.sleep(5)


def ensure_admin(state: dict[str, str]) -> str:
    password = state.get("GITEA_ADMIN_PASSWORD", "")
    if password and Client.basic(API, ADMIN, password, "admin").get("/user", check=False).ok:
        return password
    listed = gitea_cli("admin", "user", "list", "--admin")
    if re.search(rf"\s{re.escape(ADMIN)}\s", listed):
        raise RuntimeError(
            f"{ADMIN} exists but .state/live.env has no working password for it; "
            "run `python -m tests.live.harness down --volumes` and seed again"
        )
    out = gitea_cli(
        "admin", "user", "create", "--admin", "--username", ADMIN,
        "--email", f"{ADMIN}@example.invalid", "--random-password",
        "--must-change-password=false",
    )  # fmt: skip
    match = re.search(r"generated random password is '([^']+)'", out)
    if not match:
        raise RuntimeError("gitea admin user create printed no generated password")
    return match.group(1)


def mint_token(user: str, password: str, name: str, scopes: list[str]) -> str:
    """A fresh token via basic auth (the only way Gitea mints one over the
    API); an older token of the same name is deleted first."""
    basic = Client.basic(API, user, password, user)
    for token in basic.get(f"/users/{user}/tokens").data or []:
        if token.get("name") == name:
            basic.delete(f"/users/{user}/tokens/{token['id']}")
    created = basic.post(f"/users/{user}/tokens", {"name": name, "scopes": scopes}).data
    return str(created["sha1"])


def token_works(token: str) -> bool:
    return bool(token) and Client(API, token_header(token), "probe").get("/user", check=False).ok


def token_header(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}"}


def ensure_user(admin: Client, state: dict[str, str], user: str, key: str) -> str:
    password = state.get(key, "")
    if admin.get(f"/users/{user}", check=False).status == 404:
        password = secrets.token_urlsafe(18)
        admin.post(
            "/admin/users",
            {
                "username": user,
                "email": f"{user}@example.invalid",
                "password": password,
                "must_change_password": False,
                "source_id": 0,
                "login_name": user,
            },
        )
    elif not password:
        password = secrets.token_urlsafe(18)
        admin.patch(
            f"/admin/users/{user}",
            {
                "login_name": user,
                "source_id": 0,
                "password": password,
                "must_change_password": False,
            },
        )
    return password


def ensure_bot(admin: Client, state: dict[str, str]) -> str:
    if admin.get(f"/users/{BOT}", check=False).status == 404:
        gitea_cli(
            "admin", "user", "create", "--user-type", "bot", "--username", BOT,
            "--email", f"{BOT}@example.invalid", "--must-change-password=false",
        )  # fmt: skip
    # A bot made before the flag above still owes a password change, which
    # refuses every API call its token makes.
    admin.patch(
        f"/admin/users/{BOT}", {"login_name": BOT, "source_id": 0, "must_change_password": False}
    )
    token = state.get("GITEA_BOT_TOKEN", "")
    if token_works(token):
        return token
    name = f"sbxloop-live-{secrets.token_hex(3)}"
    out = gitea_cli(
        "admin", "user", "generate-access-token", "--username", BOT, "--token-name", name,
        "--scopes", "write:repository,write:issue,read:user", "--raw",
    )  # fmt: skip
    return out.strip()


def b64(text: str | bytes) -> str:
    return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()


def seed(admin: Client, developer: Client, reviewer: Client) -> dict[str, Any]:
    if admin.get(f"/orgs/{ORG}", check=False).status == 404:
        admin.post("/orgs", {"username": ORG, "visibility": "public"})
    if admin.get(f"/repos/{SLUG}", check=False).status == 404:
        admin.post(
            f"/orgs/{ORG}/repos",
            {"name": REPO, "auto_init": True, "default_branch": "main", "readme": "Default"},
        )
    for user in (DEVELOPER, REVIEWER, BOT):
        admin.put(f"/repos/{SLUG}/collaborators/{user}", {"permission": "write"})

    if admin.get(f"/repos/{SLUG}/branches/{BRANCH}", check=False).status == 404:
        admin.post(
            f"/repos/{SLUG}/branches", {"new_branch_name": BRANCH, "old_branch_name": "main"}
        )
    if (
        developer.get(f"/repos/{SLUG}/contents/one.txt", query={"ref": BRANCH}, check=False).status
        == 404
    ):
        developer.post(
            f"/repos/{SLUG}/contents/one.txt",
            {"content": b64("one\ntwo\nthree\n"), "message": "Add one.txt", "branch": BRANCH},
        )

    pulls = developer.get(f"/repos/{SLUG}/pulls", query={"state": "open"}).data or []
    pull = next((p for p in pulls if p["head"]["ref"] == BRANCH), None)
    if pull is None:
        pull = developer.post(
            f"/repos/{SLUG}/pulls",
            {"head": BRANCH, "base": "main", "title": "Add one.txt", "body": "Seeded change."},
        ).data
    number = int(pull["number"])
    reviews = reviewer.get(f"/repos/{SLUG}/pulls/{number}/reviews").data or []
    if not any(r.get("user", {}).get("login") == REVIEWER for r in reviews):
        reviewer.post(
            f"/repos/{SLUG}/pulls/{number}/reviews",
            {
                "event": "COMMENT",
                "body": "One question.",
                "comments": [{"path": "one.txt", "new_position": 2, "body": "Why two?"}],
            },
        )

    labels = admin.get(f"/repos/{SLUG}/labels").data or []
    label = next((lb for lb in labels if lb["name"] == "bug"), None)
    if label is None:
        label = admin.post(f"/repos/{SLUG}/labels", {"name": "bug", "color": "#ee0701"}).data
    issues = admin.get(f"/repos/{SLUG}/issues", query={"state": "all", "type": "issues"}).data or []
    titles = {i["title"]: i for i in issues}
    if "Seeded open issue" not in titles:
        developer.post(
            f"/repos/{SLUG}/issues", {"title": "Seeded open issue", "labels": [label["id"]]}
        )
    if "Seeded closed issue" not in titles:
        closed = developer.post(f"/repos/{SLUG}/issues", {"title": "Seeded closed issue"}).data
        developer.patch(f"/repos/{SLUG}/issues/{closed['number']}", {"state": "closed"})

    rule = {
        "rule_name": "main",
        "enable_push": False,
        "required_approvals": 1,
        "enable_status_check": True,
        "status_check_contexts": REQUIRED,
        "block_on_rejected_reviews": True,
        "dismiss_stale_approvals": True,
    }
    if admin.get(f"/repos/{SLUG}/branch_protections/main", check=False).status == 404:
        admin.post(f"/repos/{SLUG}/branch_protections", rule)
    else:
        admin.patch(f"/repos/{SLUG}/branch_protections/main", rule)

    base_sha = admin.get(f"/repos/{SLUG}/branches/main").data["commit"]["id"]
    head_sha = admin.get(f"/repos/{SLUG}/branches/{BRANCH}").data["commit"]["id"]
    for sha, contexts in ((base_sha, REQUIRED), (head_sha, ["ci"])):
        present = {s["context"] for s in admin.get(f"/repos/{SLUG}/commits/{sha}/statuses").data}
        for context in contexts:
            if context not in present:
                admin.post(
                    f"/repos/{SLUG}/statuses/{sha}",
                    {"state": "success", "context": context, "description": f"{context} passed"},
                )
    return {"pull": number, "base_sha": base_sha, "head_sha": head_sha}


def main() -> int:
    state = read_env_file(LIVE_ENV)
    version = wait_for_api()
    admin_password = ensure_admin(state)
    admin_token = state.get("GITEA_ADMIN_TOKEN", "")
    if not token_works(admin_token):
        admin_token = mint_token(ADMIN, admin_password, "sbxloop-live-admin", ["all"])
    admin = Client(API, token_header(admin_token), "site admin")
    dev_password = ensure_user(admin, state, DEVELOPER, "GITEA_DEVELOPER_PASSWORD")
    rev_password = ensure_user(admin, state, REVIEWER, "GITEA_REVIEWER_PASSWORD")
    dev_token = state.get("GITEA_TOKEN", "")
    if not token_works(dev_token):
        dev_token = mint_token(DEVELOPER, dev_password, "sbxloop-live", HUMAN_SCOPES)
    rev_token = state.get("GITEA_REVIEWER_TOKEN", "")
    if not token_works(rev_token):
        rev_token = mint_token(REVIEWER, rev_password, "sbxloop-live", HUMAN_SCOPES)
    bot_token = ensure_bot(admin, state)
    developer = Client(API, token_header(dev_token), "write collaborator")
    reviewer = Client(API, token_header(rev_token), "write collaborator")
    seeded = seed(admin, developer, reviewer)
    write_env_file(
        LIVE_ENV,
        {
            "SBXLOOP_LIVE_CA_FILE": str(LIVE_ENV.parent / "certs" / "ca.crt"),
            "SBXLOOP_LIVE_GITEA_URL": API,
            "SBXLOOP_LIVE_GITEA_VERSION": version,
            "SBXLOOP_LIVE_GITEA_REPO": SLUG,
            "SBXLOOP_LIVE_GITEA_PULL": str(seeded["pull"]),
            "SBXLOOP_LIVE_GITEA_ADMIN": ADMIN,
            "SBXLOOP_LIVE_GITEA_DEVELOPER": DEVELOPER,
            "SBXLOOP_LIVE_GITEA_REVIEWER": REVIEWER,
            "SBXLOOP_LIVE_GITEA_BOT": BOT,
            "GITEA_ADMIN_PASSWORD": admin_password,
            "GITEA_DEVELOPER_PASSWORD": dev_password,
            "GITEA_REVIEWER_PASSWORD": rev_password,
            "GITEA_ADMIN_TOKEN": admin_token,
            "GITEA_TOKEN": dev_token,
            "GITEA_REVIEWER_TOKEN": rev_token,
            "GITEA_BOT_TOKEN": bot_token,
        },
    )
    print(f"gitea {version} at {WEB}")
    print(f"  api root        SBXLOOP_LIVE_GITEA_URL = {API}")
    print(f"  repository      {SLUG}, pull request #{seeded['pull']} from {BRANCH}")
    print(f"  site admin      {ADMIN}         token in GITEA_ADMIN_TOKEN")
    print(f"  developer       {DEVELOPER}     token in GITEA_TOKEN (write collaborator)")
    print(f"  reviewer        {REVIEWER}       token in GITEA_REVIEWER_TOKEN")
    print(f"  bot account     {BOT}        token in GITEA_BOT_TOKEN")
    print(f"  protection      main: 1 approval, required checks {', '.join(REQUIRED)}")
    print(f"  env file        {LIVE_ENV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
