"""cakewalk against every other way to walk a directory tree in Python.

Produces the table in README.md ("Against other walkers") and on the performance page.

    python benchmarks/compare_walkers.py [TARGET] [--rounds N]

`scandir-rs` is included when it is installed and skipped otherwise:

    pip install scandir-rs

Two things this script does that a naive benchmark does not, both because the naive
version produced nonsense here:

**It checks agreement before it reports speed.** A walker that misses entries is not a
fast walker, and the walkers genuinely disagree on some real trees -- Windows directory
junctions are traversed by os.walk, reported-but-not-entered by pathlib and cakewalk, and
omitted entirely by scandir-rs. On a tree with pnpm-style node_modules that is thousands
of entries of difference. The run aborts rather than comparing unlike things.

**It interleaves the walkers round-robin and reports medians.** Walks of the live
filesystem on a Windows machine with an on-access scanner vary by up to 10x run to run;
index reads vary by well under 2x. Timing each walker to completion in turn hands whichever
one ran during a quiet stretch an advantage it did not earn. Interleaving spreads the drift
over all of them. The min and max columns are printed because the spread is itself worth
seeing.
"""
import argparse
import os
import statistics
import sys
import tempfile
import time

from cakewalk import cakewalk as Cakewalk

try:
    from cakewalk._cakewalk import live_walk
except ImportError:  # pragma: no cover - build artifact missing
    live_walk = None

try:
    import scandir_rs
except ImportError:
    scandir_rs = None


def default_target():
    """Somewhere with tens of thousands of files that the user can read."""
    if sys.platform == "win32":
        return os.path.expandvars(r"%LOCALAPPDATA%\Programs")
    for candidate in ("/usr/lib", "/usr/share", os.path.expanduser("~")):
        if os.path.isdir(candidate):
            return candidate
    return os.getcwd()


def consume(walker):
    """Drain a walk, returning (directories, files) so results can be compared."""
    dirs = files = 0
    for _dirpath, dirnames, filenames in walker:
        dirs += len(dirnames)
        files += len(filenames)
    return dirs, files


def manual_scandir(target):
    """A careful hand-rolled walk: what you get with no library at all."""
    def rec(path):
        dirs, files = [], []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    bucket = dirs if entry.is_dir(follow_symlinks=False) else files
                    bucket.append(entry.name)
        except OSError:
            return
        yield path, dirs, files
        for name in dirs:
            yield from rec(os.path.join(path, name))
    return rec(target)


def pathlib_walk(target):
    from pathlib import Path
    return ((str(p), d, f) for p, d, f in Path(target).walk())


def build_walkers(target, scanner):
    walkers = [
        ("os.walk (stdlib)", "filesystem", lambda: consume(os.walk(target))),
        ("pathlib.Path.walk", "filesystem", lambda: consume(pathlib_walk(target))),
        ("hand-rolled os.scandir", "filesystem", lambda: consume(manual_scandir(target))),
    ]
    if scandir_rs is not None:
        def collect():
            toc = scandir_rs.Walk(target).collect()
            return len(toc.dirs), len(toc.files)
        walkers += [
            ("scandir_rs.Walk.collect()", "filesystem", collect),
            ("scandir_rs.Walk (iterate)", "filesystem",
             lambda: consume(scandir_rs.Walk(target))),
        ]
    if live_walk is not None:
        walkers.append(
            ("cakewalk cache miss (jwalk)", "filesystem",
             lambda: consume(live_walk(target)))
        )
    walkers += [
        ("cakewalk validate='full'", "index + stat/dir",
         lambda: consume(scanner.walk(target, validate="full"))),
        ("cakewalk validate='root'", "index",
         lambda: consume(scanner.walk(target, validate="root"))),
        ("cakewalk validate='none'", "index",
         lambda: consume(scanner.walk(target, validate="none"))),
    ]
    return walkers


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?", default=None,
                    help="directory to walk (default: a large system directory)")
    ap.add_argument("--rounds", type=int, default=9,
                    help="interleaved rounds; the reported figure is the median")
    args = ap.parse_args()

    target = os.path.abspath(args.target or default_target())
    if not os.path.isdir(target):
        ap.error(f"not a directory: {target}")

    db = os.path.join(tempfile.gettempdir(), "cakewalk_bench_walkers.db")
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
    nodes = info["files"] + info["dirs"] + 1

    print(target)
    print(f"{nodes:,} nodes ({info['dirs']:,} dirs / {info['files']:,} files), "
          f"{info['size'] / 2**30:.2f} GiB   index build {scan_seconds:.1f}s")
    if scandir_rs is None:
        print("scandir-rs not installed; skipping it (pip install scandir-rs)")
    print(f"{args.rounds} interleaved rounds\n")

    walkers = build_walkers(target, scanner)
    samples = {label: [] for label, _kind, _fn in walkers}
    results = {}
    for _round in range(args.rounds):
        for label, _kind, fn in walkers:
            start = time.perf_counter()
            got = fn()
            samples[label].append(time.perf_counter() - start)
            results.setdefault(label, set()).add(got)

    unstable = {k: v for k, v in results.items() if len(v) != 1}
    if unstable:
        print("ABORT: a walker returned different results between rounds:",
              file=sys.stderr)
        for label, seen in unstable.items():
            print(f"  {label}: {sorted(seen)}", file=sys.stderr)
        return 2

    agreed = {next(iter(v)) for v in results.values()}
    if len(agreed) != 1:
        print("ABORT: the walkers do not agree on what is in this tree, so their "
              "speeds are not comparable.", file=sys.stderr)
        for label, seen in sorted(results.items()):
            d, f = next(iter(seen))
            print(f"  {label:<30} {d:>9,} dirs {f:>9,} files", file=sys.stderr)
        print("\nOn Windows this is usually directory junctions: os.walk descends "
              "through them, pathlib and cakewalk do not, scandir-rs omits them. "
              "Pick a tree without junctions to compare speed.", file=sys.stderr)
        return 2

    dirs, files = next(iter(agreed))
    print(f"all walkers agree every round: {dirs:,} dirs / {files:,} files\n")

    baseline = statistics.median(samples["os.walk (stdlib)"])
    print(f"{'walker':<30}{'reads':>18}{'median':>10}{'min':>9}{'max':>9}"
          f"{'ns/node':>9}{'vs os.walk':>12}")
    for label, kind, _fn in walkers:
        s = samples[label]
        median = statistics.median(s)
        print(f"{label:<30}{kind:>18}{median * 1000:>9.0f}m{min(s) * 1000:>8.0f}m"
              f"{max(s) * 1000:>8.0f}m{median / nodes * 1e9:>9.0f}"
              f"{baseline / median:>11.2f}x")

    scanner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
