"""The contiguous-child-block layout, and the reader that depends on it.

The layout is what makes a walk one forward pass over the database instead of a seek per
directory. These tests pin both halves: that the scanner produces a layout with the
invariants the reader assumes, and that the reader still reproduces os.walk exactly --
including yield *order*, which a set- or sort-based comparison would not catch.
"""
import os
import sqlite3

import pytest

import cakewalk as cw
from cakewalk import cakewalk


def build_tree(root):
    """A deliberately awkward tree: empty dirs, a deep chain, dotfiles, uneven fanout."""
    os.makedirs(os.path.join(root, "a", "a1", "a1x", "deep", "deeper"))
    os.makedirs(os.path.join(root, "a", "a2"))
    os.makedirs(os.path.join(root, "b", "b1"))
    os.makedirs(os.path.join(root, "empty"))
    os.makedirs(os.path.join(root, ".hidden", "sub"))
    os.makedirs(os.path.join(root, "z"))
    for rel, names in (
        ((), ["top.txt", "another.txt", ".dotfile"]),
        (("a",), ["in_a.txt"]),
        (("a", "a1"), ["x.txt", "y.txt"]),
        (("b",), ["in_b.txt"]),
        ((".hidden",), ["secret"]),
        (("a", "a1", "a1x", "deep", "deeper"), ["bottom.txt"]),
    ):
        for name in names:
            with open(os.path.join(root, *rel, name), "w") as fh:
                fh.write("data")


#: The read paths, fastest first. An index carries every layout at once, so which one a
#: walk takes is a capability decision -- and all of them still have to reproduce os.walk
#: exactly. Forcing the flags simulates an older index or extension without writing one.
READERS = ("native", "dir_blocks", "fs_blocks", "per_directory")


def _select_reader(scanner, reader):
    if reader != "native":
        scanner._native_reader = False
    if reader in ("fs_blocks", "per_directory"):
        scanner._dir_blocks_available = False
    if reader == "per_directory":
        scanner._blocks_available = False
    return scanner


@pytest.fixture(params=READERS)
def scanned(tmp_path, request):
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    scanner = cakewalk(str(tmp_path / "cache.db"))
    scanner.start_scan(root)
    yield root, _select_reader(scanner, request.param)
    scanner.close()


@pytest.fixture
def scanned_default(tmp_path):
    """As `scanned`, but without forcing a reader -- for tests about the index itself."""
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    scanner = cakewalk(str(tmp_path / "cache.db"))
    scanner.start_scan(root)
    yield root, scanner
    scanner.close()


def normalise(walker, root):
    """Keep yield order; sort only within a tuple, since scandir order is not defined."""
    return [
        (os.path.relpath(dirpath, root), sorted(dirnames), sorted(filenames))
        for dirpath, dirnames, filenames in walker
    ]


@pytest.mark.parametrize("validate", ["none", "root", "full"])
@pytest.mark.parametrize("topdown", [True, False])
def test_matches_os_walk_including_order(scanned, validate, topdown):
    root, scanner = scanned
    assert normalise(scanner.walk(root, topdown=topdown, validate=validate), root) == \
        normalise(os.walk(root, topdown=topdown), root)


@pytest.mark.parametrize("validate", ["none", "root", "full"])
@pytest.mark.parametrize("dropped", [{"a1"}, {"a"}, {"a", "b"}, {"deep"},
                                     {"a", "b", "empty", ".hidden", "z"}])
def test_in_place_pruning(scanned, validate, dropped):
    """os.walk lets a caller delete entries from dirnames to skip those subtrees."""
    root, scanner = scanned

    def prune(walker):
        out = []
        for dirpath, dirnames, filenames in walker:
            for name in list(dirnames):
                if name in dropped:
                    dirnames.remove(name)
            out.append((os.path.relpath(dirpath, root), sorted(dirnames), sorted(filenames)))
        return out

    assert prune(scanner.walk(root, validate=validate)) == prune(os.walk(root))


