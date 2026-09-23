# unit tests for [importICS.locationReplacements]: TOML escaping of backrefs,
# first-match-wins ordering, invalid pattern skip
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tomllib

from importICS import IcsEvent
from loggingTools import Logger
from pipelineTools import apply_location_replacements, compile_location_replacements


def ev(loc: str) -> IcsEvent:
    return IcsEvent(uid="u1", summary="S", date="2026-07-01", date_end="", location_text=loc)


def log() -> Logger:
    return Logger(str(Path(tempfile.mkdtemp()) / "log.txt"))


def parse_cfg(toml_text: str) -> dict:
    p = Path("config_test.toml")
    p.write_bytes(toml_text.encode())
    cfg = tomllib.loads(p.read_text())
    p.unlink()
    return cfg


# runtime TOML: backrefs must use literal (single-quoted) strings
CONFIG = r"""
[importICS.locationReplacements]
"^SAF( rm .*| room .*)?$" = 'Sir Alexander Fleming building\1, Imperial, London SW7 2AZ'
"^X\\d+$" = 'Room \g<0>, elsewhere'
"[x" = 'never-matches'
"""


def test_toml_escaping_preserves_backrefs():
    cfg = parse_cfg(CONFIG)
    rules = compile_location_replacements(cfg, log())
    assert len(rules) == 2  # invalid pattern skipped
    assert rules[0][0].pattern == r"^SAF( rm .*| room .*)?$"
    assert rules[0][1].startswith(r"Sir Alexander Fleming building\1, Imperial")
    assert rules[1][1] == r"Room \g<0>, elsewhere"


def test_replacement_applied():
    cfg = parse_cfg(CONFIG)
    rules = compile_location_replacements(cfg, log())
    events = [ev("SAF"), ev("SAF rm 23"), ev("SAF room B1"), ev("X7"), ev("other")]
    apply_location_replacements(events, rules, log())
    base = "Sir Alexander Fleming building"
    assert events[0].location_text == f"{base}, Imperial, London SW7 2AZ"
    assert events[1].location_text == f"{base} rm 23, Imperial, London SW7 2AZ"
    assert events[2].location_text == f"{base} room B1, Imperial, London SW7 2AZ"
    assert events[3].location_text == "Room X7, elsewhere"
    assert events[4].location_text == "other"  # unmatched left alone


def test_first_match_wins():
    cfg = {"importICS": {"locationReplacements": {"^a": "A", "a": "B"}}}
    rules = [(re.compile(p), r) for p, r in cfg["importICS"]["locationReplacements"].items()]
    e = ev("ax a")
    apply_location_replacements([e], rules, log())
    assert e.location_text == "Ax a"  # rule 1 matched, rule 2 never applied
