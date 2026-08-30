# Benchmarks

The scripts behind the numbers in [README.md](../README.md) and the [performance
page](https://vxlk.github.io/cakewalk/docs/performance). They are here so the claims can be
argued with rather than taken on trust.

Both build a throwaway index in your temp directory and read a real tree. Neither writes to
the tree it walks.

## `compare_walkers.py`

cakewalk against `os.walk`, `pathlib.Path.walk`, a hand-rolled `os.scandir`, and
[`scandir-rs`](https://pypi.org/project/scandir-rs/). Produces the "Against other walkers"
table.

```bash
python benchmarks/compare_walkers.py                        # a large system directory
python benchmarks/compare_walkers.py /path/to/tree --rounds 15
pip install scandir-rs                                      # optional, skipped if absent
```

## `reader_bench.py`

cakewalk's own read paths against each other, the SQL surface against walking, and what the
index costs on disk. Produces the "Walk speed", "Querying the index" and "Memory" tables.

```bash
python benchmarks/reader_bench.py
python benchmarks/reader_bench.py /path/to/tree
```

An index carries every layout at once and `walk()` picks the fastest reader available, so
the older paths are still reachable by forcing the capability flags (`_native_reader`,
`_dir_blocks_available`, `_blocks_available`). That is how one index yields four readers.
Those flags are private and exist for this and for the tests; do not build on them.

## Two things these scripts do that a naive benchmark does not

**They check agreement before they report speed.** A walker that misses entries is not a
fast walker. `compare_walkers.py` aborts if the walkers disagree, and they genuinely do
disagree on some real trees: Windows directory junctions are traversed by `os.walk`,
reported-but-not-entered by `pathlib` and cakewalk, and omitted by `scandir-rs`. On a
checkout with pnpm-style `node_modules` that is thousands of entries of difference.
`reader_bench.py` compares an order-sensitive digest, so two readers must agree on
sequence as well as contents.

**`compare_walkers.py` interleaves round-robin and reports medians.** Walks of the live
filesystem on a Windows machine with an on-access scanner vary by up to 10x run to run;
index reads vary by well under 2x. Timing each walker to completion in turn hands whichever
one ran during a quiet stretch an advantage it did not earn — we measured `scandir-rs` at
170 ms and 1965 ms on consecutive identical runs. Interleaving spreads the drift evenly,
and the min/max columns show how much of it there was.

`reader_bench.py` does not interleave, because it times a single process against one index
where the variance is small. It does everything in one run for the same reason: page cache
and CPU state move every figure by 2x or more between runs, which is more than enough to
invent a result that is not there.

## What these cannot tell you

**Everything here reads a warm page cache.** The case cakewalk is built for — a cold
multi-terabyte share on slow or remote media — is not measured by any of it, and the
project's central performance argument still rests on a model rather than a measurement.
The measured parts of that argument are the byte counts and seek counts in
`reader_bench.py`; the wall-clock consequence is arithmetic. Treat it accordingly.

If you have a Linux box (`echo 3 > /proc/sys/vm/drop_caches`), a real SMB or NFS mount, or
a throttled device, running `compare_walkers.py` there would settle the question these
scripts cannot.
