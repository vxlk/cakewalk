---
id: sql
title: Querying the index
sidebar_position: 5
---

# Querying the index

`walk()` is the compatibility layer. The index underneath it is an ordinary SQLite database with a documented schema, and for most questions that is both the faster tool and the simpler one.

## Why the query wins, and by how much

The reason is structural rather than a matter of tuning. Decomposing the older `fs_nodes` reader on a 4.7-million-node index, warm:

| | time | share |
|---|---:|---:|
| SQLite engine | 1.5 ms | 0.0% |
| `sqlite3` bridge — rows and columns crossing into Python | 6265.8 ms | 50.6% |
| unpacking, dir/file branch | 167.0 ms | 1.3% |
| building lists, joining paths, yielding tuples | 5942.8 ms | 48.0% |

The database does none of the work. Nearly all of the time is spent turning rows into Python objects, and a walk has no choice: `os.walk`'s contract is a list of names, so five million names is five million string objects. A query that ends in `sum()`, `count()` or `LIMIT 20` never builds them — SQLite does the work in C and hands back one row.

Measured on `%LOCALAPPDATA%\Programs` (92,365 nodes, 11,347 directories, 4.11 GiB), warm page cache, in the same process as the reader timings on the [performance page](./performance.md#walk-speed):

| question | `os.walk` | `cakewalk.walk` | SQL | SQL vs `os.walk` |
|---|---:|---:|---:|---:|
| total size of the tree | 6810.6 ms | — | 9.94 ms | 685x |
| every `*.dll` beneath it | 1100.5 ms | 35.1 ms | 14.99 ms | 73x |
| 20 largest files | 7184.1 ms | — | 13.34 ms | 539x |
| count and bytes by extension | 1271.4 ms | — | 70.74 ms | 18x |
| total size, via [`du()`](./api.md#du) | 6933.0 ms | — | 0.025 ms | 278,322x |

The `*.dll` row is the fair comparison: neither side calls `stat()`, so both are doing the same thing with names. `cakewalk.walk` is 31x faster than `os.walk`; the query is 73x. That gap is not clever SQL. It is 92,365 Python strings versus one integer.

The three rows that call `stat()` flatter the index for a reason worth naming: `os.walk` gives you names and nothing else, so anything about *size* costs a syscall per file. The index already has it.

:::tip
Before writing a query, check whether [`du()`](./api.md#du) already answers it. Total bytes, file count and directory count for any directory were computed during the scan and cost one lookup.
:::

## Getting a connection

```python
import cakewalk

conn = cakewalk.connect()
```

Opened read-only, so a query cannot corrupt the cache or block a scan. It is a plain [`sqlite3.Connection`](https://docs.python.org/3/library/sqlite3.html) — use it with pandas, Polars, or anything else that speaks DBAPI.

For a non-default index, use the class:

```python
from cakewalk import cakewalk as Cakewalk

scanner = Cakewalk("/var/cache/index.db")
conn = scanner.connect()
```

## Subtree ranges

The [relayout](./architecture.md) that makes walking fast has a second consequence: **a directory's descendants occupy one contiguous run of row ids**. "Everything under this path" is therefore a primary-key range scan — no recursive CTE, no join, no path prefix `LIKE` over a text column.

```python
lo, hi = cakewalk.subtree_range("D:\\share\\projects")

conn.execute(
    "SELECT sum(size), count(*) FROM fs_nodes "
    "WHERE id BETWEEN ? AND ? AND is_dir = 0",
    (lo, hi),
).fetchone()
```

Two details:

- The directory **itself** is not in the range. Its own rollups live on its row, which is what `du()` reads.
- An empty directory yields `(0, -1)`, which matches nothing. No special case needed.

`subtree_range()` returns `None` if the path is not indexed, or if the index predates the block layout.

## Rows to paths

`fs_nodes` stores a `name`, not a path — otherwise every row would carry a copy of its ancestry. `path_of()` reconstructs one:

```python
for (node_id,) in conn.execute(
    "SELECT id FROM fs_nodes WHERE id BETWEEN ? AND ? "
    "AND is_dir = 0 AND name LIKE '%.log'",
    (lo, hi),
):
    print(cakewalk.path_of(node_id))
```

It costs one lookup per level of depth, so resolve paths for the rows you are keeping, not for every row you scan. Aggregates usually need none.

## Schema

```sql
CREATE TABLE fs_nodes (
    id            INTEGER PRIMARY KEY,
    parent_id     INTEGER,          -- NULL for a scanned root
    name          TEXT NOT NULL,
    is_dir        BOOLEAN NOT NULL,
    node_hash     INTEGER NOT NULL,
    last_modified INTEGER NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    file_count    INTEGER NOT NULL DEFAULT 0,
    dir_count     INTEGER NOT NULL DEFAULT 0,
    child_start   INTEGER NOT NULL DEFAULT 0,
    child_end     INTEGER NOT NULL DEFAULT -1,
    subtree_last  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(parent_id) REFERENCES fs_nodes(id) ON DELETE CASCADE
);

CREATE TABLE scan_meta (
    root_id    INTEGER PRIMARY KEY,
    scanned_at INTEGER NOT NULL
);
```

| column | meaning |
|---|---|
| `id` | dense, and the physical order of the table |
| `parent_id` | `NULL` marks a scanned root, whose `name` is its full path |
| `name` | the entry's own name |
| `is_dir` | 1 for directories |
| `size` | files: bytes at scan time. directories: rolled-up subtree total |
| `file_count`, `dir_count` | rolled-up descendant counts; 0 for files |
| `last_modified` | **whole seconds** for directories, **nanoseconds** for files |
| `node_hash` | xxHash64 Merkle digest; how the scan skips unchanged subtrees |
| `child_start`, `child_end` | this directory's children as a contiguous id range; `(0, -1)` if none |
| `subtree_last` | highest id in this subtree; with `child_start`, the descendant range |

One index can hold **many roots**. Filter by a subtree range, or by `parent_id IS NULL` to list them.

There is a second table, `dir_blocks` — one row per directory with child names packed into NUL-joined strings, which is what [`walk()` reads](./architecture.md#dir_blocks-the-walk-projection). It is a derived projection, rebuilt from `fs_nodes` on every relayout. Query `fs_nodes`; it is the source of truth and the only one with sizes, hashes and mtimes.

:::warning
Row ids are not stable across scans. Any change to the tree triggers a full relayout and every id is reassigned. Do not store them; resolve to a path first.
:::

The mixed `last_modified` resolution is a real trap. Directory mtimes are truncated to seconds so that a comparison against a live `stat()` is reliable across filesystems that round differently; file mtimes are kept in nanoseconds. Compare against the right unit for the rows you are selecting.

## Recipes

```sql
-- the 20 largest files under a subtree
SELECT id, name, size FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0
ORDER BY size DESC LIMIT 20;

-- where the space went: the heaviest immediate children of one directory,
-- using its own child_start/child_end rather than a subtree range
SELECT name, size FROM fs_nodes
WHERE id BETWEEN :child_start AND :child_end
ORDER BY size DESC;

-- count and bytes by extension
SELECT lower(substr(name, instr(name, '.'))) AS ext, count(*), sum(size)
FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0 AND name LIKE '%.%'
GROUP BY ext ORDER BY 2 DESC;

-- everything modified since a timestamp (files: nanoseconds)
SELECT id FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0 AND last_modified > :ns;

-- directories over a gigabyte
SELECT id, name, size FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 1 AND size > 1073741824
ORDER BY size DESC;

-- candidate duplicates: same name, same size
SELECT name, size, count(*) AS n FROM fs_nodes
WHERE id BETWEEN :lo AND :hi AND is_dir = 0
GROUP BY name, size HAVING n > 1 ORDER BY size * n DESC;

-- the roots this index holds, and when each was last swept
SELECT n.name, m.scanned_at FROM fs_nodes n
JOIN scan_meta m ON m.root_id = n.id
WHERE n.parent_id IS NULL;
```

## When to walk instead

Query when the answer is an aggregate, a filtered subset, a ranking, or a histogram.

Walk when you genuinely need to **visit** every file — hashing, copying, opening, parsing. There the per-file Python object is not overhead, it is the point, and `walk()` streams it with `O(depth)` memory.

And note what a stale index costs you in each case. A query returns the tree as it was at scan time with no indication that it has moved. A walk can at least be asked to check — see [Freshness](./freshness.md).
