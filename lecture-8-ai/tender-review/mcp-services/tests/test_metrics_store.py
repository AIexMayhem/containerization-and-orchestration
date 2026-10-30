from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tender_mcp.metrics_store import EventConflict, EventStore


def test_finished_run_is_counted_once_after_reopen(tmp_path):
    path = tmp_path / "nested" / "events.sqlite3"
    store = EventStore(path)

    assert store.totals() == {"complete": 0, "incomplete": 0, "error": 0}
    assert store.record_start("collect", "execution-1", 100.0) is True
    assert store.record_start("collect", "execution-1", 100.0) is False
    assert store.record_finish("collect", "execution-1", 120.0, "complete") is True
    assert store.record_finish("collect", "execution-1", 120.0, "complete") is False

    assert EventStore(path).totals() == {"complete": 1, "incomplete": 0, "error": 0}


def test_start_replay_does_not_reopen_a_finished_run(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 100)
    store.record_finish("collect", "one", 110, "incomplete")

    assert store.record_start("collect", "one", 100) is False
    assert store.oldest_running_age(200) == 0
    assert store.totals() == {"complete": 0, "incomplete": 1, "error": 0}


def test_conflicting_start_does_not_replace_original_timestamp(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 100)

    with pytest.raises(EventConflict):
        store.record_start("collect", "one", 120)

    assert store.oldest_running_age(150) == 50


@pytest.mark.parametrize("changed_finish", [(120, "error"), (121, "complete")])
def test_conflicting_finish_does_not_change_recorded_result(tmp_path, changed_finish):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 100)
    store.record_finish("collect", "one", 120, "complete")

    with pytest.raises(EventConflict):
        store.record_finish("collect", "one", *changed_finish)

    assert store.totals() == {"complete": 1, "incomplete": 0, "error": 0}


def test_finish_without_start_is_rejected(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventConflict):
        store.record_finish("collect", "unknown", 100, "error")

    assert store.totals() == {"complete": 0, "incomplete": 0, "error": 0}


def test_finish_before_start_does_not_close_the_run(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 100)

    with pytest.raises(EventConflict):
        store.record_finish("collect", "one", 99, "complete")

    assert store.oldest_running_age(110) == 10
    assert store.totals() == {"complete": 0, "incomplete": 0, "error": 0}


def test_each_outcome_is_counted_independently_across_workflows(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    for workflow, outcome in [
        ("first", "complete"),
        ("second", "incomplete"),
        ("third", "error"),
    ]:
        store.record_start(workflow, "same-id", 100)
        store.record_finish(workflow, "same-id", 100, outcome)

    assert store.totals() == {"complete": 1, "incomplete": 1, "error": 1}


def test_oldest_active_run_is_restored_after_reopen(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    assert store.oldest_running_age(200) == 0
    store.record_start("collect", "oldest", 100)
    store.record_start("collect", "later", 120)
    store.record_start("another-workflow", "newest", 140)

    store = EventStore(path)
    assert store.oldest_running_age(90) == 0
    assert store.oldest_running_age(200) == 100
    store.record_finish("collect", "oldest", 180, "complete")
    assert store.oldest_running_age(200) == 80
    store.record_finish("collect", "later", 190, "error")
    assert store.oldest_running_age(200) == 60
    store.record_finish("another-workflow", "newest", 200, "incomplete")
    assert store.oldest_running_age(250) == 0


def test_concurrent_duplicate_finishes_count_only_once(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    store.record_start("collect", "one", 100)
    barrier = Barrier(8)

    def finish(_):
        barrier.wait(timeout=5)
        return store.record_finish("collect", "one", 120, "complete")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(finish, range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert EventStore(path).totals() == {"complete": 1, "incomplete": 0, "error": 0}


@pytest.mark.parametrize(
    "timestamp", [float("nan"), float("inf"), -float("inf"), -1, True, False, "100", None]
)
@pytest.mark.parametrize("operation", ["start", "finish", "age"])
def test_invalid_timestamps_are_rejected(tmp_path, timestamp, operation):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 0)

    with pytest.raises(ValueError) as error:
        if operation == "start":
            store.record_start("collect", "two", timestamp)
        elif operation == "finish":
            store.record_finish("collect", "one", timestamp, "complete")
        else:
            store.oldest_running_age(timestamp)

    assert not isinstance(error.value, EventConflict)
    assert store.totals() == {"complete": 0, "incomplete": 0, "error": 0}


@pytest.mark.parametrize(
    ("workflow", "execution_id"),
    [
        ("", "one"),
        ("  ", "one"),
        ("w" * 65, "one"),
        (None, "one"),
        ("collect", ""),
        ("collect", "e" * 129),
        ("collect", 123),
    ],
)
@pytest.mark.parametrize("operation", ["start", "finish"])
def test_invalid_identities_are_rejected(tmp_path, workflow, execution_id, operation):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(ValueError) as error:
        if operation == "start":
            store.record_start(workflow, execution_id, 0)
        else:
            store.record_finish(workflow, execution_id, 10, "complete")

    assert not isinstance(error.value, EventConflict)


@pytest.mark.parametrize("outcome", ["success", "", None, True, []])
def test_invalid_outcomes_do_not_finish_the_run(tmp_path, outcome):
    store = EventStore(tmp_path / "events.sqlite3")
    store.record_start("collect", "one", 0)

    with pytest.raises(ValueError):
        store.record_finish("collect", "one", 10, outcome)

    assert store.oldest_running_age(20) == 20


def test_zero_timestamps_and_maximum_length_identities_are_valid(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    workflow, execution_id = "w" * 64, "e" * 128

    assert store.record_start(workflow, execution_id, 0) is True
    assert store.record_finish(workflow, execution_id, 0, "complete") is True
    assert store.totals() == {"complete": 1, "incomplete": 0, "error": 0}
