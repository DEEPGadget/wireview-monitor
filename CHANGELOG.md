# Changelog

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

Known limit (E1): sampler and API still share one GIL. Under continuous heavy queries the largest gap is 0.2–0.4 s. 1.0 showed 10–26 s under the same loads. The 100 ms target needs the sampler in its own process.

## 1.0.0 (tag v1.0.0)

Initial release (commits 253f4b7, 7700c09).
