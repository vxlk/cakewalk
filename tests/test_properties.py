import os
import random
import tempfile
from hypothesis import assume, given, settings, strategies as st
import cakewalk as cakewalk_mod
from cakewalk import cakewalk

reserved_windows_names = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

valid_names = st.text(
    alphabet=st.characters(blacklist_categories=('Cc', 'Cs'), blacklist_characters=['\\', '/', ':', '*', '?', '"', '<', '>', '|', '\0']), 
    min_size=1, 
    max_size=20
# Only *trailing* dots and spaces are illegal on Windows. Stripping leading dots too would
# make it impossible for this strategy to ever generate a dotfile, which is exactly the case
# that used to diverge between the jwalk and SQLite paths.
).map(lambda s: s.rstrip('. ')).filter(lambda s: bool(s) and s.upper() not in reserved_windows_names)

def distinct_on_disk(entries):
    """Drop siblings that collide once they reach the filesystem.

    Windows matches names case-insensitively, so 'Ď' and 'ď' are one directory
    entry there even though they are distinct Python strings. Without this the generator
    produces trees that cannot be realised, and the test dies in setup instead of
    exercising a reader.
    """
    seen = {}
    for name, subtree in entries.items():
        seen.setdefault(os.path.normcase(name).casefold(), (name, subtree))
    return dict(seen.values())


def trees():
    return st.recursive(
        st.none(),
        lambda children: st.dictionaries(valid_names, children, max_size=5)
            .map(distinct_on_disk)
    )

def realize_tree(base_path, tree):
    """Write a generated tree to disk.

    Names the OS refuses outright are not a cakewalk property, so an example that cannot
    be created is discarded rather than failed. `distinct_on_disk` removes the common
    cause; this covers whatever Windows naming rules it does not model.
    """
    if tree is None:
        return
    for name, subtree in tree.items():
        path = os.path.join(base_path, name)
        try:
            if subtree is None:
                with open(path, "w") as f:
                    f.write("test")
            else:
                os.makedirs(path, exist_ok=True)
        except OSError:
            assume(False)
        if subtree is not None:
            realize_tree(path, subtree)

@settings(max_examples=50, deadline=None)
@given(trees())
def test_walk_equivalence(tree):
    if tree is None:
        return
        
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "cache.db")
        test_root = os.path.join(td, "root")
        os.makedirs(test_root)
        
        realize_tree(test_root, tree)
        
        # Baseline: os.walk
        os_walk_results = []
        for root, dirs, files in os.walk(test_root):
            dirs.sort()
            files.sort()
            os_walk_results.append((root, list(dirs), list(files)))
        os_walk_results.sort(key=lambda x: x[0])
            
        import cakewalk
        
        # 1. Test Cache Miss Path (jwalk fallback)
        # Ensure cakewalk uses our test db path
        cakewalk._default_scanner = cakewalk.cakewalk(db_path)
        
        jwalk_results = []
        for root, dirs, files in cakewalk.walk(test_root):
            dirs.sort()
            files.sort()
            jwalk_results.append((root, list(dirs), list(files)))
        jwalk_results.sort(key=lambda x: x[0])
        
        assert jwalk_results == os_walk_results, "Cache miss (jwalk) path failed equivalence"
        
        # 2. Test Cache Hit Path (SQLite traversal)
        cakewalk.update_cache(test_root)
        
        sqlite_results = []
        for root, dirs, files in cakewalk.walk(test_root):
            dirs.sort()
            files.sort()
            sqlite_results.append((root, list(dirs), list(files)))
        sqlite_results.sort(key=lambda x: x[0])
            
        assert sqlite_results == os_walk_results, "Cache hit (SQLite) path failed equivalence"
        
        cakewalk._default_scanner.close()
        cakewalk._default_scanner = None


#: An index carries every layout at once and picks the fastest reader available. All of
#: them have to reproduce os.walk, so the fuzzing drives each one rather than whichever
#: happens to win the dispatch.
READERS = ("native", "dir_blocks", "fs_blocks", "per_directory")


def _reader(db_path, which):
    scanner = cakewalk_mod.cakewalk(db_path)
    if which != "native":
        scanner._native_reader = False
    if which in ("fs_blocks", "per_directory"):
        scanner._dir_blocks_available = False
    if which == "per_directory":
        scanner._blocks_available = False
    return scanner


def ordered(walker):
    """Preserve yield order; sort only within a tuple, since scandir order is undefined."""
    return [(p, sorted(d), sorted(f)) for p, d, f in walker]


