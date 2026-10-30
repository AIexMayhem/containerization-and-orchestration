import asyncio
import time

import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

from tender_mcp.metrics import create_app
from tender_mcp.metrics_store import EventStore


def samples(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


def count(values, outcome):
    return values[("tender_collection_runs_total", (("outcome", outcome),))]


def age(values):
    return values[("tender_collection_oldest_running_age_seconds", ())]


def test_metrics_publish_initial_zero_counters_without_claiming_dependencies_ready(tmp_path):
    with TestClient(create_app(EventStore(tmp_path / "events.db"), probes={})) as client:
        values = samples(client)
        assert [count(values, outcome) for outcome in ("complete", "incomplete", "error")] == [
            0,
            0,
            0,
        ]
        assert age(values) == 0
        assert not any(key[0] == "tender_dependency_ready" for key in values)


def test_events_update_age_and_outcomes_once_and_survive_new_app(tmp_path):
    path = tmp_path / "events.db"
    now = [100.0]
    start = {"workflow": "tender-precheck", "execution_id": "17", "started_at": 100}
    finish = {
        "workflow": "tender-precheck",
        "execution_id": "17",
        "finished_at": 140,
        "outcome": "incomplete",
    }
    app = create_app(EventStore(path), probes={}, clock=lambda: now[0])
    with TestClient(app) as client:
        assert client.post("/events/start", json=start).status_code == 200
        assert client.post("/events/start", json=start).status_code == 200
        now[0] = 130
        assert age(samples(client)) == 30
        assert client.post("/events/finish", json=finish).status_code == 200
        assert client.post("/events/finish", json=finish).status_code == 200
        assert client.post("/events/start", json=start).status_code == 200
        assert count(samples(client), "incomplete") == 1
        assert age(samples(client)) == 0
        start2 = {**start, "execution_id": "18", "started_at": 150}
        assert client.post("/events/start", json=start2).status_code == 200
    now[0] = 180
    with TestClient(create_app(EventStore(path), probes={}, clock=lambda: now[0])) as client:
        values = samples(client)
        assert count(values, "incomplete") == 1
        assert count(values, "complete") == 0
        assert age(values) == 30


def test_conflicting_or_unknown_events_return_409_without_changing_metrics(tmp_path):
    start = {"workflow": "tender-precheck", "execution_id": "17", "started_at": 100}
    finish = {
        "workflow": "tender-precheck",
        "execution_id": "17",
        "finished_at": 120,
        "outcome": "complete",
    }
    with TestClient(
        create_app(EventStore(tmp_path / "events.db"), probes={}, clock=lambda: 150)
    ) as client:
        assert client.post("/events/finish", json=finish).status_code == 409
        assert client.post("/events/start", json=start).status_code == 200
        assert client.post("/events/start", json={**start, "started_at": 101}).status_code == 409
        assert client.post("/events/finish", json={**finish, "finished_at": 99}).status_code == 409
        assert age(samples(client)) == 50
        assert client.post("/events/finish", json=finish).status_code == 200
        assert client.post("/events/finish", json={**finish, "outcome": "error"}).status_code == 409
        assert count(samples(client), "complete") == 1
        assert count(samples(client), "error") == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"workflow": "another-workflow"},
        {"execution_id": 17},
        {"execution_id": " "},
        {"started_at": True},
        {"started_at": "100"},
        {"started_at": -1},
        {"documents": "unnecessary source content"},
    ],
)
def test_invalid_event_is_rejected_before_registration(tmp_path, patch):
    payload = {"workflow": "tender-precheck", "execution_id": "17", "started_at": 100, **patch}
    with TestClient(create_app(EventStore(tmp_path / "events.db"), probes={})) as client:
        response = client.post("/events/start", json=payload)
        assert response.status_code == 422
        assert "unnecessary source content" not in response.text
        assert age(samples(client)) == 0
        assert count(samples(client), "complete") == 0


def test_malformed_json_and_invalid_outcome_do_not_finish_run(tmp_path):
    with TestClient(
        create_app(EventStore(tmp_path / "events.db"), probes={}, clock=lambda: 120)
    ) as client:
        assert client.post("/events/start", content="{broken").status_code == 400
        start = {"workflow": "tender-precheck", "execution_id": "17", "started_at": 100}
        assert client.post("/events/start", json=start).status_code == 200
        finish = {"workflow": "tender-precheck", "execution_id": "17", "finished_at": 110}
        assert client.post("/events/finish", json={**finish, "outcome": "match"}).status_code == 422
        assert age(samples(client)) == 20


def test_background_probes_report_failure_recover_and_do_not_block_scrape(tmp_path):
    available = [False]

    async def changing_probe():
        if not available[0]:
            raise OSError("dependency down")
        return True

    async def blocked_probe():
        await asyncio.sleep(60)
        return True

    probes = {"documents": changing_probe, "ollama": blocked_probe}
    app = create_app(
        EventStore(tmp_path / "events.db"), probes=probes, probe_interval=0.02, probe_timeout=0.03
    )
    with TestClient(app) as client:
        started = time.monotonic()
        initial = samples(client)
        assert time.monotonic() - started < 1
        assert not any(
            name == "tender_dependency_ready" and labels == (("component", "ollama"),)
            for name, labels in initial
        )

        def wait_for(expected):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                values = samples(client)
                key = ("tender_dependency_ready", (("component", "documents"),))
                ollama = ("tender_dependency_ready", (("component", "ollama"),))
                if values.get(key) == expected and values.get(ollama) == 0:
                    return values
                time.sleep(0.01)
            pytest.fail("background readiness did not reach expected state")

        wait_for(0)
        available[0] = True
        values = wait_for(1)
        assert (
            values[
                ("tender_dependency_last_probe_timestamp_seconds", (("component", "documents"),))
            ]
            > 0
        )


@pytest.mark.parametrize("interval,timeout", [(0, 1), (1, 0), (-1, 1), (1, float("nan"))])
def test_invalid_probe_timing_rejected(tmp_path, interval, timeout):
    with pytest.raises(ValueError):
        create_app(
            EventStore(tmp_path / "events.db"),
            probes={},
            probe_interval=interval,
            probe_timeout=timeout,
        )
