# Kibana queries for the Quest App logs

## How to use these

1. Open Kibana Discover: http://localhost:5601/app/discover
2. Pick the data view **quest-app-logs** (index pattern `quest-app-logs-*`, time field `@timestamp`).
3. Set the time range at the top right (for example **Last 7 days**).
4. Type a query into the KQL search bar and press Enter.

The app writes one JSON object per log line. Filebeat parses it and stores the
fields under `app.*` (for example `app.severity`, `app.message`, `app.request_id`,
`app.event`, `app.endpoint`, `app.status`, `app.duration_ms`).

Kibana shows times in the browser's timezone. The raw logs and Elasticsearch use UTC
(for example 19:58 UTC shows as 00:58 in Pakistan, UTC+5).

## Queries tested and working (22 Sep 2026)

| Query | What it finds | Result when tested |
|---|---|---|
| `app.severity : "WARNING"` | Every warning line (client errors and app warnings) | 2 documents |
| `app.request_id : "08d08ced29b7"` | Every log line written while handling one request | 2 documents ("quest not found" and "request completed") |
| `app.event : "quest_generated"` | Each time a quest was handed out by `POST /quests` (`/quest/new` in Assignment 1) | 16 documents |
| `app.status >= 400` | HTTP requests that failed with a 4xx or 5xx status | 1 document |
| `app.severity : "ERROR"` | Server errors (unhandled exceptions, 5xx responses) | 0 documents (no server errors so far) |

Counts grow as more traffic is generated. They are the results at the time of testing.

`app.status` only exists on the "request completed" lines, so `app.status >= 400`
finds fewer documents than `app.severity : "WARNING"`, which also matches the
"quest not found" line written inside the app.

## How to find one request or one error

- Find every log line of one request: copy its `request_id` (the app also returns it in
  the `X-Request-ID` response header) and search `app.request_id : "<id>"`.
- Find one kind of event: `app.event : "quest_not_found"` (also `quest_generated`,
  `quest_resolved`, `quest_already_resolved`, `request_completed`). The
  `quest_not_found` search was verified through the Elasticsearch search API.

## Planned for Part E1 (not tested yet)

- `app.duration_ms >= 400` to find slow requests during the fault experiment.

## Log retention setting (Elasticsearch, applied once)

Logs are kept for at least 7 days using a data stream lifecycle. This setting is stored
inside Elasticsearch, so re-run it if the Elasticsearch data volume is ever deleted:

```
curl -X PUT "http://localhost:9200/_data_stream/quest-app-logs-*/_lifecycle" -H "Content-Type: application/json" -d "{\"data_retention\":\"7d\"}"
```

Check it with:

```
curl "http://localhost:9200/_data_stream/quest-app-logs-*?pretty"
```

## Assignment 2: one task across the HTTP and gRPC services

The gRPC client (`clients/grpc_client.py`) sends one request ID to both
services: the `X-Request-ID` header on its HTTP call and `x-request-id`
metadata on its gRPC call. Both services log it as `app.request_id`, so one
search shows the whole task, and `app.service` tells the services apart.

| KQL (Kibana Discover, data view `quest-app-logs`) | What it shows |
|---|---|
| `app.request_id : "demo-ok-001"` | Every log line of one task, from both `quest-app` and `quest-grpc` |
| `app.service : "quest-grpc"` | All gRPC service logs |
| `app.event : "rpc_completed" and not app.grpc_code : "OK"` | Failed gRPC calls, with their status code |

Add the columns `app.service`, `app.message`, `app.status` and `app.grpc_code`
to see the sequence at a glance.

## Assignment 3: Envoy's event together with the replica's event

Envoy writes one JSON access-log line per request (`app.service : "envoy"`,
`app.event : "envoy_access"`), with the request ID, the replica it picked
(`app.replica`, from the replica's `X-Replica` answer) and the replica's
address (`app.upstream_host`). The replicas log the same request ID, so one
search on `app.request_id` returns both lines. The fixed-rate test client
gives request number i of a run the ID `<run id>-<i>` (for example `lt5b2523-7`).

| KQL (Kibana Discover, data view `quest-app-logs`) | What it shows |
|---|---|
| `app.request_id : "<http run id>-7"` | HTTP: Envoy's line (`app.listener : http`) and the replica's `request completed` line |
| `app.request_id : "<grpc run id>-7"` | gRPC: Envoy's line (`app.listener : grpc`) and the replica's `rpc completed` line |
| `app.service : "envoy" and app.replica : "A"` | Every request Envoy sent to replica A |
| `app.service : "envoy" and not app.replica : *` | Requests Envoy could not hand to any replica |

Add the columns `app.service`, `app.replica`, `app.listener`, `app.upstream_host`,
`app.message`, `app.status` and `app.grpc_code` to see the pair side by side.
