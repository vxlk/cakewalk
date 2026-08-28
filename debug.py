import os
import tempfile
import sqlite3
from cakewalk import cakewalk

def debug():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test.db")
        dummy_dir = os.path.join(td, "CaseTest")
        os.makedirs(dummy_dir)
        with open(os.path.join(dummy_dir, "file.txt"), "w") as f:
            f.write("test")
            
        print("dummy_dir:", repr(dummy_dir))
        
        scanner = cakewalk(db_path)
        scanner.start_scan(dummy_dir)
        
        c = sqlite3.connect(db_path).cursor()
        print("DB roots:", c.execute("SELECT id, name FROM fs_nodes WHERE parent_id IS NULL").fetchall())
        
        results = list(scanner.walk(dummy_dir))
        print("walk results:", results)
        scanner.close()

if __name__ == "__main__":
    debug()
