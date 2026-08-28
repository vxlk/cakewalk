import os
import tempfile
from hypothesis import given, settings, strategies as st
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
).map(lambda s: s.strip('. ')).filter(lambda s: bool(s) and s.upper() not in reserved_windows_names)

def trees():
    return st.recursive(
        st.none(),
        lambda children: st.dictionaries(valid_names, children, max_size=5)
    )

def realize_tree(base_path, tree):
    if tree is None:
        pass
    else:
        for name, subtree in tree.items():
            path = os.path.join(base_path, name)
            if subtree is None:
                with open(path, "w") as f:
                    f.write("test")
            else:
                os.makedirs(path, exist_ok=True)
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
