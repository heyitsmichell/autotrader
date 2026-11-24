# Server-Sent Events: `GET /api/stream`

Two modes share one URL.

## 1. Multiplexed hub stream (no `symbol` / `symbols` param)

```
GET /api/stream
Accept: text/event-stream
```

No auth header is needed (GET). Each message is:

```
event: <type>\n
data: <json>\n
\n
```

### Connection lifecycle

1. **Prime.** The server first sends the hub's latest value for every type it cannot recompute
   (`events`, `kill`), then a **fresh** `status`, `portfolio` (only if the broker is connected)
   and `proposals` snapshot. A client therefore has full state after the first few messages.
2. **Live.** Events published on `app.eventhub.hub` are forwarded as they happen.
3. **Server-side diff, per connection.** A payload identical to the previous payload of the same
   type on this connection is dropped. For `status`, the volatile keys `server_time`, `now_et` and
   `data_age_seconds` are ignored when comparing (they change every second). `fill` and
   `heartbeat` are never diffed.
4. **Heartbeat.** If nothing was sent for `STREAM_HEARTBEAT_SECONDS` (default 15), the server sends
   `event: heartbeat`. Use it as a liveness signal: no message for about 2x that interval means
   reconnect (`EventSource` does this automatically).
5. **Disconnect.** The subscription is removed from the hub as soon as the client goes away.
   A slow client whose queue (500 events) fills drops its oldest events; the next snapshot re-syncs.

### Publisher task

The server runs one background task (started in the app lifespan) that, every 2 seconds, builds
`status`, `portfolio` and `proposals` and publishes each **only when it changed** (same volatile-key
rule). It skips the tick while nobody is subscribed, so a closed dashboard costs no broker calls.
The DB additionally publishes `proposals` and `events` on every write, and the engine publishes `kill` and `fill` the moment they occur. Re-publishing a snapshot type (`status`, `portfolio`, `proposals`, `events`, `kill`) with a payload identical to the latest one is a no-op in the hub.

### Event types

| type | payload | source |
|---|---|---|
| `status` | Same object as `GET /api/status` (all keys, including `data_age_seconds`, `broker_mode_warning`, `paused`, `daily_loss_guard`, `token_required`). | publisher (2 s, on change) |
| `portfolio` | Same object as `GET /api/portfolio`: `{balance, positions, orders, ..., equity: [...], protective: [...]}`. Not sent while the broker is disconnected (REST returns 409). | publisher |
| `proposals` | Same list as `GET /api/proposals` (default `limit=50`, newest first, JSON columns decoded). Published by the DB on every proposal write and by the publisher on change; the hub drops a publish identical to the latest one. | DB + publisher |
| `events` | The latest 50 activity-log rows, newest first: the same list as `GET /api/events?limit=50` (`[{id, ts, level, message, broker_mode, webull_environment, account_id}, ...]`). Published by the DB after every `db.log` (coalesced: at most one publish per event-loop turn), replayed on connect. Replace the client's list with it. | DB (`app/db/notify.py`) |
| `quote` | `{"symbol": "AAPL", "price": 189.12, "bid": 189.1, "ask": 189.14, "ts": 1760000000.0, "as_of": 1760000000.0}`. `bid`/`ask` may be `null`. `ts`/`as_of` are epoch seconds. Throttled to at most 4 per second per symbol. | feed (`app/feed/bus.py`) |
| `fill` | `{"proposal_id": 12, "symbol": "AAPL", "side": "BUY", "quantity": 2, "price": 189.1, "client_order_id": "...", "at": "2026-01-02T15:04:05+00:00"}`. Sent when order reconcile sees a fill for a proposal. `quantity`/`price` fall back to the proposal's quantity/limit when the broker did not report the filled values. Never diffed, not replayed. Treat it as a cue to refetch portfolio and trades. | engine (`app/engine/reconcile.py`) |
| `kill` | `{"on": true, "reason": "daily loss limit", "at": "2026-01-02T15:04:05+00:00"}`. `reason` is `null` when `on` is false; `at` is the time of the change (on or off). Sent on every change and replayed on connect. | engine |
| `heartbeat` | `{"ts": 1760000000.123}` (server epoch seconds). | stream |

Types are listed in `app.eventhub.EVENT_TYPES`; `web/src/lib/stream.ts` must be kept in sync.

### Client sketch

```ts
const es = new EventSource("/api/stream");
for (const t of ["status", "portfolio", "proposals", "events", "quote", "fill", "kill", "heartbeat"])
  es.addEventListener(t, (e) => handle(t, JSON.parse((e as MessageEvent).data)));
```

## 2. Chart tick stream (`symbol=AAPL` or `symbols=AAPL,MSFT`)

Unchanged from before (MarketChart compatibility). Symbols must be on the watchlist (404 otherwise);
an empty value is a 400.

```
event: hello
data: {"mode": "stream|poll", "last": {"AAPL": <last tick or null>}}

data: {"symbol": "AAPL", "t": 1760000000.0, "p": 189.12, "v": 100}     (one per price update, no event name)

: keep-alive                                                           (comment line every 15 s of silence)
```

## Auth note

`GET` requests are unauthenticated (loopback Host only). Every non-GET `/api/*` request must send
`X-Trader: 1` and `X-Trader-Token: <token>`; the token is in `<meta name="trader-token">` in the
served HTML, in `data/api_token` (or `API_TOKEN_FILE`), or from `GET /api/auth/token` with
`X-Trader: 1`.
