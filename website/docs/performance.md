---
id: performance
title: Performance
sidebar_position: 7
---

# Performance

All figures below were measured on one Windows machine with a local SSD. They are here to be argued with, so each one says what it actually measured.

## The headline is not `walk()`

`walk()` is about 30x faster than `os.walk`. Queries against the same index are 18x to 685x faster, and `du()` is faster still. The rest of this page explains both, but if you only read one table, read [this one](./sql.md#why-the-query-wins-and-by-how-much).

The asymmetry is structural. `walk()` must return a Python string for every name in the tree; a query that ends in `sum()` or `LIMIT 20` returns one row. See [Where the time goes](#where-the-time-goes) below — the database contributes essentially nothing to `walk()`'s cost, so no amount of query tuning will close that gap.

## Memory and the object floor

Producing N Python strings from data already in RAM costs about **95 ns each** on this machine, and that is the floor for any `os.walk`-shaped API. Two things follow, both measured:

- Handing back `bytes` instead of `str` costs 84 ns — a 12% saving, not a multiple. The allocation dominates, so changing the object *type* is not a lever.
- Not materialising at all costs 5 ns. That is the only large saving available, and it is why an aggregate answered in SQL is 100x rather than 5x.

The current reader runs at 334 ns/node against a ~95 ns/name floor, so roughly 3.5x of headroom remains. Most of what was reachable has now been taken: moving the reader into Rust removed the `sqlite3` module from the hot path entirely, and it bought 1.21x.

## Against other walkers

Same tree, same process, 92,365 nodes, warm page cache:

| walker | reads | median | ns/node | vs `os.walk` |
|---|---|---:|---:|---:|
| `os.walk` (stdlib) | filesystem | 1459 ms | 15796 | 1.00x |
| `pathlib.Path.walk` | filesystem | 1114 ms | 12058 | 1.31x |
| hand-rolled `os.scandir` | filesystem | 1005 ms | 10886 | 1.45x |
| `scandir_rs.Walk.collect()` | filesystem | 528 ms | 5717 | 2.76x |
| `scandir_rs.Walk` (iterate) | filesystem | 393 ms | 4259 | 3.71x |
| **cakewalk, cache miss (jwalk)** | filesystem | 335 ms | 3624 | 4.36x |
| cakewalk `validate='full'` | index + one `stat`/dir | 1043 ms | 11291 | 1.40x |
| cakewalk `validate='root'` | index | 48 ms | 521 | **30.30x** |
| cakewalk `validate='none'` | index | 49 ms | 526 | **30.01x** |

Every walker was checked to return the identical set of entries in every round before being timed.

Two things are worth separating here.

**On the filesystem, cakewalk has no real edge.** Its cache-miss path is jwalk, and `scandir-rs` is also jwalk; 4.36x versus 3.71x is the same library with different plumbing, not a different idea. Anyone comparing cold-path traversal should treat these as equivalent.

**The index is the whole difference** — 30x against the standard library, and about 7x against the fastest live walker available. That gap does not come from traversing better; it comes from not traversing.

Timing note: live-filesystem walks on this machine vary by up to 10x run to run (an on-access scanner, most likely), while index reads vary by well under 2x. The walkers are therefore interleaved round-robin and the figure reported is the median of 9 rounds, so drift lands on all of them equally. A grouped benchmark would have handed whichever walker ran during a quiet stretch an advantage it had not earned.

## Walk speed

A real index of 92,365 nodes (11,347 directories, 4.11 GiB), warm OS page cache. All three readers were checked to produce byte-identical output before timing:

| reader | time | ns/node | vs `os.walk` | vs previous |
|---|---:|---:|---:|---:|
| `os.walk` | 1050.6 ms | 11375 | 1.00x | |
| seek per directory | 256.6 ms | 2778 | 4.09x | 4.09x |
| `fs_nodes` block layout | 151.3 ms | 1638 | 6.94x | 1.70x |
| `dir_blocks`, Python reader | 37.4 ms | 405 | 28.08x | 4.04x |
| `dir_blocks`, Rust reader | **30.8 ms** | **334** | **34.06x** | **1.21x** |

Each row is a design cakewalk actually shipped, so the deltas isolate what each change bought. The last one — [one row per directory with names packed](./architecture.md#dir_blocks-the-walk-projection) — is the largest, and for the reason the [decomposition](#where-the-time-goes) predicts: it removes boundary crossings rather than making queries smarter.

`topdown=False` costs the same: 33.8 ms against the `fs_nodes` reader's 149.5 ms.

An older measurement on a 36,527-node tree, kept because it isolates the block layout on its own:

| | time | vs `os.walk` |
|---|---:|---:|
| `os.walk` | 395.1 ms | 1.00x |
| `cakewalk.walk`, seek-per-directory index | 117.3 ms | 3.37x |
| `cakewalk.walk`, block layout | 62.2 ms | **6.35x** |

Smaller tree, 13,871 nodes, showing the validate modes:

| | time | vs `os.walk` |
|---|---:|---:|
| `os.walk` | 201.2 ms | 1.00x |
| `validate='none'` | 26.3 ms | 7.66x |
| `validate='root'` | 27.7 ms | 7.26x |
| `validate='full'` | 185.2 ms | 1.09x |

`'full'` is the honest one. It stats every directory rather than enumerating it, so on the 92,365-node tree above it comes in at 1.40x `os.walk` — a little ahead, not the 30x the other modes get. If you need that mode, the index is buying you very little.

:::note
That 1.40x is recent. Until it was fixed, `validate='full'` seeded its traversal with no cached mtime for the top directory, which `_check_fresh` reads as stale — so it declared the root stale on every walk and fell straight through to a live `os.walk` of the whole tree. It never consulted the index at all, and measured at 0.47x. Results were always correct, which is why nothing caught it.
:::

## How it scales

The two projection readers against each other, driving the shipped code over synthesised indexes in the real schema. Output was verified byte-identical at every size:

| shape | nodes | % dirs | index | `fs_nodes` | Python | Rust | Rust gain |
|---|---:|---:|---:|---:|---:|---:|---:|
| d8 s3 f8 | 36,081 | 27.3% | 5.6 MiB | 65 ms | 25 ms | 16 ms | 1.52x |
| d10 s3 f12 | 442,861 | 20.0% | 71.3 MiB | 819 ms | 243 ms | 192 ms | 1.26x |
| d12 s3 f16 | 5,048,681 | 15.8% | 814.5 MiB | 9039 ms | 2608 ms | 1781 ms | 1.46x |
| d11 s3 f40 | 3,808,640 | 7.0% | 640.3 MiB | 5389 ms | 990 ms | 803 ms | 1.23x |

The projection's advantage over `fs_nodes` grows as trees get more file-heavy — 2.6x at 27% directories, 5.4x at 7% — which is the direction real trees lie in. The native reader's own gain does **not** grow with scale; it sits between 1.13x and 1.52x throughout, because what remains is the cost of the Python strings rather than anything the reader controls.

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
| Index without `dir_blocks` | 99.0 bytes/node |
| Index with `dir_blocks` | 121.8 bytes/node (**+23%**) |
| Index at 50M nodes | ~5.7 GiB |

Walk memory is `O(depth)` — the ancestors of the current directory — not `O(tree)`. It does not grow with tree size.

Both measured on a real 92,365-node tree with real filenames. The layout columns account for about 8%; the projection duplicates every name in the tree, which is where the other 23% goes. In exchange, a walk reads **2.6x fewer bytes**: 2.01 MiB of `dir_blocks` instead of 5.25 MiB of `fs_nodes`.

## Scan cost

`update_cache()` is the expensive half, on purpose. A 13,871-node tree takes ~640 ms. The scan always does full filesystem IO; the Merkle tree skips database writes for unchanged subtrees, not directory reads. Any change also triggers a full relayout and `VACUUM`.

Scan memory is proportional to the number of *directories*, so indexing a very large tree needs real RAM. Walking does not.

## Reproducing

```bash
python -m pytest tests/
```

`tests/test_layout.py` asserts the properties the performance depends on — notably that an unpruned walk performs zero re-seeks, which is the difference between a sequential pass and a seek per directory.
