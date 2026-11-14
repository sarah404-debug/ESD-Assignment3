# Quest App — Load Balancing and Observability

Enterprise Software Development, Assignment 3.

This project extends my Assignment 2 Quest App
(https://github.com/sarah404-debug/ESD-Assignment2), which extended
Assignment 1 (https://github.com/sarah404-debug/quest-app). The first commit
of this repository is a copy of the finished Assignment 2 code; Assignment 3
changes are made on top of it.

Three replicas of the app (A, B, C) run behind **Envoy**, which forwards HTTP
requests and gRPC calls to them. Prometheus/Grafana and Filebeat/Elasticsearch/
Kibana show which replica handled what, and a fixed-rate test client measures
each load-balancing setup.

Architecture: `docs/architecture-a3.png` (source `docs/architecture-a3.svg`).

```
HTTP clients  (localhost:8000)  ─┐                 ┌─► replica A  esd-a3-quest-app-1  HTTP :8000 + gRPC :50051
gRPC clients  (localhost:50051) ─┴─► Envoy ─────────┼─► replica B  esd-a3-quest-app-2
                                   (admin :9901)    └─► replica C  esd-a3-quest-app-3
                                                          all three share SQLite /data/quests.db (volume questdata)
```

## What is where

| Part | Files |
|---|---|
| Replicas (one process: HTTP + gRPC) | `app/main.py` (starts the gRPC server in its lifespan), `grpc_service/server.py`, `docker-compose.yml` (`quest-app`, `deploy.replicas: 3`) |
| Replica identity (A/B/C) | `quest_core/replica.py` (looks up its own number in Docker DNS); `replica` on every log line, `quest_replica_info` metric, `X-Replica` header / `x-replica` gRPC metadata |
| Shared rules and data | `quest_core/rules.py`, `quest_core/store.py` (SQLite on volume `questdata`, idempotency keys in the same database) |
| Envoy | `envoy/envoy.yaml` (normal: equal round robin over A, B, C) and one file per experiment (below) |
| Prometheus | `monitoring/prometheus.yml` (scrapes A, B, C and Envoy directly) |
| Grafana dashboard | `monitoring/grafana/dashboards/quest-app-a3.json` (provisioned automatically) |
| Logs | `logging/filebeat.yml` (replicas + Envoy access log), `logging/kibana-queries.md` |
| Test client | `scripts/fixed_rate_load.py` → results in `results/runs.csv` |

## Setup

1. Open Docker Desktop and wait until it is running.
2. Start everything (from this folder):
   ```
   docker compose up -d --build
   ```
   Elasticsearch and Kibana need a minute or two. After a restart of Docker
   or the laptop, run `docker compose up -d` again so all containers come back.
3. One-time Python environment for the test client and gRPC client:
   ```
   python -m venv venv
   venv\Scripts\python -m pip install -r requirements-dev.txt
   ```
4. One-time Kibana data view: Kibana → Stack Management → Data views →
   Create data view: name `quest-app-logs`, index pattern `quest-app-logs-*`,
   timestamp field `@timestamp`.

Check it works:
```
docker compose ps
curl -i http://localhost:8000/
venv\Scripts\python clients\grpc_client.py --new completed
```
`docker compose ps` lists `esd-a3-quest-app-1`, `-2`, `-3` and `esd-a3-envoy-1`
(only Envoy publishes ports). The `curl` answer has headers `server: envoy`
and `x-replica: A`, `B` or `C`.

| What | Where |
|---|---|
| App through Envoy (HTTP, Swagger at `/docs`, UI at `/ui`) | http://localhost:8000 |
| gRPC through Envoy (`QuestService.ResolveQuest`) | `localhost:50051` |
| Envoy admin (stats, `/clusters` shows each destination's health) | http://localhost:9901 |
| Prometheus targets | http://localhost:9090/targets |
| Grafana dashboard "Quest App A3" | http://localhost:3000/d/quest-app-a3 (view without login; admin / admin to edit) |
| Kibana Discover | http://localhost:5601/app/discover |

`http://localhost:8000/metrics` goes through Envoy, so it shows whichever
replica answered; Prometheus scrapes each replica directly instead.

## Fixed-rate test client

`scripts/fixed_rate_load.py` sends requests through Envoy at a fixed rate for
a fixed time **without waiting for answers** (request *i* is sent at
`start + i / rate`; its time is measured from that planned moment). It never
retries. Test operations: HTTP `GET /quests/{id}` (read-only) and gRPC
`ResolveQuest` asking for `completed` on quests that are already completed (a
safe repeat). It first creates and completes 20 quests to use.

```
venv\Scripts\python scripts\fixed_rate_load.py http --rate 20 --duration 60 --label NAME
venv\Scripts\python scripts\fixed_rate_load.py grpc --rate 20 --duration 60 --label NAME
```
It prints target and actual rate, sent, succeeded, failed, error rate,
client p50/p95 and each replica's share (from `X-Replica` / `x-replica`),
and adds a line to `results/runs.csv`. Request IDs are `<run id>-<i>`, so one
request can be found in Kibana.

All experiments below use `--rate 20 --duration 60` (failure test: 90 s).

## Experiments

Envoy loads `envoy/envoy.yaml` unless `ENVOY_CONFIG` names another file in
`envoy/`. Each experiment file is a copy of `envoy.yaml` with only that
experiment's change (written at the top of the file). Commands are for the
Windows Command Prompt (`set`); in PowerShell use `$env:ENVOY_CONFIG="..."`.

**Recovery (back to normal: equal round robin, no delay)** — run after every experiment:
```
set ENVOY_CONFIG=
set SLOW_REPLICA=
set SLOW_DELAY_MS=
docker compose up -d quest-app envoy
```
Check: Envoy admin http://localhost:9901/clusters lists three hosts per
cluster, or the dashboard's "healthy destinations" panel shows 3.

### Part B baseline: replica A only
```
set ENVOY_CONFIG=envoy-a-only.yaml
docker compose up -d envoy
venv\Scripts\python scripts\fixed_rate_load.py http --label baseline-A-only
venv\Scripts\python scripts\fixed_rate_load.py grpc --label baseline-A-only
```
Expected: A 100 %, B and C idle.

### Part C: round robin over A, B, C (the normal setup)
```
set ENVOY_CONFIG=
docker compose up -d envoy
venv\Scripts\python scripts\fixed_rate_load.py http --label roundrobin
venv\Scripts\python scripts\fixed_rate_load.py grpc --label roundrobin
```
Expected: about 33 % each. The gRPC listener is an HTTP/2 connection manager,
so Envoy routes every call on its own although the client uses one connection.

### Experiment 1: weighted round robin A:B:C = 2:1:1
```
set ENVOY_CONFIG=envoy-weighted.yaml
docker compose up -d envoy
venv\Scripts\python scripts\fixed_rate_load.py http --label weighted-2-1-1
venv\Scripts\python scripts\fixed_rate_load.py grpc --label weighted-2-1-1
```
Expected: 50 / 25 / 25 %.

### Experiment 2: slow replica B, round robin vs least request
`SLOW_REPLICA=B` with `SLOW_DELAY_MS=1000` makes only B wait 1 s before every
HTTP request and gRPC call.
```
set SLOW_REPLICA=B
set SLOW_DELAY_MS=1000
docker compose up -d quest-app
docker compose restart envoy
venv\Scripts\python scripts\fixed_rate_load.py http --label slowB-roundrobin
venv\Scripts\python scripts\fixed_rate_load.py grpc --label slowB-roundrobin
set ENVOY_CONFIG=envoy-least-request.yaml
docker compose up -d envoy
venv\Scripts\python scripts\fixed_rate_load.py http --label slowB-leastrequest
venv\Scripts\python scripts\fixed_rate_load.py grpc --label slowB-leastrequest
```
Then run the recovery commands. Expected: round robin keeps B at 33 %; least
request moves most traffic to A and C (about 11 % to B).

### Experiment 3: failure and recovery with active health checks
`envoy-healthcheck.yaml` adds health checks to both clusters: interval 2 s,
timeout 1 s, unhealthy after 2 failures, healthy after 2 successes (HTTP:
`GET /` must return 200; gRPC: `grpc.health.v1.Health/Check` for
`quest.v1.QuestService` must return `SERVING`). The test client stops replica
B's container at 20 s and starts it again at 50 s, and prints a
second-by-second table (also saved as `results/timeline-*.csv`).
```
set ENVOY_CONFIG=envoy-healthcheck.yaml
docker compose up -d envoy
venv\Scripts\python scripts\fixed_rate_load.py http --duration 90 --label failure-recovery --stop-container esd-a3-quest-app-2 --stop-at 20 --start-at 50
venv\Scripts\python scripts\fixed_rate_load.py grpc --duration 90 --label failure-recovery --stop-container esd-a3-quest-app-2 --stop-at 20 --start-at 50
```
Wait until Envoy shows B healthy again between the two runs (dashboard or
http://localhost:9901/clusters). If B is still stopped after an interrupted
run: `docker start esd-a3-quest-app-2`. Then run the recovery commands.

## Observability

- **Prometheus** scrapes `esd-a3-quest-app-1/-2/-3:8000` (labels `replica`
  A/B/C) and `envoy:9901/stats/prometheus` directly, every 5 s.
- **Grafana** dashboard `quest-app-a3`: for HTTP `GET /quests/{id}` (left) and
  gRPC `ResolveQuest` (right), per replica: requests per second, share of
  traffic (red line at 33.3 %), active requests, p50/p95 response time and
  errors by status, all over a **1 minute** window; plus Envoy's requests per
  destination group, healthy destinations, and failed health checks /
  connections.
- **Logs:** each replica logs JSON with `service`, `replica` and `request_id`;
  Envoy writes one JSON access-log line per request with `request_id`,
  `listener`, `upstream_host` and `replica`. One Kibana search finds both:
  ```
  app.request_id : "<run id>-7"
  ```
  Requests per replica for one run, from Envoy's access log (Discover → ES|QL):
  ```
  FROM quest-app-logs-* | WHERE app.service == "envoy" AND app.request_id LIKE "<run id>-*" AND NOT app.request_id LIKE "<run id>-setup-*" | STATS requests = COUNT(*) BY app.replica, app.upstream_host, app.listener | SORT app.replica
  ```
  More searches: `logging/kibana-queries.md`.

## Shared state across replicas

Quest data and idempotency records are in the shared SQLite database, so any
replica can read or resolve any quest, and duplicate prevention holds across
replicas (create runs in a `BEGIN IMMEDIATE` transaction; resolve is one
conditional `UPDATE`). Rate-limit counters for `POST /quests` stay in each
replica's memory, so with three replicas the limit counts per replica; see the
report.

## The API (from Assignments 1 and 2)

Through Envoy on port 8000: `POST /quests` (201, optional `Idempotency-Key`),
`GET /quests`, `GET /quests/{id}`, `PATCH /quests/{id}` with
`{"status": "completed" | "skipped" | "abandoned"}`; errors use one JSON
format (`error`, `message`, `request_id`). OpenAPI: `openapi/openapi.json`
(regenerate with `venv\Scripts\python scripts\export_openapi.py`), docs at
http://localhost:8000/docs. gRPC contract: `questpb/quest.proto`
(regenerate the code with `venv\Scripts\python -m grpc_tools.protoc -I.
--python_out=. --pyi_out=. --grpc_python_out=. questpb/quest.proto`).
Other A2 tools still work through Envoy: `clients/grpc_client.py`,
`clients/quest_caller.py`, `scripts/compare_http_grpc.py`,
`scripts/rate_limit_demo.py`.

## Tests

```
venv\Scripts\python -m unittest discover -s tests -v
```
Unit tests for the shared rules and the gRPC service (throwaway database, no
Docker needed).

## Safely clean up

- **Pause everything** (keeps all data): `docker compose stop`
- **Resume:** `docker compose up -d`
- `docker compose down` removes the containers; quests and logs survive in the
  volumes `questdata` and `esdata`, but Prometheus has no volume, so metric
  history is lost. `docker compose down -v` also deletes those volumes.

## Notes

- Images for Prometheus, Grafana and Node Exporter use `:latest`; Envoy is
  pinned to `v1.31.2`, Elasticsearch/Kibana/Filebeat to `8.15.0`.
- Reused work: the app, gRPC service, logging and metrics come from my
  Assignments 1 and 2 (links above).
- AI assistance was used in this project; see the accompanying report for a
  statement of what was used and how, per the course's AI use policy.
