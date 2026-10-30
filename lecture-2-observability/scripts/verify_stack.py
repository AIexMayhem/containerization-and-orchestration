"""Verify the local stack; --exercise also triggers alerts and restores the API."""

import argparse
import base64
import contextlib
import json
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

CONTEXT = "kind-labs"
EVIDENCE = Path(tempfile.gettempdir()) / "lab2-verification"


def kubectl(*args):
    return subprocess.check_output(
        ["kubectl", "--context", CONTEXT, "--request-timeout=15s", *args], text=True
    )


@contextlib.contextmanager
def forward(namespace, service, port):
    process = subprocess.Popen(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            namespace,
            "port-forward",
            "svc/" + service,
            ":" + str(port),
            "--address",
            "127.0.0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        deadline = time.monotonic() + 20
        local_port = None
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 1)[0]:
                line = process.stdout.readline()
                match = re.search(r"127\.0\.0\.1:(\d+) ->", line)
                if match:
                    local_port = match.group(1)
                    break
                if process.poll() is not None:
                    break
        if local_port is None:
            raise RuntimeError("Port-forward failed for " + service)

        def drain():
            for _line in process.stdout:
                pass

        threading.Thread(target=drain, daemon=True).start()
        yield "http://127.0.0.1:" + local_port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def request(url, payload=None, headers=None, timeout=12):
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=request_headers)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read().decode()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return response.code, response.headers, data


def json_get(url, payload=None, headers=None):
    status, _, data = request(url, payload, headers)
    assert status == 200, (url, status, data)
    return data


