"""TaskGraph and model validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sbxloop.engine.model import (
    DEFAULT_ARTIFACT_EXCLUDES,
    GITIGNORED,
    MAX_SUMMARY_CHARS,
    PIPELINE_STAGES,
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
    EgressSpec,
    JudgeVerdict,
    RunRecord,
    RunResult,
    SteerVerdict,
    TaskGraph,
    TaskNeeds,
    TaskOutput,
    TaskRecord,
    TaskSpec,
    WorkloadPlan,
    artifact_files,
    artifacts_dir,
    scan_artifacts,
    workload_summary,
)
from sbxloop.paths import SbxloopHome


def spec(id: str, deps: list[str] | None = None) -> dict[str, object]:
    return {"id": id, "title": id.upper(), "depends_on": deps or []}


class TestTaskGraph:
    def test_valid_graph_topo_order(self) -> None:
        graph = TaskGraph.model_validate(
            {"tasks": [spec("t3", ["t1", "t2"]), spec("t1"), spec("t2", ["t1"])]}
        )
        assert [t.id for t in graph.topo_order()] == ["t1", "t2", "t3"]

    def test_topo_order_stable_for_independent_tasks(self) -> None:
        graph = TaskGraph.model_validate({"tasks": [spec("b"), spec("a"), spec("c")]})
        assert [t.id for t in graph.topo_order()] == ["b", "a", "c"]  # authored order

    def test_empty_graph_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one task"):
            TaskGraph.model_validate({"tasks": []})

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate task ids"):
            TaskGraph.model_validate({"tasks": [spec("t1"), spec("t1")]})

    def test_unknown_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown tasks"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["ghost"])]})

    def test_self_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="depends on itself"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t1"])]})

    def test_cycle_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t2"]), spec("t2", ["t1"])]})


class TestPlanTitle:
    """#621: the decomposer may name the pull request; whitespace is folded
    and an empty title is no title."""

    def test_pr_title_is_optional_and_folded(self) -> None:
        assert TaskGraph.model_validate({"tasks": [spec("t1")]}).pr_title is None
        graph = TaskGraph.model_validate(
            {"tasks": [spec("t1")], "pr_title": "  feat:\n  add   the thing "}
        )
        assert graph.pr_title == "feat: add the thing"
        assert TaskGraph.model_validate({"tasks": [spec("t1")], "pr_title": " "}).pr_title is None
        assert TaskGraph.model_validate({"tasks": [spec("t1")], "pr_title": None}).pr_title is None


class TestTaskSpec:
    def test_task_spec_defaults(self) -> None:
        t = TaskSpec(id="t1", title="X")
        assert t.acceptance_criteria == []
        assert t.verify_commands == []
        assert t.egress == []

    def test_task_egress_domains_validated(self) -> None:
        """Egress moved from the plan to the task spec; the same domain
        validation guards it — a scheme, path, or bare `*` is rejected at
        graph acceptance, not at grant time."""
        t = TaskSpec.model_validate(
            {
                "id": "t1",
                "title": "X",
                "egress": [{"domain": "Registry.NPMJS.org", "reason": "npm"}],
            }
        )
        assert t.egress[0].domain == "registry.npmjs.org"  # normalised
        for bad in ("https://x.com", "x.com/path", "*"):
            with pytest.raises(ValidationError, match="domain"):
                EgressSpec(domain=bad)


class TestArtifactFiles:
    """The exclusion is a targeted denylist, not "anything dot-prefixed":
    dot-path artifacts agents produce on purpose (.github/, .gitignore) must
    survive listings and delivery (#67)."""

    def make_workspace(self, tmp_path: Path) -> Path:
        root = tmp_path / "ws"
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (root / ".gitignore").write_text("*.pyc\n")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("pass\n")
        (root / ".git" / "refs").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref\n")
        (root / ".git" / "refs" / "x").write_text("sha\n")
        return root

    def test_dot_path_artifacts_kept_git_excluded(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        rels = [p.relative_to(root).as_posix() for p in artifact_files(root)]
        assert rels == [".github/workflows/ci.yml", ".gitignore", "src/main.py"]

    def test_scan_counts_exclusions_per_entry(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 2}
        assert scan.excluded_total == 2
        assert scan.excluded_note == "2 file(s) excluded (.git)"

    def test_nested_excluded_dir_is_caught(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        (root / "vendor" / ".git").mkdir(parents=True)
        (root / "vendor" / ".git" / "config").write_text("x\n")
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 3}
        assert all(".git" not in p.parts for p in scan.files)

    def test_sbxloop_state_dir_excluded_by_default(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        scan = scan_artifacts(root)
        assert scan.excluded == {".git": 2, ".sbxloop": 1}
        assert scan.excluded_note == "3 file(s) excluded (.git, .sbxloop)"

    def test_custom_exclude_list(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        scan = scan_artifacts(root, exclude=[".git", "src"])
        assert [p.relative_to(root).as_posix() for p in scan.files] == [
            ".github/workflows/ci.yml",
            ".gitignore",
        ]
        assert scan.excluded == {".git": 2, "src": 1}

    def test_no_exclusions_has_no_note(self, tmp_path: Path) -> None:
        root = tmp_path / "clean"
        root.mkdir()
        (root / "a.txt").write_text("a")
        scan = scan_artifacts(root)
        assert scan.excluded == {}
        assert scan.excluded_note is None

    def test_egg_info_glob_excluded_by_default(self, tmp_path: Path) -> None:
        """pip build metadata is named after the project — every project's
        differently — so only the *.egg-info glob can catch it."""
        root = self.make_workspace(tmp_path)
        egg = root / "src" / "samplepkg.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text("Metadata-Version: 2.1\n")
        (egg / "SOURCES.txt").write_text("src/main.py\n")
        scan = scan_artifacts(root)
        assert all(".egg-info" not in p.as_posix() for p in scan.files)
        # tallied under the pattern, not each matched directory name
        assert scan.excluded == {".git": 2, "*.egg-info": 2}

    def test_glob_matches_component_not_substring(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        # a FILE merely mentioning egg-info in its name is kept: the glob
        # matches whole path components only when the component matches
        (root / "notes-about.egg-information.txt").write_text("keep\n")
        (root / "kept.egg-info.bak").write_text("keep\n")
        scan = scan_artifacts(root, exclude=["*.egg-info"])
        assert [p.name for p in scan.files] == [
            "kept.egg-info.bak",
            "notes-about.egg-information.txt",
        ]
        assert scan.excluded == {}

    def test_custom_glob_entries_work(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / "cache-a").mkdir(parents=True)
        (root / "cache-a" / "x").write_text("x")
        (root / "keep").mkdir()
        (root / "keep" / "y").write_text("y")
        scan = scan_artifacts(root, exclude=["cache-*"])
        assert [p.relative_to(root).as_posix() for p in scan.files] == ["keep/y"]
        assert scan.excluded == {"cache-*": 1}

    def test_config_default_mirrors_model_default(self) -> None:
        # config.py keeps a literal copy (importing engine.model there would
        # be circular); this pins the two against drift.
        from sbxloop.config import ArtifactsConfig

        assert tuple(ArtifactsConfig().exclude) == DEFAULT_ARTIFACT_EXCLUDES

    def test_default_entries_are_unique_and_config_valid(self) -> None:
        """Every default entry must survive the [artifacts] exclude validator
        — a default the user cannot re-type into their own config would be a
        latent trap."""
        from sbxloop.config import ArtifactsConfig

        assert len(set(DEFAULT_ARTIFACT_EXCLUDES)) == len(DEFAULT_ARTIFACT_EXCLUDES)
        # Round-trips through validation unchanged.
        echoed = ArtifactsConfig(exclude=list(DEFAULT_ARTIFACT_EXCLUDES))
        assert tuple(echoed.exclude) == DEFAULT_ARTIFACT_EXCLUDES


class TestSymlinksInScan:
    """#695: a symlink is an artifact in its own right — git tracks the link,
    so the scan keeps it as itself, target resolving or not — and nothing is
    followed into a symlinked directory."""

    def test_symlinks_are_kept_as_themselves(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / "shared").mkdir(parents=True)
        (root / "shared" / "a.txt").write_text("a\n")
        (root / "cfg.txt").write_text("c\n")
        (root / "cfg.link").symlink_to("cfg.txt")
        (root / "shared.link").symlink_to("shared")
        (root / "dangling").symlink_to("nowhere")
        rels = [p.relative_to(root).as_posix() for p in artifact_files(root)]
        assert rels == ["cfg.link", "cfg.txt", "dangling", "shared/a.txt", "shared.link"]
        # the directory symlink is listed once, not walked into
        assert "shared.link/a.txt" not in rels


class TestDefaultBuildOutputExcludes:
    """The default denylist covers regenerable dependency/build trees for the
    supported languages, and deliberately leaves ambiguous generic names in."""

    def make_workspace(self, tmp_path: Path, rels: list[str]) -> Path:
        root = tmp_path / "ws"
        for rel in rels:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n")
        return root

    def test_per_language_build_output_is_excluded(self, tmp_path: Path) -> None:
        root = self.make_workspace(
            tmp_path,
            [
                "keep.txt",
                "node_modules/left-pad/index.js",  # JS / TS
                "src/__pycache__/main.cpython-312.pyc",  # Python
                ".venv/lib/python3.12/site-packages/x.py",
                "venv/bin/activate",
                ".pytest_cache/v/cache/lastfailed",
                ".mypy_cache/3.12/x.json",
                ".ruff_cache/content",
                ".tox/py312/log.txt",
                ".nox/tests/marker",
                "target/release/app",  # Rust (cargo) / Java (Maven)
                ".gradle/8.5/fileHashes.bin",  # Java (Gradle)
                "obj/Debug/net8.0/app.dll",  # C# / .NET
                ".bundle/config",  # Ruby
                "CMakeFiles/app.dir/main.o",  # C / C++
            ],
        )
        scan = scan_artifacts(root)
        assert [p.relative_to(root).as_posix() for p in scan.files] == ["keep.txt"]
        assert scan.excluded_total == 14

    def test_generic_names_are_deliberately_kept(self, tmp_path: Path) -> None:
        """bin/build/dist/out/lib/vendor mean build output in one ecosystem
        and hand-written content in the next, so they stay in the listing."""
        rels = [
            "bin/setup",
            "build/release.sh",
            "dist/app.js",
            "out/report.txt",
            "lib/helper.rb",
            "vendor/github.com/pkg/errors/errors.go",
        ]
        root = self.make_workspace(tmp_path, rels)
        scan = scan_artifacts(root)
        assert [p.relative_to(root).as_posix() for p in scan.files] == sorted(rels)
        assert scan.excluded == {}

    def test_nested_dependency_dirs_are_caught(self, tmp_path: Path) -> None:
        """Monorepo layouts bury node_modules/target several levels down."""
        root = self.make_workspace(
            tmp_path,
            [
                "packages/web/src/app.ts",
                "packages/web/node_modules/react/index.js",
                "packages/web/node_modules/react/lib/x.js",
                "crates/core/src/lib.rs",
                "crates/core/target/debug/core.rlib",
            ],
        )
        scan = scan_artifacts(root)
        assert [p.relative_to(root).as_posix() for p in scan.files] == [
            "crates/core/src/lib.rs",
            "packages/web/src/app.ts",
        ]
        assert scan.excluded == {"node_modules": 2, "target": 1}

    def test_exclusions_are_surfaced_not_silent(self, tmp_path: Path) -> None:
        """A dropped 100k-file node_modules must be visible in the note that
        run summaries, `sbxloop artifacts` and delivery PR bodies print."""
        root = self.make_workspace(
            tmp_path, ["app.py", "node_modules/a/i.js", "target/debug/x", ".git/HEAD"]
        )
        scan = scan_artifacts(root)
        assert scan.excluded_note == "3 file(s) excluded (.git, node_modules, target)"

    def test_manifests_that_regenerate_the_tree_are_still_delivered(self, tmp_path: Path) -> None:
        """Dropping the tree is only safe because the lockfiles/manifests it
        is reproducible from are kept."""
        rels = [
            "Cargo.lock",
            "Cargo.toml",
            "Gemfile.lock",
            "package-lock.json",
            "package.json",
            "pyproject.toml",
            "requirements.txt",
        ]
        root = self.make_workspace(tmp_path, [*rels, "node_modules/a/i.js"])
        assert [p.relative_to(root).as_posix() for p in artifact_files(root)] == sorted(rels)

    def test_override_replaces_rather_than_extends(self, tmp_path: Path) -> None:
        """A user who names their own list opts out of the defaults entirely
        — node_modules comes back unless they keep it."""
        root = self.make_workspace(tmp_path, ["app.py", "node_modules/a/i.js"])
        scan = scan_artifacts(root, exclude=[".git"])
        assert [p.relative_to(root).as_posix() for p in scan.files] == [
            "app.py",
            "node_modules/a/i.js",
        ]


class TestGitignoreAwareScan:
    """The exclude list is a cross-ecosystem denylist; only the project's
    own .gitignore knows its dist/, vendored wheels and generated
    _version.py are byproducts (#249). Harvested copies carry .gitignore
    without .git, so the rules must apply to plain trees too."""

    def make_tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "ws"
        (root / "pkg" / "_vendor").mkdir(parents=True)
        (root / "dist").mkdir()
        (root / ".gitignore").write_text("dist/\n_vendor/\n_version.py\n")
        for rel in ("dist/app.whl", "pkg/_vendor/w.whl", "pkg/_version.py", "pkg/m.py"):
            (root / rel).write_text("x\n")
        return root

    def test_gitignored_files_dropped_and_tallied(self, tmp_path: Path) -> None:
        root = self.make_tree(tmp_path)
        scan = scan_artifacts(root)
        assert [p.relative_to(root).as_posix() for p in scan.files] == [".gitignore", "pkg/m.py"]
        assert scan.excluded == {GITIGNORED: 3}
        assert scan.excluded_note == "3 file(s) excluded (gitignored)"

    def test_name_based_entries_take_precedence_in_tally(self, tmp_path: Path) -> None:
        root = self.make_tree(tmp_path)
        (root / ".gitignore").write_text("dist/\nnode_modules/\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "i.js").write_text("x\n")
        scan = scan_artifacts(root)
        assert scan.excluded == {GITIGNORED: 1, "node_modules": 1}

    def test_operator_exclude_still_applies_on_top(self, tmp_path: Path) -> None:
        root = self.make_tree(tmp_path)
        scan = scan_artifacts(root, exclude=["pkg"])
        assert [p.relative_to(root).as_posix() for p in scan.files] == [".gitignore"]
        assert scan.excluded == {GITIGNORED: 1, "pkg": 3}

    def test_opt_out(self, tmp_path: Path) -> None:
        root = self.make_tree(tmp_path)
        scan = scan_artifacts(root, gitignore=False)
        assert len(scan.files) == 5
        assert scan.excluded == {}

    def test_tree_without_gitignore_never_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop import hostgit

        def boom(root: Path) -> frozenset[str]:
            raise AssertionError("probe must not run")

        monkeypatch.setattr(hostgit, "gitignored_files", boom)
        root = tmp_path / "plain"
        root.mkdir()
        (root / "a.txt").write_text("a")
        assert [p.name for p in scan_artifacts(root).files] == ["a.txt"]

    def test_failed_probe_degrades_to_name_based_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop import hostgit

        monkeypatch.setattr(hostgit, "gitignored_files", lambda root: None)
        root = self.make_tree(tmp_path)
        assert len(scan_artifacts(root).files) == 5


class TestArtifactsDir:
    def test_mounted_run_uses_workspace(self) -> None:
        record = RunRecord(
            run_id="r1",
            outcome="x",
            state="completed",
            created_at=1.0,
            updated_at=1.0,
            workspace=Path("/tmp/ws"),
            mounted=True,
        )
        assert artifacts_dir(record, SbxloopHome(Path("/state"))) == Path("/tmp/ws")

    def test_unmounted_run_uses_harvest_dir(self) -> None:
        record = RunRecord(
            run_id="r1",
            outcome="x",
            state="completed",
            created_at=1.0,
            updated_at=1.0,
            workspace=Path("/tmp/ws"),
            mounted=False,
        )
        assert artifacts_dir(record, SbxloopHome(Path("/state"))) == Path(
            "/state/runs/r1/artifacts"
        )

    def test_never_provisioned_run_has_none(self) -> None:
        record = RunRecord(
            run_id="r1", outcome="x", state="created", created_at=1.0, updated_at=1.0
        )
        assert artifacts_dir(record, SbxloopHome(Path("/state"))) is None


class TestSteerVerdict:
    def test_continue_needs_no_guidance(self) -> None:
        verdict = SteerVerdict(reply="all fine")
        assert verdict.action == "continue"
        assert verdict.guidance == ""

    def test_steer_actions_require_guidance(self) -> None:
        with pytest.raises(ValidationError, match="guidance"):
            SteerVerdict(reply="ok", action="steer_task")
        with pytest.raises(ValidationError, match="guidance"):
            SteerVerdict(reply="ok", action="steer_run", guidance="   ")
        verdict = SteerVerdict(reply="ok", action="steer_run", guidance="use Go")
        assert verdict.guidance == "use Go"

    def test_unknown_action_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SteerVerdict(reply="ok", action="abort_everything", guidance="g")


class TestWorkloadPlan:
    """#756: the operator's plan is a task graph with a run title and, per
    task, the needs it declares by name."""

    def test_needs_default_empty_and_persist_on_the_spec(self) -> None:
        task = TaskSpec.model_validate(spec("t1"))
        assert task.needs.empty
        assert task.needs == TaskNeeds()
        loaded = TaskSpec.model_validate_json(task.model_dump_json())
        assert loaded.needs.empty

    def test_needs_are_names_never_values(self) -> None:
        needs = TaskNeeds.model_validate(
            {
                "hosts": [" API.Example.com ", "*.data.example.org"],
                "credentials": ["example-api"],
                "sink": "chat",
                "repo": "owner/name",
            }
        )
        assert needs.hosts == ["api.example.com", "*.data.example.org"]
        assert needs.credentials == ["example-api"]
        assert not needs.empty
        with pytest.raises(ValidationError, match=r"needs\.hosts"):
            TaskNeeds(hosts=["https://api.example.com/v1"])
        with pytest.raises(ValidationError, match=r"needs\.hosts"):
            TaskNeeds(hosts=["*"])
        with pytest.raises(ValidationError):
            TaskNeeds.model_validate({"credentials": ["x"], "token": "sk-live"})

    def test_plan_title_folds_like_the_pr_title(self) -> None:
        plan = WorkloadPlan.model_validate(
            {"title": "  Count the  widgets ", "tasks": [spec("t1")]}
        )
        assert plan.title == "Count the widgets"
        assert WorkloadPlan.model_validate({"title": "   ", "tasks": [spec("t1")]}).title is None
        assert WorkloadPlan.model_validate({"tasks": [spec("t1")]}).title is None
        assert plan.topo_order()[0].id == "t1"

    def test_plan_validates_the_graph(self) -> None:
        with pytest.raises(ValidationError):
            WorkloadPlan.model_validate({"tasks": [spec("t1", ["t2"]), spec("t2", ["t1"])]})


class TestJudgeVerdict:
    def test_passed_needs_nothing_else(self) -> None:
        verdict = JudgeVerdict(passed=True)
        assert verdict.unmet == [] and verdict.notes == ""

    def test_a_failing_verdict_names_a_criterion(self) -> None:
        with pytest.raises(ValidationError, match="unmet"):
            JudgeVerdict(passed=False)
        with pytest.raises(ValidationError, match="unmet"):
            JudgeVerdict(passed=False, unmet=["  ", ""])
        verdict = JudgeVerdict(passed=False, unmet=["  the file\n exists ", ""], notes="n")
        assert verdict.unmet == ["the file exists"]

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JudgeVerdict.model_validate({"passed": True, "score": 10})


class TestRunStates:
    """One run carries its work to a merged PR: the pipeline stages are
    run states, every one resumable, and the terminal set is what liveness
    and reporting key on."""

    def test_pipeline_stages_are_ordered_and_resumable(self) -> None:
        assert PIPELINE_STAGES == (
            "gating",
            "delivering",
            "reviewing",
            "fixing",
            "awaiting_ci",
            "landing",
        )
        assert set(PIPELINE_STAGES) <= RESUMABLE_RUN_STATES
        assert not set(PIPELINE_STAGES) & TERMINAL_RUN_STATES

    def test_terminal_states(self) -> None:
        assert {
            "merged",
            "completed",
            "failed",
            "blocked",
            "cancelled",
            "gated",
            "awaiting_review",
            "held",
        } == TERMINAL_RUN_STATES
        # A finished run is finished; a stopped one may be picked up again.
        # `gated` is finished too: the approve path lands the PR with gh ops
        # alone, never by resuming an engine. `awaiting_review` is both: an
        # approval lands it with gh ops, a changes-requested review resumes
        # it for a fix round (#675). `held` is both as well: a workload
        # parked at publishing (#760) whose release *is* a resume.
        assert {"merged", "completed", "gated"}.isdisjoint(RESUMABLE_RUN_STATES)
        assert {"failed", "blocked", "cancelled", "awaiting_review", "held"} <= RESUMABLE_RUN_STATES
        assert {"created", "provisioning", "decomposing", "building"} <= RESUMABLE_RUN_STATES

    def test_legacy_state_names_are_gone(self) -> None:
        for legacy in ("running", "finalizing"):
            assert legacy not in RESUMABLE_RUN_STATES
            assert legacy not in TERMINAL_RUN_STATES
            with pytest.raises(ValidationError):
                RunRecord(run_id="r", outcome="o", state=legacy, created_at=1.0, updated_at=1.0)  # type: ignore[arg-type]

    def test_run_record_pipeline_defaults(self) -> None:
        run = RunRecord(run_id="r1", outcome="x", state="created", created_at=1.0, updated_at=1.0)
        assert run.stage is None and run.reason is None
        assert (run.pr_number, run.pr_url, run.pr_node_id, run.branch, run.head_sha) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert (run.review_rounds, run.ci_rounds, run.update_attempts) == (0, 0, 0)
        assert run.update_head is None and run.last_verdict is None


class TestRunResult:
    @pytest.mark.parametrize("state", ["merged", "completed"])
    def test_succeeded_states(self, state: str) -> None:
        result = RunResult(run_id="r1", state=state)  # type: ignore[arg-type]
        assert result.succeeded
        assert result.pr_number is None and result.pr_url is None and result.reason is None

    @pytest.mark.parametrize("state", ["failed", "blocked", "cancelled", "landing", "created"])
    def test_unfinished_and_stopped_states_did_not_succeed(self, state: str) -> None:
        assert not RunResult(run_id="r1", state=state).succeeded  # type: ignore[arg-type]

    def test_carries_the_pr_and_the_reason(self) -> None:
        result = RunResult(
            run_id="r1",
            state="blocked",
            tasks=[TaskRecord(spec=TaskSpec(id="t1", title="T"), state="done")],
            pr_number=9,
            pr_url="https://x/pull/9",
            reason="its draft status could not be cleared",
        )
        assert result.pr_number == 9
        assert result.reason == "its draft status could not be cleared"
        assert [t.state for t in result.tasks] == ["done"]


class TestTaskOutput:
    def test_cuts_the_result_section_and_leads_with_its_first_line(self) -> None:
        report = (
            "I'll count the lines.\n\n"
            "## Approach\n\nread every file.\n\n"
            "## Result\n\n"
            "wrote `summary.csv` with 3 rows\n\n- one\n- two\n"
        )
        out = TaskOutput.from_report(report, files=["summary.csv"])
        assert out.summary == "wrote `summary.csv` with 3 rows"
        assert out.text == "wrote `summary.csv` with 3 rows\n\n- one\n- two"
        assert out.files == ["summary.csv"]
        assert out.file_count == 1

    def test_the_last_results_heading_wins_at_any_level(self) -> None:
        report = "### Results\nfirst\n\ntext\n\n# RESULT: final\nsecond line\n"
        assert TaskOutput.from_report(report).summary == "second line"

    def test_a_report_without_a_result_heading_is_taken_whole(self) -> None:
        out = TaskOutput.from_report("\n\n  nothing to report  \nmore\n")
        assert out.text == "nothing to report  \nmore"
        assert out.summary == "nothing to report"

    def test_an_empty_report_has_an_empty_summary(self) -> None:
        out = TaskOutput.from_report("   \n\n")
        assert out.summary == "" and out.text == "" and out.files == []

    def test_the_summary_is_one_clipped_line(self) -> None:
        long = "word " * 100
        out = TaskOutput.from_report("## Result\n" + long + "\nsecond")
        assert len(out.summary) == MAX_SUMMARY_CHARS
        assert out.summary.endswith("…")
        assert "\n" not in out.summary
        assert "  " not in TaskOutput.from_report("## Result\na   b\tc").summary

    def test_more_files_counts_toward_the_file_count(self) -> None:
        out = TaskOutput.from_report("x", files=["a", "b"], more_files=5)
        assert out.file_count == 7

    def test_round_trips_through_json(self) -> None:
        out = TaskOutput.from_report("## Result\nok", files=["a"], more_files=1)
        assert TaskOutput.model_validate_json(out.model_dump_json()) == out


class TestWorkloadSummary:
    @staticmethod
    def _task(id: str, state: str, summary: str | None, files: int = 0) -> TaskRecord:
        record = TaskRecord(spec=TaskSpec(id=id, title=id.upper()), state=state)  # type: ignore[arg-type]
        if summary is not None:
            record.output = TaskOutput(summary=summary, files=[f"f{i}" for i in range(files)])
        return record

    def test_counts_the_passed_tasks_and_lists_each_result(self) -> None:
        tasks = [
            self._task("t1", "done", "wrote the csv", files=2),
            self._task("t2", "failed", "could not reach the host"),
            self._task("t3", "skipped", None),
        ]
        # a task that never reported (skipped) has no line; an empty summary does
        tasks[1].output = TaskOutput(summary="", files=["log"])
        assert workload_summary(tasks) == (
            "1/3 task(s) passed the judge\n"
            "t1: wrote the csv (2 files)\n"
            "t2: (no result reported) (1 file)"
        )

    def test_the_title_leads_when_the_plan_named_the_work(self) -> None:
        tasks = [self._task("t1", "done", "ok")]
        text = workload_summary(tasks, "Count the lines")
        assert text.startswith("Count the lines — 1/1 task(s) passed the judge")

    def test_no_tasks(self) -> None:
        assert workload_summary([]) == "no tasks ran"
        assert workload_summary([], "Title") == "Title — no tasks ran"


class TestRunResultOutputs:
    def test_outputs_pairs_each_task_with_its_output(self) -> None:
        out = TaskOutput(summary="done")
        t1 = TaskRecord(spec=TaskSpec(id="t1", title="A"), state="done", output=out)
        t2 = TaskRecord(spec=TaskSpec(id="t2", title="B"), state="skipped")
        result = RunResult(run_id="r1", state="completed", tasks=[t1, t2], summary="s")
        assert result.outputs == [("t1", out)]
        assert result.summary == "s"
        assert RunResult(run_id="r1", state="merged").summary is None
