"""Fixed-rate test client: sends HTTP or gRPC requests through Envoy at a fixed rate
for a fixed time, WITHOUT waiting for each answer before sending the next.

Why "without waiting" (an open-loop test): a client that waits for each answer
slows down when the server slows down, so it hides the problem. Here request
number i is always sent at start + i / rate, whatever happened to earlier ones.
Each request's time is measured from that planned send moment, so any delay
caused by a slow server is counted.

Test operations (one per interface, from A2):
  http  GET /quests/{instance_id}         read-only
  grpc  QuestService.ResolveQuest          asks for "completed" on quests that are
                                           already completed: a safe repeat that
                                           changes nothing and answers OK

Before the run, a small pool of quests is created and completed over HTTP
(POST /quests + PATCH), so the test requests always have real quests to use.
No retries and no circuit breaker: every request is sent once, and a failure is
counted as a failure.

Examples (run from the project folder with the venv's Python):
    python scripts/fixed_rate_load.py http --rate 20 --duration 60 --label baseline
    python scripts/fixed_rate_load.py grpc --rate 20 --duration 60 --label baseline

At the end it prints the results and adds one line to results/runs.csv.

Failure and recovery (Part D experiment 3): stop a replica's container during the
run and start it again, then print a second-by-second timeline of successes,
failures and which replicas answered (also saved to results/timeline-*.csv):
    python scripts/fixed_rate_load.py http --duration 90 --label failure --stop-container esd-a3-quest-app-2 --stop-at 20 --start-at 50
"""
import argparse
import csv
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from questpb import quest_pb2, quest_pb2_grpc  # noqa: E402

RESULTS_CSV = ROOT / "results" / "runs.csv"


