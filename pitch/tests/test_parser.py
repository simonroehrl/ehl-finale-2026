from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import parse_line


def test_view_line():
    events = parse_line('[erp]   107s view {"site": "erp", "url": "https://example/mmbe"}')
    assert events[0]["type"] == "view"
    assert events[0]["site"] == "erp"
    assert events[0]["t"] == 107


def test_record_line():
    events = parse_line('[mail]   140s record {"kind": "messages", "records": [{"id": "1"}, {"id": "2"}]}')
    assert events[0]["type"] == "record"
    assert events[0]["kind"] == "messages"
    assert events[0]["records"] == 2


def test_round_and_bid():
    events = parse_line("After round 0 at 185s: 5 positions priced, 0 gaps, 0 implausible prices")
    assert events[0]["type"] == "phase"
    events = parse_line("Bid at 185s (184.8s research, $0.150):")
    assert events[0]["type"] == "bid"
