#!/usr/bin/env python3
"""Reconstruct gaps in a saved comparison API response using actual Hub timestamps.

Creates local evidence batches for import_quote_batches.py; never writes production.
"""
import argparse
import json
import math
from pathlib import Path

from backfill_quotes import BATCH_BLOCKS, Reconstruction, reconstruct_targets


def missing_intervals(rows, threshold=120):
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('Gap threshold must be positive and finite')
    previous = None
    gaps = []
    for stamp, price, block in rows:
        if (not math.isfinite(stamp) or stamp <= 0 or
                not math.isfinite(price) or price <= 0 or block is not None or
                (previous is not None and stamp <= previous)):
            raise ValueError('Expected ordered comparison samples')
        if previous is not None and stamp - previous > threshold:
            gaps.append((previous, stamp))
        previous = stamp
    return gaps


def repair(worker, gaps, output):
    count = 0
    for left, right in gaps:
        first, last = worker.locate([left * 1000, right * 1000])
        # The left boundary already exists. Filter the right boundary by its
        # actual timestamp too, including when an endpoint falls between blocks.
        targets = []
        for begin in range(first + 1, last + 1, BATCH_BLOCKS):
            numbers = list(range(begin, min(last + 1, begin + BATCH_BLOCKS)))
            for record in worker.read_blocks(numbers):
                stamp = record['timestamp'] / 1000
                if left < stamp < right:
                    target = [stamp, 1, record['number']]
                    # Multiple Hub blocks can share a timestamp. The chart has
                    # one observation per time; use the final block's state,
                    # while reconstruct_targets still scans intervening trades.
                    if targets and targets[-1][0] == stamp:
                        targets[-1] = target
                    else:
                        targets.append(target)
        if targets:
            reconstruct_targets(worker, targets, output)
            count += len(targets)
        print(f'Reconstructed {len(targets)} samples between {left} and {right}', flush=True)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True,
                        help='Saved /api/coingecko/prices?format=raw response with explicit start/end')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True,
                        help='Resumable two-leg reconstruction cache')
    parser.add_argument('--gap-seconds', type=float, default=120)
    args = parser.parse_args()
    gaps = missing_intervals(json.loads(args.history.read_text())['samples'], args.gap_seconds)
    print(f'Found {len(gaps)} gaps', flush=True)
    if gaps:
        count = repair(Reconstruction(args.work), gaps, args.output)
        print(f'Complete: {count} samples ready for import', flush=True)


if __name__ == '__main__':
    main()
