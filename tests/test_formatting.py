from app.bot import TELEGRAM_LIMIT, fmt_pct, format_snapshot, parse_command, with_analysis
from app.market import build_snapshot


def test_fmt_pct():
    assert fmt_pct(1.234) == "+1.23%"
    assert fmt_pct(-0.5) == "-0.50%"
    assert fmt_pct(None) == "n/a"


def test_snapshot_escapes_html(uptrend):
    snap = build_snapshot("TEST", uptrend, name="Johnson & <Johnson>")
    text = format_snapshot(snap)
    assert "Johnson &amp; &lt;Johnson&gt;" in text


def test_analysis_is_escaped_and_truncated():
    out = with_analysis("<b>header</b>", "<script>" + "x" * 10_000)
    assert "&lt;script&gt;" in out
    assert len(out) <= TELEGRAM_LIMIT
    assert out.startswith("<b>header</b>")


def test_parse_command():
    assert parse_command("/price AAPL") == ("price", ["AAPL"])
    assert parse_command("/Compare@NadavBot AAPL MSFT") == ("compare", ["AAPL", "MSFT"])
    assert parse_command("/market") == ("market", [])


def test_parse_command_strips_rtl_marks():
    # Hebrew keyboards can prepend U+200F, which hides the command from Telegram.
    assert parse_command("‏/price AAPL") == ("price", ["AAPL"])
    assert parse_command("⁧/analyze NVDA⁩") == ("analyze", ["NVDA"])


def test_parse_command_rejects_non_commands():
    assert parse_command("AAPL") is None
    assert parse_command("/") is None
    assert parse_command("   ") is None
