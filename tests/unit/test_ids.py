from sbxloop import ids


def test_run_id_shape_and_uniqueness() -> None:
    seen = {ids.new_run_id() for _ in range(200)}
    assert len(seen) == 200
    for rid in seen:
        assert ids.is_run_id(rid)
        assert rid.startswith("r")
        assert len(rid) == 9


def test_is_run_id_rejects_bad_values() -> None:
    assert not ids.is_run_id("r123")  # too short
    assert not ids.is_run_id("x12345678")  # wrong prefix
    assert not ids.is_run_id("rABCDEFGH")  # uppercase not allowed
    assert not ids.is_run_id("rilouilou")  # lookalike chars excluded


def test_job_id_shape() -> None:
    jid = ids.new_job_id()
    assert jid.startswith("j")
    assert len(jid) == 11


def test_task_id() -> None:
    assert ids.task_id(1) == "t1"
    assert ids.task_id(12) == "t12"


def test_task_id_rejects_zero() -> None:
    import pytest

    with pytest.raises(ValueError, match="starts at 1"):
        ids.task_id(0)


def test_branch_name_takes_the_operators_prefix() -> None:
    assert ids.branch_name("r12345678") == "sbxloop/r12345678"
    assert ids.branch_name("r12345678", "bot/") == "bot/r12345678"