def http_call(method, url, request_id, body=None, timeout=2.0):
    """One HTTP request. Returns (status, replica); status is an int or an error name."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Request-ID": request_id}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status, response.headers.get("X-Replica")
    except urllib.error.HTTPError as error:  # 4xx / 5xx: the server (or Envoy) answered
        return error.code, error.headers.get("X-Replica")
    except TimeoutError:
        return "timeout", None
    except urllib.error.URLError as error:
        return "timeout" if isinstance(error.reason, TimeoutError) else "connection_error", None
    except OSError:
        return "connection_error", None


def prepare_quests(http_url, count, run_id):
    """Create `count` quests and complete them, over HTTP. Returns their IDs."""
    ids = []
    for n in range(count):
        request = urllib.request.Request(f"{http_url}/quests", method="POST",
                                         headers={"X-Request-ID": f"{run_id}-setup-{n}"})
        while True:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    quest = json.loads(response.read())
                break
            except urllib.error.HTTPError as error:
                if error.code != 429:  # POST /quests is rate limited: wait as told, then go on
                    raise
                time.sleep(int(error.headers.get("Retry-After", "1")))
        status, _ = http_call("PATCH", f"{http_url}/quests/{quest['instance_id']}",
                              f"{run_id}-setup-{n}", {"status": "completed"}, timeout=5)
        if status != 200:
            raise SystemExit(f"setup failed: PATCH returned {status}")
        ids.append(quest["instance_id"])
    return ids


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))]


def main():
    parser = argparse.ArgumentParser(description="Fixed-rate (open-loop) test client through Envoy")
    parser.add_argument("interface", choices=["http", "grpc"])
    parser.add_argument("--rate", type=float, default=20, help="requests per second (default 20)")
    parser.add_argument("--duration", type=float, default=60, help="seconds to send for (default 60)")
    parser.add_argument("--timeout", type=float, default=2.0, help="seconds before a request counts as failed")
    parser.add_argument("--label", default="run", help="name for this run in results/runs.csv")
    parser.add_argument("--pool", type=int, default=20, help="quests to prepare before the run")
    parser.add_argument("--http-url", default="http://127.0.0.1:8000", help="Envoy's HTTP address")
    parser.add_argument("--grpc-target", default="localhost:50051", help="Envoy's gRPC address")
    # Part D experiment 3 (failure and recovery): stop one replica's container
    # during the run and start it again, at fixed seconds into the run.
    parser.add_argument("--stop-container", help="container to stop and restart, e.g. esd-a3-quest-app-2 (B)")
    parser.add_argument("--stop-at", type=float, default=20, help="seconds into the run to stop it (default 20)")
    parser.add_argument("--start-at", type=float, default=50, help="seconds into the run to start it (default 50)")
    args = parser.parse_args()

    run_id = "lt" + uuid.uuid4().hex[:6]
    total = int(args.rate * args.duration)
    print(f"run {run_id}: {args.interface} at {args.rate:g}/s for {args.duration:g}s = {total} requests "
          f"(timeout {args.timeout:g}s, no retries)")

    quest_ids = prepare_quests(args.http_url, args.pool, run_id)
    print(f"prepared {len(quest_ids)} completed quests")

    lock = threading.Lock()
    latencies, outcomes, replicas = [], Counter(), Counter()
    timeline = {}  # second of the run -> Counter of "ok", "failed", "A", "B", "C"
    done = threading.Event()
    finished = [0]
    start_ref = [0.0]  # set just before the first send

    def record(planned, outcome, replica):
        took = time.perf_counter() - planned
        with lock:
            outcomes[str(outcome)] += 1
            second = timeline.setdefault(int(planned - start_ref[0]), Counter())
            second["ok" if outcome in (200, "OK") else "failed"] += 1
            if replica:
                replicas[replica] += 1
                second[replica] += 1
            if outcome in (200, "OK"):
                latencies.append(took)
            finished[0] += 1
            if finished[0] == total:
                done.set()

    channel = grpc.insecure_channel(args.grpc_target)  # one connection to Envoy for the whole run
    stub = quest_pb2_grpc.QuestServiceStub(channel)
    # Enough threads that a slow server never holds back the sending (HTTP only;
    # gRPC calls are sent with .future(), which does not block).
    pool = ThreadPoolExecutor(max_workers=max(16, int(args.rate * (args.timeout + 1))))

    def send_http(i, planned):
        url = f"{args.http_url}/quests/{quest_ids[i % len(quest_ids)]}"
        status, replica = http_call("GET", url, f"{run_id}-{i}", timeout=args.timeout)
        record(planned, status, replica)

    def send_grpc(i, planned):
        request = quest_pb2.ResolveQuestRequest(instance_id=quest_ids[i % len(quest_ids)],
                                                outcome=quest_pb2.OUTCOME_COMPLETED)
        future = stub.ResolveQuest.future(request, timeout=args.timeout,
                                          metadata=(("x-request-id", f"{run_id}-{i}"),))

        def on_done(f):
            try:
                f.result()
                code = "OK"
            except grpc.RpcError as error:
                code = error.code().name
            try:
                replica = dict(f.initial_metadata() or ()).get("x-replica")
            except Exception:
                replica = None
            record(planned, code, replica)

        future.add_done_callback(on_done)

    events = []  # (seconds into the run, what happened) for the stop/start of a container

    def container_ip(name):
        result = subprocess.run(["docker", "inspect", "-f",
                                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                                capture_output=True, text=True)
        return result.stdout.strip() or "-"

    def stop_and_start():
        for at, action in ((args.stop_at, "stop"), (args.start_at, "start")):
            time.sleep(max(0.0, start_ref[0] + at - time.perf_counter()))
            began = time.perf_counter() - start_ref[0]
            ip_before = container_ip(args.stop_container)
            subprocess.run(["docker", action, args.stop_container], capture_output=True)
            ended = time.perf_counter() - start_ref[0]
            events.append((began, f"docker {action} {args.stop_container} issued "
                                  f"(finished at {ended:.1f}s; IP before {ip_before}, after "
                                  f"{container_ip(args.stop_container)})"))

    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()
    start_ref[0] = start
    if args.stop_container:
        threading.Thread(target=stop_and_start, daemon=True).start()
    late = 0.0
    for i in range(total):
        planned = start + i / args.rate
        wait = planned - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        else:
            late = max(late, -wait)
        if args.interface == "http":
            pool.submit(send_http, i, planned)
        else:
            send_grpc(i, planned)
    send_time = time.perf_counter() - start

    done.wait(timeout=args.timeout + 10)
    ended_at = datetime.now(timezone.utc)
    pool.shutdown(wait=False)
    channel.close()

    ok = sum(n for o, n in outcomes.items() if o in ("200", "OK"))
    failed = sum(outcomes.values()) - ok
    # Sends happen at 0, 1/rate, 2/rate, ...: total-1 gaps between the first and last send.
    actual_rate = (total - 1) / send_time if total > 1 and send_time else 0
    p50, p95 = percentile(latencies, 50), percentile(latencies, 95)
    answered = sum(replicas.values())
    shares = {r: 100 * replicas[r] / answered for r in sorted(replicas)} if answered else {}

    print()
    print(f"interface      {args.interface}   (label: {args.label}, run id: {run_id})")
    print(f"time (UTC)     {started_at:%Y-%m-%d %H:%M:%S} to {ended_at:%H:%M:%S}")
    print(f"target rate    {args.rate:g} req/s   actual send rate {actual_rate:.2f} req/s "
          f"(latest send {late * 1000:.0f} ms behind plan)")
    print(f"sent           {total}")
    print(f"succeeded      {ok}")
    print(f"failed         {failed}   {dict(o for o in outcomes.items() if o[0] not in ('200', 'OK')) or ''}")
    print(f"error rate     {100 * failed / total:.2f} %")
    if p95 is not None:
        print(f"client p50     {p50 * 1000:.1f} ms")
        print(f"client p95     {p95 * 1000:.1f} ms")
    print(f"replica shares " + ("  ".join(f"{r}: {s:.1f}%" for r, s in shares.items()) or "(none)"))

    RESULTS_CSV.parent.mkdir(exist_ok=True)
    new_file = not RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["label", "interface", "run_id", "start_utc", "end_utc", "target_rps", "actual_rps",
                             "sent", "succeeded", "failed", "error_rate_pct", "client_p50_ms", "client_p95_ms",
                             "share_A_pct", "share_B_pct", "share_C_pct", "failures"])
        writer.writerow([
            args.label, args.interface, run_id, f"{started_at:%Y-%m-%d %H:%M:%S}", f"{ended_at:%H:%M:%S}",
            f"{args.rate:g}", f"{actual_rate:.2f}", total, ok, failed, f"{100 * failed / total:.2f}",
            f"{p50 * 1000:.1f}" if p50 is not None else "", f"{p95 * 1000:.1f}" if p95 is not None else "",
            *(f"{shares.get(r, 0):.1f}" for r in "ABC"),
            ";".join(f"{o}={n}" for o, n in outcomes.items() if o not in ("200", "OK")),
        ])
    print(f"saved to       {RESULTS_CSV.relative_to(ROOT)}")

    if args.stop_container:
        # Second-by-second timeline (by planned send time) for the failure run.
        timeline_csv = RESULTS_CSV.parent / f"timeline-{args.label}-{args.interface}-{run_id}.csv"
        with timeline_csv.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["second", "succeeded", "failed", "A", "B", "C"])
            for second in sorted(timeline):
                c = timeline[second]
                writer.writerow([second, c["ok"], c["failed"], c["A"], c["B"], c["C"]])
        print(f"timeline       {timeline_csv.relative_to(ROOT)}")
        print()
        for at, what in events:
            print(f"at {at:5.1f}s     {what}")
        failed_seconds = [s for s in sorted(timeline) if timeline[s]["failed"]]
        if failed_seconds:
            print(f"failures       {sum(timeline[s]['failed'] for s in failed_seconds)} requests, sent between "
                  f"second {failed_seconds[0]} and second {failed_seconds[-1]}")
        print("second by second (succeeded / failed / A B C):")
        for second in sorted(timeline):
            c = timeline[second]
            print(f"  {second:3d}s  {c['ok']:3d} / {c['failed']:2d}   A {c['A']:2d}  B {c['B']:2d}  C {c['C']:2d}")


if __name__ == "__main__":
    main()
