from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import sync_wildfire_hotspots as cli


def configure(monkeypatch, result):
    event = SimpleNamespace(slug="sumatra-2026")
    session = MagicMock()
    session.__enter__.return_value.execute.return_value.scalar_one.return_value = event
    factory = MagicMock(return_value=session)
    sync = MagicMock(return_value=result)
    monkeypatch.setattr(cli, "SessionLocal", factory)
    monkeypatch.setattr(cli, "sync_event_hotspots", sync)
    monkeypatch.setattr("sys.argv", [
        "sync_wildfire_hotspots", "--slug", event.slug,
        "--from", "2026-08-01", "--to", "2026-10-07",
    ])
    return factory, sync


def test_sync_cli_reports_success(monkeypatch, capsys):
    _, sync = configure(monkeypatch, {"stored": 123, "errors": []})
    cli.main()
    sync.assert_called_once()
    assert '"stored": 123' in capsys.readouterr().out


def test_sync_cli_fails_when_a_source_was_incomplete(monkeypatch, capsys):
    configure(monkeypatch, {"stored": 10, "errors": ["upstream unavailable"]})
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "incomplete" in capsys.readouterr().err


def test_sync_cli_rejects_reversed_dates_before_accessing_database(monkeypatch):
    factory, sync = configure(monkeypatch, {"stored": 123, "errors": []})
    monkeypatch.setattr("sys.argv", [
        "sync_wildfire_hotspots", "--slug", "sumatra-2026",
        "--from", "2026-10-07", "--to", "2026-08-01",
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    factory.assert_not_called()
    sync.assert_not_called()
