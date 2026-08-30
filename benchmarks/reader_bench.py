"""cakewalk's own read paths, its SQL surface, and what the index costs.

Produces the "Walk speed", "Querying the index" and "Memory" tables in the docs.

    python benchmarks/reader_bench.py [TARGET]

An index carries every layout at once and `walk()` picks the fastest reader available, so
the older paths are still reachable by forcing the capability flags. That is what lets
this compare four readers over one index rather than four indexes.

Everything is timed in one process, back to back. Cross-run comparison is not valid here:
page cache and CPU state move every figure by 2x or more between runs, which is enough to
invent a result that is not there.
"""
import argparse
import hashlib
import heapq
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import timeit
from collections import Counter

from cakewalk import cakewalk as Cakewalk

#: Reader, slowest first, and the capability flags that select it.
READERS = (
    ("per_directory", "seek per directory"),
    ("fs_blocks", "fs_nodes block layout"),
    ("dir_blocks", "dir_blocks, Python reader"),
    ("native", "dir_blocks, Rust reader"),
)


def reader(db, which):
    scanner = Cakewalk(db)
    if which != "native":
        scanner._native_reader = False
    if which in ("fs_blocks", "per_directory"):
        scanner._dir_blocks_available = False
    if which == "per_directory":
        scanner._blocks_available = False
    return scanner


def digest(walker):
    """Order-sensitive fingerprint, so two readers must agree on sequence as well as
    contents."""
    h = hashlib.blake2b(digest_size=16)
    entries = 0
    for path, dirnames, filenames in walker:
        h.update(path.encode()); h.update(b"\1")
        h.update("\0".join(dirnames).encode()); h.update(b"\2")
        h.update("\0".join(filenames).encode()); h.update(b"\3")
        entries += 1 + len(dirnames) + len(filenames)
    return h.hexdigest(), entries


def consume(walker):
    dirs = files = 0
    for _path, dirnames, filenames in walker:
        dirs += len(dirnames)
        files += len(filenames)
    return dirs, files


def best(fn, k=5):
    times = []
    for _ in range(k):
        start = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - start)
    return min(times), result


def default_target():
    if sys.platform == "win32":
        return os.path.expandvars(r"%LOCALAPPDATA%\Programs")
    for candidate in ("/usr/lib", "/usr/share", os.path.expanduser("~")):
        if os.path.isdir(candidate):
            return candidate
    return os.getcwd()


