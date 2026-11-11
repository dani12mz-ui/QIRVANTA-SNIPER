"""Check that the missed-move indicator differs only in its observation adapter."""

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "releases/gold-pro-unified/QIRVANTA_GOLD_PRO_UNIFIED.pine"
CANDIDATE = Path(__file__).with_name("QIRVANTA_GOLD_PRO_UNIFIED_MISSED_MOVE.pine")
BASELINE_SHA = "03150c89a587cc81559d8712118f8c0179eaf66496e61e0ec8689dba79c2c588"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"Expected a unique source anchor for {old!r}")
    return source.replace(old, new, 1)


def remove_section(source: str, start: str, end: str) -> str:
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError(f"Could not isolate observation section {start!r}")
    first = source.index(start)
    last = source.index(end, first)
    return source[:first] + source[last:]


def verify_source(baseline: bytes, candidate: bytes) -> None:
    if hashlib.sha256(baseline).hexdigest() != BASELINE_SHA:
        raise ValueError("The baseline does not match the pinned 48b2539 Pine bytes")
    source = candidate.decode("utf-8")
    for start, end in (
        ("// Source transitions are taken", "// Presentation reads the frozen decision and observation streams."),
        ("// Use the original renderer on the actual close bar.", "// Each horizontal structural level starts"),
        ("// Closed EARLY observations", "// Optional audit of the unchanged in-indicator RAW Paper stream"),
    ):
        source = remove_section(source, start, end)

    source = remove_section(
        source, "    // Research reuses the existing observation slot.",
        "    if (not mmEnabled or (mmVisible.rank == 0 and not mmShadowFresh)) and rcViewDetailed"
    )

    changes = (
        ('"QIRVANTA GOLD PRO · UNIFIED MISSED MOVE RESEARCH", shorttitle="QIRVANTA MM"',
         '"QIRVANTA GOLD PRO · UNIFIED", shorttitle="QIRVANTA GOLD"'),
        ("// Experimental observation adapter. Existing S5/RC2 decisions remain the only permission source.\n", ""),
        ("int rcEarlyObservedSide = rcCanDrawBar and not tvTimeGap ?",
         "int rcEarlySide = rcCanDrawBar and rcEarlyEnabled and not tvTimeGap ?"),
        ("int rcEarlySide = rcEarlyEnabled ? rcEarlyObservedSide : 0\n", ""),
        ("    DtGeometry mmGeometry = na\n    float mmReference = na\n", ""),
        ("DtObservation.new(state, setup.side, time_close, reason, setup.level, rank, geometry, close)",
         "DtObservation.new(state, setup.side, time_close, reason, setup.level, rank)"),
        ("    if (not mmEnabled or (mmVisible.rank == 0 and not mmShadowFresh)) and rcViewDetailed and observation != \"\"\n",
         "    if rcViewDetailed and observation != \"\"\n"),
        ("    else if not mmEnabled or (mmVisible.rank == 0 and not mmShadowFresh)\n        table.clear(rcViewTable, 0, 18, 11, 18)\n",
         "    else\n        table.clear(rcViewTable, 0, 18, 11, 18)\n"),
    )
    for old, new in changes:
        source = replace_once(source, old, new)
    if source.encode("utf-8") != baseline:
        raise ValueError("The candidate modifies the pinned baseline outside the observation adapter")


if __name__ == "__main__":
    candidate_bytes = CANDIDATE.read_bytes()
    verify_source(BASELINE.read_bytes(), candidate_bytes)
    print(f"Protected Pine bytes match 48b2539. Candidate SHA-256: {hashlib.sha256(candidate_bytes).hexdigest()}")
