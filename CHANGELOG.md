# Changelog

## 1.2.0 (branch v1.2)

wvd now runs as three processes under a small supervisor (`wvd` itself). Each has its own GIL, so heavy queries can no longer delay sampling or the live streams (E1).

- **recorder**: device reads, DB writes, the event engine. It publishes a live feed (NDJSON over a Unix socket) with a bounded queue per subscriber, and answers a control socket (clear faults, relay session changes).
- **front**: the HTTP port.
  - Serves the dashboard, WS/SSE, latest, health and metrics from the feed in memory.
  - Checks auth for everything.
  - Passes every other `/api/*` request (and `/docs`) to the api process over its Unix socket.
- **api**: history, stats, export, sessions. Its ring is filled from the feed, so queries include samples not yet committed to the DB.
- **Supervisor**: restarts a dead child after 1 s, and exits with 70 after more than 5 restarts of one child within 60 s. Children die with it (`PR_SET_PDEATHSIG`).
- **Process restarts**:
  - Seqs and event ids never repeat across a recorder restart: the writer keeps a high-water mark in the `meta` table.
  - The restart pause shows as `gap_s` and as a `sampler.gap` event.
- **Health**:
  - New fields `processes`, `api_ok`, `recorder_status_age_s`, `feed_connected`, `feed_dropped`.
  - A closed feed reports `down` at once.
  - `/metrics` adds `wvd_process_restarts_total`.
- **Stress results** (simulator, 50 Hz, 3 h DB, 60 s):
  - Largest gap between stored samples: 20.1 ms, the sample period itself, with 100 % of samples under every workload. 1.1 measured 0.26–0.38 s and 1.0 measured 10.9–26.5 s.
  - SSE delivery delay: at most 14 ms.
- Packaging: `RuntimeDirectory=wvd` in the systemd unit. `down.sh` stops the supervisor when it finds the port held by the front child. New dependency: httpx.

## 1.1.0 (branch v1.1)

Fixes from `reviews/wireview-monitor-issues.html` (seriesA usage, 2026-10-07).

- **C1**: the sampler survived only `OSError`. A USB re-enumeration raised `termios.error`, which killed the thread while HTTP kept serving the last value. Every exception now drops the connection and reconnects. A loop-level guard catches anything else. If the thread still ends, wvd exits with code 70 so systemd restarts it.
- **C2, E2**: the sampler no longer takes the store lock.
  - A writer thread owns all inserts and pruning, and prunes in 5k-row transactions.
  - Readers use their own SQLite connections (WAL) and merge the uncommitted tail from the ring.
  - If the DB stalls, sampling continues and drops only the DB copy after 5 minutes of backlog (`db_dropped`).
- **C3**:
  - Finished sessions get their stats computed once and stored (`sessions.stats`).
  - The session list carries the stored stats, and the dashboard no longer requests 15 session details on every session event.
- **C4**: every sample has `gap_s`, the time since the previous sample.
  - Pauses longer than 2 periods are counted in health (`gaps_total`, `max_gap_s_5m`, `last_gap`).
  - Pauses of 0.5 s or more are also logged as `sampler.gap` events.
- **E3**:
  - Exports stream in chunks with no row cap.
  - History over 500k samples says `"truncated": true`.
  - Stats are computed in one pass. p95 uses strided values beyond 200k samples (`p95_stride`).
- **E4**:
  - Each subscriber's queue holds about 5 s instead of 40 s. On overflow the backlog is dropped and a `lag` message is sent.
  - SSE checks for disconnects once a second instead of per message.
- **E5**: health reports `sampler_alive`, `writer_alive`, `fatal`, `last_sample_wall`, gaps, `db_queue`, `db_dropped` and `stream_dropped`. A dead thread gives `status: down`. `/sensors/latest` adds `age_s`.
- **E6**: no auth, by decision (internal test use). Heavy queries run at most 2 at a time; the rest wait 30 s, then get 503.
- History responses are JSON-encoded in 500-sample slices, which avoids a ~160 ms GIL hold per 20k samples and FastAPI's ~1 s `jsonable_encoder` pass.
- Graceful shutdown is capped at 3 s, so open streams no longer hold a stop.
- `tests/wvd_stress.py` load test; test fault injection via `WVD_TEST_FAULT`.

Known limit (E1), fixed in 1.2: sampler and API still shared one GIL. Under continuous heavy queries the largest gap was 0.2–0.4 s (1.0: 10–26 s).

## 1.0.0 (tag v1.0.0)

Initial release (commits 253f4b7, 7700c09).
