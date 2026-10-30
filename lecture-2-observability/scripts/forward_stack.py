"""Keep the six Lab 2 services available on localhost until Ctrl-C."""

import argparse
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

CONTEXT = "kind-labs"
SERVICES = (
    ("Grafana", "default", "grafana", 3000, 80),
    ("API", "labs", "lab2-api", 18080, 80),
    ("Jaeger", "monitoring", "lab2-jaeger", 16686, 16686),
    ("Alertmanager", "default", "prometheus-alertmanager", 9093, 9093),
    ("Karma", "monitoring", "lab2-karma", 8080, 8080),
    ("Receiver", "monitoring", "lab2-alert-receiver", 18081, 8080),
)


def stop(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def drain(stream, messages):
    with stream:
        for line in stream:
            try:
                messages.put_nowait(line.rstrip())
            except queue.Full:
                pass
    try:
        messages.put_nowait(None)
    except queue.Full:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30,
        help="Seconds to wait for each port-forward (default: 30)",
    )
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    if not shutil.which("kubectl"):
        parser.error("Required command is missing: kubectl")
    processes = []
    reservations = {}
    previous = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupted)
        # Check all ports before spawning children. Release each reservation only
        # immediately before kubectl binds it; kubectl also detects bind races.
        for name, _namespace, _service, local, _remote in SERVICES:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                reservation.bind(("127.0.0.1", local))
            except OSError as exc:
                reservation.close()
                raise RuntimeError(
                    f"{name}: localhost port {local} is unavailable"
                ) from exc
            reservations[local] = reservation
        for name, namespace, service, local, remote in SERVICES:
            reservations.pop(local).close()
            process = subprocess.Popen(
                [
                    "kubectl",
                    "--context",
                    CONTEXT,
                    "--namespace",
                    namespace,
                    "port-forward",
                    f"svc/{service}",
                    f"{local}:{remote}",
                    "--address",
                    "127.0.0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes.append(process)
            messages = queue.Queue(maxsize=32)
            threading.Thread(
                target=drain, args=(process.stdout, messages), daemon=True
            ).start()
            deadline = time.monotonic() + args.startup_timeout
            ready = False
            detail = "no startup confirmation"
            while time.monotonic() < deadline:
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                detail = line
                if f"Forwarding from 127.0.0.1:{local} ->" in line:
                    ready = True
                    break
            if not ready:
                raise RuntimeError(f"{name}: port-forward failed: {detail}")
            print(f"{name}: http://127.0.0.1:{local}", flush=True)
        print("Port-forwards are running. Press Ctrl-C to stop all six.", flush=True)
        while True:
            for spec, process in zip(SERVICES, processes, strict=True):
                if process.poll() is not None:
                    raise RuntimeError(f"{spec[0]}: port-forward exited; stopping all")
            time.sleep(0.2)
    finally:
        # A second signal must not interrupt child cleanup.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        for reservation in reservations.values():
            reservation.close()
        stop(processes)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("All port-forwards stopped.")
    except (OSError, RuntimeError) as exc:
        print(f"Port-forward failed: {exc}", file=sys.stderr)
        sys.exit(1)