@pytest.mark.parametrize("rel", ["a", "a/a1", "empty", ".hidden"])
def test_walk_from_a_subdirectory(scanned, rel):
    root, scanner = scanned
    start = os.path.join(root, *rel.split("/"))
    assert normalise(scanner.walk(start, validate="none"), start) == \
        normalise(os.walk(start), start)


#: Every reader that counts its own query restarts, and the flag that selects it. The
#: native reader is a Rust class rather than a subclassable one, so the probe delegates
#: instead of inheriting.
SEEK_COUNTERS = {
    "native": ("DirBlockWalk", "native"),
    "dir_blocks": ("_DirBlockReader", "dir_blocks"),
    "fs_blocks": ("_BlockReader", "fs_blocks"),
}


def _probe(monkeypatch, attr):
    """Record every reader the walk constructs, without changing its behaviour."""
    made = []
    original = getattr(cw, attr)

    class Probe:
        def __init__(self, *args, **kwargs):
            self._inner = original(*args, **kwargs)
            made.append(self)

        def __iter__(self):
            return iter(self._inner)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(cw, attr, Probe)
    return made


@pytest.mark.parametrize("which", list(SEEK_COUNTERS))
def test_unpruned_walk_never_reseeks(tmp_path, monkeypatch, which):
    """The point of the layout: a full walk is one sequential pass, no re-queries.

    A regression here would not change any result, only make every walk slow again, so it
    needs its own assertion. Every reader must hold the property.
    """
    attr, flavour = SEEK_COUNTERS[which]
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    scanner = _select_reader(cakewalk(str(tmp_path / "cache.db")), flavour)
    scanner.start_scan(root)
    made = _probe(monkeypatch, attr)

    try:
        list(scanner.walk(root, validate="none"))
        assert made, f"{attr} was not used at all"
        # One query is needed to start the stream; anything beyond that is a re-seek.
        assert made[-1].seeks == 1

        made.clear()
        for _dirpath, dirnames, _filenames in scanner.walk(root, validate="none"):
            if "a1" in dirnames:
                dirnames.remove("a1")
        assert made[-1].seeks > 1, "pruning should seek past the skipped subtree"
    finally:
        scanner.close()


@pytest.mark.parametrize("which", ["native", "dir_blocks"])
@pytest.mark.parametrize("fanout", [3, 40])
def test_pruning_everything_costs_a_constant_number_of_seeks(
    tmp_path, monkeypatch, which, fanout
):
    """Dropping every subdirectory of a node must not cost a seek per sibling.

    The naive implementation seeks past each pruned subtree in turn, which is one query
    per sibling. Because a directory's whole subtree is a contiguous run of rows, the
    reader can retire the parent instead -- so the count must not move with `fanout`.
    """
    attr, flavour = SEEK_COUNTERS[which]
    root = str(tmp_path / "tree")
    os.makedirs(root)
    for i in range(fanout):
        os.makedirs(os.path.join(root, f"sub{i:03d}", "nested"))
        with open(os.path.join(root, f"sub{i:03d}", "inner.txt"), "w") as fh:
            fh.write("x")
    with open(os.path.join(root, "top.txt"), "w") as fh:
        fh.write("x")

    scanner = _select_reader(cakewalk(str(tmp_path / "cache.db")), flavour)
    scanner.start_scan(root)
    made = _probe(monkeypatch, attr)

    try:
        out = []
        for dirpath, dirnames, filenames in scanner.walk(root, validate="none"):
            out.append((os.path.relpath(dirpath, root), sorted(filenames)))
            dirnames[:] = []
        assert out == [(".", ["top.txt"])]
        # One query to start the stream, at most one to resume past the pruned run.
        assert made[-1].seeks <= 2
    finally:
        scanner.close()


