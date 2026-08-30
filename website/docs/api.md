---
id: api
title: API reference
sidebar_position: 5
---

# API reference

Module-level functions share a single default index in your temp directory. For control over where the index lives, use the [`cakewalk` class](#class-cakewalk).

## `walk`

```python
cakewalk.walk(top, topdown=True, onerror=None, followlinks=False, validate='root')
```

Drop-in replacement for `os.walk`. Yields `(dirpath, dirnames, filenames)`.

Matches `os.walk` including **yield order** and **in-place pruning**: entries removed from `dirnames` after the yield are skipped, and cakewalk seeks past the pruned subtree rather than reading it.

```python
for root, dirs, files in cakewalk.walk("D:\\share"):
    dirs[:] = [d for d in dirs if not d.startswith(".")]
```

| argument | behaviour |
|---|---|
| `topdown` | `True` yields a directory before its children; `False` after. Both honoured exactly. |
| `onerror` | Called with the `OSError` when a directory cannot be stat'd. Note that supplying it disables the jwalk fallback on a cache miss, falling back to `os.walk` instead. |
| `followlinks` | **Accepted and ignored.** Symlinks are never followed. |
| `validate` | `'none'`, `'root'` (default) or `'full'`. See [Freshness](./freshness.md). |

If `top` is not in the index, cakewalk falls back to a live parallel `jwalk` sweep, so results are always correct.

Raises `ValueError` if `validate` is not one of the three accepted values.

## `scandir`

```python
cakewalk.scandir(path='.')
```

Drop-in replacement for `os.scandir`. Yields [`cakewalkDirEntry`](#cakewalkdirentry) objects. Falls back to `os.scandir` on a cache miss. Usable as a context manager.

## `update_cache`

```python
cakewalk.update_cache(path=None, background=False)
```

Sweeps the filesystem and rebuilds the index for `path`. With `path=None` on Windows, scans every mapped drive.

`background=True` runs the scan on four threads at lowered thread and IO priority instead of the default 64, for when it shares a machine with something latency-sensitive.

Releases the GIL for the entire sweep.

This always performs full filesystem IO — see [Architecture](./architecture.md#what-the-merkle-tree-is-for).

## `du`

```python
cakewalk.du(path)
# {'size': 41160, 'files': 11760, 'dirs': 2110}
```

Total bytes, file count and directory count beneath `path`, from rollups computed during the scan. One indexed lookup, `O(1)` regardless of subtree size. Returns `None` if `path` is not indexed.

`size` for a directory is the rolled-up size of its whole subtree. `dirs` counts descendant directories, not including `path` itself.

## `cache_info`

```python
cakewalk.cache_info(path)
# {'size': ..., 'files': ..., 'dirs': ...,
#  'mtime': ..., 'scanned_at': 1735600000, 'age_seconds': 42}
```

The rollups, plus `mtime` as recorded at scan time and how old the scan is. `scanned_at` is stamped only after the writer commits, so its presence guarantees the tree it describes is durable. Returns `None` if `path` is not indexed.

## Validation constants

```python
cakewalk.VALIDATE_NONE   # 'none'
cakewalk.VALIDATE_ROOT   # 'root'
cakewalk.VALIDATE_FULL   # 'full'
```

## `cakewalkDirEntry`

Returned by `scandir()`. Mirrors `os.DirEntry`.

| member | notes |
|---|---|
| `name`, `path` | as `os.DirEntry` |
| `size` | **Served from the index, no IO.** Files: size at scan time. Directories: rolled-up subtree size. |
| `is_dir()`, `is_file()` | From the index, no IO |
| `is_symlink()` | Always `False` — symlinks are not tracked |
| `stat()` | Hits the filesystem, then caches the result |
| `inode()` | Via `stat()`, so it does hit the filesystem |

`size` being free is the main reason to prefer this over `os.scandir` for aggregation.

## Class: `cakewalk`

```python
from cakewalk import cakewalk as Cakewalk

scanner = Cakewalk("/var/cache/index.db")
```

Same surface as the module-level functions — `walk`, `scandir`, `du`, `cache_info` — against an index you choose. One index can hold many roots; scanning one leaves the others intact.

| method | notes |
|---|---|
| `start_scan(root=None, background=False)` | As `update_cache()` |
| `walk(...)` | As `walk()`, but raises `FileNotFoundError` if the index file does not exist rather than falling back |
| `scandir(path)` | Raises `FileNotFoundError` if `path` is not indexed |
| `close()` | Closes the connection |

The module-level functions auto-recover from a corrupt index by deleting it; the class does not.
