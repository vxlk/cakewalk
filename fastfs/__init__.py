import sqlite3
import os

try:
    # Try importing the compiled Rust extension
    from fastfs._fastfs import run_scan
except ImportError:
    # Fallback if installed via Maturin directly into site-packages
    from _fastfs import run_scan

class FastFS:
    def __init__(self, db_location: str):
        self.db_location = db_location
        self.conn = None

    def start_scan(self):
        """
        Triggers the Rust scanner to detect all drives, calculate Merkle hashes,
        and build the SQLite adjacency list at self.db_location.
        """
        run_scan(self.db_location)

    def _get_conn(self):
        if self.conn is None:
            if not os.path.exists(self.db_location):
                raise FileNotFoundError(f"Database not found at {self.db_location}. Call start_scan() first.")
            self.conn = sqlite3.connect(self.db_location)
            # Optimize read performance
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            self.conn.execute("PRAGMA cache_size = -64000;") # 64MB cache
        return self.conn

    def walk(self, top: str):
        """
        Mimics os.walk() but reads entirely from the SQLite database.
        Yields (dirpath, dirnames, filenames).
        """
        conn = self._get_conn()
        
        # We need to find the top folder's ID first.
        # SQLite recursive CTE to resolve paths is possible, but resolving the top path 
        # iteratively down from the root is easy.
        
        parts = top.split(os.sep)
        if not parts:
            return
            
        # Standardize the drive letter (e.g. C: -> C:\)
        if len(parts[0]) == 2 and parts[0][1] == ':':
            parts[0] = parts[0] + '\\'
            
        # Find the root drive node
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id IS NULL", (parts[0],))
        row = cursor.fetchone()
        
        if not row:
            return # Top path not found in DB
            
        current_id = row[0]
        
        # Traverse down to the target folder
        for part in parts[1:]:
            if not part: # Handle trailing slashes
                continue
            cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id = ? AND is_dir = 1", (part, current_id))
            row = cursor.fetchone()
            if not row:
                return # Path doesn't exist in DB
            current_id = row[0]
            
        # Now we have the starting ID, we can do an iterative breadth-first search to yield results.
        # We use a stack to manage traversal, similar to os.walk top-down.
        
        stack = [(top, current_id)]
        
        while stack:
            current_path, node_id = stack.pop()
            
            # Fetch all children of this node
            cursor.execute("SELECT name, is_dir FROM fs_nodes WHERE parent_id = ?", (node_id,))
            children = cursor.fetchall()
            
            dirnames = []
            filenames = []
            
            for name, is_dir in children:
                if is_dir:
                    dirnames.append(name)
                else:
                    filenames.append(name)
                    
            yield current_path, dirnames, filenames
            
            # Push directories onto the stack (in reverse so they are processed in order)
            for dirname in reversed(dirnames):
                child_path = os.path.join(current_path, dirname)
                cursor.execute("SELECT id FROM fs_nodes WHERE parent_id = ? AND name = ? AND is_dir = 1", (node_id, dirname))
                child_row = cursor.fetchone()
                if child_row:
                    stack.append((child_path, child_row[0]))
                    
    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
