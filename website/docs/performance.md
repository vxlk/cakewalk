---
id: performance
title: Performance
sidebar_position: 7
---

# Performance

All figures below were measured on one Windows machine with a local SSD. They are here to be argued with, so each one says what it actually measured.

## The headline is not `walk()`

`walk()` is about 6x faster than `os.walk`. Queries against the same index are 19x to 895x faster, and `du()` is faster still. The rest of this page explains both, but if you only read one table, read [this one](./sql.md#why-the-query-wins-and-by-how-much).

The asymmetry is structural. `walk()` must return a Python string for every name in the tree; a query that ends in `sum()` or `LIMIT 20` returns one row. See [Where the time goes](#where-the-time-goes) below — the database contributes essentially nothing to `walk()`'s cost, so no amount of query tuning will close that gap.

## Walk speed

36,527 nodes (4,270 directories, 32,256 files), warm OS page cache:

| | time | vs `os.walk` |
|---|---:|---:|
| `os.walk` | 395.1 ms | 1.00x |
| `cakewalk.walk`, seek-per-directory index | 117.3 ms | 3.37x |
| `cakewalk.walk`, block layout | 62.2 ms | **6.35x** |

The middle row is cakewalk's own previous design — one query per directory against the same data — so the last row isolates what the [contiguous-block layout](./architecture.md) is worth: **1.89x** on top of an already-working cache.

Smaller tree, 13,871 nodes, showing the validate modes:

| | time | vs `os.walk` |
|---|---:|---:|
| `os.walk` | 201.2 ms | 1.00x |
| `validate='none'` | 26.3 ms | 7.66x |
| `validate='root'` | 27.7 ms | 7.26x |
| `validate='full'` | 185.2 ms | 1.09x |

`'full'` is the honest one. It stats every directory, which is precisely what `os.walk` does, so it lands at `os.walk` speed. In that mode the index is buying you almost nothing.

## How it scales

The block layout's advantage over per-directory queries, driving the real reader against synthesised indexes:

| nodes | index size | seek-per-directory | block layout | improvement |
|---:|---:|---:|---:|---:|
| 8,592 | 0.5 MiB | 62.9 ms | 23.5 ms | 2.67x |
| 54,611 | 3.2 MiB | 366.4 ms | 139.0 ms | 2.64x |
| 873,811 | 53.8 MiB | 5,940 ms | 2,351 ms | 2.53x |
| 4,702,923 | 291.1 MiB | 33,538 ms | 13,279 ms | 2.53x |

**Flat at ~2.5x across three orders of magnitude.** Not a growing advantage. Every one of these reads from RAM, so this measures query and CPU cost only.

## The part that isn't in the table

The reason the layout matters on a real shared drive is not in any number above, because none of these benchmarks touch a disk.

Measured on a real scan of 13,871 nodes:

| | page moves | backward seeks |
|---|---:|---:|
| seek-per-directory layout | 3,340 | 1,559 (0.112/node) |
| block layout | 256 | **0** |

While the index fits in RAM, a backward seek is a pointer chase and costs nothing you can measure. Once it does not — a 20M-node index is around 1.4 GiB — each one is a real read. At 0.112 per node that is roughly 2.2M scattered reads to walk a 20M-node tree. On mechanical media at ~7 ms a seek, that is hours, to read the *cache*. The block layout makes it one forward pass.

**We have not measured that end to end.** Producing a genuinely cold multi-terabyte read on spinning media was not something this benchmarking could do. The seek counts are measured; the wall-clock consequence is arithmetic from standard device latencies. Treat it as a mechanism with evidence behind it, not as a benchmark result.

Note also that `os.walk` in the tables above is reading a warm page cache too. Against a cold network share it is far slower than shown, which widens the gap in cakewalk's favour — again, not measured here.

## Where the time goes

Attribution on an 8,169-row index, separating SQLite's work from the Python bridge:

```
count(*) full scan                             0.02 ms   <- SQLite reads every row
count(*) via recursive CTE                     6.85 ms
1257 per-directory count(*) queries           33.73 ms   <- zero rows returned

full scan, 1 column to Python                  6.06 ms
full scan, 2 columns                           8.40 ms
full scan, 3 columns                          11.04 ms
full scan, 4 columns                          13.16 ms

build 8169 tuples from a Python list           1.11 ms
```

Three things follow, and they drove the design:

1. **SQLite's data access is free.** 8,169 rows scanned in 20 microseconds.
2. **Query dispatch dominates.** A thousand-odd `execute()` calls cost ~34 ms while transferring *nothing*. This is why the reader issues one query for an entire walk.
3. **Row transfer is real but is not interpreted Python.** It scales with column count and persists even when results are discarded in C — while building the same tuples from a list costs 1.11 ms. This is why the walk query carries four columns and not five.

The same decomposition at 4,702,923 nodes, against the shipped reader:

| | time | share |
|---|---:|---:|
| SQLite engine (`count(*)` over every row) | 1.5 ms | 0.0% |
| `sqlite3` bridge — rows and columns crossing into Python | 6265.8 ms | 50.6% |
| unpacking and the dir/file branch | 167.0 ms | 1.3% |
| building lists, joining paths, yielding tuples | 5942.8 ms | 48.0% |
| **total** | **12377.1 ms** | |

Two consequences worth stating plainly:

- **The database is not the bottleneck and never was.** 0.0% of a full walk. Optimising SQL further cannot help `walk()`.
- **The cost is the Python objects themselves.** Producing 4.7M Python strings from data already in memory measures at ~450 ms on this machine, which is the floor any `os.walk`-shaped API has to pay. The shipped reader is roughly 27x above that floor, and most of the gap is the `sqlite3` module's per-row and per-column overhead — around 470 ns per row plus 215 ns per column value.

That last number is the one to attack if `walk()` is to get materially faster, and it is not attackable from Python.

## Memory

| | |
|---|---|
| Peak Python objects, full walk of 2,110 directories | 13.6 KiB |
| Index size | ~76 bytes/node |
| Index at 50M nodes | ~3.5 GiB |

Walk memory is `O(depth × fanout)` — the directories on the current path — not `O(tree)`. It does not grow with tree size.

The layout columns account for about 8% of index size.

## Scan cost

`update_cache()` is the expensive half, on purpose. A 13,871-node tree takes ~640 ms. The scan always does full filesystem IO; the Merkle tree skips database writes for unchanged subtrees, not directory reads. Any change also triggers a full relayout and `VACUUM`.

Scan memory is proportional to the number of *directories*, so indexing a very large tree needs real RAM. Walking does not.

## Reproducing

```bash
python -m pytest tests/
```

`tests/test_layout.py` asserts the properties the performance depends on — notably that an unpruned walk performs zero re-seeks, which is the difference between a sequential pass and a seek per directory.