def test_layout_invariants(scanned_default):
    """Whatever the reader assumes about the layout, the scanner must guarantee."""
    root, scanner = scanned_default
    conn = scanner._get_conn()
    rows = conn.execute(
        "SELECT id, parent_id, name, is_dir, child_start, child_end, subtree_last "
        "FROM fs_nodes ORDER BY id"
    ).fetchall()
    assert rows

    ids = [r[0] for r in rows]
    assert ids == list(range(1, len(ids) + 1)), "ids must be dense, starting at 1"

    children = {}
    for node_id, parent_id, *_ in rows:
        children.setdefault(parent_id, []).append(node_id)

    for node_id, _parent, name, is_dir, start, end, last in rows:
        kids = sorted(children.get(node_id, []))
        if kids:
            assert kids == list(range(start, end + 1)), \
                f"{name}: children {kids} are not the contiguous block {start}..{end}"
        else:
            assert end < start, f"{name} has no children but claims block {start}..{end}"

        # subtree_last must cover every descendant, or a pruning seek would skip live rows.
        descendants, stack = set(), list(kids)
        while stack:
            node = stack.pop()
            descendants.add(node)
            stack.extend(children.get(node, []))
        if descendants:
            assert last == max(descendants)
            assert descendants == set(range(start, last + 1)), \
                f"{name}: descendants are not contiguous, so a prune seek would be wrong"
        else:
            assert last == node_id


def test_unchanged_rescan_preserves_layout(scanned_default):
    """Relayout is a full table rewrite, so it must not run when nothing changed."""
    root, scanner = scanned_default
    query = "SELECT id, name, child_start, child_end, subtree_last FROM fs_nodes ORDER BY id"
    before = scanner._get_conn().execute(query).fetchall()
    scanner.close()

    again = cakewalk(scanner.db_location)
    again.start_scan(root)
    assert again._get_conn().execute(query).fetchall() == before
    again.close()


def test_change_triggers_relayout(scanned_default):
    root, scanner = scanned_default
    scanner.close()

    with open(os.path.join(root, "a", "a1", "brand_new.txt"), "w") as fh:
        fh.write("q")

    again = cakewalk(scanner.db_location)
    again.start_scan(root)
    assert normalise(again.walk(root, validate="none"), root) == normalise(os.walk(root), root)
    test_layout_invariants((root, again))
    again.close()


def test_repeated_incremental_edits(tmp_path):
    """Rescan after every kind of edit, checking the tree and the layout each time.

    This is where id reuse goes wrong. New nodes take ids above the existing high-water
    mark, and getting that mark from the wrong set of rows hands a new node an id a live
    row already owns -- which the upsert resolves by overwriting it, silently losing a
    file. A single-edit test does not reliably reach that state.
    """
    import random
    import shutil

    random.seed(7)
    root = str(tmp_path / "tree")
    os.makedirs(root)
    for i in range(5):
        for j in range(4):
            leaf = os.path.join(root, f"d{i}", f"s{j}")
            os.makedirs(leaf)
            for x in range(3):
                with open(os.path.join(leaf, f"f{x}.txt"), "w") as fh:
                    fh.write("x")

    db = str(tmp_path / "cache.db")

    def rescan_and_check():
        scanner = cakewalk(db)
        scanner.start_scan(root)
        scanner.close()
        scanner = cakewalk(db)
        try:
            assert normalise(scanner.walk(root, validate="none"), root) == \
                normalise(os.walk(root), root)
            test_layout_invariants((root, scanner))
        finally:
            scanner.close()

    rescan_and_check()

    for step in range(1, 16):
        directories = [d for d, _, _ in os.walk(root)]
        target = random.choice(directories)
        action = random.choice(["add_file", "add_dir", "delete_file", "delete_dir", "rewrite"])
        try:
            if action == "add_file":
                with open(os.path.join(target, f"new_{step}.txt"), "w") as fh:
                    fh.write("y" * step)
            elif action == "add_dir":
                os.makedirs(os.path.join(target, f"nd_{step}"), exist_ok=True)
            elif action == "delete_file":
                files = [f for f in os.listdir(target)
                         if os.path.isfile(os.path.join(target, f))]
                if files:
                    os.remove(os.path.join(target, random.choice(files)))
            elif action == "delete_dir" and target != root:
                shutil.rmtree(target)
            else:
                with open(os.path.join(target, "rewritten.txt"), "w") as fh:
                    fh.write("z" * step)
        except OSError:
            continue
        rescan_and_check()


