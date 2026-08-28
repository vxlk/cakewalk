import os
import tempfile
import fastfs
import pytest

def test_hybrid_jwalk_fallback():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_fastfs.db")
        fastfs._default_scanner = fastfs.FastFS(db_path)
        
        # Create a simple tree
        os.makedirs(os.path.join(td, "a", "b"))
        with open(os.path.join(td, "a", "file1.txt"), "w") as f: f.write("test")
        
        # Call fastfs.walk() directly. This should miss the cache and hit jwalk.
        jwalk_results = []
        for root, dirs, files in fastfs.walk(td):
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
        fastfs._default_scanner.close()
        fastfs._default_scanner = None

def test_concurrency_no_wipe():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_fastfs.db")
        fastfs._default_scanner = fastfs.FastFS(db_path)
        
        try:
            dir_a = os.path.join(td, "A")
            dir_b = os.path.join(td, "B")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            
            fastfs.update_cache(dir_a)
            
            conn = fastfs._default_scanner._get_conn()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM fs_nodes WHERE name = ?", (dir_a,))
            assert c.fetchone()[0] == 1
            
            fastfs.update_cache(dir_b)
            
            c.execute("SELECT COUNT(*) FROM fs_nodes WHERE name = ?", (dir_a,))
            assert c.fetchone()[0] == 1
        finally:
            fastfs._default_scanner.close()
            fastfs._default_scanner = None

def test_symlink_loop_safety():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_fastfs.db")
        fastfs._default_scanner = fastfs.FastFS(db_path)
        
        dir_a = os.path.join(td, "A")
        os.makedirs(dir_a)
        
        try:
            import _winapi
            _winapi.CreateJunction(td, os.path.join(dir_a, "loop"))
        except (AttributeError, OSError):
            pytest.skip("Could not create junction point")
            
        fastfs.update_cache(td)
        results = list(fastfs.walk(td))
        assert len(results) >= 1
        
        fastfs._default_scanner.close()
        fastfs._default_scanner = None
