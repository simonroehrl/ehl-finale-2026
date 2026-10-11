import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import parse_line

events = parse_line('[erp]   107s view {"site": "erp", "url": "https://example/mmbe"}')
assert events[0]["type"] == "view" and events[0]["t"] == 107

events = parse_line('[mail]   140s record {"kind": "messages", "records": [{"id": "1"}, {"id": "2"}]}')
assert events[0]["kind"] == "messages" and events[0]["records"] == 2

events = parse_line("After round 0 at 185s: 5 positions priced, 0 gaps, 0 implausible prices")
assert events[0]["type"] == "phase"

events = parse_line("Bid at 185s (184.8s research, $0.150):")
assert events[0]["type"] == "bid"

print("parser ok")
