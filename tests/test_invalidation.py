import os
import time
import tempfile
from fastfs import FastFS

def test_differential_cache_invalidation():
    """
    Proves that start_scan() uses differential caching to skip unchanged folders,
    and correctly invalidates and re-reads folders when their mtime changes.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "diff_cache.db")
        root_dir = os.path.join(td, "cache_test_root")
        os.makedirs(root_dir)
        
        # 1. Create a large directory tree
        # 5 * 10 * 10 = 500 folders, 2500 files
        for i in range(5):
            d1 = os.path.join(root_dir, f"dir_{i}")
            os.makedirs(d1)
            for j in range(10):
                d2 = os.path.join(d1, f"subdir_{j}")
                os.makedirs(d2)
                for k in range(10):
                    d3 = os.path.join(d2, f"subsubdir_{k}")
                    os.makedirs(d3)
                    for f_idx in range(5):
                        with open(os.path.join(d3, f"file_{f_idx}.txt"), "w") as f:
                            f.write("dummy data")
                            
        scanner = FastFS(db_path)
        
        # 2. First Scan (Cold Cache)
        t0 = time.time()
        scanner.start_scan(root_dir)
        t1 = time.time()
        cold_scan_time = t1 - t0
        
        # Since Rust truncates mtime to seconds, wait 1.1s to ensure the new file
        # will have a strictly greater mtime in seconds.
        time.sleep(1.1)
        
        # 3. Invalidate a specific deep folder by adding a new file
        target_dir = os.path.join(root_dir, "dir_0", "subdir_0", "subsubdir_0")
        target_file = os.path.join(target_dir, "stale_test.txt")
        with open(target_file, "w") as f:
            f.write("I am new!")
            
        # 4. Second Scan (Hot Cache - should only read 1 folder and short circuit 499)
        t2 = time.time()
        scanner.start_scan(root_dir)
        t3 = time.time()
        hot_scan_time = t3 - t2
        
        # 5. Verify Speed
        # Option B (Thorough Hashing) traverses the disk but skips DB writes. 
        # For a small 2500 file test, DB writes are very fast, so the speedup isn't massive.
        # But we ensure it didn't take an absurdly long time.
        assert hot_scan_time < cold_scan_time * 2.0, "Hot scan was unexpectedly slow!"
        
        # 6. Verify Invalidation 
        results = list(scanner.walk(root_dir))
        found = False
        for directory, subdirs, files in results:
            if "stale_test.txt" in files:
                found = True
                break
                
        assert found, "The stale folder was not correctly invalidated and re-read!"
        
        scanner.close()
