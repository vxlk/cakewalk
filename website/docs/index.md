---
id: index
title: Introduction
sidebar_position: 1
---

# cakewalk

cakewalk is a drop-in replacement for `os.walk` and `os.scandir` backed by a SQLite index of your filesystem. It is a native extension built with Rust, PyO3, jwalk and SQLite.

It exists for one situation in particular: repeatedly walking a very large tree — a multi-terabyte shared drive, a network mount, a spinning disk — where the filesystem itself is the bottleneck. You sweep it once, then walk the index instead of the disk.

```python
import cakewalk

cakewalk.update_cache("D:\\share")          # sweep the filesystem, build the index

for root, dirs, files in cakewalk.walk("D:\\share"):
    ...                                      # reads the index, not the disk
```

## Is this the right tool?

**Yes, if** you walk the same large tree repeatedly, you can tolerate the index being a snapshot rather than live truth, and you control when it is refreshed.

**No, if** you need a walk to reflect the filesystem as it is at this instant. `validate='full'` will give you that, but it stats every directory, which puts you back at roughly `os.walk` speed. A cache you have to verify is not a cache.

**Probably not, if** you walk a directory once. The scan costs more than the walk it saves. cakewalk pays off on the second traversal and every one after.

## What it actually does

Three things, in order:

1. **[`update_cache()`](./getting-started.md)** sweeps the filesystem in parallel, hashes it into a Merkle tree, and writes a SQLite index — including size and count rollups for every directory.
2. **A relayout pass** rewrites the table so each directory's children sit in one contiguous run of row ids. This is the part that makes reads fast, and it is explained in [Architecture](./architecture.md).
3. **[`walk()`](./api.md#walk)** streams that layout on a single forward cursor: one query for the whole tree, memory proportional to depth rather than tree size.

## Honesty about the numbers

There is a [Performance](./performance.md) page with measurements, and it is deliberate about what was and was not measured. The short version:

- Against a warm `os.walk` on a local disk, cakewalk is about **6x** faster.
- The contiguous-block layout accounts for about **2.5x** of that, consistently from 8.6k to 4.7M nodes.
- The layout's *real* benefit — turning scattered reads into one sequential pass — does not appear in any warm benchmark, and we have not measured it end to end on cold spinning media. It is a mechanism with measured seek counts behind it, not a benchmark result.

## Next

- [Getting started](./getting-started.md) — install and first walk
- [Freshness](./freshness.md) — how stale the index is allowed to be, and what each mode catches
- [Architecture](./architecture.md) — why the layout is shaped the way it is
- [API reference](./api.md)
- [Limitations](./limitations.md) — read this before depending on it
