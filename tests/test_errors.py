import os
import tempfile
import sqlite3
import fastfs
import pytest

def test_db_corruption_recovery():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "corrupt.db")
        fastfs._default_scanner = fastfs.FastFS(db_path)
        
        # Write garbage to the DB to simulate severe corruption
        with open(db_path, "wb") as f:
            f.write(os.urandom(4096))
            
        test_dir = os.path.join(td, "test")
        os.makedirs(test_dir)
        
        # When walk is called, it should catch sqlite3.DatabaseError,
        # delete the corrupt db to trigger auto-recovery next time, 
        # and gracefully fallback to jwalk
        results = list(fastfs.walk(test_dir))
        assert len(results) == 1
        assert results[0][0] == test_dir
        
        # DB should have been deleted for recovery
        assert not os.path.exists(db_path)
        
        fastfs._default_scanner.close()
        fastfs._default_scanner = None

def test_onerror_callback_fallback():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test.db")
        fastfs._default_scanner = fastfs.FastFS(db_path)
        
        test_dir = os.path.join(td, "test")
        os.makedirs(test_dir)
        
        def my_onerror(e):
            pass
            
        # Passing onerror forces fallback to native os.walk which fully supports Python callbacks
        # We can't easily assert exactly which path was taken from the output, but it shouldn't crash
        results = list(fastfs.walk(test_dir, onerror=my_onerror))
        assert len(results) == 1
        
        fastfs._default_scanner.close()
        fastfs._default_scanner = None
