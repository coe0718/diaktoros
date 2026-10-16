#!/usr/bin/env python3
"""Split the unit suite into CI shards (#850): every ``tests/test_*.py`` in exactly one shard.

    python tests/shard.py 3 1      # the modules of shard 1 of 3, one per line
    python tests/shard.py 3 --all  # every shard, with its weight (for checking the balance)

Discovery is the source of truth: the files come from the directory, never from a list, so a new
test file is always run. ``shard_weights.json`` only balances the split: each file's measured
seconds (``python tests/leakguard.py discover -s tests -p 'test_*.py' --durations 0``, summed per
module). A file with no weight counts as the median. Files are dealt heaviest first to the
lightest shard, ties broken by name, so the split is the same on every runner.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WEIGHTS = TESTS / "shard_weights.json"


def modules() -> list[str]:
    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def weights() -> dict[str, float]:
    known = json.loads(WEIGHTS.read_text()) if WEIGHTS.exists() else {}
    names = modules()
    fallback = statistics.median(known.values()) if known else 1.0
    return {name: float(known.get(name, fallback)) for name in names}


def shards(count: int) -> list[list[str]]:
    if count < 1:
        raise ValueError("at least one shard")
    load = [0.0] * count
    out: list[list[str]] = [[] for _ in range(count)]
    for name, seconds in sorted(weights().items(), key=lambda item: (-item[1], item[0])):
        lightest = min(range(count), key=lambda i: (load[i], i))
        out[lightest].append(name)
        load[lightest] += seconds
    return [sorted(shard) for shard in out]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[0].isdigit():
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    split = shards(int(argv[0]))
    if argv[1] == "--all":
        w = weights()
        for index, shard in enumerate(split, 1):
            print(f"{index}: {len(shard)} files, {sum(w[n] for n in shard):.0f}s")
        return 0
    if not argv[1].isdigit() or not 1 <= int(argv[1]) <= len(split):
        print(f"shard must be 1..{len(split)}", file=sys.stderr)
        return 2
    print("\n".join(split[int(argv[1]) - 1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
