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


@pytest.fixture
def scanned(tmp_path):
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


def test_unpruned_walk_never_reseeks(scanned, monkeypatch):
    """The point of the layout: a full walk is one sequential pass, no re-queries.

    A regression here would not change any result, only make every walk slow again, so it
    needs its own assertion.
    """
    root, scanner = scanned
    made = []
    original = cw._BlockReader

    class Probe(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(cw, "_BlockReader", Probe)

    list(scanner.walk(root, validate="none"))
    assert made, "the block reader was not used at all"
    # One execute is needed to start the cursor; anything beyond that is a re-seek.
    assert made[-1].seeks == 1

    made.clear()
    for _dirpath, dirnames, _filenames in scanner.walk(root, validate="none"):
        if "a1" in dirnames:
            dirnames.remove("a1")
    assert made[-1].seeks > 1, "pruning should seek past the skipped subtree"


def test_layout_invariants(scanned):
    """Whatever the reader assumes about the layout, the scanner must guarantee."""
    root, scanner = scanned
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


def test_unchanged_rescan_preserves_layout(scanned):
    """Relayout is a full table rewrite, so it must not run when nothing changed."""
    root, scanner = scanned
    query = "SELECT id, name, child_start, child_end, subtree_last FROM fs_nodes ORDER BY id"
    before = scanner._get_conn().execute(query).fetchall()
    scanner.close()

    again = cakewalk(scanner.db_location)
    again.start_scan(root)
    assert again._get_conn().execute(query).fetchall() == before
    again.close()


def test_change_triggers_relayout(scanned):
    root, scanner = scanned
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
    for column in ("child_start", "child_end", "subtree_last"):
        conn.execute(f"ALTER TABLE fs_nodes DROP COLUMN {column}")
    conn.commit()
    conn.close()

    legacy = cakewalk(db)
    assert not legacy._has_blocks()
    assert normalise(legacy.walk(root, validate="none"), root) == expected
    legacy.close()

    # ...and a rescan must migrate it back onto the fast path.
    upgraded = cakewalk(db)
    upgraded.start_scan(root)
    upgraded.close()
    upgraded = cakewalk(db)
    assert upgraded._has_blocks()
    assert normalise(upgraded.walk(root, validate="none"), root) == expected
    upgraded.close()
