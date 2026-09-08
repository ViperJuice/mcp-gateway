#!/usr/bin/env python3
"""Cross-check two registries in a phase plan that MUST agree:
  (a) lane task tables, 'Tests owned' column -> what a lane is contracted to write
  (b) EC-<ALIAS>-<N> proving commands, ::node ids -> what phase close actually runs
A node id in (b) with no lane contracted to write it => phase close fails collection.
A name in (a) that no EC runs => a lane writes a test nothing proves (warn only).

Why this exists: three consecutive advisor-board rounds on Consiliency/pmcp#239 each
found the same defect class in `plans/phase-plan-v13-*.md` — a safety rule corrected in
the interface freeze and in the acceptance criteria, but not in the lane task table that
tells an executing lane which tests to write. The result is a plan whose own phase-close
command fails collection, or worse, a lane contracted to write a test named after the
unsafe rule that was replaced. Careful reading did not catch it three times; this does.

Usage:
    python3 scripts/check_plan_consistency.py plans/phase-plan-v13-*.md
Exits non-zero if any EC runs a node id no lane is contracted to write.
"""

import re
import sys
import pathlib


def check(path):
    s = pathlib.Path(path).read_text()
    lane, lane_of = set(), {}
    for ln in s.splitlines():
        m = re.match(r"\|\s*(SL-[\w.]+)\s*\|", ln)
        if not m:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        for n in re.findall(r"`(test_[a-z0-9_]+)`", cells[4]):
            lane.add(n)
            lane_of.setdefault(n, m.group(1))
    ec, ec_of = set(), {}
    for ln in s.splitlines():
        m = re.match(r"- \[[ x]\] (EC-[A-Z]+-\d+) —", ln)
        if not m:
            continue
        for n in re.findall(r"::(test_[a-z0-9_]+)", ln):
            ec.add(n)
            ec_of.setdefault(n, m.group(1))
    print(f"\n=== {pathlib.Path(path).name}")
    print(f"  lane-contracted: {len(lane)}   EC-proved node ids: {len(ec)}")
    bad = 0
    for n in sorted(ec - lane):
        print(f"  [BLOCKING] {ec_of[n]} runs ::{n} — no lane is contracted to write it")
        bad += 1
    for n in sorted(lane - ec):
        print(f"  [warn]     {lane_of[n]} writes {n} — no EC proves it")
    if not bad and lane >= ec:
        print("  consistent")
    return bad


total = sum(check(p) for p in sys.argv[1:])
print(f"\nblocking inconsistencies: {total}")
sys.exit(1 if total else 0)
