"""Seed the live GitLab CE with the shape the field questions and the
conformance suite need. Idempotent: a second run reuses what the first
made and repairs what is missing.

What it builds, all under ``acme/widgets``:

- a personal access token for ``root`` (scopes ``api``, ``sudo``), minted
  with ``gitlab-rails runner`` because a token is needed to mint a token
  over the API;
- ``dev-alice`` (Developer, the reduced-permission token the field
  questions read with), ``rev-bob`` (Developer, a second human who
  reviews), and a project access token at Developer, whose bot user is
  what V3 inspects;
- ``main`` with a README, ``feature/one`` with one commit, an open merge
  request from it with a diff discussion, a labelled open issue and a
  closed issue;
- ``main`` protected with no one allowed to push and Developers allowed to
  merge; the project set to merge only when the pipeline succeeds and every
  discussion is resolved; one required approval attempted through both
  approval APIs (Premium on GitLab; the seed records what CE answers);
- external commit statuses ``ci`` and ``lint`` green on ``main``'s head and
  ``ci`` alone on the merge request's head.

Tokens land in ``.state/live.env`` and nowhere else. Run:
``python -m tests.live.seed_gitlab``.
"""

from __future__ import annotations

import datetime
import re
import secrets
import subprocess  # nosec B404 - docker exec against the harness container
import sys
import time
from typing import Any
from urllib.parse import quote

from tests.live._http import Client, read_env_file, write_env_file
from tests.live.harness import CONTAINERS, LIVE_ENV

WEB = "https://localhost:8929"
API = f"{WEB}/api/v4"
GROUP, PROJECT = "acme", "widgets"
SLUG = f"{GROUP}/{PROJECT}"
DEVELOPER, REVIEWER = "dev-alice", "rev-bob"
BRANCH = "feature/one"
REQUIRED = ["ci", "lint"]
DEVELOPER_ACCESS = 30

MINT_ROOT_TOKEN = """
user = User.find_by_username('root')
user.personal_access_tokens.active.where(name: 'sbxloop-live-admin').each(&:revoke!)
result = PersonalAccessTokens::CreateService.new(
  current_user: user, target_user: user,
  organization_id: user.namespace.organization_id,
  params: { name: 'sbxloop-live-admin', scopes: %w[api sudo read_user],
            expires_at: 60.days.from_now.to_date }
).execute
raise result.message.to_s unless result.success?
puts "SBXLOOP_TOKEN=#{result.payload[:personal_access_token].token}"
"""


def expiry(days: int = 60) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def wait_for_api(timeout_s: float = 600) -> None:
    anon = Client(API, {}, "anonymous")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if anon.get("/projects", query={"per_page": 1}, check=False).status in (200, 401):
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("GitLab did not answer /api/v4")
        time.sleep(10)


def header(token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token}


def token_works(token: str) -> bool:
    return bool(token) and Client(API, header(token), "probe").get("/user", check=False).ok


