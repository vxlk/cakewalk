"""The index is a documented SQLite database, and that is a supported way to use it.

These tests pin the three primitives the SQL surface rests on: a read-only connection,
the subtree id range that makes whole-subtree questions a primary-key scan, and the
reverse lookup from a row id back to a path.
"""
import os
import sqlite3

import pytest

from cakewalk import cakewalk


SIZES = {
    ("a", "a1", "one.txt"): 100,
    ("a", "a1", "two.log"): 200,
    ("a", "a2", "three.txt"): 300,
    ("b", "four.log"): 400,
    ("top.txt",): 500,
}


def build_tree(root):
    for parts, size in SIZES.items():
        target = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"x" * size)


@pytest.fixture
def scanned(tmp_path):
    root = str(tmp_path / "tree")
    os.makedirs(root)
    build_tree(root)
    scanner = cakewalk(str(tmp_path / "cache.db"))
    scanner.start_scan(root)
    yield root, scanner
    scanner.close()


def test_connect_is_read_only(scanned):
    root, scanner = scanned
    conn = scanner.connect()
    try:
        assert conn.execute("SELECT count(*) FROM fs_nodes").fetchone()[0] > 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM fs_nodes")
    finally:
        conn.close()


def test_connect_before_any_scan(tmp_path):
    scanner = cakewalk(str(tmp_path / "missing.db"))
    with pytest.raises(FileNotFoundError):
        scanner.connect()


def test_subtree_range_selects_exactly_the_descendants(scanned):
    """Every directory's descendants must be one contiguous id run -- the whole point."""
    root, scanner = scanned
    conn = scanner.connect()
    try:
        for dirpath, _dirnames, _filenames in os.walk(root):
            lo, hi = scanner.subtree_range(dirpath)
            got = {
                scanner.path_of(node_id)
                for (node_id,) in conn.execute(
                    "SELECT id FROM fs_nodes WHERE id BETWEEN ? AND ?", (lo, hi)
                )
            }
            expected = set()
            for sub, dirnames, filenames in os.walk(dirpath):
                for name in dirnames + filenames:
                    expected.add(os.path.join(sub, name))
            assert got == expected, dirpath
    finally:
        conn.close()


def test_subtree_range_of_empty_directory_matches_nothing(scanned):
    root, scanner = scanned
    empty = os.path.join(root, "empty")
    os.makedirs(empty)
    scanner.close()

    again = cakewalk(scanner.db_location)
    again.start_scan(root)
    lo, hi = again.subtree_range(empty)
    assert lo > hi
    conn = again.connect()
    try:
        assert conn.execute(
            "SELECT count(*) FROM fs_nodes WHERE id BETWEEN ? AND ?", (lo, hi)
        ).fetchone()[0] == 0
    finally:
        conn.close()
        again.close()


def test_subtree_range_unknown_path(scanned):
    root, scanner = scanned
    assert scanner.subtree_range(os.path.join(root, "nope")) is None


def test_aggregate_over_a_range_matches_the_filesystem(scanned):
    """A range scan is only useful if it agrees with the tree it describes."""
    root, scanner = scanned
    lo, hi = scanner.subtree_range(os.path.join(root, "a"))
    conn = scanner.connect()
    try:
        total, count = conn.execute(
            "SELECT ifnull(sum(size), 0), count(*) FROM fs_nodes "
            "WHERE id BETWEEN ? AND ? AND is_dir = 0",
            (lo, hi),
        ).fetchone()
    finally:
        conn.close()

    expected = {k: v for k, v in SIZES.items() if k[0] == "a"}
    assert count == len(expected)
    assert total == sum(expected.values())


def test_path_of_round_trips(scanned):
    root, scanner = scanned
    conn = scanner.connect()
    try:
        rows = conn.execute("SELECT id, name FROM fs_nodes WHERE is_dir = 0").fetchall()
    finally:
        conn.close()

    on_disk = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        on_disk.update(os.path.join(dirpath, name) for name in filenames)

    assert {scanner.path_of(node_id) for node_id, _name in rows} == on_disk
    for node_id, name in rows:
        assert os.path.basename(scanner.path_of(node_id)) == name


def test_path_of_root_and_missing(scanned):
    root, scanner = scanned
    conn = scanner.connect()
    try:
        root_id = conn.execute(
            "SELECT id FROM fs_nodes WHERE parent_id IS NULL"
        ).fetchone()[0]
        highest = conn.execute("SELECT max(id) FROM fs_nodes").fetchone()[0]
    finally:
        conn.close()
    assert scanner.path_of(root_id) == root
    assert scanner.path_of(highest + 1000) is None


def test_pattern_search_beats_walking_to_the_same_answer(scanned):
    """The documented recipe has to return what walking returns."""
    root, scanner = scanned
    lo, hi = scanner.subtree_range(root)
    conn = scanner.connect()
    try:
        found = {
            scanner.path_of(node_id)
            for (node_id,) in conn.execute(
                "SELECT id FROM fs_nodes WHERE id BETWEEN ? AND ? "
                "AND is_dir = 0 AND name LIKE '%.log'",
                (lo, hi),
            )
        }
    finally:
        conn.close()

    walked = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        walked.update(
            os.path.join(dirpath, name) for name in filenames if name.endswith(".log")
        )
    assert found == walked


def test_subtree_range_needs_the_layout(scanned):
    """Databases written before the layout existed have no ranges to hand out."""
    root, scanner = scanned
    scanner.close()

    conn = sqlite3.connect(scanner.db_location)
    for column in ("child_start", "child_end", "subtree_last"):
        conn.execute(f"ALTER TABLE fs_nodes DROP COLUMN {column}")
    conn.commit()
    conn.close()

    legacy = cakewalk(scanner.db_location)
    assert legacy.subtree_range(root) is None
    legacy.close()
