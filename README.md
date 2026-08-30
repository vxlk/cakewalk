# cakewalk

[![Tests](https://github.com/vxlk/cakewalk/actions/workflows/test.yml/badge.svg)](https://github.com/vxlk/cakewalk/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**cakewalk** indexes a filesystem into SQLite and gives you two ways to read it back: a drop-in `os.walk` / `os.scandir`, and the database itself. It is a native extension built with **Rust**, **PyO3**, **jwalk** and **SQLite**.

It is built for one job in particular: repeatedly asking questions about a very large tree — a multi-terabyte shared drive, a network mount, a spinning disk — where the filesystem itself is the bottleneck. You sweep it once, then read the index instead of the disk.

If you already have code written against `os.walk`, `cakewalk.walk` is a two-line change and about 6x faster. If you are willing to write a query instead, the same questions are answered **one to three orders of magnitude** faster than that — see [Query the index directly](#query-the-index-directly).

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

## Query the index directly

`walk()` is the compatibility layer. The index underneath it is an ordinary SQLite database with a documented schema, and for most questions that is the faster and simpler tool.

The reason is structural. `walk()` has to build a Python string for every name in the tree — 5 million names is 5 million objects no matter how good the reader is. A query that ends in `sum()`, `count()` or `LIMIT 20` never builds them at all; SQLite answers inside C and hands back one row.

```python
import cakewalk

conn = cakewalk.connect()                       # read-only, cannot corrupt the cache
lo, hi = cakewalk.subtree_range("D:\\share")    # every descendant, as an id range

conn.execute(
    "SELECT sum(size), count(*) FROM fs_nodes "
    "WHERE id BETWEEN ? AND ? AND is_dir = 0", (lo, hi)
).fetchone()
```

### Why a range works

The relayout that makes walking fast has a second consequence: **a directory's descendants occupy one contiguous run of row ids**. So "everything under this path" is `id BETWEEN ? AND ?` — a primary-key range scan, not a recursive CTE and not a join. `subtree_range()` hands you the two numbers.

Rows store a `name`, not a path, so `path_of(id)` walks a row back up to an absolute path when you need one. Aggregates usually do not.

### Schema

| column | meaning |
|---|---|
| `id` | row id; dense, and the physical order of the table |
| `parent_id` | `NULL` for a scanned root, whose `name` is its full path |
| `name` | the entry's own name |
| `is_dir` | 1 for directories |
| `size` | files: bytes at scan time. directories: rolled-up subtree total |
| `file_count`, `dir_count` | rolled-up descendant counts; 0 for files |
| `last_modified` | mtime — whole seconds for directories, nanoseconds for files |
| `node_hash` | xxHash64 Merkle digest, used by the scan to skip unchanged subtrees |
| `child_start`, `child_end` | this directory's children, as a contiguous id range |
| `subtree_last` | highest id in this subtree; with `child_start`, the descendant range |

`scan_meta(root_id, scanned_at)` records when each root was last swept.

Row ids are **not stable across scans** — any change to the tree triggers a relayout. Do not store them.

### Recipes

```sql
-- the 20 largest files under a subtree
SELECT id, name, size FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0
ORDER BY size DESC LIMIT 20;

-- where the space went: the heaviest immediate children of one directory
SELECT name, size FROM fs_nodes
WHERE id BETWEEN :child_start AND :child_end
ORDER BY size DESC;

-- count and bytes by extension
SELECT lower(substr(name, instr(name, '.'))) AS ext, count(*), sum(size)
FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0 AND name LIKE '%.%'
GROUP BY ext ORDER BY 2 DESC;

-- everything touched since a timestamp (file mtimes are nanoseconds)
SELECT id FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0 AND last_modified > :ns;

-- directories holding more than a gigabyte
SELECT id, name, size FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 1 AND size > 1073741824
ORDER BY size DESC;
```

### What it is worth

Measured on `%LOCALAPPDATA%\Programs` — 81,018 files, 11,346 directories, 4.11 GiB — warm page cache:

| question | `os.walk` | `cakewalk.walk` | SQL | SQL vs `os.walk` |
|---|---:|---:|---:|---:|
| total size of the tree | 9715.1 ms | — | 10.9 ms | 895x |
| every `*.dll` beneath it | 1480.9 ms | 321.7 ms | 21.9 ms | 68x |
| 20 largest files | 10854.8 ms | — | 18.2 ms | 596x |
| count and bytes by extension | 1586.4 ms | — | 85.6 ms | 19x |
| total size, via `du()` rollup | 9715.1 ms | — | 0.03 ms | 325,000x |

The `*.dll` row is the honest apples-to-apples one: no `stat()` calls on either side, just names. `cakewalk.walk` is 4.6x faster than `os.walk` there; the query is 68x. The gap is not query cleverness — it is that the walk materialises 92,365 Python strings and the query materialises one integer.

`du()` is the extreme case, and the reason to reach for the rollups before writing anything: the answer was computed during the scan.

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

### Where `walk()` spends its time

Decomposed on a 4.7-million-node index, warm:

| | time | share |
|---|---:|---:|
| SQLite engine | 1.5 ms | 0.0% |
| `sqlite3` bridge — rows and columns crossing into Python | 6265.8 ms | 50.6% |
| unpacking and the dir/file branch | 167.0 ms | 1.3% |
| building lists, joining paths, yielding tuples | 5942.8 ms | 48.0% |

The database is free. Essentially all of the time is spent turning rows into Python objects — which is why a query that avoids building them wins so heavily, and why [`du()`](#rollups) wins by five orders of magnitude.

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