def test_database_without_layout_still_reads(tmp_path):
    """A cache written before the layout existed must keep working, just more slowly."""
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    db = str(tmp_path / "cache.db")

    scanner = cakewalk(db)
    scanner.start_scan(root)
    expected = normalise(scanner.walk(root, validate="none"), root)
    scanner.close()

    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE dir_blocks")
    for column in ("child_start", "child_end", "subtree_last"):
        conn.execute(f"ALTER TABLE fs_nodes DROP COLUMN {column}")
    conn.commit()
    conn.close()

    legacy = cakewalk(db)
    assert not legacy._has_dir_blocks()
    assert not legacy._has_blocks()
    assert normalise(legacy.walk(root, validate="none"), root) == expected
    legacy.close()

    # ...and a rescan must migrate it back onto the fast path.
    upgraded = cakewalk(db)
    upgraded.start_scan(root)
    upgraded.close()
    upgraded = cakewalk(db)
    assert upgraded._has_dir_blocks()
    assert upgraded._has_blocks()
    assert normalise(upgraded.walk(root, validate="none"), root) == expected
    upgraded.close()


def test_missing_projection_is_rebuilt_even_when_nothing_changed(tmp_path):
    """An index from before dir_blocks would otherwise never gain one.

    Relayout is skipped when the tree hash is unchanged, which is exactly the state an
    existing user's cache is in after upgrading. Without a separate trigger the fast path
    would stay unreachable until something on disk happened to move.
    """
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    db = str(tmp_path / "cache.db")

    scanner = cakewalk(db)
    scanner.start_scan(root)
    expected = normalise(scanner.walk(root, validate="none"), root)
    scanner.close()

    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE dir_blocks")
    conn.commit()
    conn.close()

    stale = cakewalk(db)
    assert not stale._has_dir_blocks()
    # Falls back to the fs_nodes block reader, so results are still right.
    assert normalise(stale.walk(root, validate="none"), root) == expected
    stale.close()

    rescanned = cakewalk(db)
    rescanned.start_scan(root)          # nothing on disk has changed
    rescanned.close()

    rebuilt = cakewalk(db)
    assert rebuilt._has_dir_blocks()
    assert normalise(rebuilt.walk(root, validate="none"), root) == expected
    rebuilt.close()


def test_projection_invariants(scanned_default):
    """dir_blocks is a second source of truth for the same tree; it has to agree.

    Checked against fs_nodes rather than against os.walk, so a scanner bug cannot make
    both halves wrong in the same direction and still pass.
    """
    _root, scanner = scanned_default
    conn = scanner._get_conn()

    blocks = conn.execute(
        "SELECT dfs, node_id, parent, name, dnames, fnames, subtree_end "
        "FROM dir_blocks ORDER BY dfs"
    ).fetchall()
    assert blocks

    dfs_ids = [b[0] for b in blocks]
    assert dfs_ids == list(range(len(blocks))), "dfs must be dense, starting at 0"

    directories = conn.execute(
        "SELECT id FROM fs_nodes WHERE is_dir = 1"
    ).fetchall()
    assert {b[1] for b in blocks} == {d[0] for d in directories}, \
        "every directory needs a row, including empty ones"

    by_dfs = {b[0]: b for b in blocks}
    for dfs, node_id, parent, name, dnames, fnames, subtree_end in blocks:
        # The packed names must match what fs_nodes holds for the same directory.
        children = conn.execute(
            "SELECT name, is_dir FROM fs_nodes WHERE parent_id = ? ORDER BY id", (node_id,)
        ).fetchall()
        assert (dnames.split("\0") if dnames else []) == \
            [c[0] for c in children if c[1]]
        assert (fnames.split("\0") if fnames else []) == \
            [c[0] for c in children if not c[1]]

        row_name = conn.execute(
            "SELECT name FROM fs_nodes WHERE id = ?", (node_id,)
        ).fetchone()[0]
        assert name == row_name

        # Pre-order means a subtree is a contiguous run of dfs values, and every row in it
        # descends from this one -- which is what makes a prune a single seek.
        assert subtree_end >= dfs
        descendants = set()
        stack = [dfs]
        while stack:
            current = stack.pop()
            for other in blocks:
                if other[2] == current:
                    descendants.add(other[0])
                    stack.append(other[0])
        assert descendants == set(range(dfs + 1, subtree_end + 1))

        if parent == -1:
            assert conn.execute(
                "SELECT parent_id FROM fs_nodes WHERE id = ?", (node_id,)
            ).fetchone()[0] is None
        else:
            assert by_dfs[parent][1] == conn.execute(
                "SELECT parent_id FROM fs_nodes WHERE id = ?", (node_id,)
            ).fetchone()[0]


