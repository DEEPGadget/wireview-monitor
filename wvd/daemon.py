"""`wvd` entry point: parse options and serve."""
from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys

from .events import Limits
from .store import default_db_path
from .transport import SCENARIOS


def _limit(text: str) -> float | None:
    return None if text.lower() in ("none", "off", "") else float(text)


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="wvd", description="WireView Pro II measurement daemon (REST/WS API + dashboard)")
    p.add_argument("--host", default=env("WVD_HOST", "0.0.0.0"), help="bind address (default 0.0.0.0, all interfaces)")
    p.add_argument("--port", type=int, default=int(env("WVD_PORT", "8765")), help="HTTP port (default 8765)")
    p.add_argument("--device", default=env("WVD_DEVICE"), help="serial port (default: auto-detect 0483:5740)")
    p.add_argument("--simulate", choices=SCENARIOS, default=env("WVD_SIMULATE"),
                   help="use a simulated device instead of hardware")
    p.add_argument("--rate", type=float, default=float(env("WVD_RATE", "10")), help="samples per second, 1-50 (default 10)")
    p.add_argument("--db", default=env("WVD_DB", default_db_path()), help="SQLite path (':memory:' for none)")
    p.add_argument("--retention", default=env("WVD_RETENTION", "72h"), help="keep raw samples this long (default 72h)")
    p.add_argument("--token", default=env("WVD_TOKEN"), help="require this Bearer token (env WVD_TOKEN)")
    p.add_argument("--allow-write", action="store_true", default=env("WVD_ALLOW_WRITE") == "1",
                   help="enable device write commands (clear-faults)")
    p.add_argument("--limit-pin-a", type=_limit, default=9.5, help="per-pin current warning, A (default 9.5)")
    p.add_argument("--limit-total-w", type=_limit, default=600.0, help="total power warning, W (default 600)")
    p.add_argument("--limit-temp-c", type=_limit, default=80.0, help="temperature warning, C (default 80)")
    p.add_argument("--limit-imbalance", type=_limit, default=1.5, help="pin imbalance warning (default 1.5)")
    p.add_argument("--log-level", default=env("WVD_LOG_LEVEL", "info"))
    return p


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not 1 <= args.rate <= 50:
        sys.exit("wvd: --rate must be between 1 and 50")
    open_bind = not _is_loopback(args.host) and not args.token
    if open_bind and args.allow_write:
        sys.exit(f"wvd: --allow-write on {args.host} needs --token: anyone on the network could send device commands")

    import uvicorn

    from .api import Config, create_app, parse_duration

    cfg = Config(host=args.host, port=args.port, token=args.token, allow_write=args.allow_write,
                 rate_hz=args.rate, retention_s=parse_duration(args.retention), db_path=args.db,
                 device_port=args.device, simulate=args.simulate,
                 limits=Limits(args.limit_pin_a, args.limit_total_w, args.limit_temp_c, args.limit_imbalance))
    if open_bind:
        logging.getLogger("wvd").warning("no --token: anyone who can reach %s:%d can read the data", args.host, args.port)
    logging.getLogger("wvd").info("serving on http://%s:%d (db %s, %.0f Hz%s)", cfg.host, cfg.port, cfg.db_path,
                                  cfg.rate_hz, f", simulate={cfg.simulate}" if cfg.simulate else "")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=args.log_level.lower(),
                access_log=False, ws_ping_interval=20)


if __name__ == "__main__":
    main()
