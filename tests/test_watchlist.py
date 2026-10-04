import pytest

from app.watchlist import Watchlist


def test_add_list_remove(tmp_path):
    wl = Watchlist(str(tmp_path / "data" / "watchlist.db"))
    assert wl.add("aapl") == "AAPL"
    wl.add("$teva.ta")
    wl.add("AAPL")  # duplicates are ignored
    assert wl.tickers() == ["AAPL", "TEVA.TA"]
    assert wl.remove("aapl") is True
    assert wl.remove("AAPL") is False
    assert wl.tickers() == ["TEVA.TA"]


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "watchlist.db")
    Watchlist(path).add("NVDA")
    assert Watchlist(path).tickers() == ["NVDA"]


def test_rejects_invalid_ticker(tmp_path):
    with pytest.raises(ValueError):
        Watchlist(str(tmp_path / "w.db")).add("DROP;TABLE")
