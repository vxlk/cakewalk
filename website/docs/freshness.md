---
id: freshness
title: Freshness
sidebar_position: 3
---

# Freshness

The index is a snapshot taken when you last ran `update_cache()`. Everything about how cakewalk behaves follows from that.

`walk()` takes a `validate` argument that controls how much filesystem access it is willing to do to check itself.

| mode | filesystem access | catches | cost |
|---|---|---|---|
| `'none'` | none | nothing | fastest |
| `'root'` *(default)* | one `stat` | additions and deletions **directly in** the directory you are walking | one syscall |
| `'full'` | one `stat` per directory | everything | ~1.4x `os.walk` |

```python
cakewalk.walk(path, validate="none")   # or cakewalk.VALIDATE_NONE
cakewalk.walk(path, validate="root")   # default
cakewalk.walk(path, validate="full")
```

## `'root'` — the default, and its sharp edge

`'root'` stats the directory you asked for and compares its mtime against the index. If they match, the entire subtree beneath it is trusted without further checks. If they differ, cakewalk falls back to a live `os.walk` of that tree.

The sharp edge is what a directory mtime actually means: **it changes when entries are added to or removed from that directory, and not otherwise.** So:

```
share/                 <- 'root' stats this
  reports/
    2024/
      q3.csv           <- deleting this does NOT change share/'s mtime
```

Deleting `q3.csv` moves the mtime of `share/reports/2024/`, but not of `share/`. A walk of `share/` with `validate='root'` will still list it.

Adding or deleting something *directly in* `share/` does move its mtime, and is caught.

This is a deliberate trade, not an oversight. One syscall cannot verify a million nodes. `'root'` buys you a cheap guard against the most common case — the directory you pointed at has been reorganised — and is explicit that it buys nothing deeper.

Directory mtimes are stored truncated to whole seconds, because NTFS lazy-flushes them and sub-second comparisons cause spurious invalidation storms. Comparisons happen at the same resolution.

## `'full'` — exact, and priced accordingly

`'full'` stats every directory as it walks, and re-reads any whose mtime has moved. It is correct. It also does one syscall per directory, which is the same order of cost `os.walk` pays — measured at 1.40x `os.walk` on a 92,365-node tree, against 30x for the other two modes. It is ahead because a `stat` is cheaper than an enumeration, not because the index is doing much for you. If you need this mode on every walk, think hard about whether you want a cache at all.

Use it when correctness matters more than latency, and be clear-eyed that in that mode you are mostly not using the cache.

## `'none'` — pure index read

No filesystem access at all. Use it when you have just scanned, when you are aggregating and approximate answers are fine, or when the index is refreshed on a schedule you control and you accept the window.

## Knowing how stale you are

```python
info = cakewalk.cache_info("/mnt/share")
# {'size': ..., 'files': ..., 'dirs': ...,
#  'mtime': ..., 'scanned_at': 1735600000, 'age_seconds': 42}
```

`scanned_at` is stamped only after the writer has committed, so if you can see it, the tree it describes is durable. `age_seconds` is the number to gate on.

A reasonable pattern:

```python
info = cakewalk.cache_info(root)
if info is None or info["age_seconds"] > MAX_AGE:
    cakewalk.update_cache(root)

for dirpath, dirnames, filenames in cakewalk.walk(root, validate="none"):
    ...
```

Refresh on a schedule, then read with `'none'`. That is the shape cakewalk is built for: it is on you to keep the index current, and in exchange reads are cheap.

## What a rescan costs

A rescan always reads the entire filesystem. The Merkle tree lets an unchanged subtree skip its *database writes* — it does not skip the directory read. Any change also triggers a full relayout of the table.

So `update_cache()` is expensive, deliberately. See [Architecture](./architecture.md#why-the-scan-is-allowed-to-be-slow).