def wait_for(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(2)
    raise AssertionError("Condition did not become true before timeout")


def save(name, value):
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def lab_alert_name(labels):
    if labels.get("namespace") == "labs" and labels.get("service") == "lab2-api":
        return labels.get("alertname")
    return None


def fresh_firing_notifications(events, after_id, started_at):
    names = set()
    for event in events["events"]:
        if event["id"] <= after_id:
            continue
        if datetime.fromisoformat(event["received_at"]) < started_at:
            continue
        for alert in event["payload"].get("alerts", []):
            name = lab_alert_name(alert.get("labels", {}))
            if alert.get("status") == "firing" and name:
                names.add(name)
    return names


def karma_active_alerts(data):
    names = set()
    for grid in data.get("grids", []):
        for group in grid.get("alertGroups", []):
            group_labels = {
                label["name"]: label["value"]
                for label in (
                    group.get("labels", []) + group.get("shared", {}).get("labels", [])
                )
            }
            for alert in group.get("alerts", []):
                labels = group_labels | {
                    label["name"]: label["value"] for label in alert.get("labels", [])
                }
                name = lab_alert_name(labels)
                if alert.get("state") == "active" and name:
                    names.add(name)
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--duration", type=int, default=210)
    args = parser.parse_args()
    if args.exercise and not 150 <= args.duration <= 600:
        parser.error("Exercise duration must be between 150 and 600 seconds")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = {"checked_at_utc": datetime.now(UTC).isoformat(), "context": CONTEXT}
    with contextlib.ExitStack() as stack:
        services = {
            "api": ("labs", "lab2-api", 80),
            "jaeger": ("monitoring", "lab2-jaeger", 16686),
            "prometheus": ("default", "prometheus-server", 80),
            "alertmanager": ("default", "prometheus-alertmanager", 9093),
            "receiver": ("monitoring", "lab2-alert-receiver", 8080),
            "karma": ("monitoring", "lab2-karma", 8080),
            "grafana": ("default", "grafana", 80),
            "loki": ("monitoring", "loki", 3100),
        }
        urls = {
            name: stack.enter_context(forward(*spec)) for name, spec in services.items()
        }
        print(json.dumps({"temporary_urls": urls}), flush=True)

        def prom(expr):
            data = json_get(
                urls["prometheus"]
                + "/api/v1/query?"
                + urllib.parse.urlencode({"query": expr})
            )
            assert data["status"] == "success", data
            return data["data"]["result"]

        def rules():
            groups = json_get(urls["prometheus"] + "/api/v1/rules")["data"]["groups"]
            return [
                rule
                for group in groups
                if group["name"] == "lab2-critical"
                for rule in group["rules"]
            ]

        def firing():
            return sorted(rule["name"] for rule in rules() if rule["state"] == "firing")

        def karma():
            return json_get(
                urls["karma"] + "/alerts.json",
                {"filters": ["@state=active"], "defaultGroupLimit": 100},
            )

        trace_ids = {}
        for name, path, expected in [
            ("health", "/health", 200),
            ("slow", "/slow?seconds=2", 200),
            ("fail", "/fail", 500),
            ("load", "/load?count=100&concurrency=20", 200),
        ]:
            status, headers, data = request(urls["api"] + path)
            assert status == expected, (path, status, data)
            trace_id = headers.get("X-Trace-ID", "")
            assert re.fullmatch(r"[0-9a-f]{32}", trace_id) and int(trace_id, 16), (
                trace_id
            )
            trace_ids[name] = trace_id
        result["trace_ids"] = trace_ids
        print(json.dumps({"trace_ids": trace_ids}), flush=True)

        def read_trace(trace_id):
            status, _, data = request(urls["jaeger"] + "/api/traces/" + trace_id)
            if status == 404:
                return None
            assert status == 200, (status, data)
            return data if data.get("data") else None

        slow_trace = wait_for(lambda: read_trace(trace_ids["slow"]))
        fail_trace = wait_for(lambda: read_trace(trace_ids["fail"]))
        slow_spans = slow_trace["data"][0]["spans"]
        children = [span for span in slow_spans if span["operationName"] == "slow-op"]
        assert children and children[0]["duration"] >= 1_800_000, children
        assert any(ref["refType"] == "CHILD_OF" for ref in children[0]["references"])
        fail_spans = fail_trace["data"][0]["spans"]
        assert any(
            tag["key"] == "error" and tag["value"] is True
            for span in fail_spans
            for tag in span["tags"]
        ), fail_spans
        save("2026-10-30-slow-trace.json", slow_trace)
        save("2026-10-30-fail-trace.json", fail_trace)
        result["slow_child_duration_us"] = children[0]["duration"]
        result["fail_span_error"] = True

        query = (
            '{namespace="labs",container="lab2-api"} | json | trace_id="'
            + trace_ids["fail"]
            + '"'
        )

        def matching_logs():
            end = time.time_ns()
            data = json_get(
                urls["loki"]
                + "/loki/api/v1/query_range?"
                + urllib.parse.urlencode(
                    {
                        "query": query,
                        "start": end - 10 * 60 * 10**9,
                        "end": end,
                        "limit": 10,
                    }
                )
            )
            return data if data.get("data", {}).get("result") else None

        logs = wait_for(matching_logs)
        save("2026-10-30-correlated-error-log.json", logs)
        result["matching_loki_trace_id"] = trace_ids["fail"]

        secret = json.loads(
            kubectl("-n", "default", "get", "secret", "grafana", "-o", "json")
        )["data"]
        token = base64.b64encode(
            base64.b64decode(secret["admin-user"])
            + b":"
            + base64.b64decode(secret["admin-password"])
        ).decode()
        auth = {"Authorization": "Basic " + token}
        sources = json_get(urls["grafana"] + "/api/datasources", headers=auth)
        result["datasources"] = [
            {
                key: source.get(key)
                for key in ["name", "uid", "type", "url", "isDefault"]
            }
            for source in sources
        ]
        assert {source["type"] for source in sources} == {
            "prometheus",
            "loki",
            "jaeger",
        }
        loki_source = next(source for source in sources if source["type"] == "loki")
        derived = loki_source["jsonData"]["derivedFields"][0]
        assert (
            derived["datasourceUid"] == "lab2-jaeger"
            and "__value.raw" in derived["url"]
        )
        line = next(
            values[1]
            for stream in logs["data"]["result"]
            for values in stream["values"]
        )
        assert re.search(derived["matcherRegex"], line).group(1) == trace_ids["fail"]
        grafana_trace = json_get(
            urls["grafana"]
            + "/api/datasources/proxy/uid/lab2-jaeger/api/traces/"
            + trace_ids["fail"],
            headers=auth,
        )
        assert grafana_trace.get("data"), grafana_trace
        result["grafana_log_to_trace_wired"] = True
        dashboard = json_get(
            urls["grafana"] + "/api/dashboards/uid/admghnj", headers=auth
        )["dashboard"]
        assert len(dashboard["panels"]) == 3
        assert (
            dashboard["panels"][0]["fieldConfig"]["defaults"]["custom"][
                "scaleDistribution"
            ]["type"]
            == "linear"
        )
        result["red_dashboard_uid"] = dashboard["uid"]
        active_rules = rules()
        assert len(active_rules) == 3 and all(
            rule["health"] == "ok" for rule in active_rules
        ), active_rules
        result["rules"] = [
            {
                key: rule.get(key)
                for key in ["name", "state", "health", "query", "duration"]
            }
            for rule in active_rules
        ]
        initial_karma = karma()
        assert initial_karma["upstreams"]["counters"]["healthy"] == 1, initial_karma[
            "upstreams"
        ]
        result["karma_upstream_healthy"] = True
        assert json_get(urls["receiver"] + "/health")["status"] == "ok"
        result["prometheus_api_up"] = wait_for(
            lambda: (
                samples
                if len(samples := prom('up{namespace="labs",service="lab2-api"}')) == 2
                and all(sample["value"][1] == "1" for sample in samples)
                else None
            ),
            100,
        )
        print(
            json.dumps(
                {
                    "smoke_checks": "passed",
                    "slow_child_us": result["slow_child_duration_us"],
                    "rules": [rule["name"] for rule in active_rules],
                }
            ),
            flush=True,
        )

        if args.exercise:
            exercise_started = datetime.now(UTC)
            receiver_baseline = json_get(urls["receiver"] + "/events")["received_total"]
            result["exercise_started_at_utc"] = exercise_started.isoformat()
            result["receiver_baseline_received_total"] = receiver_baseline

            def karma_with_alerts(required):
                data = karma()
                return data if required.issubset(karma_active_alerts(data)) else None

            observed = set()
            karma_observed = set()
            job_name = "lab2-red-check-" + str(int(time.time()))
            source = Path(__file__).with_name("load_probe.py").read_text()
            job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": job_name, "namespace": "labs"},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": args.duration + 90,
                    "ttlSecondsAfterFinished": 300,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "traffic",
                                    "image": "lab2-api:1.0",
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": [
                                        "python",
                                        "-u",
                                        "-c",
                                        source,
                                        str(args.duration),
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "32Mi"},
                                        "limits": {"cpu": "500m", "memory": "128Mi"},
                                    },
                                }
                            ],
                        }
                    },
                },
            }
            subprocess.run(
                ["kubectl", "--context", CONTEXT, "create", "-f", "-"],
                input=json.dumps(job),
                text=True,
                check=True,
                capture_output=True,
            )
            save("2026-10-30-load-job.json", job)
            started = time.monotonic()
            try:
                while time.monotonic() - started < args.duration + 90:
                    state = firing()
                    observed.update(state)
                    print(
                        json.dumps(
                            {
                                "phase": "traffic_job",
                                "seconds": round(time.monotonic() - started),
                                "firing": state,
                            }
                        ),
                        flush=True,
                    )
                    save(
                        "2026-10-30-alerts-during-traffic.json",
                        json_get(urls["prometheus"] + "/api/v1/alerts"),
                    )
                    karma_state = karma()
                    karma_observed.update(karma_active_alerts(karma_state))
                    save("2026-10-30-karma-during-traffic.json", karma_state)
                    status = json.loads(
                        kubectl("-n", "labs", "get", "job", job_name, "-o", "json")
                    )["status"]
                    if status.get("succeeded") or status.get("failed"):
                        raw = kubectl("-n", "labs", "logs", "job/" + job_name)
                        (EVIDENCE / "2026-10-30-load-job.log").write_text(raw)
                        assert status.get("succeeded"), raw
                        traffic_result = json.loads(raw.strip().splitlines()[-1])
                        break
                    time.sleep(15)
                else:
                    raise AssertionError("Load Job did not finish")
            finally:
                kubectl(
                    "-n",
                    "labs",
                    "delete",
                    "job",
                    job_name,
                    "--ignore-not-found",
                    "--wait=false",
                )
            observed.update(firing())
            traffic_alerts = {"Lab2High5xxRate", "Lab2HighP95"}
            assert traffic_alerts.issubset(observed), observed
            if not traffic_alerts.issubset(karma_observed):
                karma_state = wait_for(lambda: karma_with_alerts(traffic_alerts), 60)
                karma_observed.update(karma_active_alerts(karma_state))
                save("2026-10-30-karma-during-traffic.json", karma_state)
            assert traffic_alerts.issubset(karma_observed), karma_observed
            result.update(traffic_result)
            result["p95_under_load"] = prom(
                "histogram_quantile(0.95,sum by(le)(rate("
                'http_request_duration_seconds_bucket{namespace="labs",'
                'service="lab2-api"}[5m])))'
            )
            try:
                kubectl("-n", "labs", "scale", "deployment/lab2-api", "--replicas=0")
                # Removed targets can remain queryable until service discovery and
                # stale markers catch up; allow that delay plus the 1m `for`.
                for second in range(0, 361, 15):
                    state = firing()
                    observed.update(state)
                    print(
                        json.dumps(
                            {
                                "phase": "controlled_outage",
                                "seconds": second,
                                "firing": state,
                            }
                        ),
                        flush=True,
                    )
                    if "Lab2ApiUnavailable" in state:
                        save(
                            "2026-10-30-alerts-outage.json",
                            json_get(urls["prometheus"] + "/api/v1/alerts"),
                        )
                        karma_state = wait_for(
                            lambda: karma_with_alerts({"Lab2ApiUnavailable"}), 60
                        )
                        karma_observed.update(karma_active_alerts(karma_state))
                        save("2026-10-30-karma-outage.json", karma_state)
                        time.sleep(15)
                        break
                    time.sleep(15)
            finally:
                kubectl("-n", "labs", "scale", "deployment/lab2-api", "--replicas=2")
                subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        CONTEXT,
                        "-n",
                        "labs",
                        "rollout",
                        "status",
                        "deployment/lab2-api",
                        "--timeout=120s",
                    ],
                    check=True,
                )
            assert "Lab2ApiUnavailable" in observed, observed
            result["observed_firing_alerts"] = sorted(observed)
            result["karma_active_alerts"] = sorted(karma_observed)

            def complete_notifications():
                events = json_get(urls["receiver"] + "/events")
                names = fresh_firing_notifications(
                    events, receiver_baseline, exercise_started
                )
                return events if observed.issubset(names) else None

            notifications = wait_for(complete_notifications, 60)
            save("2026-10-30-receiver-notifications.json", notifications)
            delivered = fresh_firing_notifications(
                notifications, receiver_baseline, exercise_started
            )
            assert observed.issubset(delivered), (observed, delivered)
            result["receiver_firing_alerts"] = sorted(delivered)
            result["receiver_notifications"] = notifications["received_total"]

            def restored_targets():
                samples = prom('up{namespace="labs",service="lab2-api"}')
                if len(samples) == 2 and all(
                    sample["value"][1] == "1" for sample in samples
                ):
                    return samples
                return None

            result["prometheus_api_up_after_restore"] = wait_for(restored_targets, 120)
            result["api_restored"] = True
            print(
                json.dumps(
                    {
                        "exercise": "passed",
                        "alerts": sorted(observed),
                        "notifications": notifications["received_total"],
                    }
                ),
                flush=True,
            )

        pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
        result["unready_pods"] = [
            p["metadata"]["namespace"] + "/" + p["metadata"]["name"]
            for p in pods
            if p["status"]["phase"] != "Succeeded"
            and not all(
                c.get("ready") for c in p["status"].get("containerStatuses", [{}])
            )
        ]
        assert not result["unready_pods"], result["unready_pods"]
        result["finished_at_utc"] = datetime.now(UTC).isoformat()
        save("2026-10-30-stack-verification.json", result)
        print(
            json.dumps(
                {
                    "result": "passed",
                    "evidence": str(EVIDENCE / "2026-10-30-stack-verification.json"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
