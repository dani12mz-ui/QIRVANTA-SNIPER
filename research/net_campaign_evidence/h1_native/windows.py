"""Print UTC export boundaries from the exact registered primary OHLC tape."""

import argparse
import csv
from pathlib import Path
import sys

from research.core_htf_readiness.compare_native import utc
from research.net_campaign_evidence.h1_native.run import pinned_archive


WINDOW_SIZE = 350


def rows(repo):
    clocks = tuple(pinned_archive(repo)["market"])
    for offset in range(0, len(clocks), WINDOW_SIZE):
        end = min(offset + WINDOW_SIZE, len(clocks)) - 1
        yield (offset // WINDOW_SIZE + 1, offset, end, end - offset + 1, utc(clocks[offset]), utc(clocks[end]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("window", "first_index", "last_index", "bars", "first_close_utc", "last_close_utc"))
    writer.writerows(rows(args.repo.resolve()))


if __name__ == "__main__":
    main()
