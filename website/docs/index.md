---
id: index
title: Introduction
sidebar_position: 1
---

# cakewalk

cakewalk indexes a filesystem into SQLite and gives you two ways to read it back: a drop-in replacement for `os.walk` and `os.scandir`, and the database itself. It is a native extension built with Rust, PyO3, jwalk and SQLite.

It exists for one situation in particular: repeatedly asking questions about a very large tree — a multi-terabyte shared drive, a network mount, a spinning disk — where the filesystem itself is the bottleneck. You sweep it once, then read the index instead of the disk.

```python
import cakewalk

cakewalk.update_cache("D:\\share")          # sweep the filesystem, build the index

for root, dirs, files in cakewalk.walk("D:\\share"):
    ...                                      # reads the index, not the disk
```

If you already have code written against `os.walk`, that is a two-line change and about 30x faster. If you are willing to write a query instead, the same questions come back one to three orders of magnitude faster:

```python
conn = cakewalk.connect()
lo, hi = cakewalk.subtree_range("D:\\share")

conn.execute("SELECT sum(size) FROM fs_nodes "
             "WHERE id BETWEEN ? AND ? AND is_dir = 0", (lo, hi)).fetchone()
```

That is not a different database — it is the same index `walk()` reads, and [Querying the index](./sql.md) explains why the gap is so large.

## Is this the right tool?

**Yes, if** you walk the same large tree repeatedly, you can tolerate the index being a snapshot rather than live truth, and you control when it is refreshed.

**No, if** you need a walk to reflect the filesystem as it is at this instant. `validate='full'` will give you that, but it stats every directory, which puts you back at roughly `os.walk` speed. A cache you have to verify is not a cache.

**Probably not, if** you walk a directory once. The scan costs more than the walk it saves. cakewalk pays off on the second traversal and every one after.

## What it actually does

Three things, in order:

1. **[`update_cache()`](./getting-started.md)** sweeps the filesystem in parallel, hashes it into a Merkle tree, and writes a SQLite index — including size and count rollups for every directory.
2. **A relayout pass** rewrites the table so each directory's children sit in one contiguous run of row ids, and writes `dir_blocks` — a projection with one row per *directory* and its children's names packed into two strings. Both are explained in [Architecture](./architecture.md).
3. **[`walk()`](./api.md#walk)** streams that projection on a single forward cursor: one query for the whole tree, memory proportional to depth rather than tree size. Or **[a query](./sql.md)** answers the same question without building a Python object per node at all.

The relayout has a second consequence worth knowing about: a directory's descendants end up as one contiguous id range, so "everything under this path" is a primary-key range scan rather than a recursive query.

## Honesty about the numbers

There is a [Performance](./performance.md) page with measurements, and it is deliberate about what was and was not measured. The short version:

- Against a warm `os.walk` on a local disk, `walk()` is about **30x** faster.
- The contiguous-block layout accounts for about **2.5x** of that, consistently from 8.6k to 4.7M nodes.
- Queries against the index are **18x to 685x** faster than `os.walk`, and `du()` is faster still, because they never build a Python object per node. This is the larger effect by a wide margin.
- The layout's *real* benefit — turning scattered reads into one sequential pass — does not appear in any warm benchmark, and we have not measured it end to end on cold spinning media. It is a mechanism with measured seek counts behind it, not a benchmark result.

## Next

- [Getting started](./getting-started.md) — install and first walk
- [Freshness](./freshness.md) — how stale the index is allowed to be, and what each mode catches
- [Architecture](./architecture.md) — why the layout is shaped the way it is
- [Querying the index](./sql.md) — the schema, subtree ranges, and why SQL beats walking
- [API reference](./api.md)
- [Limitations](./limitations.md) — read this before depending on it
