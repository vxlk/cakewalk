import os
import time
import tempfile

with tempfile.TemporaryDirectory() as td:
    d = os.path.join(td, "testdir")
    os.makedirs(d)
    
    t1 = os.path.getmtime(d)
    print("Initial dir mtime:", t1)
    time.sleep(0.1)
    
    f = os.path.join(d, "file.txt")
    with open(f, "w") as fp:
        fp.write("hello")
        
    t2 = os.path.getmtime(d)
    print("After file create:", t2, "Changed:", t2 != t1)
    time.sleep(0.1)
    
    with open(f, "w") as fp:
        fp.write("world")
        
    t3 = os.path.getmtime(d)
    print("After file modify:", t3, "Changed:", t3 != t2)
    time.sleep(0.1)
    
    os.remove(f)
    t4 = os.path.getmtime(d)
    print("After file delete:", t4, "Changed:", t4 != t3)
