"""CLI and client against a simulated daemon via the `wireview` fixture
(or real hardware when WVD_URL is set)."""
import json
import time

from wvd.cli import main


def test_client_session(wireview):
    with wireview.session("pytest-client") as s:
        time.sleep(0.5)
    st = s.stats()
    assert st["count"] > 0 and st["fields"]["total_w"]["max"] > 0


def test_cli_now_json(wireview, capsys):
    assert main(["--url", wireview.url, "now", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["pins"]) == 6


def test_cli_assert_pass_and_fail(wireview, capsys):
    assert main(["--url", wireview.url, "assert", "--last", "5s", "--max-total-w", "100000"]) == 0
    assert main(["--url", wireview.url, "assert", "--last", "5s", "--max-total-w", "0.001", "--json"]) == 1
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert report["pass"] is False


def test_cli_unreachable():
    assert main(["--url", "http://127.0.0.1:9", "now"]) == 2
