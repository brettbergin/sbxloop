"""The six field questions of #1016, asked of live forges.

Each probe runs against a seeded forge (``seed_gitlab`` / ``seed_gitea``),
records every exchange it makes (method, path, body, status, the response
trimmed to the fields a finding rests on; credentials redacted, headers
never kept) and returns its findings: the facts the spike's capability
matrix and decisions are written from, and the facts
``test_field_verify.py`` holds the forge to. A probe that writes works on
a branch of its own, named with a fresh suffix, and never moves ``main``.

``python -m tests.live.fieldverify --out DIR`` runs every probe for every
configured forge and writes one JSON transcript per question and forge.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tests.live._http import Client, Recorder
from tests.live.env import LiveForge, live_forge

# Bytes no text codec round-trips: a NUL, a lone high byte, a PNG header.
BINARY = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\xff\xfe\x80"


@dataclass
class Evidence:
    question: str
    forge: str
    version: str
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    findings: dict[str, Any] = field(default_factory=dict)


def b64(data: str | bytes) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def suffix() -> str:
    return secrets.token_hex(3)


# -- GitLab ---------------------------------------------------------------------

GITLAB_SETTINGS = (
    "only_allow_merge_if_pipeline_succeeds",
    "only_allow_merge_if_all_discussions_are_resolved",
    "allow_merge_on_skipped_pipeline",
    "merge_method",
    "squash_option",
    "merge_pipelines_enabled",
    "merge_trains_enabled",
    "approvals_before_merge",
    "permissions",
)
MR_STATE = (
    "iid",
    "draft",
    "merge_status",
    "detailed_merge_status",
    "blocking_discussions_resolved",
)
SETTLING = frozenset({"checking", "unchecked", "preparing", "approvals_syncing", ""})


class GitlabProbe:
    def __init__(self, forge: LiveForge) -> None:
        self.forge = forge
        self.pid = forge.get("SBXLOOP_LIVE_GITLAB_PROJECT_ID")
        self.p = f"/projects/{self.pid}"

    def clients(self, rec: Recorder) -> dict[str, Client]:
        f = self.forge
        return {
            "dev": f.client("GITLAB_TOKEN", "Developer (personal access token)").recording(rec),
            "rev": f.client("GITLAB_REVIEWER_TOKEN", "Developer (reviewer)").recording(rec),
            "bot": f.client("GITLAB_BOT_TOKEN", "Developer (project access token)").recording(rec),
            "admin": f.client("GITLAB_ADMIN_TOKEN", "administrator").recording(rec),
        }

    def evidence(self, question: str) -> tuple[Evidence, dict[str, Client]]:
        rec = Recorder()
        clients = self.clients(rec)
        version = clients["dev"].get("/version").data
        ev = Evidence(question, "gitlab", f"GitLab CE {version['version']} ({version['revision']})")
        ev.exchanges = rec.exchanges
        rec.exchanges.clear()
        return ev, clients

    def branch_with_file(self, dev: Client, branch: str, path: str, content: str) -> None:
        dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "start_branch": "main",
                "commit_message": f"Add {path}",
                "actions": [{"action": "create", "file_path": path, "content": content}],
            },
            keep=("id", "parent_ids"),
        )

    def open_mr(self, dev: Client, branch: str, title: str) -> int:
        mr = dev.post(
            f"{self.p}/merge_requests",
            {"source_branch": branch, "target_branch": "main", "title": title},
            keep=MR_STATE,
        )
        return int(mr.data["iid"])

    def diff_refs(self, client: Client, iid: int) -> dict[str, str]:
        for _ in range(60):
            refs = client.get(f"{self.p}/merge_requests/{iid}").data.get("diff_refs")
            if refs and refs.get("head_sha"):
                return dict(refs)
            time.sleep(1)
        raise RuntimeError(f"merge request !{iid} never reported diff_refs")

    def settled(self, client: Client, iid: int, note: str) -> dict[str, Any]:
        """The merge request once GitLab has finished re-deciding its status."""
        data: dict[str, Any] = {}
        for _ in range(45):
            data = client.get(f"{self.p}/merge_requests/{iid}").data
            if data.get("detailed_merge_status", "") not in SETTLING:
                break
            time.sleep(2)
        client.get(f"{self.p}/merge_requests/{iid}", keep=(*MR_STATE, "head_pipeline"), note=note)
        return data

    def discussion(self, client: Client, iid: int, discussion_id: str, note: str) -> dict[str, Any]:
        listed = client.get(f"{self.p}/merge_requests/{iid}/discussions").data
        found = next((d for d in listed if d["id"] == discussion_id), None)
        client.get(
            f"{self.p}/merge_requests/{iid}/discussions/{discussion_id}",
            note=note,
            keep=("id", "individual_note", "notes"),
            check=False,
        )
        return found or {}

    # V1 -------------------------------------------------------------------------

    def v1(self) -> Evidence:
        ev, c = self.evidence("V1")
        dev, rev = c["dev"], c["rev"]
        s = suffix()
        branch, path = f"v1/{s}", f"v1-{s}.txt"
        self.branch_with_file(dev, branch, path, "alpha\nbeta\ngamma\n")
        iid = self.open_mr(dev, branch, f"V1 {s}")
        refs = self.diff_refs(dev, iid)
        created = rev.post(
            f"{self.p}/merge_requests/{iid}/discussions",
            {
                "body": "beta looks wrong",
                "position": {
                    "base_sha": refs["base_sha"],
                    "start_sha": refs["start_sha"],
                    "head_sha": refs["head_sha"],
                    "position_type": "text",
                    "old_path": path,
                    "new_path": path,
                    "new_line": 2,
                },
            },
            keep=("id", "individual_note", "notes"),
        ).data
        discussion_id = str(created["id"])
        root = created["notes"][0]
        dev.post(
            f"{self.p}/merge_requests/{iid}/discussions/{discussion_id}/notes",
            {"body": "fixed in the next push"},
            keep=("id", "type", "resolvable", "resolved", "author"),
        )
        resolved = dev.put(
            f"{self.p}/merge_requests/{iid}/discussions/{discussion_id}",
            {"resolved": True},
            keep=("id", "notes"),
            note="the merge request's author resolves the reviewer's discussion",
        ).data
        before = self.settled(dev, iid, "after resolving")

        dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "commit_message": "Touch another file",
                "actions": [{"action": "create", "file_path": f"{path}.other", "content": "x\n"}],
            },
            keep=("id",),
            note="a push that leaves the discussed line alone",
        )
        after_push = self.discussion(rev, iid, discussion_id, "after a push elsewhere")
        dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "commit_message": "Rewrite the discussed line",
                "actions": [
                    {"action": "update", "file_path": path, "content": "alpha\nBETA\ngamma\n"}
                ],
            },
            keep=("id",),
            note="a push that rewrites the discussed line",
        )
        after_rewrite = self.discussion(rev, iid, discussion_id, "after the line is rewritten")
        rev.put(
            f"{self.p}/merge_requests/{iid}/discussions/{discussion_id}",
            {"resolved": False},
            keep=("id",),
            note="the reviewer reopens it",
        )
        reopened = self.discussion(rev, iid, discussion_id, "after reopening")

        def root_note(d: dict[str, Any]) -> dict[str, Any]:
            return d["notes"][0] if d.get("notes") else {}

        ev.findings = {
            "discussion_id": discussion_id,
            "note_type": root.get("type"),
            "resolvable": root.get("resolvable"),
            "resolved_by_api": all(
                n.get("resolved") for n in resolved["notes"] if n.get("resolvable")
            ),
            "blocking_discussions_resolved_after_resolve": before.get(
                "blocking_discussions_resolved"
            ),
            "id_stable_after_push": after_push.get("id") == discussion_id,
            "resolved_after_push": bool(root_note(after_push).get("resolved")),
            "id_stable_after_line_rewrite": after_rewrite.get("id") == discussion_id,
            "resolved_after_line_rewrite": bool(root_note(after_rewrite).get("resolved")),
            "position_head_sha_moved": (root_note(after_rewrite).get("position") or {}).get(
                "head_sha"
            )
            != refs["head_sha"],
            "reopenable_by_reviewer": not root_note(reopened).get("resolved", True),
            "note_ids_are_integers": isinstance(root.get("id"), int),
            "discussion_id_kind": "40-hex string"
            if len(discussion_id) == 40
            else type(discussion_id).__name__,
        }
        return ev

    # V2 -------------------------------------------------------------------------

    def v2(self) -> Evidence:
        ev, c = self.evidence("V2")
        dev = c["dev"]
        meta = dev.get("/metadata", keep=("version", "enterprise")).data
        protected = dev.get(
            f"{self.p}/protected_branches/main",
            keep=(
                "name",
                "push_access_levels",
                "merge_access_levels",
                "allow_force_push",
                "code_owner_approval_required",
            ),
            check=False,
        )
        listing = dev.get(f"{self.p}/protected_branches", keep=("name",), check=False)
        settings = dev.get(self.p, keep=GITLAB_SETTINGS).data
        premium = {
            path: dev.get(
                f"{self.p}{path}", check=False, keep=("approvals_required", "rules")
            ).status
            for path in (
                "/approvals",
                "/approval_rules",
                "/external_status_checks",
                "/merge_requests/1/approval_state",
                "/merge_requests/1/approval_rules",
            )
        }
        mr_approvals = dev.get(f"{self.p}/merge_requests/1/approvals", check=False)

        s = suffix()
        branch = f"v2/{s}"
        self.branch_with_file(dev, branch, f"v2-{s}.txt", "v2\n")
        iid = self.open_mr(dev, branch, f"V2 {s}")
        head = self.diff_refs(dev, iid)["head_sha"]
        states: dict[str, Any] = {}
        states["no_status"] = self.settled(dev, iid, "no status reported")
        dev.post(
            f"{self.p}/statuses/{head}",
            {"state": "running", "name": "ci"},
            keep=("name", "status", "allow_failure"),
        )
        states["ci_running"] = self.settled(dev, iid, "ci running")
        dev.post(
            f"{self.p}/statuses/{head}",
            {"state": "success", "name": "ci"},
            keep=("name", "status", "allow_failure"),
        )
        states["ci_green_lint_absent"] = self.settled(dev, iid, "ci green, lint never reported")
        dev.post(
            f"{self.p}/statuses/{head}",
            {"state": "failed", "name": "docs", "allow_failure": True},
            keep=("name", "status", "allow_failure"),
        )
        states["allowed_failure_red"] = self.settled(dev, iid, "an allow_failure status red")
        dev.post(
            f"{self.p}/statuses/{head}",
            {"state": "failed", "name": "lint"},
            keep=("name", "status", "allow_failure"),
        )
        states["lint_red"] = self.settled(dev, iid, "lint red")
        statuses = dev.get(
            f"{self.p}/repository/commits/{head}/statuses",
            keep=("name", "status", "allow_failure", "pipeline_id"),
        ).data
        merge = dev.put(
            f"{self.p}/merge_requests/{iid}/merge",
            {},
            check=False,
            keep=("message",),
            note="a Developer asks to merge while a status is red",
        )
        ev.findings = {
            "tier": "CE (enterprise: false)" if meta.get("enterprise") is False else "EE",
            "enterprise": meta.get("enterprise"),
            "developer_reads_protected_branch": protected.status,
            "developer_lists_protected_branches": listing.status,
            "push_access_levels": [
                a["access_level"] for a in protected.data.get("push_access_levels", [])
            ]
            if protected.ok
            else None,
            "merge_access_levels": [
                a["access_level"] for a in protected.data.get("merge_access_levels", [])
            ]
            if protected.ok
            else None,
            "developer_reads_merge_settings": {k: settings.get(k) for k in GITLAB_SETTINGS[:3]},
            "developer_project_access_level": (settings.get("permissions") or {}).get(
                "project_access"
            ),
            "premium_endpoints": premium,
            "mr_approvals_status": mr_approvals.status,
            "mr_approvals_keys": sorted(mr_approvals.data) if mr_approvals.ok else None,
            "detailed_merge_status": {k: v.get("detailed_merge_status") for k, v in states.items()},
            "head_pipeline_status": {
                k: (v.get("head_pipeline") or {}).get("status") for k, v in states.items()
            },
            "statuses_carry_allow_failure": all("allow_failure" in st for st in statuses),
            "merge_refused_status": merge.status,
            "merge_refused_message": (merge.data or {}).get("message")
            if isinstance(merge.data, dict)
            else merge.data,
        }
        ev.exchanges.extend([])
        return ev

    # V3 -------------------------------------------------------------------------

    def v3(self) -> Evidence:
        ev, c = self.evidence("V3")
        dev, bot = c["dev"], c["bot"]
        user_keys = ("id", "username", "name", "bot", "state")
        me = bot.get("/user", keep=user_keys).data
        as_dev = dev.get(f"/users/{me['id']}", keep=user_keys).data
        human = dev.get(
            "/users", query={"username": self.forge.get("SBXLOOP_LIVE_GITLAB_REVIEWER")}
        ).data[0]
        human_full = dev.get(f"/users/{human['id']}", keep=user_keys).data
        search = dev.get("/users", query={"username": me["username"]}, keep=user_keys).data
        note = bot.post(
            f"{self.p}/merge_requests/1/notes",
            {"body": "an automated note"},
            keep=("id", "author", "system"),
        ).data
        members = dev.get(f"{self.p}/members/all", keep=("username", "access_level", "bot")).data
        ev.findings = {
            "bot_username": me["username"],
            "bot_username_pattern": me["username"].startswith(f"project_{self.pid}_bot_"),
            "users_id_bot_flag_for_bot": as_dev.get("bot"),
            "users_id_bot_flag_for_human": human_full.get("bot"),
            "readable_by": "Developer",
            "users_search_carries_bot": "bot" in (search[0] if search else {}),
            "note_author_carries_bot": "bot" in note["author"],
            "members_carry_bot": any("bot" in m for m in members),
        }
        return ev

    # V5 -------------------------------------------------------------------------

    def v5(self) -> Evidence:
        ev, c = self.evidence("V5")
        dev = c["dev"]
        s = suffix()
        branch = f"v5/{s}"
        commit_keys = ("id", "parent_ids", "stats", "message")
        readme = dev.get(f"{self.p}/repository/files/README.md", query={"ref": "main"}).data
        first = dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "start_branch": "main",
                "commit_message": "V5: several files, one commit, a new branch",
                "actions": [
                    {"action": "create", "file_path": f"v5/{s}/a.txt", "content": "a\n"},
                    {
                        "action": "create",
                        "file_path": f"v5/{s}/b.bin",
                        "content": b64(BINARY),
                        "encoding": "base64",
                    },
                    {
                        "action": "update",
                        "file_path": "README.md",
                        "content": b64(base64.b64decode(readme["content"]) + b"\nV5\n"),
                        "encoding": "base64",
                    },
                ],
            },
            keep=commit_keys,
            check=False,
        )
        binary = dev.get(
            f"{self.p}/repository/files/{quote(f'v5/{s}/b.bin', safe='')}",
            query={"ref": branch},
            keep=("file_path", "encoding", "size"),
            check=False,
        )
        second = dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "commit_message": "V5: delete and move",
                "actions": [
                    {"action": "delete", "file_path": f"v5/{s}/a.txt"},
                    {
                        "action": "move",
                        "previous_path": f"v5/{s}/b.bin",
                        "file_path": f"v5/{s}/c.bin",
                    },
                ],
            },
            keep=commit_keys,
            check=False,
        )
        head_before = dev.get(f"{self.p}/repository/branches/{quote(branch, safe='')}").data[
            "commit"
        ]["id"]
        atomic = dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": branch,
                "commit_message": "V5: one good action, one bad",
                "actions": [
                    {"action": "create", "file_path": f"v5/{s}/ok.txt", "content": "ok\n"},
                    {"action": "update", "file_path": f"v5/{s}/missing.txt", "content": "nope\n"},
                ],
            },
            keep=("message",),
            check=False,
        )
        head_after = dev.get(f"{self.p}/repository/branches/{quote(branch, safe='')}").data[
            "commit"
        ]["id"]
        ok_file = dev.get(
            f"{self.p}/repository/files/{quote(f'v5/{s}/ok.txt', safe='')}",
            query={"ref": branch},
            check=False,
        )
        protected = dev.post(
            f"{self.p}/repository/commits",
            {
                "branch": "main",
                "commit_message": "V5: straight onto the protected base",
                "actions": [{"action": "create", "file_path": f"v5-{s}.txt", "content": "no\n"}],
            },
            keep=("message",),
            check=False,
            note="main allows no one to push",
        )
        ev.findings = {
            "multi_file_new_branch": first.status,
            "binary_round_trip": binary.ok and base64.b64decode(binary.data["content"]) == BINARY,
            "delete_and_move": second.status,
            "second_commit_parent_is_first": second.ok
            and first.ok
            and second.data["parent_ids"] == [first.data["id"]],
            "partial_failure_status": atomic.status,
            "partial_failure_left_branch_unmoved": head_before == head_after,
            "partial_failure_wrote_nothing": ok_file.status == 404,
            "protected_base_status": protected.status,
            "protected_base_message": (protected.data or {}).get("message")
            if isinstance(protected.data, dict)
            else protected.data,
        }
        return ev

    # V6 -------------------------------------------------------------------------

    def v6(self) -> Evidence:
        ev, c = self.evidence("V6")
        keys = ("id", "name", "scopes", "active", "revoked", "expires_at", "user_id")
        pat = c["dev"].get("/personal_access_tokens/self", keep=keys)
        project = c["bot"].get("/personal_access_tokens/self", keep=keys)
        listing = c["bot"].get(f"{self.p}/access_tokens", keep=(*keys, "access_level"), check=False)
        listing_dev = c["dev"].get(f"{self.p}/access_tokens", keep=keys, check=False)
        ev.findings = {
            "pat_self_status": pat.status,
            "pat_self_expires_at": pat.data.get("expires_at") if pat.ok else None,
            "project_token_self_status": project.status,
            "project_token_self_expires_at": project.data.get("expires_at") if project.ok else None,
            "project_token_self_scopes": project.data.get("scopes") if project.ok else None,
            "project_access_tokens_list_as_the_token": listing.status,
            "project_access_tokens_list_as_developer": listing_dev.status,
        }
        return ev

    # The rest of the capability matrix ------------------------------------------

    def matrix(self) -> Evidence:
        ev, c = self.evidence("matrix")
        dev, rev = c["dev"], c["rev"]
        trains = dev.get(f"{self.p}/merge_trains", check=False, keep=("message", "error"))
        s = suffix()
        branch = f"matrix/{s}"
        self.branch_with_file(dev, branch, f"matrix-{s}.txt", "m\n")
        mr = dev.post(
            f"{self.p}/merge_requests",
            {"source_branch": branch, "target_branch": "main", "title": f"Draft: matrix {s}"},
            keep=MR_STATE,
        ).data
        iid = int(mr["iid"])
        head = self.diff_refs(dev, iid)["head_sha"]
        drafted = self.settled(dev, iid, "opened with a Draft: title")
        dev.put(
            f"{self.p}/merge_requests/{iid}",
            {"title": f"matrix {s}"},
            keep=("iid", "draft", "title"),
        )
        dev.post(
            f"{self.p}/statuses/{head}", {"state": "success", "name": "ci"}, keep=("name", "status")
        )
        ready = self.settled(dev, iid, "title without the prefix, pipeline green")

        own = dev.post(
            f"{self.p}/merge_requests/{iid}/approve",
            check=False,
            keep=("approved", "user_has_approved", "approved_by"),
            note="the merge request's author approves it",
        )
        dev.post(f"{self.p}/merge_requests/{iid}/unapprove", check=False, keep=("approved",))

        reviewer_id = rev.get("/user").data["id"]
        dev.put(
            f"{self.p}/merge_requests/{iid}",
            {"reviewer_ids": [reviewer_id]},
            keep=("iid", "reviewers"),
        )
        rev.post(
            f"{self.p}/merge_requests/{iid}/draft_notes",
            {"note": "please change this"},
            keep=("id", "note"),
        )
        published = rev.post(
            f"{self.p}/merge_requests/{iid}/draft_notes/bulk_publish",
            {"reviewer_state": "requested_changes"},
            check=False,
            note="the reviewer submits their review as requesting changes",
        )
        reviewers = dev.get(f"{self.p}/merge_requests/{iid}/reviewers", keep=("user", "state")).data
        for discussion in dev.get(f"{self.p}/merge_requests/{iid}/discussions").data:
            if any(n.get("resolvable") and not n.get("resolved") for n in discussion["notes"]):
                dev.put(
                    f"{self.p}/merge_requests/{iid}/discussions/{discussion['id']}",
                    {"resolved": True},
                    keep=("id",),
                    note="the review comment is a discussion; resolve it, leaving the review state",
                )
        requested = self.settled(dev, iid, "after the reviewer requested changes")
        signature = dev.get(
            f"{self.p}/repository/commits/{head}/signature", check=False, keep=("message",)
        )
        dev.put(f"{self.p}/merge_requests/{iid}", {"state_event": "close"}, keep=("iid", "state"))
        ev.findings = {
            "merge_trains_endpoint": trains.status,
            "draft_from_title_prefix": drafted.get("draft"),
            "draft_status_blocks": drafted.get("detailed_merge_status"),
            "draft_cleared_by_retitle": ready.get("draft") is False,
            "ready_status": ready.get("detailed_merge_status"),
            "author_approves_own": own.status,
            "author_approval_counted": (own.data or {}).get("approved")
            if isinstance(own.data, dict)
            else None,
            "request_changes_publish": published.status,
            "reviewer_state": [r.get("state") for r in reviewers],
            "request_changes_blocks_merge": requested.get("detailed_merge_status")
            == "requested_changes",
            "status_after_request_changes": requested.get("detailed_merge_status"),
            "api_commit_signature": signature.status,
        }
        return ev


# -- Gitea ----------------------------------------------------------------------

BRANCH_VIEW = (
    "name",
    "protected",
    "required_approvals",
    "enable_status_check",
    "status_check_contexts",
    "user_can_push",
    "user_can_merge",
    "effective_branch_protection_name",
)
ADMIN_ONLY_FLAGS = (
    "block_on_rejected_reviews",
    "block_on_official_review_requests",
    "block_on_outdated_branch",
    "dismiss_stale_approvals",
    "require_signed_commits",
    "protected_file_patterns",
    "enable_approvals_whitelist",
    "block_admin_merge_override",
)


class GiteaProbe:
    def __init__(self, forge: LiveForge) -> None:
        self.forge = forge
        self.r = f"/repos/{forge.repo}"
        self.web = forge.api_url.removesuffix("/api/v1")

    def evidence(self, question: str) -> tuple[Evidence, dict[str, Client]]:
        rec = Recorder()
        f = self.forge
        clients = {
            "dev": f.client("GITEA_TOKEN", "write collaborator").recording(rec),
            "rev": f.client("GITEA_REVIEWER_TOKEN", "write collaborator (reviewer)").recording(rec),
            "admin": f.client("GITEA_ADMIN_TOKEN", "site administrator").recording(rec),
        }
        version = clients["dev"].get("/version").data["version"]
        rec.exchanges.clear()
        ev = Evidence(question, "gitea", f"Gitea {version}")
        ev.exchanges = rec.exchanges
        return ev, clients

    def swagger(self) -> dict[str, Any]:
        data = Client(self.web, {}, "anonymous").get("/swagger.v1.json").data
        assert isinstance(data, dict)
        return data

    def contents(
        self, dev: Client, body: dict[str, Any], note: str = "", check: bool = False
    ) -> Any:
        return dev.post(
            f"{self.r}/contents", body, keep=("commit", "files"), check=check, note=note
        )

    # V1 -------------------------------------------------------------------------

    def v1(self) -> Evidence:
        ev, c = self.evidence("V1")
        dev = c["dev"]
        number = self.forge.get("SBXLOOP_LIVE_GITEA_PULL")
        reviews = dev.get(
            f"{self.r}/pulls/{number}/reviews", keep=("id", "state", "user", "comments_count")
        ).data
        review = next(r for r in reviews if r.get("comments_count"))
        comments = dev.get(
            f"{self.r}/pulls/{number}/reviews/{review['id']}/comments",
            keep=("id", "path", "position", "resolver", "pull_request_review_id"),
        ).data
        paths = self.swagger()["paths"]
        review_paths = sorted(
            p for p in paths if "/pulls/" in p and ("review" in p or "comment" in p)
        )
        ev.exchanges.append(
            {
                "as": "anonymous",
                "request": "GET /swagger.v1.json",
                "status": 200,
                "response": {"paths under /pulls/ naming a review or comment": review_paths},
            }
        )
        ev.findings = {
            "comment_has_resolver_field": all("resolver" in cm for cm in comments),
            "resolver_value": comments[0].get("resolver") if comments else None,
            "api_paths_naming_resolve": sorted(p for p in paths if "resolve" in p.lower()),
            "review_comment_reply_path": any("repl" in p for p in review_paths),
            "review_paths": review_paths,
        }
        return ev

    # V3 -------------------------------------------------------------------------

    def v3(self) -> Evidence:
        ev, c = self.evidence("V3")
        dev = c["dev"]
        bot = dev.get(f"/users/{self.forge.get('SBXLOOP_LIVE_GITEA_BOT')}").data
        human = dev.get(f"/users/{self.forge.get('SBXLOOP_LIVE_GITEA_REVIEWER')}").data
        ev.exchanges[-2]["response"] = sorted(bot)
        ev.exchanges[-1]["response"] = sorted(human)
        actions = dev.get("/users/gitea-actions", check=False, keep=("message",))
        user_schema = sorted(self.swagger()["definitions"]["User"]["properties"])
        ev.findings = {
            "bot_account_created_as": "gitea admin user create --user-type bot",
            "bot_and_human_keys_identical": sorted(bot) == sorted(human),
            "differing_values": sorted(
                k
                for k in bot
                if k
                not in (
                    "id",
                    "login",
                    "username",
                    "email",
                    "avatar_url",
                    "html_url",
                    "created",
                    "last_login",
                    "login_name",
                )
                and bot.get(k) != human.get(k)
            ),
            "user_schema_has_type": any(
                k in user_schema for k in ("type", "user_type", "is_bot", "bot")
            ),
            "actions_user_lookup_status": actions.status,
        }
        return ev

    # V4 -------------------------------------------------------------------------

    def v4(self) -> Evidence:
        ev, c = self.evidence("V4")
        dev, admin = c["dev"], c["admin"]
        listing = dev.get(f"{self.r}/branch_protections", check=False, keep=("message",))
        one = dev.get(f"{self.r}/branch_protections/main", check=False, keep=("message",))
        branch_view = dev.get(f"{self.r}/branches/main", keep=BRANCH_VIEW).data
        perms = dev.get(self.r, keep=("permissions",)).data
        full = admin.get(
            f"{self.r}/branch_protections/main", keep=("rule_name", *ADMIN_ONLY_FLAGS)
        ).data

        s = suffix()
        branch = f"v4/{s}"
        self.contents(
            dev,
            {
                "branch": "main",
                "new_branch": branch,
                "message": "V4",
                "files": [{"operation": "create", "path": f"v4-{s}.txt", "content": b64("v4\n")}],
            },
            check=True,
        )
        pull = dev.post(
            f"{self.r}/pulls",
            {"head": branch, "base": "main", "title": f"V4 {s}"},
            keep=("number", "mergeable", "head"),
        ).data
        number, head = pull["number"], pull["head"]["sha"]
        status_keys = ("state", "total_count", "statuses")
        observed: dict[str, Any] = {}

        def look(label: str) -> None:
            combined = dev.get(f"{self.r}/commits/{head}/status", keep=status_keys, note=label).data
            pr = dev.get(f"{self.r}/pulls/{number}", keep=("number", "mergeable")).data
            merge = dev.post(
                f"{self.r}/pulls/{number}/merge",
                {"Do": "merge"},
                check=False,
                keep=("message",),
                note=f"merge attempt: {label}",
            )
            observed[label] = {
                "combined_state": combined.get("state"),
                "contexts": sorted(st["context"] for st in combined.get("statuses") or []),
                "mergeable": pr.get("mergeable"),
                "merge_status": merge.status,
                "merge_message": (merge.data or {}).get("message")
                if isinstance(merge.data, dict)
                else merge.data,
            }

        look("no status")
        dev.post(
            f"{self.r}/statuses/{head}",
            {"state": "success", "context": "ci"},
            keep=("context", "status"),
        )
        dev.post(
            f"{self.r}/statuses/{head}",
            {"state": "success", "context": "docs"},
            keep=("context", "status"),
        )
        look("ci green, lint never reported, an unrequired docs green")
        dev.post(
            f"{self.r}/statuses/{head}",
            {"state": "failure", "context": "lint"},
            keep=("context", "status"),
        )
        look("lint red")
        ev.findings = {
            "write_collaborator_permissions": perms.get("permissions"),
            "branch_protections_list_as_write": listing.status,
            "branch_protection_get_as_write": one.status,
            "branch_view_as_write": {k: branch_view.get(k) for k in BRANCH_VIEW},
            "flags_only_admin_can_read": {k: full.get(k) for k in ADMIN_ONLY_FLAGS},
            "observed": observed,
        }
        return ev

    # V5 -------------------------------------------------------------------------

    def v5(self) -> Evidence:
        ev, c = self.evidence("V5")
        dev = c["dev"]
        s = suffix()
        branch = f"v5/{s}"
        readme = dev.get(
            f"{self.r}/contents/README.md", query={"ref": "main"}, keep=("path", "sha")
        ).data
        first = self.contents(
            dev,
            {
                "branch": "main",
                "new_branch": branch,
                "message": "V5: several files, one commit, a new branch",
                "files": [
                    {"operation": "create", "path": f"v5/{s}/a.txt", "content": b64("a\n")},
                    {"operation": "create", "path": f"v5/{s}/b.bin", "content": b64(BINARY)},
                    {
                        "operation": "update",
                        "path": "README.md",
                        "sha": readme["sha"],
                        "content": b64(
                            base64.b64decode(
                                dev.get(f"{self.r}/contents/README.md", query={"ref": "main"}).data[
                                    "content"
                                ]
                            )
                            + b"\nV5\n"
                        ),
                    },
                ],
            },
        )
        binary = dev.get(
            f"{self.r}/contents/v5/{s}/b.bin",
            query={"ref": branch},
            keep=("path", "encoding", "size"),
            check=False,
        )
        a_sha = dev.get(f"{self.r}/contents/v5/{s}/a.txt", query={"ref": branch}).data["sha"]
        b_sha = dev.get(f"{self.r}/contents/v5/{s}/b.bin", query={"ref": branch}).data["sha"]
        second = self.contents(
            dev,
            {
                "branch": branch,
                "message": "V5: delete and move",
                "files": [
                    {"operation": "delete", "path": f"v5/{s}/a.txt", "sha": a_sha},
                    {
                        "operation": "update",
                        "from_path": f"v5/{s}/b.bin",
                        "path": f"v5/{s}/c.bin",
                        "sha": b_sha,
                        "content": b64(BINARY),
                    },
                ],
            },
        )
        head_before = dev.get(f"{self.r}/branches/{quote(branch, safe='')}").data["commit"]["id"]
        atomic = self.contents(
            dev,
            {
                "branch": branch,
                "message": "V5: one good operation, one bad",
                "files": [
                    {"operation": "create", "path": f"v5/{s}/ok.txt", "content": b64("ok\n")},
                    {
                        "operation": "update",
                        "path": f"v5/{s}/missing.txt",
                        "sha": "0" * 40,
                        "content": b64("nope\n"),
                    },
                ],
            },
        )
        head_after = dev.get(f"{self.r}/branches/{quote(branch, safe='')}").data["commit"]["id"]
        ok_file = dev.get(
            f"{self.r}/contents/v5/{s}/ok.txt",
            query={"ref": branch},
            check=False,
            keep=("message",),
        )
        protected = self.contents(
            dev,
            {
                "branch": "main",
                "message": "V5: onto the protected base",
                "files": [{"operation": "create", "path": f"v5-{s}.txt", "content": b64("no\n")}],
            },
            note="main has enable_push: false",
        )
        single = dev.post(
            f"{self.r}/contents/v5/{s}/single.txt",
            {"branch": branch, "message": "V5: one file", "content": b64("one\n")},
            keep=("commit",),
            check=False,
        )
        commit_parents = (
            [p["sha"] for p in (second.data or {}).get("commit", {}).get("parents", [])]
            if second.ok
            else []
        )
        ev.findings = {
            "multi_file_new_branch": first.status,
            "binary_round_trip": binary.ok
            and base64.b64decode(
                dev.get(
                    f"{self.r}/contents/v5/{s}/c.bin", query={"ref": branch}, check=False
                ).data.get("content")
                or ""
            )
            == BINARY
            if second.ok
            else binary.ok,
            "delete_and_move": second.status,
            "second_commit_parent_is_first": first.ok
            and commit_parents == [first.data["commit"]["sha"]],
            "partial_failure_status": atomic.status,
            "partial_failure_message": (atomic.data or {}).get("message")
            if isinstance(atomic.data, dict)
            else atomic.data,
            "partial_failure_left_branch_unmoved": head_before == head_after,
            "partial_failure_wrote_nothing": ok_file.status == 404,
            "protected_base_status": protected.status,
            "protected_base_message": (protected.data or {}).get("message")
            if isinstance(protected.data, dict)
            else protected.data,
            "single_file_contents_path": single.status,
        }
        return ev

    # V6 -------------------------------------------------------------------------

    def v6(self) -> Evidence:
        ev, c = self.evidence("V6")
        f = self.forge
        user = f.get("SBXLOOP_LIVE_GITEA_DEVELOPER")
        as_token = c["dev"].get(f"/users/{user}/tokens", check=False, keep=("message",))
        basic = Client.basic(
            f.api_url, user, f.get("GITEA_DEVELOPER_PASSWORD"), "the user, basic auth"
        )
        basic.recorder = c["dev"].recorder
        listed = basic.get(f"/users/{user}/tokens", check=False)
        defs = self.swagger()["definitions"]
        token_schema = sorted(defs["AccessToken"]["properties"])
        create_schema = sorted(defs["CreateAccessTokenOption"]["properties"])
        ev.exchanges.append(
            {
                "as": "anonymous",
                "request": "GET /swagger.v1.json",
                "status": 200,
                "response": {"AccessToken": token_schema, "CreateAccessTokenOption": create_schema},
            }
        )
        ev.findings = {
            "token_reads_own_tokens": as_token.status,
            "basic_auth_lists_tokens": listed.status,
            "listed_token_keys": sorted(listed.data[0]) if listed.ok and listed.data else None,
            "expiry_in_token_schema": any("expir" in k for k in token_schema),
            "expiry_settable_at_creation": any("expir" in k for k in create_schema),
        }
        return ev

    # The rest of the capability matrix ------------------------------------------

    def matrix(self) -> Evidence:
        ev, c = self.evidence("matrix")
        dev, rev = c["dev"], c["rev"]
        bot = self.forge.client("GITEA_BOT_TOKEN", "write collaborator (bot account)")
        bot.recorder = dev.recorder
        swagger = self.swagger()
        paths = swagger["paths"]
        create_fields = sorted(swagger["definitions"]["CreatePullRequestOption"]["properties"])
        merge_fields = sorted(swagger["definitions"]["MergePullRequestOption"]["properties"])
        ev.exchanges.append(
            {
                "as": "anonymous",
                "request": "GET /swagger.v1.json",
                "status": 200,
                "response": {
                    "CreatePullRequestOption": create_fields,
                    "MergePullRequestOption": merge_fields,
                    "paths naming a queue or train": [
                        p for p in paths if "queue" in p or "train" in p
                    ],
                },
            }
        )
        s = suffix()
        branch = f"matrix/{s}"
        written = self.contents(
            dev,
            {
                "branch": "main",
                "new_branch": branch,
                "message": "matrix",
                "files": [
                    {"operation": "create", "path": f"matrix-{s}.txt", "content": b64("m\n")}
                ],
            },
            check=True,
        )
        commit = dev.get(
            f"{self.r}/commits", query={"sha": branch, "limit": 1}, keep=("sha", "commit")
        ).data[0]
        pull = dev.post(
            f"{self.r}/pulls",
            {"head": branch, "base": "main", "title": f"WIP: matrix {s}"},
            keep=("number", "draft", "title"),
        ).data
        number, head = pull["number"], written.data["commit"]["sha"]
        dev.patch(
            f"{self.r}/pulls/{number}", {"title": f"matrix {s}"}, keep=("number", "draft", "title")
        )
        undrafted = dev.get(f"{self.r}/pulls/{number}", keep=("number", "draft")).data
        for context in ("ci", "lint"):
            dev.post(
                f"{self.r}/statuses/{head}",
                {"state": "success", "context": context},
                keep=("context", "status"),
            )
        github_word = rev.post(
            f"{self.r}/pulls/{number}/reviews",
            {"event": "APPROVE", "body": "GitHub's spelling"},
            check=False,
            keep=("id", "state", "official"),
            note="GitHub's event name, which Gitea does not define",
        )
        own = dev.post(
            f"{self.r}/pulls/{number}/reviews",
            {"event": "APPROVED", "body": "mine"},
            check=False,
            keep=("id", "state", "official", "message"),
            note="the pull request's author approves it",
        )
        rev.post(
            f"{self.r}/pulls/{number}/reviews",
            {"event": "APPROVED", "body": "fine"},
            keep=("id", "state", "official"),
        )
        requested = bot.post(
            f"{self.r}/pulls/{number}/reviews",
            {"event": "REQUEST_CHANGES", "body": "not yet"},
            check=False,
            keep=("id", "state", "official"),
        )
        merge = dev.post(
            f"{self.r}/pulls/{number}/merge",
            {"Do": "merge"},
            check=False,
            keep=("message",),
            note="checks green, one approval, changes requested; block_on_rejected_reviews on",
        )
        dev.patch(
            f"{self.r}/pulls/{number}", {"state": "closed"}, keep=("number", "state"), check=False
        )
        ev.findings = {
            "queue_paths": [p for p in paths if "queue" in p or "train" in p],
            "auto_merge_option": "merge_when_checks_succeed" in merge_fields,
            "draft_create_field": "draft" in create_fields,
            "draft_from_title_prefix": pull.get("draft"),
            "draft_cleared_by_retitle": undrafted.get("draft") is False,
            "unknown_event_becomes": (github_word.data or {}).get("state")
            if isinstance(github_word.data, dict)
            else None,
            "unknown_event_status": github_word.status,
            "author_approves_own": own.status,
            "author_approval_message": (own.data or {}).get("message")
            if isinstance(own.data, dict)
            else None,
            "request_changes_review": requested.status,
            "request_changes_state": (requested.data or {}).get("state")
            if isinstance(requested.data, dict)
            else None,
            "merge_with_requested_changes": merge.status,
            "merge_refusal_message": (merge.data or {}).get("message")
            if isinstance(merge.data, dict)
            else merge.data,
            "api_commit_verification": (commit.get("commit") or {}).get("verification"),
        }
        return ev


PROBES: dict[str, dict[str, Callable[[Any], Evidence]]] = {
    "gitlab": {
        q: getattr(GitlabProbe, q.lower()) for q in ("V1", "V2", "V3", "V5", "V6", "matrix")
    },
    "gitea": {q: getattr(GiteaProbe, q.lower()) for q in ("V1", "V3", "V4", "V5", "V6", "matrix")},
}
PROBE_TYPES: dict[str, type] = {"gitlab": GitlabProbe, "gitea": GiteaProbe}


def run(kind: str, question: str) -> Evidence:
    forge = live_forge(kind)
    if isinstance(forge, str):
        raise RuntimeError(forge)
    probe = PROBE_TYPES[kind](forge)
    return PROBES[kind][question](probe)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.live.fieldverify")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("questions", nargs="*")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for kind, probes in PROBES.items():
        if isinstance(live_forge(kind), str):
            print(f"{kind}: not configured, skipped")
            continue
        for question in probes:
            if args.questions and question not in args.questions:
                continue
            ev = run(kind, question)
            (args.out / f"{question}-{kind}.json").write_text(json.dumps(asdict(ev), indent=2))
            print(f"{question} {kind} ({ev.version}): {json.dumps(ev.findings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
