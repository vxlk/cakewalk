---
id: getting-started
title: Getting started
sidebar_position: 2
---

# Getting started

## Install

cakewalk is a compiled extension, so it is built from source:

```bash
pip install maturin
pip install -e .
```

The compiled module is a build artifact and is not committed to the repository — you need to build it before `import cakewalk` will work.

Verify:

```python
import cakewalk
print(cakewalk.walk)
```

## Your first walk

Without an index, `walk()` still works — it falls back to a parallel `jwalk` sweep of the live filesystem:

```python
import cakewalk

for root, dirs, files in cakewalk.walk("D:\\projects"):
    print(root, len(dirs), len(files))
```

This is already faster than `os.walk`, but it is doing full filesystem IO. To get the index:

```python
cakewalk.update_cache("D:\\projects")
```

Now the same loop reads SQLite instead of the disk.

## Where the index lives

The module-level functions share one index in your temp directory. For anything you care about, create the scanner yourself and choose the location:

```python
from cakewalk import cakewalk as Cakewalk

scanner = Cakewalk("/var/cache/share-index.db")
scanner.start_scan("/mnt/share")

for root, dirs, files in scanner.walk("/mnt/share"):
    ...

scanner.close()
```

One index can hold many roots. Scanning one root leaves the others intact.

## Scanning without hogging the machine

A scan saturates a 64-thread pool by default. If it is running alongside something a user is waiting on, drop it to four threads at lowered thread and IO priority:

```python
scanner.start_scan("/mnt/share", background=True)
```

`start_scan` releases the GIL for the whole sweep, so other Python threads keep running either way.

## Keeping it current

The index is a snapshot, and refreshing it is your job. A rescan is differential — unchanged directories skip their database writes — but it still reads the whole filesystem, so schedule it against how fast your data actually changes.

```python
import cakewalk

info = cakewalk.cache_info("/mnt/share")
if info is None or info["age_seconds"] > 3600:
    cakewalk.update_cache("/mnt/share")
```

See [Freshness](./freshness.md) for what a stale index will and will not show you.

## Directory totals

Sizes and counts are rolled up during the scan, so aggregates are one indexed lookup no matter how large the subtree:

```python
cakewalk.du("/mnt/share")
# {'size': 41160, 'files': 11760, 'dirs': 2110}
```

That is `O(1)`, not a traversal.
