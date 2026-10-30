"""Install or update the seven Lab 2 Helm releases in the kind-labs context."""

import argparse
import base64
import binascii
import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

CONTEXT = "kind-labs"
LAB = Path(__file__).resolve().parents[1]
HELM = LAB / "helm"


def download(chart, directory):
    """Use only locked archives; the GitHub API bypasses failed release redirects."""
    destination = directory / f"{chart['name']}-{chart['version']}.tgz"
    urls = [chart["url"]]
    if chart.get("api_asset_url"):
        urls.append(chart["api_asset_url"])
    tls = ssl.create_default_context()
    # Python.org macOS builds may lack their own CA bundle. Add the OS bundle
    # while retaining hostname and certificate verification.
    system_ca = Path("/etc/ssl/cert.pem")
    if sys.platform == "darwin" and system_ca.is_file():
        tls.load_verify_locations(cafile=system_ca)
    for index, url in enumerate(urls):
        headers = {"User-Agent": "lab2-stack-deployer"}
        if url == chart.get("api_asset_url"):
            headers.update(
                Accept="application/octet-stream",
                **{"X-GitHub-Api-Version": "2022-11-28"},
            )
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20, context=tls) as response:
                with destination.open("wb") as target:
                    shutil.copyfileobj(response, target)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            destination.unlink(missing_ok=True)
            if index + 1 < len(urls):
                print(f"{chart['name']}: release download failed; trying GitHub API")
                continue
            raise RuntimeError(f"Could not download {chart['name']}") from exc
        with destination.open("rb") as downloaded:
            actual = hashlib.file_digest(downloaded, "sha256").hexdigest()
        if actual != chart["sha256"]:
            raise RuntimeError(
                f"SHA-256 mismatch for {chart['name']}; refusing archive"
            )
        break

    chart_directory = directory / f"{chart['name']}-{chart['version']}"
    chart_directory.mkdir()
    with tarfile.open(destination, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != chart["name"]
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError(f"Unsafe archive member in {chart['name']}")
        archive.extractall(chart_directory, members=members, filter="data")
    result = chart_directory / chart["name"]
    metadata = result / "Chart.yaml"
    if not metadata.is_file():
        raise RuntimeError(f"Missing Chart.yaml in {chart['name']}")
    return result


def grafana_credentials():
    """Read the existing generated Secret without exposing values to the terminal."""
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "--request-timeout=15s",
            "-n",
            "default",
            "get",
            "secret",
            "grafana",
            "--ignore-not-found",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Cannot read existing Grafana Secret; deployment stopped")
    if not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)["data"]
        return {
            name: base64.b64decode(data[key], validate=True).decode("utf-8")
            for name, key in (
                ("adminUser", "admin-user"),
                ("adminPassword", "admin-password"),
            )
        }
    except (KeyError, ValueError, UnicodeError, binascii.Error) as exc:
        raise RuntimeError("Existing Grafana credentials cannot be decoded") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        default="10m",
        help="Helm wait timeout for each release (default: 10m)",
    )
    args = parser.parse_args()
    for command in ("helm", "kubectl"):
        if not shutil.which(command):
            parser.error(f"Required command is missing: {command}")
    credentials = grafana_credentials()

    def run(command):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        output = result.stdout + result.stderr
        for value in credentials.values():
            if value:
                output = output.replace(value, "[REDACTED]")
        if output.strip():
            print(output.rstrip(), flush=True)
        if result.returncode:
            raise RuntimeError(f"{command[0]} exited with status {result.returncode}")

    # TemporaryDirectory is private (0700); credentials are additionally 0600.
    with tempfile.TemporaryDirectory(prefix="lab2-deploy-") as temporary:
        cache = Path(temporary)
        locked = json.loads((HELM / "chart-lock.json").read_text())
        charts = {chart["name"]: download(chart, cache) for chart in locked}
        credential_values = cache / "grafana-private-values.json"
        descriptor = os.open(
            credential_values, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w") as out:
            json.dump(credentials, out)

        def install(release, namespace, chart, values=None, extra=(), reuse=False):
            print(f"Installing/updating {release} in {namespace}", flush=True)
            command = [
                "helm",
                "upgrade",
                "--install",
                release,
                str(chart),
                "--kube-context",
                CONTEXT,
                "--namespace",
                namespace,
                "--create-namespace",
                "--wait",
                "--timeout",
                args.timeout,
                "--hide-notes",
            ]
            if reuse:
                command.append("--reuse-values")
            if values:
                command.extend(["--values", str(values)])
            command.extend(extra)
            run(command)

        install(
            "loki",
            "monitoring",
            charts["loki"],
            LAB / "api/loki-values.yaml",
            reuse=True,
        )
        install(
            "alloy",
            "monitoring",
            charts["alloy"],
            LAB / "api/alloy-values.yaml",
            reuse=True,
        )
        install("lab2-jaeger", "monitoring", HELM / "jaeger")
        install("lab2-alerting", "monitoring", HELM / "alerting")
        install("lab2-api", "labs", HELM / "api", extra=("--take-ownership",))
        install(
            "prometheus",
            "default",
            charts["prometheus"],
            HELM / "prometheus-values.yaml",
            reuse=True,
        )
        install(
            "grafana",
            "default",
            charts["grafana"],
            HELM / "grafana-values.yaml",
            extra=(
                "--values",
                str(credential_values),
                "--set-file",
                "dashboards.default.api-red.json="
                + str(HELM / "dashboards/api-red-dashboard.json"),
                "--set-file",
                "dashboards.default.api-logs.json="
                + str(HELM / "dashboards/api-logs-dashboard.json"),
            ),
            reuse=True,
        )
    print(
        "Seven Lab 2 releases are ready. Run scripts/forward_stack.py for local access."
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, tarfile.TarError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            "Deployment interrupted; inspect release status before retrying.",
            file=sys.stderr,
        )
        sys.exit(130)
