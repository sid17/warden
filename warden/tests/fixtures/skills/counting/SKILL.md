---
name: counting
description: "Maintain a running count across turns / continue counting on request."
---

# Counting

Maintain a running count that survives across turns.

## State

The current count lives in `COUNT.txt` in the working directory, one number per
line. This file is the single source of truth — always read it before counting.

## On a fresh start

If `COUNT.txt` does not exist, start at 1. Write numbers 1 through N (one per
line) into `COUNT.txt` and report the range you wrote (e.g. "1 to 10").

## On "continue"

1. Read `COUNT.txt` and find the last number written.
2. Append the next N numbers (continuing from last + 1), one per line.
3. Report the new range (e.g. "11 to 20").

You may use `scripts/count.sh <start> <how_many>` to emit the next numbers.
