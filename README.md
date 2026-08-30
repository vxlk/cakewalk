# cakewalk

[![Tests](https://github.com/vxlk/cakewalk/actions/workflows/test.yml/badge.svg)](https://github.com/vxlk/cakewalk/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**cakewalk** is a drop-in replacement for `os.walk` and `os.scandir` backed by a SQLite index of your filesystem. It is a native extension built with **Rust**, **PyO3**, **jwalk** and **SQLite**.

It is built for one job in particular: repeatedly walking a very large tree — a multi-terabyte shared drive, a network mount, a spinning disk — where the filesystem itself is the bottleneck. You sweep it once, then walk the index instead of the disk.

📖 **[Full documentation](https://vxlk.github.io/cakewalk/)**

## How it works

```python
import cakewalk

cakewalk.update_cache("D:\\share")          # sweep the filesystem, build the index

for root, dirs, files in cakewalk.walk("D:\\share"):
    ...                                      # reads the index, not the disk
```

`update_cache()` walks the tree in parallel with `jwalk` and `rayon`, computes a bottom-up Merkle tree of `xxHash64` digests, and writes it to SQLite. It then rewrites the table so that **every directory's children occupy one contiguous range of row ids, with the blocks laid out depth-first**.

That last step is what makes reads fast. Row ids handed out by a parallel scan follow discovery order, which is scattered relative to walk order — measured at 0.141 backward page seeks per node. That costs nothing while the database fits in RAM and becomes the dominant cost when it does not. After the rewrite, a full walk reads the file strictly forward, so readahead works and the access pattern is sequential.

`walk()` then streams that layout on a single forward-moving cursor: one query for the whole tree, `O(depth)` memory, and no re-seeks unless you prune.

If a path is not in the index, `walk()` falls back to a parallel `jwalk` sweep of the live filesystem, so it always returns correct results.

## Freshness

The index is a snapshot. `walk()` takes a `validate` argument controlling how hard it checks:

| mode | filesystem access | catches |
|---|---|---|
| `'none'` | none | nothing — pure index read |
| `'root'` *(default)* | one `stat` | additions and deletions **directly in** the directory you are walking |
| `'full'` | one `stat` per directory | everything, by re-reading any directory whose mtime moved |

`'root'` is the default because it is the useful trade: one syscall confirms the top of the tree, and everything beneath is trusted. A change five levels down does **not** move the top directory's mtime and will not be seen until the next `update_cache()`. Keeping the index current is the caller's job — run `update_cache()` on whatever schedule your data changes.

`'full'` is exact but bounded by syscalls rather than by query cost, so it runs at roughly `os.walk` speed. Use it when correctness matters more than latency.

## Rollups

The scan totals sizes and counts on the way up, so directory aggregates are a single indexed lookup regardless of subtree size:

```python
cakewalk.du("D:\\share")
# {'size': 41160, 'files': 11760, 'dirs': 2110}

cakewalk.cache_info("D:\\share")
# {'size': ..., 'files': ..., 'dirs': ..., 'mtime': ..., 'scanned_at': ..., 'age_seconds': 42}
```

Check `age_seconds` if you need to know how much you are trusting.

## Performance

Measured on a 36,527-node tree, warm OS page cache, on the same machine:

| | time | vs `os.walk` |
|---|---:|---:|
| `os.walk` | 395.1 ms | 1.00x |
| `cakewalk.walk`, seek-per-directory index | 117.3 ms | 3.37x |
| `cakewalk.walk`, block layout | 62.2 ms | 6.35x |

The index costs about **76 bytes per node** — roughly 3.5 GiB for 50 million nodes — and a full walk holds a few KiB of Python objects regardless of tree size.

Be aware of what these numbers are and are not. Every row above reads from RAM, so they measure query and CPU cost only; the block layout is worth about 2.5x there, consistently from 8.6k to 4.7M nodes. The reason the layout matters far more than that on a real shared drive is the seek behaviour, which does not show up in a warm benchmark at all: 0.112 backward page seeks per node becomes 0.000. On a 20M-node index that will not fit in RAM, that is the difference between millions of scattered reads and one sequential pass — but we have not measured that end to end on cold spinning media, and you should treat it as a mechanism, not a benchmark.

`os.walk` in the table is also reading a warm page cache. Against a genuinely cold multi-terabyte share it is far slower than shown here, which is the case cakewalk exists for.

## Caveats

- **The scan does full filesystem IO, every time.** The Merkle tree lets an unchanged subtree skip its *database writes*; it does not skip reading the directory. `update_cache()` on a large tree is slow, on purpose — the scan side is where the cost is paid so the walk side is cheap.
- **The scan holds directory metadata in memory** proportional to the number of directories, so an extremely large tree needs real RAM to index. Walking is unaffected.
- **Symlinks are not followed.** `followlinks` is accepted for signature compatibility and ignored.
- **Row ids are not stable.** Any change to the tree triggers a full relayout, and ids are reassigned.

## Usage

```python
import cakewalk

# Drop-in for os.walk, including in-place pruning of dirnames
for root, dirs, files in cakewalk.walk("D:\\share"):
    dirs[:] = [d for d in dirs if d != ".git"]   # subtree is skipped with a seek

# Drop-in for os.scandir
for entry in cakewalk.scandir("D:\\share"):
    print(entry.name, entry.is_dir(), entry.size)   # .size is served from the index

# Bottom-up, and explicit freshness
for root, dirs, files in cakewalk.walk("D:\\share", topdown=False, validate="full"):
    ...
```

For control over where the index lives, use the class directly:

```python
from cakewalk import cakewalk as Cakewalk

scanner = Cakewalk("/var/cache/index.db")
scanner.start_scan("/mnt/share", background=True)   # lower thread + IO priority
for root, dirs, files in scanner.walk("/mnt/share"):
    ...
scanner.close()
```

The module-level functions share a default index in your temp directory.

## PyInstaller compatibility

cakewalk is built with `maturin` and compiles to a single extension module, which PyInstaller's dependency analyzer discovers and bundles automatically. No `sys._MEIPASS` extraction or custom `datas` hooks needed.

## cakewalk vs. Voidtools "Everything"

**Use Everything when:** you are building a system-wide search tool, can require Administrator privileges, and want the maximum speed physically available — it reads the NTFS Master File Table and USN Journal directly.

**Use cakewalk when:** you are shipping a portable Python application and cannot ask for UAC elevation; you want no background service; or you need to index **network drives**, which Everything struggles with because it depends on the local NTFS MFT.

## Building from source

```bash
pip install maturin
pip install -e .
```

The compiled extension is a build artifact and is not committed — you need to build it before the package will import.

Run the tests with:

```bash
python -m pytest tests/
```
