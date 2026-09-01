import os
import sqlite3
import pytest
from hypothesis import assume, given, settings, strategies as st
import tempfile

from cakewalk import cakewalk
from cakewalk import query

# Re-use tree strategies from test_properties for property tests
from tests.test_properties import trees, realize_tree

SIZES = {
    ("a", "a1", "one.txt"): 100,
    ("a", "a1", "two.log"): 200,
    ("a", "a2", "three.txt"): 300,
    ("b", "four.log"): 400,
    ("top.txt",): 500,
    ("empty_file.txt",): 0,
    ("no_ext_file",): 600,
}

def build_tree(root):
    for i, (parts, size) in enumerate(SIZES.items()):
        target = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"x" * size)
        
        # Set predictable mtime (in seconds for os.utime)
        os.utime(target, (10000 + i * 1000, 10000 + i * 1000))

@pytest.fixture
def scanned(tmp_path):
    root = str(tmp_path / "tree")
    os.makedirs(root)
    os.makedirs(os.path.join(root, "empty_dir"))
    os.makedirs(os.path.join(root, "a", "empty_sub_dir"))
    build_tree(root)
    scanner = cakewalk(str(tmp_path / "cache.db"))
    scanner.start_scan(root)
    yield root, scanner
    scanner.close()

def test_missing_cache_raises_error(tmp_path):
    scanner = cakewalk(str(tmp_path / "missing.db"))
    with pytest.raises(FileNotFoundError):
        query.largest_files(str(tmp_path), scanner=scanner)

def test_unindexed_path_raises_error(scanned):
    root, scanner = scanned
    with pytest.raises(ValueError, match="Path not found in index"):
        query.largest_files(os.path.join(root, "nonexistent"), scanner=scanner)

def test_largest_files(scanned):
    root, scanner = scanned
    # Limit 3
    res = query.largest_files(root, limit=3, scanner=scanner)
    assert len(res) == 3
    assert os.path.basename(res[0][0]) == "no_ext_file" and res[0][1] == 600
    assert os.path.basename(res[1][0]) == "top.txt" and res[1][1] == 500
    assert os.path.basename(res[2][0]) == "four.log" and res[2][1] == 400

    # Limit larger than total
    res = query.largest_files(root, limit=100, scanner=scanner)
    assert len(res) == 7

    # From subdirectory
    res = query.largest_files(os.path.join(root, "a"), limit=2, scanner=scanner)
    assert len(res) == 2
    assert os.path.basename(res[0][0]) == "three.txt" and res[0][1] == 300
    assert os.path.basename(res[1][0]) == "two.log" and res[1][1] == 200

def test_heaviest_children(scanned):
    root, scanner = scanned
    res = query.heaviest_children(root, scanner=scanner)
    
    # Children of root: a (dir), b (dir), empty_dir (dir), top.txt (file), empty_file.txt (file), no_ext_file (file)
    names = [row[0] for row in res]
    assert "a" in names
    assert "no_ext_file" in names
    
    # 'a' has size 600 (one.txt + two.log + three.txt)
    size_of_a = next(row[1] for row in res if row[0] == "a")
    assert size_of_a == 600

def test_by_extension(scanned):
    root, scanner = scanned
    res = query.by_extension(root, scanner=scanner)
    # extensions: .txt, .log. no_ext_file is skipped due to LIKE '%.%'
    exts = {row[0]: (row[1], row[2]) for row in res}
    
    # .txt: one.txt(100) + three.txt(300) + top.txt(500) + empty_file.txt(0) = 4 files, 900 bytes
    assert exts[".txt"] == (4, 900)
    # .log: two.log(200) + four.log(400) = 2 files, 600 bytes
    assert exts[".log"] == (2, 600)
    
    assert ".ext" not in exts
    assert "no_ext_file" not in exts

def test_modified_since(scanned):
    root, scanner = scanned
    # mtimes are around 10000000000000 nanoseconds, wait os.utime takes seconds.
    # The scan saves last_modified in nanoseconds.
    # Let's get the mtime of top.txt
    import stat
    mtime_ns = os.stat(os.path.join(root, "top.txt")).st_mtime_ns
    
    res = query.modified_since(root, mtime_ns, scanner=scanner)
    # files modified strict-after top.txt
    assert len(res) == 2  # empty_file.txt and no_ext_file were built after
    assert any("no_ext_file" in p for p in res)

def test_largest_dirs(scanned):
    root, scanner = scanned
    # Min size 500
    res = query.largest_dirs(root, min_size=500, scanner=scanner)
    # The root itself ("tree") is not in its own subtree_range.
    # Descendants: 'a' contains 600, 'b' contains 400.
    dir_names = {os.path.basename(row[0]): row[1] for row in res}
    assert "tree" not in dir_names
    assert dir_names["a"] == 600
    assert "b" not in dir_names # b is 400

def test_empty_directories(scanned):
    root, scanner = scanned
    res = query.empty_directories(root, scanner=scanner)
    basenames = [os.path.basename(p) for p in res]
    assert "empty_dir" in basenames
    assert "empty_sub_dir" in basenames
    assert "a" not in basenames

def test_directories_with_most_files(scanned):
    root, scanner = scanned
    res = query.directories_with_most_files(root, limit=5, scanner=scanner)
    basenames = [os.path.basename(row[0]) for row in res]
    # Root not included in subtree_range. 'a' has 3 files.
    assert basenames[0] == "a"    # 3 files
    
    file_counts = {os.path.basename(row[0]): row[1] for row in res}
    assert file_counts["a"] == 3
    assert file_counts["a1"] == 2

def test_oldest_files(scanned):
    root, scanner = scanned
    res = query.oldest_files(root, limit=2, scanner=scanner)
    # Built first: one.txt, then two.log
    assert os.path.basename(res[0][0]) == "one.txt"
    assert os.path.basename(res[1][0]) == "two.log"

def test_find_by_pattern(scanned):
    root, scanner = scanned
    res = query.find_by_pattern(root, "%.log", scanner=scanner)
    assert len(res) == 2
    assert all(p.endswith(".log") for p in res)
    
    res = query.find_by_pattern(root, "%no_match%", scanner=scanner)
    assert len(res) == 0

# Property-based tests
@settings(max_examples=30, deadline=None)
@given(trees())
def test_property_largest_files(tree):
    if tree is None: return
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "cache.db")
        test_root = os.path.join(td, "root")
        os.makedirs(test_root)
        realize_tree(test_root, tree)
        
        scanner = cakewalk(db_path)
        scanner.start_scan(test_root)
        
        # Native python os.walk equivalent
        all_files = []
        for dpath, _, fnames in os.walk(test_root):
            for f in fnames:
                path = os.path.join(dpath, f)
                try:
                    all_files.append((path, os.stat(path).st_size))
                except OSError:
                    pass
                    
        all_files.sort(key=lambda x: (-x[1], x[0])) # Sort by size desc, then path
        expected = all_files[:5]
        
        # SQL query
        got = query.largest_files(test_root, limit=5, scanner=scanner)
        # Because sizes might be tied and SQL doesn't define tie-breaking order without an extra column, 
        # we check the sizes match if they are completely identical.
        assert [s for p, s in got] == [s for p, s in expected]
        scanner.close()