def test_readers_agree_exactly(scanned_default):
    """Three layouts, one tree: any divergence is a silent wrong answer."""
    root, scanner = scanned_default
    reference = None
    for reader in READERS:
        probe = cakewalk(scanner.db_location)
        _select_reader(probe, reader)
        for topdown in (True, False):
            got = list(probe.walk(root, topdown=topdown, validate="none"))
            expected = list(os.walk(root, topdown=topdown))
            assert normalise(got, root) == normalise(expected, root), reader
        probe.close()
        if reference is None:
            reference = normalise(os.walk(root), root)
    assert reference


def test_validate_full_actually_uses_the_cache(scanned_default, monkeypatch):
    """VALIDATE_FULL must read the index for directories whose mtime has not moved.

    It seeded its stack with mtime=None for the top directory, and _check_fresh treats
    None as stale -- so the root was declared stale on every walk and the whole thing
    fell through to a live os.walk. The mode was documented as "stat every directory,
    re-read the ones that moved" and was in fact "never use the cache at all". Nothing
    caught it because the results were still correct, just produced the slow way.
    """
    root, scanner = scanned_default

    def forbidden(*args, **kwargs):
        raise AssertionError("validate='full' fell back to a live os.walk")

    monkeypatch.setattr(cw.os, "walk", forbidden)
    for topdown in (True, False):
        got = normalise(scanner.walk(root, topdown=topdown, validate="full"), root)
        assert got, "walk produced nothing"


def test_validate_full_still_sees_a_changed_directory(tmp_path):
    """...and the fix must not cost it the freshness it exists for."""
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    scanner = cakewalk(str(tmp_path / "cache.db"))
    scanner.start_scan(root)
    try:
        # Directory mtimes are compared truncated to whole seconds, so a change made in
        # the same second as the scan is invisible by design. Push the mtime forward
        # explicitly rather than sleeping: it is the comparison being tested, not the
        # clock.
        def touch(path):
            stamp = os.stat(path).st_mtime + 10
            os.utime(path, (stamp, stamp))

        # A file added straight into the walked root: the case validate='root' also sees.
        with open(os.path.join(root, "appeared.txt"), "w") as fh:
            fh.write("new")
        touch(root)
        found = normalise(scanner.walk(root, validate="full"), root)
        assert "appeared.txt" in found[0][2]

        # And one added deeper, which only validate='full' can catch -- the root's own
        # mtime is left alone here, so nothing but a per-directory stat can find it.
        deep = os.path.join(root, "a", "a1")
        with open(os.path.join(deep, "deeper_appeared.txt"), "w") as fh:
            fh.write("new")
        touch(deep)
        by_path = {p: (d, f) for p, d, f in
                   normalise(scanner.walk(root, validate="full"), root)}
        assert "deeper_appeared.txt" in by_path[os.path.join("a", "a1")][1]
    finally:
        scanner.close()
