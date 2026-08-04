import os
import time
import tempfile
import pytest
from fastfs import FastFS

def test_cache_read_speedup():
    """
    Proves that reading from the FastFS SQLite cache via `scanner.walk()` 
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
                            
        scanner = FastFS(db_path)
        
        # 1. Build the cache (Scan the folder for the first time)
        scanner.start_scan(root_dir)
        
        # 2. Time native os.walk() (Physically reads the disk)
        t0 = time.time()
        os_results = list(os.walk(root_dir))
        os_time = time.time() - t0
        
        # 3. Time FastFS.walk() (Reads entirely from the SQLite DB cache)
        t1 = time.time()
        fastfs_results = list(scanner.walk(root_dir))
        fastfs_time = time.time() - t1
        
        scanner.close()
        
        assert len(os_results) == len(fastfs_results)
        
        print(f"\nNative os.walk() time: {os_time:.5f}s")
        print(f"FastFS cache walk time: {fastfs_time:.5f}s")
        
        # Prove that reading from the FastFS cache is significantly faster than hitting the disk
        assert fastfs_time < os_time, "FastFS cache was slower than native disk reads!"
