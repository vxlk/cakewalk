import os
import tempfile
import cakewalk
import pytest

def test_hybrid_jwalk_fallback():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_cakewalk.db")
        cakewalk._default_scanner = cakewalk.cakewalk(db_path)
        
        # Create a simple tree
        os.makedirs(os.path.join(td, "a", "b"))
        with open(os.path.join(td, "a", "file1.txt"), "w") as f: f.write("test")
        
        # Call cakewalk.walk() directly. This should miss the cache and hit jwalk.
        jwalk_results = []
        for root, dirs, files in cakewalk.walk(td):
            dirs.sort()
            files.sort()
            jwalk_results.append((root, list(dirs), list(files)))
            
        # Call os.walk for comparison
        os_results = []
        for root, dirs, files in os.walk(td):
            dirs.sort()
            files.sort()
            os_results.append((root, list(dirs), list(files)))
            
        jwalk_results.sort(key=lambda x: x[0])
        os_results.sort(key=lambda x: x[0])
        
        assert jwalk_results == os_results
        cakewalk._default_scanner.close()
        cakewalk._default_scanner = None

def test_concurrency_no_wipe():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_cakewalk.db")
        cakewalk._default_scanner = cakewalk.cakewalk(db_path)
        
        try:
            dir_a = os.path.join(td, "A")
            dir_b = os.path.join(td, "B")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            
            cakewalk.update_cache(dir_a)
            
            conn = cakewalk._default_scanner._get_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM fs_nodes WHERE name = ?", (dir_a,))
            assert c.fetchone()[0] == 1
            
            cakewalk.update_cache(dir_b)
            
            c.execute("SELECT COUNT(*) FROM fs_nodes WHERE name = ?", (dir_a,))
            assert c.fetchone()[0] == 1
        finally:
            cakewalk._default_scanner.close()
            cakewalk._default_scanner = None

def test_symlink_loop_safety():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_cakewalk.db")
        cakewalk._default_scanner = cakewalk.cakewalk(db_path)
        
        dir_a = os.path.join(td, "A")
        os.makedirs(dir_a)
        
        try:
            import _winapi
            _winapi.CreateJunction(td, os.path.join(dir_a, "loop"))
        except (AttributeError, OSError):
            pytest.skip("Could not create junction point")
            
        cakewalk.update_cache(td)
        results = list(cakewalk.walk(td))
        assert len(results) >= 1
        
        cakewalk._default_scanner.close()
        cakewalk._default_scanner = None
