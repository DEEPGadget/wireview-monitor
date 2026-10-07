"""`wvctl`: command-line access to a running wvd.

Exit codes: 0 success / assertion passed, 1 assertion failed, 2 error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime

from .client import WireView, WvdError

EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def duration(text: str) -> float:
    m = _DUR.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (e.g. 30s, 5m, 1h)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def _fmt(v, nd=2) -> str:
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def render_sample(s: dict) -> str:
    t = s["temps_c"]
    d = s["derived"]
    lines = [
        f"{_ts(s['ts'])}  seq {s['seq']}",
        f"  total  {s['total_w']:8.2f} W  {s['total_a']:7.3f} A  avg {s['avg_v']:.3f} V   PSU cap {s['psu_cap_w']} W",
        "  temp   " + "  ".join(f"{k} {_fmt(v, 1)}" for k, v in t.items()) + " °C"
        + f"   fan {s['fan_pct']}%",
        f"  pins   max {d['max_pin_a']:.3f} A   imbalance {_fmt(d['pin_imbalance'])}",
    ]
    for i, p in enumerate(s["pins"], 1):
        lines.append(f"    pin{i}  {p['v']:6.3f} V  {p['a']:6.3f} A  {p['w']:7.3f} W")
    lines.append("  faults " + (", ".join(s["faults"]) if s["faults"] else "none")
                 + (f"   (log: {s['fault_log']:#06x})" if s["fault_log"] else ""))
    return "\n".join(lines)


def render_stats(st: dict) -> str:
    if not st["count"]:
        return "no samples in window"
    out = [f"{_ts(st['from'])} → {_ts(st['to'])}  {st['duration_s']:.1f} s, {st['count']} samples "
           f"({st['rate_hz']} Hz), {st['gaps']} gap(s)",
           f"energy {st['energy_wh']:.4f} Wh   faults seen: {', '.join(st['faults_seen']) or 'none'}",
           f"{'field':<15}{'min':>10}{'avg':>10}{'p95':>10}{'max':>10}{'last':>10}"]
    for name, f in st["fields"].items():
        if f is None:
            continue
        out.append(f"{name:<15}" + "".join(f"{_fmt(f[k], 3):>10}" for k in ("min", "avg", "p95", "max", "last")))
    return "\n".join(out)


# -- commands -----------------------------------------------------------------
def cmd_health(wv, a):
    h = wv.health()
    _dump(h)
    return EXIT_OK if h["status"] == "ok" else EXIT_FAIL


def cmd_info(wv, a):
    i = wv.info()
    if a.json:
        _dump(i)
    else:
        for k, v in i.items():
            print(f"{k:<11}{v}")
    return EXIT_OK


def cmd_now(wv, a):
    s = wv.latest()
    print(json.dumps(s) if a.json else render_sample(s))
    return EXIT_OK


def cmd_watch(wv, a):
    clear = sys.stdout.isatty() and not a.json
    for kind, data in wv.stream(hz=a.hz):
        if kind == "sample":
            if a.json:
                print(json.dumps(data), flush=True)
            else:
                print(("\033[H\033[J" if clear else "") + render_sample(data), flush=True)
        elif kind == "event" and not clear:
            print(f"# event {json.dumps(data)}", file=sys.stderr, flush=True)
    return EXIT_OK


def cmd_log(wv, a):
    t0 = time.time()
    if not a.quiet:
        print(f"recording {a.duration:.0f} s …", file=sys.stderr)
    time.sleep(a.duration)
    data = wv.export(from_=t0, to=time.time(), fmt=a.format)
    if a.output in (None, "-"):
        sys.stdout.buffer.write(data)
    else:
        with open(a.output, "wb") as f:
            f.write(data)
        if not a.quiet:
            print(f"wrote {a.output} ({len(data)} bytes)", file=sys.stderr)
    return EXIT_OK


def cmd_stats(wv, a):
    st = wv.stats(last=a.last, session=a.session)
    if a.json:
        _dump(st)
    else:
        print(render_stats(st))
    return EXIT_OK


def cmd_events(wv, a):
    ev = wv.events(last=a.last, type=a.type)
    if a.json:
        _dump(ev)
    else:
        for e in ev:
            rest = {k: v for k, v in e.items() if k not in ("id", "ts", "type")}
            print(f"{_ts(e['ts'])}  {e['type']:<20} {json.dumps(rest, ensure_ascii=False)}")
    return EXIT_OK


def cmd_session(wv, a):
    if a.action == "start":
        meta = json.loads(a.meta) if a.meta else {}
        s = wv.session(a.label, meta).start()
        print(json.dumps({"id": s.id, "label": s.label}) if a.json else s.id)
        return EXIT_OK
    if a.action == "list":
        ss = wv.sessions()
        if a.json:
            _dump(ss)
        else:
            for s in ss:
                state = "active" if s["active"] else f"{s['end_ts'] - s['start_ts']:.1f} s"
                print(f"{s['id']:>5}  {_ts(s['start_ts'])}  {state:>10}  {s['label']}")
        return EXIT_OK
    sid = a.id
    if sid is None:
        active = [s for s in wv.sessions() if s["active"]]
        if not active:
            raise WvdError("no active session")
        sid = active[0]["id"]
    if a.action == "stop":
        r = wv._req("POST", f"/api/v1/sessions/{sid}/stop")
    elif a.action == "export":
        sys.stdout.buffer.write(wv._raw("GET", f"/api/v1/sessions/{sid}/export?format={a.format}"))
        return EXIT_OK
    else:  # show
        r = wv._req("GET", f"/api/v1/sessions/{sid}")
    if a.json:
        _dump(r)
    else:
        print(f"session {r['id']} '{r['label']}'")
        print(render_stats(r["stats"]))
    return EXIT_OK


# check name -> (stats field, stat, comparison)
CHECKS = {
    "max_total_w": ("total_w", "max", "<="),
    "max_pin_a": ("max_pin_a", "max", "<="),
    "max_temp": ("max_temp_c", "max", "<="),
    "max_imbalance": ("pin_imbalance", "max", "<="),
    "min_avg_v": ("avg_v", "min", ">="),
    "max_avg_v": ("avg_v", "max", "<="),
}


def evaluate(st: dict, checks: dict, no_faults: bool, min_samples: int, max_gaps: int | None) -> list[dict]:
    results = []
    results.append({"check": "min_samples", "limit": min_samples, "value": st["count"],
                    "pass": st["count"] >= min_samples})
    for name, limit in checks.items():
        if limit is None:
            continue
        field, stat, op = CHECKS[name]
        f = st["fields"].get(field) if st["count"] else None
        value = f[stat] if f else None
        ok = value is None or (value <= limit if op == "<=" else value >= limit)
        results.append({"check": name, "limit": limit, "value": value, "pass": ok,
                        **({"note": "no data"} if value is None else {})})
    if no_faults:
        results.append({"check": "no_faults", "limit": [], "value": st["faults_seen"],
                        "pass": not st["faults_seen"]})
    if max_gaps is not None:
        results.append({"check": "max_gaps", "limit": max_gaps, "value": st["gaps"], "pass": st["gaps"] <= max_gaps})
    return results


def cmd_assert(wv, a):
    checks = {k: getattr(a, k) for k in CHECKS}
    if a.daemon_limits:
        lim = wv.limits()
        defaults = {"max_total_w": lim["total_w"], "max_pin_a": lim["pin_a"], "max_temp": lim["temp_c"],
                    "max_imbalance": lim["imbalance"]}
        checks = {k: (v if v is not None else defaults.get(k)) for k, v in checks.items()}
    if a.session is not None:
        st = wv.stats(session=a.session)
    elif a.duration:
        t0 = time.time()
        if not a.json:
            print(f"measuring {a.duration:.0f} s …", file=sys.stderr)
        time.sleep(a.duration)
        st = wv.stats(from_=t0, to=time.time())
    else:
        st = wv.stats(last=a.last or "60s")
    results = evaluate(st, checks, a.no_faults, a.min_samples, a.max_gaps)
    passed = all(r["pass"] for r in results)
    if a.json:
        _dump({"pass": passed, "results": results, "stats": st})
    else:
        print(f"window {_ts(st['from'])} → {_ts(st['to'])}, {st['count']} samples")
        for r in results:
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"  [{mark}] {r['check']:<14} value={r['value']!s:<24} limit={r['limit']}"
                  + (f"  ({r['note']})" if r.get("note") else ""))
        print("PASSED" if passed else "FAILED")
    return EXIT_OK if passed else EXIT_FAIL


def cmd_clear_faults(wv, a):
    _dump(wv.clear_faults(a.fault))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wvctl", description="Query a running wvd (WireView measurement daemon)")
    p.add_argument("--url", help="daemon URL (env WVD_URL, default http://127.0.0.1:8765)")
    p.add_argument("--token", help="Bearer token (env WVD_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, json_flag=True):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if json_flag:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    add("health", cmd_health, "daemon and device status (exit 1 if degraded)", json_flag=False)
    add("info", cmd_info, "device identity")
    add("now", cmd_now, "latest sample")
    sp = add("watch", cmd_watch, "live samples (JSON lines with --json)")
    sp.add_argument("--hz", type=float, default=2.0, help="max update rate (default 2)")
    sp = add("log", cmd_log, "record for a duration, then write CSV/JSONL", json_flag=False)
    sp.add_argument("--duration", type=duration, required=True)
    sp.add_argument("-o", "--output", help="file (default stdout)")
    sp.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp = add("stats", cmd_stats, "window statistics")
    sp.add_argument("--last", default="60s")
    sp.add_argument("--session", type=int)
    sp = add("events", cmd_events, "fault / limit / connection events")
    sp.add_argument("--last", default="24h")
    sp.add_argument("--type", help="type prefix, e.g. fault or limit")

    sp = add("session", cmd_session, "test sessions: start | stop | show | list | export")
    sp.add_argument("action", choices=("start", "stop", "show", "list", "export"))
    sp.add_argument("label_or_id", nargs="?", help="label for start, id for the others (default: active)")
    sp.add_argument("--meta", help="JSON metadata for start")
    sp.add_argument("--format", choices=("csv", "jsonl"), default="csv")

    sp = add("assert", cmd_assert, "check a window against limits (exit 0 pass / 1 fail)")
    w = sp.add_mutually_exclusive_group()
    w.add_argument("--duration", type=duration, help="measure from now for this long")
    w.add_argument("--last", help="evaluate the past window (default 60s)")
    w.add_argument("--session", type=int, help="evaluate a session")
    sp.add_argument("--max-total-w", type=float)
    sp.add_argument("--max-pin-a", type=float)
    sp.add_argument("--max-temp", type=float)
    sp.add_argument("--max-imbalance", type=float)
    sp.add_argument("--min-avg-v", type=float)
    sp.add_argument("--max-avg-v", type=float)
    sp.add_argument("--no-faults", action="store_true", help="fail if any fault was active")
    sp.add_argument("--min-samples", type=int, default=1)
    sp.add_argument("--max-gaps", type=int)
    sp.add_argument("--daemon-limits", action="store_true", help="fill unset checks from the daemon's limits")

    sp = add("clear-faults", cmd_clear_faults, "clear faults (daemon needs --allow-write)", json_flag=False)
    sp.add_argument("--fault", help="only this fault, e.g. OCP")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "session":
        if a.action == "start":
            if not a.label_or_id:
                print("wvctl: session start needs a label", file=sys.stderr)
                return EXIT_ERROR
            a.label, a.id = a.label_or_id, None
        else:
            a.id = int(a.label_or_id) if a.label_or_id else None
    wv = WireView(a.url, a.token)
    try:
        return a.fn(wv, a)
    except WvdError as e:
        print(f"wvctl: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK
    except BrokenPipeError:
        return EXIT_OK


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