def triples(walker):
    """The walk as a set-like list: order-independent, so sibling order cannot matter.

    Sibling order genuinely differs between os.walk and the index, and neither promises
    one. `os.scandir` on NTFS yields UTF-16 code-unit order, while the index orders by
    SQLite's BINARY collation over UTF-8 -- which disagree for any directory holding both
    a non-BMP name and a name in U+E000..U+FFFF (U+1F600 sorts first as UTF-16, last as
    UTF-8). CPython documents no ordering for os.walk at all, so the guarantee under test
    is the *contents* plus the parent/child ordering below, not the sequence.
    """
    return sorted((p, tuple(sorted(d)), tuple(sorted(f))) for p, d, f in walker)


def ancestors_ordered(walked, topdown):
    """topdown must yield a directory before every descendant; bottom-up, after."""
    position = {path: i for i, (path, _d, _f) in enumerate(walked)}
    for path, dirnames, _filenames in walked:
        for name in dirnames:
            child = os.path.join(path, name)
            if child not in position:
                continue
            if topdown and position[child] <= position[path]:
                return False
            if not topdown and position[child] >= position[path]:
                return False
    return True


def all_names(tree):
    out = set()
    if tree:
        for name, sub in tree.items():
            out.add(name)
            if sub:
                out |= all_names(sub)
    return out


@settings(max_examples=100, deadline=None)
@given(trees())
def test_every_reader_matches_os_walk(tree):
    """Same tree, same traversal shape, from all three layouts.

    Contents are compared as a set and the pre/post-order property separately, because
    sibling order is not something either side promises -- see `triples`. Strict sequence
    equality is asserted in test_layout.py, on names where the two collations agree.
    """
    if not tree:
        return
    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "root")
        os.makedirs(root)
        realize_tree(root, tree)
        db_path = os.path.join(td, "cache.db")
        cakewalk_mod.cakewalk(db_path).start_scan(root)

        for topdown in (True, False):
            expected = triples(os.walk(root, topdown=topdown))
            for which in READERS:
                scanner = _reader(db_path, which)
                try:
                    walked = list(scanner.walk(root, topdown=topdown, validate="none"))
                    assert triples(walked) == expected, which
                    assert ancestors_ordered(ordered(walked), topdown), which
                finally:
                    scanner.close()


@settings(max_examples=100, deadline=None)
@given(trees(), st.integers(min_value=0, max_value=2**32 - 1))
def test_pruning_matches_under_fuzz(tree, seed):
    """Prune an arbitrary subset of names and require identical output to os.walk.

    This is where the projection reader is most likely to be wrong: a prune makes it
    abandon the sequential stream and seek, and a seek to the wrong row would drop or
    duplicate a subtree.
    """
    if not tree:
        return
    names = sorted(all_names(tree))
    if not names:
        return
    rng = random.Random(seed)
    dropped = {n for n in names if rng.random() < 0.4}

    def prune(walker):
        out = []
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = [d for d in dirnames if d not in dropped]
            out.append((dirpath, tuple(sorted(dirnames)), tuple(sorted(filenames))))
        return sorted(out)

    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "root")
        os.makedirs(root)
        realize_tree(root, tree)
        db_path = os.path.join(td, "cache.db")
        cakewalk_mod.cakewalk(db_path).start_scan(root)

        expected = prune(os.walk(root))
        for which in READERS:
            scanner = _reader(db_path, which)
            try:
                assert prune(scanner.walk(root, validate="none")) == expected, which
            finally:
                scanner.close()


@settings(max_examples=50, deadline=None)
@given(trees())
def test_projection_agrees_with_fs_nodes(tree):
    """dir_blocks is derived from fs_nodes; the two must never disagree about a tree.

    Names are NUL-joined in the projection, so this also fuzzes the packing against the
    unicode the name strategy generates.
    """
    if not tree:
        return
    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "root")
        os.makedirs(root)
        realize_tree(root, tree)
        db_path = os.path.join(td, "cache.db")
        scanner = cakewalk_mod.cakewalk(db_path)
        scanner.start_scan(root)
        try:
            conn = scanner._get_conn()
            for node_id, dnames, fnames in conn.execute(
                "SELECT node_id, dnames, fnames FROM dir_blocks"
            ).fetchall():
                children = conn.execute(
                    "SELECT name, is_dir FROM fs_nodes WHERE parent_id = ? ORDER BY id",
                    (node_id,),
                ).fetchall()
                assert (dnames.split("\0") if dnames else []) == \
                    [c[0] for c in children if c[1]]
                assert (fnames.split("\0") if fnames else []) == \
                    [c[0] for c in children if not c[1]]
        finally:
            scanner.close()
