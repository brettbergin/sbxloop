"""Agent-declared filenames cannot become native host paths at publication."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import TaskOutput, TaskRecord, TaskSpec
from sbxloop.engine.sinks import PublishError, safe_relative


@pytest.mark.parametrize(
    "name",
    [
        "C:/Users/operator/secrets.env",
        r"C:\Users\operator\secrets.env",
        r"\Users\operator\secrets.env",
        r"\\server\share\secrets.env",
        "C:secrets.env",
        "sub/C:secrets.env",
        r"sub\..\..\secrets.env",
        "sub/.. /secrets.env",
        "report.txt:private",
        "sub/\x00secret",
    ],
)
def test_host_path_spellings_are_refused_before_copy(tmp_path: Path, name: str) -> None:
    config = Config.model_validate({"home": str(tmp_path / "home")})
    copies: list[object] = []
    engine = SimpleNamespace(config=config, _copy_out=lambda *args: copies.append(args))
    pipeline = SimpleNamespace(pair=SimpleNamespace(mounted=False))
    run = SimpleNamespace(run_id="audit", kind="workload", workspace=tmp_path / "data")
    task = TaskRecord(spec=TaskSpec(id="t1", title="result"), output=TaskOutput(files=[name]))
    with pytest.raises(PublishError):
        LoopEngine._stage_files(engine, pipeline, run, [task])
    assert copies == []
    assert not config.paths.run_artifacts("audit").exists()


@pytest.mark.parametrize("name", ["report.txt", "sub/forecast.csv", "./sub/report.md"])
def test_portable_artifact_paths_remain_supported(name: str) -> None:
    assert safe_relative(name) is not None
