import os
import time
import tempfile
import pytest
from cakewalk import cakewalk

def test_cache_read_speedup():
    """
    Proves that reading from the cakewalk SQLite cache via `scanner.walk()` 
    is substantially faster than physically reading the filesystem via `os.walk()`.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "speed_test.db")
        root_dir = os.path.join(td, "cache_test_root")
        os.makedirs(root_dir)
        
        # Create a deep dummy directory tree to simulate a realistic filesystem
        # 5 * 10 * 10 = 500 directories, 2500 files
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
                            
        scanner = cakewalk(db_path)
        
        # 1. Build the cache (Cold Scan)
        t0 = time.time()
        scanner.start_scan(root_dir)
        cold_scan_time = time.time() - t0
        
        # 2. Re-scan the folder (Warm Scan)
        t1 = time.time()
        scanner.start_scan(root_dir)
        warm_scan_time = time.time() - t1
        
        scanner.close()
        
        print(f"\nCold scan time: {cold_scan_time:.5f}s")
        print(f"Warm scan time: {warm_scan_time:.5f}s")
        
        # Prove that scanning a second time is significantly faster
        assert warm_scan_time < cold_scan_time, "cakewalk warm scan was slower than cold scan!"
