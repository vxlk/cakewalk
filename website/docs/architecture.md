---
id: architecture
title: Architecture
sidebar_position: 4
---

# Architecture

The interesting part of cakewalk is not that it caches directory listings in SQLite. It is *how the rows are physically arranged*, which is what decides whether the cache is faster than the disk it replaces.

## The problem with the obvious design

The obvious schema is a node table with a `parent_id`, and a walk that runs one query per directory:

```sql
SELECT id, name, is_dir FROM fs_nodes WHERE parent_id = ?
```

This is correct and it is slow, for two separate reasons.

**Per-query overhead.** Each `cursor.execute()` costs roughly 13 µs of statement setup through Python's `sqlite3` module. On a tree with 1,257 directories that is ~16 ms before a single row is transferred. For comparison, SQLite can scan the entire 8,169-row table in **0.02 ms**. The data access is free; the query *dispatch* is the cost.

**Scattered physical layout.** Row ids are assigned by a parallel scanner in discovery order, which has nothing to do with walk order. Measured on a real scan, a walk hops around the id space with a median jump of 6 rows and **0.141 backward page seeks per node**.

That second one is invisible in a benchmark and fatal in production. While the database fits in RAM, a backward seek is a pointer chase. Once it does not — a 20M-node index is around 1.4 GiB — every one of those becomes a real read. On a mechanical drive, walking the *cache* would cost millions of seeks and take longer than walking the filesystem.

## The layout

The scanner finishes by rewriting the table so that:

1. **Every directory's immediate children occupy one contiguous range of row ids.** The directory stores that range as `child_start` / `child_end`.
2. **The blocks themselves are laid out depth-first**, in the order a walk visits them.
3. Each directory also stores `subtree_last`, the highest id anywhere beneath it.

An unpruned walk then reads block 0, block 1, block 2… — strictly forward, one sequential pass over the file. Backward seeks per node go from 0.141 to **0.000**, and readahead does what readahead is for.

```
id   parent  name        block      subtree_last
 1   -       share       2..5       11
 2   1       a           6..8       10
 3   1       b          11..11      11
 4   1       empty        -          4
 5   1       top.txt      -          5
 6   2       a1          9..10      10
 7   2       a2           -          7
 8   2       in_a.txt     -          8
 9   6       a1x          -          9
10   6       deep.txt     -         10
11   3       in_b.txt     -         11
```

Read `share`'s block (2–5) and you have its complete `dirnames` and `filenames`. Descend into `a` and its block is 6–8, immediately after. Then `a1` at 9–10. Then back up to `b` at 11. Monotonically forward.

## Why blocks, and not simply depth-first row ids

The first design we tried numbered every *node* depth-first. It produced a sequential scan and it was wrong, in a way that a sorted comparison against `os.walk` did not reveal.

`os.walk` yields a directory's complete `dirnames` list **before** descending into it. That is what makes in-place pruning work:

```python
for root, dirs, files in cakewalk.walk(top):
    dirs[:] = [d for d in dirs if d != ".git"]   # skip that subtree
```

With plain depth-first node ordering, a directory's immediate children are spread across its entire subtree range — you only know the full list after streaming everything beneath it, by which point descending has already happened. The walk becomes post-order, and pruning arrives too late to skip anything.

Grouping children into contiguous blocks fixes exactly this. One block read gives the complete listing, so the yield can happen before the descent, and a caller who prunes causes the reader to **seek past a contiguous run of blocks** rather than stream rows it is going to throw away. On a multi-terabyte tree that distinction is the whole value of pruning.

## The reader

`walk()` opens one cursor and moves it forward:

```sql
SELECT name, is_dir, child_start, child_end FROM fs_nodes
WHERE id BETWEEN ? AND ? ORDER BY id
```

Four columns, not five — column count is close to the dominant cost in the `sqlite3` bridge (transfer scales from 6.06 ms at one column to 13.16 ms at four, for 8,169 rows), so the query carries exactly what a walk needs and nothing else. `last_modified` is absent because only `validate='full'` needs it, and that mode uses a different path.

The reader tracks its position. If the next block it needs starts where the cursor already is, it keeps streaming. If not — which happens only when you prune — it re-issues the query from the new position. An unpruned walk performs **zero re-seeks**, and there is a test asserting exactly that, because a regression there would change no results, only make everything slow again.

Memory is `O(depth × fanout)`: only the directories on the current path are held. A full walk of a 2,110-directory tree peaks at 13.6 KiB of Python objects.

`topdown=False` uses the same forward scan. Blocks are still *read* depth-first, because that is how they are stored; only the moment each directory is handed to the caller changes.

## Why the scan is allowed to be slow

The relayout is a complete rewrite of the table, followed by a `VACUUM`. That is a deliberate trade: the scan side absorbs cost so the walk side does not have to.

It only runs when something actually changed. The root Merkle hash covers the whole subtree, so if it is unchanged, no id can have moved and the existing layout is still correct — the relayout is skipped entirely.

When it does run, it streams in `O(depth × fanout)` memory rather than loading the tree, and the table swap happens inside the transaction so a failure cannot leave you with no cache.

The `VACUUM` is not optional. Dropping the old table leaves its pages as free space; without reclaiming them the file sits at roughly twice the size it needs forever, and this is a file whose entire purpose is to be read off disk sequentially.

## What the Merkle tree is for

Each file is hashed from its name, size and mtime; each directory from its own name, mtime and the sum of its children's hashes — bottom-up, xxHash64.

It answers one question: *did anything in this subtree change?* That is used to skip **database writes** for unchanged directories, and to decide whether the relayout needs to run.

It does **not** skip filesystem reads. `update_cache()` calls `read_dir` on every directory and `symlink_metadata` on every entry, every time. The hash comparison happens after that work, not instead of it.

## Cost

The layout columns add about 6 bytes per node. The index costs roughly **76 bytes per node** in total — about 3.5 GiB for 50 million nodes, up about 8% from the same schema without the layout.
