import sqlite3
import os

try:
    from fastfs._fastfs import run_scan
except ImportError:
    from _fastfs import run_scan

class FastFS:
    def __init__(self, db_location: str):
        self.db_location = db_location
        self.conn = None

    def start_scan(self, root: str = None, background: bool = False):
        if root:
            if len(root) == 2 and root[1] == ':':
                root += '\\'
            elif len(root) > 2 and root[1] == ':' and root[-1] == '\\' and root[-2] == '\\':
                root = root.rstrip('\\') + '\\'
        run_scan(self.db_location, root, background)

    def _get_conn(self):
        if self.conn is None:
            if not os.path.exists(self.db_location):
                raise FileNotFoundError(f"Database not found at {self.db_location}. Call start_scan() first.")
            self.conn = sqlite3.connect(self.db_location)
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            self.conn.execute("PRAGMA cache_size = -64000;")
        return self.conn

    def walk(self, top: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # 1. Try exact match for root node
        cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id IS NULL", (top,))
        row = cursor.fetchone()
        
        print("DEBUG exact match:", top, "->", row)
        
        if row:
            current_id = row[0]
        else:
            # 2. Try prefix match
            cursor.execute("SELECT id, name FROM fs_nodes WHERE parent_id IS NULL")
            roots = cursor.fetchall()
            print("DEBUG all roots:", roots)
            
            matching_root = None
            # Find the longest matching root
            roots.sort(key=lambda x: len(x[1]), reverse=True)
            for r_id, r_name in roots:
                if top == r_name or top.startswith(r_name.rstrip(os.sep) + os.sep):
                    matching_root = (r_id, r_name)
                    break
                    
            if not matching_root:
                return
                
            current_id, r_name = matching_root
            
            if top != r_name:
                r_name_norm = r_name.rstrip(os.sep) + os.sep
                remainder = top[len(r_name_norm):]
                if remainder:
                    for part in remainder.split(os.sep):
                        if not part: continue
                        cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id = ? AND is_dir = 1", (part, current_id))
                        row = cursor.fetchone()
                        if not row:
                            return
                        current_id = row[0]
                        
        stack = [(top, current_id)]
        
        while stack:
            current_path, node_id = stack.pop()
            
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
