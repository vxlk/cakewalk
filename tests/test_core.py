import os
import tempfile
import pytest
from cakewalk import cakewalk

def test_missing_db():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test.db")
        scanner = cakewalk(db_path)
        with pytest.raises(FileNotFoundError):
            list(scanner.walk("C:\\"))
        scanner.close()

def test_missing_path():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test.db")
        dummy_dir = os.path.join(td, "dummy")
        os.makedirs(dummy_dir)
        
        scanner = cakewalk(db_path)
        scanner.start_scan(dummy_dir)
        
        results = list(scanner.walk(os.path.join(dummy_dir, "does_not_exist")))
        assert len(results) == 0
        scanner.close()

def test_targeted_scan_case_sensitivity():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test.db")
        dummy_dir = os.path.join(td, "CaseTest")
        os.makedirs(dummy_dir)
        with open(os.path.join(dummy_dir, "file.txt"), "w") as f:
            f.write("test")
            
        scanner = cakewalk(db_path)
        scanner.start_scan(dummy_dir)
        
        results = list(scanner.walk(dummy_dir))
        assert len(results) == 1
        assert results[0][2] == ["file.txt"]
        scanner.close()
