import os
import sqlite3
from typing import List, Tuple, Optional
from cakewalk import _get_default_scanner, cakewalk

def _get_scanner_and_conn(path: str, scanner: Optional[cakewalk] = None):
    scanner = scanner or _get_default_scanner(path)
    # This will raise FileNotFoundError if the DB doesn't exist
    conn = scanner._get_conn() 
    return scanner, conn

def largest_files(path: str, limit: int = 20, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int]]:
    """Returns the largest files under the given path, from the index.
    
    Raises FileNotFoundError if the cache database has not been built yet.
    Raises ValueError if the path is not found in the index.
    """
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
    
    lo, hi = rng
    rows = conn.execute(
        "SELECT id, size FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 0 "
        "ORDER BY size DESC LIMIT ?", (lo, hi, limit)
    ).fetchall()
    return [(scanner.path_of(row[0]), row[1]) for row in rows]

def heaviest_children(path: str, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int]]:
    """Returns the immediate children of the given path, ordered by size descending."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    
    if not scanner._has_blocks():
        raise RuntimeError("Index is too old to support this query. Re-run update_cache().")
        
    node_id = scanner._get_node_id(conn.cursor(), os.path.abspath(path))
    if node_id is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    row = conn.execute(
        "SELECT child_start, child_end FROM fs_nodes WHERE id = ?", (node_id,)
    ).fetchone()
    
    if row is None or row[0] == 0:
        return []
        
    child_start, child_end = row
    rows = conn.execute(
        "SELECT name, size FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? "
        "ORDER BY size DESC", (child_start, child_end)
    ).fetchall()
    return rows

def by_extension(path: str, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int, int]]:
    """Returns (extension, count, total_size) for files under the given path."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT lower(substr(name, instr(name, '.'))) AS ext, count(*), sum(size) "
        "FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 0 AND name LIKE '%.%' "
        "GROUP BY ext ORDER BY 3 DESC", (lo, hi)
    ).fetchall()
    return rows

def modified_since(path: str, mtime_ns: int, scanner: Optional[cakewalk] = None) -> List[str]:
    """Returns absolute paths of all files under path modified after mtime_ns."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT id FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 0 AND last_modified > ?",
        (lo, hi, mtime_ns)
    ).fetchall()
    return [scanner.path_of(row[0]) for row in rows]

def largest_dirs(path: str, min_size: int = 1073741824, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int]]:
    """Returns directories under path holding more than min_size bytes."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT id, size FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 1 AND size > ? "
        "ORDER BY size DESC", (lo, hi, min_size)
    ).fetchall()
    return [(scanner.path_of(row[0]), row[1]) for row in rows]

def empty_directories(path: str, scanner: Optional[cakewalk] = None) -> List[str]:
    """Returns absolute paths of all empty directories under the given path."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
    
    lo, hi = rng
    rows = conn.execute(
        "SELECT id FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 1 AND file_count = 0 AND dir_count = 0",
        (lo, hi)
    ).fetchall()
    return [scanner.path_of(row[0]) for row in rows]

def directories_with_most_files(path: str, limit: int = 20, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int]]:
    """Returns directories containing the highest number of descendant files."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT id, file_count FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 1 "
        "ORDER BY file_count DESC LIMIT ?", (lo, hi, limit)
    ).fetchall()
    return [(scanner.path_of(row[0]), row[1]) for row in rows]

def oldest_files(path: str, limit: int = 20, scanner: Optional[cakewalk] = None) -> List[Tuple[str, int]]:
    """Returns the oldest files under the given path, ordered by modification time (mtime_ns)."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT id, last_modified FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND is_dir = 0 "
        "ORDER BY last_modified ASC LIMIT ?", (lo, hi, limit)
    ).fetchall()
    return [(scanner.path_of(row[0]), row[1]) for row in rows]

def find_by_pattern(path: str, sql_like_pattern: str, scanner: Optional[cakewalk] = None) -> List[str]:
    """Finds files or directories matching an SQL LIKE pattern (e.g., '%.log')."""
    scanner, conn = _get_scanner_and_conn(path, scanner)
    rng = scanner.subtree_range(os.path.abspath(path))
    if rng is None:
        raise ValueError(f"Path not found in index: {path}. Run update_cache() first.")
        
    lo, hi = rng
    rows = conn.execute(
        "SELECT id FROM fs_nodes "
        "WHERE id BETWEEN ? AND ? AND name LIKE ?", 
        (lo, hi, sql_like_pattern)
    ).fetchall()
    return [scanner.path_of(row[0]) for row in rows]
