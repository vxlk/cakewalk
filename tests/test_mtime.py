import os
import time
import tempfile

def test_directory_mtime_changes():
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "testdir")
        os.makedirs(d)
        
        t1 = os.path.getmtime(d)
        time.sleep(0.1)
        
        f = os.path.join(d, "file.txt")
        with open(f, "w") as fp:
            fp.write("hello")
            
        t2 = os.path.getmtime(d)
        time.sleep(0.1)
        
        with open(f, "w") as fp:
            fp.write("world")
            
        t3 = os.path.getmtime(d)
        time.sleep(0.1)
        
        os.remove(f)
        t4 = os.path.getmtime(d)
        
        # Directory mtime changes on file creation
        assert t2 != t1, f"Directory mtime did not change on file creation (t1={t1}, t2={t2})"
        # Directory mtime does not change on file modification (contents only)
        assert t3 == t2, f"Directory mtime changed on file modification (t2={t2}, t3={t3})"
        # Directory mtime changes on file deletion
        assert t4 != t3, f"Directory mtime did not change on file deletion (t3={t3}, t4={t4})"
