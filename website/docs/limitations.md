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

## Not a search index

cakewalk indexes structure — names, sizes, mtimes, hierarchy. It does not index file contents, and there is no query language. It is `os.walk`, faster, plus `du()`.

## Compared to Voidtools Everything

If you can require Administrator privileges on Windows and want maximum speed, Everything reads the NTFS Master File Table and USN Journal directly and will beat this.

cakewalk is for when you cannot ask for elevation, cannot run a background service, or need to index network drives — where the MFT approach does not apply.