def size_after(db, work, *statements):
    """Bytes the index occupies once `statements` have run and the file is vacuumed."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(work + suffix):
            os.remove(work + suffix)
    shutil.copy(db, work)
    conn = sqlite3.connect(work)
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        return conn.execute("PRAGMA page_count").fetchone()[0] * page
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?", default=None)
    args = ap.parse_args()

    target = os.path.abspath(args.target or default_target())
    if not os.path.isdir(target):
        ap.error(f"not a directory: {target}")

    db = os.path.join(tempfile.gettempdir(), "cakewalk_bench_readers.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)

    scanner = Cakewalk(db)
    started = time.perf_counter()
    scanner.start_scan(target)
    scan_seconds = time.perf_counter() - started
    info = scanner.du(target)
    if info is None:
        print(f"nothing indexed under {target}", file=sys.stderr)
        return 1
    scanner.close()

    conn = sqlite3.connect(db)
    nodes = conn.execute("SELECT count(*) FROM fs_nodes").fetchone()[0]
    directories = conn.execute("SELECT count(*) FROM dir_blocks").fetchone()[0]
    conn.close()

    print(target)
    print(f"{nodes:,} nodes / {directories:,} directories / "
          f"{info['size'] / 2**30:.2f} GiB   (scan {scan_seconds:.1f}s)\n")

    # A faster reader that disagrees is not a faster reader.
    seen = {}
    for which, _label in READERS:
        probe = reader(db, which)
        seen[which] = digest(probe.walk(target, validate="none"))
        probe.close()
    if len({v for v in seen.values()}) != 1:
        print("ABORT: readers disagree", seen, file=sys.stderr)
        return 2
    fingerprint, entries = seen["native"]
    print(f"{len(READERS)} readers agree exactly: {fingerprint} ({entries:,} entries)\n")

    # ------------------------------------------------------------------ readers
    baseline, _ = best(lambda: consume(os.walk(target)), 3)
    times = {}
    for which, _label in READERS:
        probe = reader(db, which)
        times[which], _ = best(lambda p=probe: consume(p.walk(target, validate="none")))
        probe.close()

    print("WALK READERS")
    print(f"{'':<28}{'time':>11}{'ns/node':>10}{'vs os.walk':>12}{'vs previous':>13}")
    print(f"{'os.walk':<28}{baseline * 1000:>10.1f}m"
          f"{baseline / nodes * 1e9:>10.0f}{1.0:>11.2f}x{'':>13}")
    previous = baseline
    for which, label in READERS:
        t = times[which]
        print(f"{label:<28}{t * 1000:>10.1f}m{t / nodes * 1e9:>10.0f}"
              f"{baseline / t:>11.2f}x{previous / t:>12.2f}x")
        previous = t

    for which, label in READERS[1:]:
        probe = reader(db, which)
        t, _ = best(lambda p=probe: consume(p.walk(target, topdown=False,
                                                   validate="none")), 3)
        probe.close()
        print(f"  {label + ', topdown=False':<42}{t * 1000:>9.1f} ms")

    # ------------------------------------------------------------------ SQL
    print("\nQUESTIONS")
    scanner = Cakewalk(db)
    lo, hi = scanner.subtree_range(target)
    query = scanner.connect()
    query.execute("SELECT count(*) FROM fs_nodes").fetchone()

    suffix = ".dll" if sys.platform == "win32" else ".so"

    def os_du():
        total = 0
        for dirpath, _dirnames, filenames in os.walk(target):
            for name in filenames:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        return total

    def os_find():
        return sum(1 for _p, _d, files in os.walk(target)
                   for n in files if n.endswith(suffix))

    def cw_find():
        return sum(1 for _p, _d, files in scanner.walk(target, validate="none")
                   for n in files if n.endswith(suffix))

    def os_largest():
        heap = []
        for dirpath, _dirnames, filenames in os.walk(target):
            for name in filenames:
                try:
                    size = os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
                if len(heap) < 20:
                    heapq.heappush(heap, (size, name))
                elif size > heap[0][0]:
                    heapq.heapreplace(heap, (size, name))
        return len(heap)

    def os_hist():
        counter = Counter()
        for _p, _d, filenames in os.walk(target):
            for name in filenames:
                counter[os.path.splitext(name)[1].lower()] += 1
        return len(counter)

    questions = (
        ("total size of the tree", os_du, None,
         "SELECT sum(size) FROM fs_nodes WHERE id BETWEEN ? AND ? AND is_dir = 0"),
        (f"every *{suffix} beneath it", os_find, cw_find,
         "SELECT count(*) FROM fs_nodes WHERE id BETWEEN ? AND ? "
         f"AND is_dir = 0 AND name LIKE '%{suffix}'"),
        ("20 largest files", os_largest, None,
         "SELECT id, size FROM fs_nodes WHERE id BETWEEN ? AND ? AND is_dir = 0 "
         "ORDER BY size DESC LIMIT 20"),
        ("count and bytes by extension", os_hist, None,
         "SELECT lower(substr(name, instr(name, '.'))) ext, count(*), sum(size) "
         "FROM fs_nodes WHERE id BETWEEN ? AND ? AND is_dir = 0 AND name LIKE '%.%' "
         "GROUP BY ext ORDER BY 2 DESC"),
    )

    print(f"{'question':<30}{'os.walk':>12}{'cakewalk.walk':>16}{'SQL':>11}{'SQL vs':>9}")
    for label, walk_fn, cake_fn, sql in questions:
        t_os, _ = best(walk_fn, 3)
        t_cw = best(cake_fn, 3)[0] if cake_fn else None
        t_sql, _ = best(lambda s=sql: query.execute(s, (lo, hi)).fetchall(), 5)
        cw = f"{t_cw * 1000:>13.1f} ms" if t_cw else f"{'-':>16}"
        print(f"{label:<30}{t_os * 1000:>9.1f} ms{cw}{t_sql * 1000:>8.2f} ms"
              f"{t_os / t_sql:>8.0f}x")

    rounds = 2000
    t_du = timeit.timeit(lambda: scanner.du(target), number=rounds) / rounds
    t_walk_du, _ = best(os_du, 3)
    print(f"{'total size, via du() rollup':<30}{t_walk_du * 1000:>9.1f} ms{'-':>16}"
          f"{t_du * 1000:>8.3f} ms{t_walk_du / t_du:>8.0f}x")
    query.close()
    scanner.close()

    # ------------------------------------------------------------------ size
    print("\nINDEX SIZE")
    work = os.path.join(tempfile.gettempdir(), "cakewalk_bench_size_probe.db")
    try:
        full = size_after(db, work)
        without = size_after(db, work, "DROP TABLE dir_blocks")
        blocks_only = size_after(db, work, "DROP TABLE fs_nodes", "DROP TABLE scan_meta")
        fs_only = size_after(db, work, "DROP TABLE dir_blocks",
                             "DROP INDEX IF EXISTS idx_parent_name",
                             "DROP INDEX IF EXISTS idx_parent_id")
    finally:
        for suffix_ in ("", "-wal", "-shm"):
            if os.path.exists(work + suffix_):
                os.remove(work + suffix_)

    print(f"  with dir_blocks      {full / 2**20:>7.2f} MiB   {full / nodes:>6.1f} B/node")
    print(f"  without dir_blocks   {without / 2**20:>7.2f} MiB   "
          f"{without / nodes:>6.1f} B/node")
    print(f"  projection costs     {(full - without) / 2**20:>7.2f} MiB   "
          f"+{(full - without) / without * 100:.1f}%")
    print(f"  walk path: fs_nodes {fs_only / 2**20:.2f} MiB vs dir_blocks "
          f"{blocks_only / 2**20:.2f} MiB  -> {fs_only / blocks_only:.2f}x fewer bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
