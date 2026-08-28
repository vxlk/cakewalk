import sqlite3
import os
import tempfile
import atexit
from typing import Generator, Tuple, List, Optional

try:
    from cakewalk._cakewalk import run_scan
except ImportError:
    from _cakewalk import run_scan

class cakewalkDirEntry:
    __slots__ = ('name', 'path', '_is_dir', '_stat_cache')

    def __init__(self, name: str, path: str, is_dir: bool):
        self.name = name
        self.path = path
        self._is_dir = is_dir
        self._stat_cache = None

    def inode(self) -> int:
        return self.stat().st_ino

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return bool(self._is_dir)

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return not bool(self._is_dir)

    def is_symlink(self) -> bool:
        return False

    def stat(self, follow_symlinks: bool = True):
        if self._stat_cache is None:
            self._stat_cache = os.stat(self.path, follow_symlinks=follow_symlinks)
        return self._stat_cache

    def __fspath__(self) -> str:
        return self.path

    def __repr__(self) -> str:
        return f"<cakewalkDirEntry: {self.name!r}>"

class cakewalk:
    def __init__(self, db_location: str):
        self.db_location = db_location
        self.conn = None

    def start_scan(self, root: Optional[str] = None, background: bool = False):
        if root:
            if len(root) == 2 and root[1] == ':':
                root += '\\'
            elif len(root) > 2 and root[1] == ':' and root[-1] == '\\' and root[-2] == '\\':
                root = root.rstrip('\\') + '\\'
        run_scan(self.db_location, root, background=background)

    def _get_conn(self):
        if self.conn is None:
            if not os.path.exists(self.db_location):
                raise FileNotFoundError(f"Database not found at {self.db_location}. Call start_scan() first.")
            self.conn = sqlite3.connect(self.db_location)
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            self.conn.execute("PRAGMA cache_size = -64000;")
        return self.conn

    def _get_node_id(self, cursor, path: str):
        cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id IS NULL", (path,))
        row = cursor.fetchone()
        
        if row:
            return row[0]
            
        cursor.execute("SELECT id, name FROM fs_nodes WHERE parent_id IS NULL")
        roots = cursor.fetchall()
        
        matching_root = None
        roots.sort(key=lambda x: len(x[1]), reverse=True)
        for r_id, r_name in roots:
            if path == r_name or path.startswith(r_name.rstrip(os.sep) + os.sep):
                matching_root = (r_id, r_name)
                break
                
        if not matching_root:
            return None
            
        current_id, r_name = matching_root
        
        if path != r_name:
            r_name_norm = r_name.rstrip(os.sep) + os.sep
            remainder = path[len(r_name_norm):]
            if remainder:
                for part in remainder.split(os.sep):
                    if not part: continue
                    cursor.execute("SELECT id FROM fs_nodes WHERE name = ? AND parent_id = ? AND is_dir = 1", (part, current_id))
                    row = cursor.fetchone()
                    if not row:
                        return None
                    current_id = row[0]
                    
        return current_id

    def walk(self, top: str, topdown: bool = True, onerror = None, followlinks: bool = False):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        current_id = self._get_node_id(cursor, top)
        if current_id is None:
            return
            
        yield from self._walk_recursive(top, current_id, topdown, onerror, followlinks)

    def _walk_recursive(self, current_path, node_id, topdown, onerror, followlinks):
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT name, is_dir FROM fs_nodes WHERE parent_id = ?", (node_id,))
            children = cursor.fetchall()
        except Exception as err:
            if onerror is not None:
                onerror(err)
            return

        dirnames = []
        filenames = []
        
        for name, is_dir in children:
            if is_dir:
                dirnames.append(name)
            else:
                filenames.append(name)
                
        if topdown:
            yield current_path, dirnames, filenames
            
        for dirname in dirnames:
            child_path = os.path.join(current_path, dirname)
            cursor.execute("SELECT id FROM fs_nodes WHERE parent_id = ? AND name = ? AND is_dir = 1", (node_id, dirname))
            child_row = cursor.fetchone()
            if child_row:
                yield from self._walk_recursive(child_path, child_row[0], topdown, onerror, followlinks)
                
        if not topdown:
            yield current_path, dirnames, filenames

    def scandir(self, path: str = '.'):
        if path == '.':
            path = os.getcwd()
            
        conn = self._get_conn()
        cursor = conn.cursor()
        
        current_id = self._get_node_id(cursor, path)
        if current_id is None:
            raise FileNotFoundError(f"Path not found in cakewalk: {path}")
            
        cursor.execute("SELECT name, is_dir FROM fs_nodes WHERE parent_id = ?", (current_id,))
        for name, is_dir in cursor.fetchall():
            yield cakewalkDirEntry(name, os.path.join(path, name), is_dir)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

_default_scanner = None

def _get_default_scanner(path: str) -> cakewalk:
    global _default_scanner
    if _default_scanner is None:
        db_path = os.path.join(tempfile.gettempdir(), "cakewalk_default_v1.db")
        _default_scanner = cakewalk(db_path)
        atexit.register(_default_scanner.close)
    
    return _default_scanner

def update_cache(path: Optional[str] = None, background: bool = False):
    """Manually update the cache snapshot for the given path (or all mapped drives if None)."""
    scanner = _get_default_scanner(path or "ROOT")
    if path is not None:
        path = os.path.abspath(path)
    scanner.start_scan(path, background=background)

class cakewalkScandirIterator:
    def __init__(self, scanner, path):
        self.scanner = scanner
        self.path = path
        self._gen = self.scanner.scandir(self.path)

    def __iter__(self):
        return self._gen

    def __next__(self):
        return next(self._gen)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self._gen.close()

def walk(top: str, topdown: bool = True, onerror = None, followlinks: bool = False):
    scanner = _get_default_scanner(top)
    
    current_id = None
    try:
        conn = scanner._get_conn()
        cursor = conn.cursor()
        current_id = scanner._get_node_id(cursor, top)
    except (FileNotFoundError, sqlite3.Error) as e:
        # If DB is corrupted or permission denied, attempt to delete for auto-recovery on next scan
        if isinstance(e, sqlite3.DatabaseError) and os.path.exists(scanner.db_location):
            if scanner.conn is not None:
                scanner.conn.close()
                scanner.conn = None
            try:
                os.remove(scanner.db_location)
            except OSError:
                pass
    
    if current_id is not None:
        # Cache hit: 0-IO SQLite traversal
        yield from scanner._walk_recursive(top, current_id, topdown, onerror, followlinks)
    else:
        # Cache miss: Fallback
        if onerror is not None:
            # jwalk doesn't support Python callbacks easily, fallback to native os.walk
            yield from os.walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks)
        else:
            # blazing-fast native jwalk traversal
            from cakewalk._cakewalk import live_walk
            yield from live_walk(top)

def scandir(path: str = '.'):
    if path == '.':
        path = os.getcwd()
    
    scanner = _get_default_scanner(path)
    
    try:
        conn = scanner._get_conn()
        cursor = conn.cursor()
        node_id = scanner._get_node_id(cursor, path)
    except (FileNotFoundError, sqlite3.Error) as e:
        node_id = None
        if isinstance(e, sqlite3.DatabaseError) and os.path.exists(scanner.db_location):
            if scanner.conn is not None:
                scanner.conn.close()
                scanner.conn = None
            try:
                os.remove(scanner.db_location)
            except OSError:
                pass
        
    if node_id is not None:
        # Cache hit: Yield cakewalk iterators
        return cakewalkScandirIterator(scanner, path)
    else:
        # Cache miss: Fallback to native os.scandir
        return os.scandir(path)
