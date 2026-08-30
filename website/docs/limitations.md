---
id: limitations
title: Limitations
sidebar_position: 8
---

# Limitations

Read this before depending on cakewalk. Most of these are design choices rather than bugs, but they will all bite someone.

## The index is a snapshot

This is the fundamental one. `walk()` shows you the filesystem as it was when you last scanned. With the default `validate='root'`, one `stat` guards the top of the tree and everything below is trusted — a change five levels down is invisible until the next `update_cache()`.

If you need live truth, `validate='full'` gives it to you at roughly `os.walk` speed, which means the cache is buying you very little. See [Freshness](./freshness.md).

## The scan does full filesystem IO, every time

The Merkle tree lets an unchanged subtree skip its **database writes**. It does not skip reading the directory. `update_cache()` calls `read_dir` on every directory and `symlink_metadata` on every entry, on every run — the hash comparison happens after that work, not instead of it.

There is no incremental "only look at what changed" scan. If that is what you need, cakewalk does not do it.

## Scan memory is proportional to directory count

The scan holds directory metadata in memory for the whole tree. Indexing a very large share needs real RAM on the indexing machine.

Walking is unaffected — that side is `O(depth × fanout)` and stays in the kilobytes.

## Symlinks are not followed

`followlinks` is accepted for signature compatibility with `os.walk` and **ignored**. The scanner uses `symlink_metadata`, so a symlink is recorded as whatever it is rather than resolved. `cakewalkDirEntry.is_symlink()` always returns `False`.

If your tree relies on symlinks, results will differ from `os.walk(followlinks=True)`.

### Windows directory junctions

This one catches people out, because it differs from `os.walk` at its **default** settings, not just with `followlinks=True`.

Given a junction created with `mklink /J viajunction real`, the four walkers disagree:

| walker | result |
|---|---|
| `os.walk` | descends **through** it — reports `viajunction\inside\deep.txt` |
| `pathlib.Path.walk` | does not descend; reports `viajunction` as an entry |
| **cakewalk** | does not descend; reports `viajunction` as an entry |
| `scandir_rs.Walk` | omits it entirely |

cakewalk matches `pathlib`. The practical consequence: on a tree with pnpm-style `node_modules` junctions, `os.walk` reports substantially more entries than cakewalk does — 96,309 directories versus 91,981 on one real 790k-node checkout — because it walks the same content once per junction.

Whether that is a bug or a feature depends on you: cakewalk will not double-count or loop, and `os.walk` will. But if you are diffing the two, this will be most of the difference.

## Sibling order can differ from `os.walk`

Entries within one directory are ordered by SQLite's `BINARY` collation over UTF-8. `os.walk` returns `os.scandir` order, which on NTFS is UTF-16 code-unit order.

These agree for ASCII and for most text. They disagree when a directory holds **both** a non-BMP name and a name in U+E000–U+FFFF: `U+1F600` is a surrogate pair starting `0xD83D`, so NTFS sorts it before `U+FF21`, while UTF-8 sorts it after. Because descent follows sibling order, this also reorders the *sequence of directories* a walk yields.

Neither side promises an order — CPython documents none for `os.walk`, and on ext4 it is hash order — so this is a divergence rather than a bug. But if you are diffing cakewalk's output against `os.walk`'s, sort before comparing.

The contents are always identical, and the topdown/bottom-up guarantee always holds: a directory is yielded before all of its descendants, or after all of them.

## Row ids are not stable

Any change to the tree triggers a full relayout, and every row id is reassigned. Do not store cakewalk ids and expect them to mean anything after a rescan.

## A rescan rewrites the whole table

Depth-first block ordering is fundamentally at odds with cheap incremental insertion: adding one node mid-tree shifts the ids after it. Rather than pretend otherwise, cakewalk rewrites the table and `VACUUM`s.

This is fine when the scan is a scheduled job and reads are what you care about. It is not fine if you need cheap frequent incremental updates.

## `validate='full'` is not faster than `os.walk`

It stats every directory. That is `os.walk`'s cost. Expect parity, not a speedup.

## `onerror` disables the fast fallback

On a cache miss, cakewalk normally falls back to a parallel `jwalk` sweep. Supplying `onerror` falls back to plain `os.walk` instead, because jwalk cannot call back into Python cheaply. Cached walks are unaffected.

## Windows-shaped in places

`update_cache(None)` enumerates mapped drive letters, which is meaningless elsewhere. Directory mtimes are truncated to whole seconds to work around NTFS lazy flushing. The test suite is Windows-oriented. Linux and macOS build and pass, but have had less exercise.

## Not a content index

cakewalk indexes structure — names, sizes, mtimes, hierarchy. It does not index file contents.

There *is* a query language, though: the index is [a SQLite database you can query directly](./sql.md), and for aggregates and filters that is far faster than walking.

## Compared to Voidtools Everything

If you can require Administrator privileges on Windows and want maximum speed, Everything reads the NTFS Master File Table and USN Journal directly and will beat this.

cakewalk is for when you cannot ask for elevation, cannot run a background service, or need to index network drives — where the MFT approach does not apply.