def mint_root_token() -> str:
    result = subprocess.run(  # nosec B603 B607 - fixed argv; the token comes back on stdout
        ["docker", "exec", "-i", CONTAINERS["gitlab"], "gitlab-rails", "runner", "-"],
        input=MINT_ROOT_TOKEN,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    match = re.search(r"^SBXLOOP_TOKEN=(\S+)$", result.stdout, re.MULTILINE)
    if result.returncode != 0 or not match:
        raise RuntimeError(f"gitlab-rails runner did not mint a token: {result.stderr[-600:]}")
    return match.group(1)


def ensure_user(admin: Client, state: dict[str, str], username: str, key: str) -> tuple[int, str]:
    found = admin.get("/users", query={"username": username}).data
    password = state.get(key, "")
    if not found:
        password = secrets.token_urlsafe(18)
        created = admin.post(
            "/users",
            {
                "email": f"{username}@example.invalid",
                "username": username,
                "name": username,
                "password": password,
                "skip_confirmation": True,
            },
        ).data
        return int(created["id"]), password
    user_id = int(found[0]["id"])
    if not password:
        password = secrets.token_urlsafe(18)
        admin.put(f"/users/{user_id}", {"password": password})
    return user_id, password


def ensure_user_token(admin: Client, state: dict[str, str], user_id: int, key: str) -> str:
    token = state.get(key, "")
    if token_works(token):
        return token
    created = admin.post(
        f"/users/{user_id}/personal_access_tokens",
        {"name": "sbxloop-live", "scopes": ["api"], "expires_at": expiry()},
    ).data
    return str(created["token"])


def seed(admin: Client, state: dict[str, str]) -> dict[str, Any]:
    dev_id, dev_password = ensure_user(admin, state, DEVELOPER, "GITLAB_DEVELOPER_PASSWORD")
    rev_id, rev_password = ensure_user(admin, state, REVIEWER, "GITLAB_REVIEWER_PASSWORD")
    dev_token = ensure_user_token(admin, state, dev_id, "GITLAB_TOKEN")
    rev_token = ensure_user_token(admin, state, rev_id, "GITLAB_REVIEWER_TOKEN")

    group = admin.get(f"/groups/{GROUP}", check=False)
    if group.status == 404:
        group = admin.post("/groups", {"name": GROUP, "path": GROUP, "visibility": "private"})
    encoded = quote(SLUG, safe="")
    project = admin.get(f"/projects/{encoded}", check=False)
    if project.status == 404:
        project = admin.post(
            "/projects",
            {
                "name": PROJECT,
                "path": PROJECT,
                "namespace_id": group.data["id"],
                "initialize_with_readme": True,
                "default_branch": "main",
                "visibility": "private",
            },
        )
    pid = int(project.data["id"])
    members = {m["id"] for m in admin.get(f"/projects/{pid}/members").data}
    for user_id in (dev_id, rev_id):
        if user_id not in members:
            admin.post(
                f"/projects/{pid}/members", {"user_id": user_id, "access_level": DEVELOPER_ACCESS}
            )

    bot_token = state.get("GITLAB_BOT_TOKEN", "")
    if not token_works(bot_token):
        created = admin.post(
            f"/projects/{pid}/access_tokens",
            {
                "name": "ci-bot",
                "scopes": ["api"],
                "access_level": DEVELOPER_ACCESS,
                "expires_at": expiry(),
            },
        ).data
        bot_token = str(created["token"])
    bot = Client(API, header(bot_token), "project access token (Developer)").get("/user").data

    developer = Client(API, header(dev_token), "Developer")
    if (
        admin.get(
            f"/projects/{pid}/repository/branches/{quote(BRANCH, safe='')}", check=False
        ).status
        == 404
    ):
        admin.post(f"/projects/{pid}/repository/branches", {"branch": BRANCH, "ref": "main"})
    if (
        developer.get(
            f"/projects/{pid}/repository/files/one.txt", query={"ref": BRANCH}, check=False
        ).status
        == 404
    ):
        developer.post(
            f"/projects/{pid}/repository/commits",
            {
                "branch": BRANCH,
                "commit_message": "Add one.txt",
                "actions": [
                    {"action": "create", "file_path": "one.txt", "content": "one\ntwo\nthree\n"}
                ],
            },
        )

    admin.put(
        f"/projects/{pid}",
        {
            "only_allow_merge_if_pipeline_succeeds": True,
            "only_allow_merge_if_all_discussions_are_resolved": True,
            "allow_merge_on_skipped_pipeline": False,
        },
    )
    approvals = {
        "POST /projects/:id/approvals": admin.post(
            f"/projects/{pid}/approvals", {"approvals_before_merge": 1}, check=False
        ).status,
        "POST /projects/:id/approval_rules": admin.post(
            f"/projects/{pid}/approval_rules",
            {"name": "any reviewer", "approvals_required": 1},
            check=False,
        ).status,
    }

    protected = admin.get(f"/projects/{pid}/protected_branches/main", check=False)
    wanted = protected.ok and (
        [a["access_level"] for a in protected.data["push_access_levels"]] == [0]
        and [a["access_level"] for a in protected.data["merge_access_levels"]] == [DEVELOPER_ACCESS]
    )
    if not wanted:
        if protected.ok:
            admin.delete(f"/projects/{pid}/protected_branches/main")
        admin.post(
            f"/projects/{pid}/protected_branches",
            {"name": "main", "push_access_level": 0, "merge_access_level": DEVELOPER_ACCESS},
        )

    opened = developer.get(
        f"/projects/{pid}/merge_requests", query={"state": "opened", "source_branch": BRANCH}
    ).data
    if opened:
        iid = int(opened[0]["iid"])
    else:
        iid = int(
            developer.post(
                f"/projects/{pid}/merge_requests",
                {"source_branch": BRANCH, "target_branch": "main", "title": "Add one.txt"},
            ).data["iid"]
        )
    reviewer = Client(API, header(rev_token), "Developer")
    discussions = reviewer.get(f"/projects/{pid}/merge_requests/{iid}/discussions").data
    if not any(
        n.get("type") == "DiffNote" and n["author"]["username"] == REVIEWER
        for d in discussions
        for n in d["notes"]
    ):
        refs = None
        for _ in range(30):
            refs = reviewer.get(f"/projects/{pid}/merge_requests/{iid}").data.get("diff_refs")
            if refs and refs.get("head_sha"):
                break
            time.sleep(2)
        if not refs:
            raise RuntimeError("the merge request never reported diff_refs")
        reviewer.post(
            f"/projects/{pid}/merge_requests/{iid}/discussions",
            {
                "body": "Why two?",
                "position": {
                    "base_sha": refs["base_sha"],
                    "start_sha": refs["start_sha"],
                    "head_sha": refs["head_sha"],
                    "position_type": "text",
                    "old_path": "one.txt",
                    "new_path": "one.txt",
                    "new_line": 2,
                },
            },
        )

    if not any(lb["name"] == "bug" for lb in admin.get(f"/projects/{pid}/labels").data):
        admin.post(f"/projects/{pid}/labels", {"name": "bug", "color": "#ee0701"})
    titles = {
        i["title"] for i in admin.get(f"/projects/{pid}/issues", query={"per_page": 100}).data
    }
    if "Seeded open issue" not in titles:
        developer.post(f"/projects/{pid}/issues", {"title": "Seeded open issue", "labels": "bug"})
    if "Seeded closed issue" not in titles:
        closed = developer.post(f"/projects/{pid}/issues", {"title": "Seeded closed issue"}).data
        developer.put(f"/projects/{pid}/issues/{closed['iid']}", {"state_event": "close"})

    base_sha = admin.get(f"/projects/{pid}/repository/branches/main").data["commit"]["id"]
    head_sha = admin.get(f"/projects/{pid}/merge_requests/{iid}").data["sha"]
    for sha, contexts in ((base_sha, REQUIRED), (head_sha, ["ci"])):
        statuses = admin.get(f"/projects/{pid}/repository/commits/{sha}/statuses").data
        present = {s["name"] for s in statuses}
        for context in contexts:
            if context not in present:
                admin.post(f"/projects/{pid}/statuses/{sha}", {"state": "success", "name": context})

    return {
        "project_id": pid,
        "iid": iid,
        "developer_password": dev_password,
        "reviewer_password": rev_password,
        "dev_token": dev_token,
        "rev_token": rev_token,
        "bot_token": bot_token,
        "bot_username": bot["username"],
        "approvals": approvals,
    }


def main() -> int:
    state = read_env_file(LIVE_ENV)
    wait_for_api()
    admin_token = state.get("GITLAB_ADMIN_TOKEN", "")
    if not token_works(admin_token):
        admin_token = mint_root_token()
    admin = Client(API, header(admin_token), "administrator")
    version = admin.get("/version").data
    seeded = seed(admin, state)
    write_env_file(
        LIVE_ENV,
        {
            "SBXLOOP_LIVE_CA_FILE": str(LIVE_ENV.parent / "certs" / "ca.crt"),
            "SBXLOOP_LIVE_GITLAB_URL": API,
            "SBXLOOP_LIVE_GITLAB_VERSION": f"{version['version']} ({version.get('revision', '')})",
            "SBXLOOP_LIVE_GITLAB_REPO": SLUG,
            "SBXLOOP_LIVE_GITLAB_PROJECT_ID": str(seeded["project_id"]),
            "SBXLOOP_LIVE_GITLAB_MR": str(seeded["iid"]),
            "SBXLOOP_LIVE_GITLAB_DEVELOPER": DEVELOPER,
            "SBXLOOP_LIVE_GITLAB_REVIEWER": REVIEWER,
            "SBXLOOP_LIVE_GITLAB_BOT": seeded["bot_username"],
            "GITLAB_DEVELOPER_PASSWORD": seeded["developer_password"],
            "GITLAB_REVIEWER_PASSWORD": seeded["reviewer_password"],
            "GITLAB_ADMIN_TOKEN": admin_token,
            "GITLAB_TOKEN": seeded["dev_token"],
            "GITLAB_REVIEWER_TOKEN": seeded["rev_token"],
            "GITLAB_BOT_TOKEN": seeded["bot_token"],
        },
    )
    print(f"gitlab {version['version']} at {WEB}")
    print(f"  api root        SBXLOOP_LIVE_GITLAB_URL = {API}")
    print(f"  repository      {SLUG} (id {seeded['project_id']}), merge request !{seeded['iid']}")
    print("  administrator   root              token in GITLAB_ADMIN_TOKEN")
    print(f"  developer       {DEVELOPER}         token in GITLAB_TOKEN (Developer)")
    print(f"  reviewer        {REVIEWER}           token in GITLAB_REVIEWER_TOKEN (Developer)")
    print(f"  bot account     {seeded['bot_username']}  token in GITLAB_BOT_TOKEN")
    print("  protection      main: no push, Developers merge, pipeline must succeed")
    for call, status in seeded["approvals"].items():
        print(f"  approvals       {call} -> HTTP {status}")
    print(f"  env file        {LIVE_ENV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
