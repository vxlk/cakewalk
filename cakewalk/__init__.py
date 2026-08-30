import sqlite3
import os
import tempfile
import atexit
from itertools import islice
from typing import Generator, Tuple, List, Optional

try:
    from cakewalk._cakewalk import run_scan
except ImportError:
    from _cakewalk import run_scan

#: Trust the cache completely. Zero filesystem access on the read path.
VALIDATE_NONE = 'none'
#: Stat only the directory being walked (one syscall). If its mtime matches the cache, the
#: entire subtree below it is trusted. This catches additions and deletions *directly* in
#: that directory; a change deeper in the tree does not move its mtime and will not be seen
#: until the next update_cache(). Default.
VALIDATE_ROOT = 'root'
#: Stat every directory as it is walked, re-reading any whose mtime moved. Exact, but costs
#: one syscall per directory.
VALIDATE_FULL = 'full'


class cakewalkDirEntry:
    __slots__ = ('name', 'path', '_is_dir', '_stat_cache', 'size')

    def __init__(self, name: str, path: str, is_dir: bool, size: int = 0):
        self.name = name
        self.path = path
        self._is_dir = is_dir
        self._stat_cache = None
        #: Cached size. For files, the size in bytes at scan time. For directories, the
        #: rolled-up size of the whole subtree. Served from the index, no IO.
        self.size = size

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

#: Columns the block reader pulls per node. Every column costs real time: the sqlite3
#: bridge converts each value individually, and transfer scales close to linearly with
#: column count, so this list is kept to exactly what a walk needs.
_BLOCK_SQL = (
    "SELECT name, is_dir, child_start, child_end FROM fs_nodes "
    "WHERE id BETWEEN ? AND ? ORDER BY id"
)


class _BlockReader:
    """Reads directory blocks out of the index with a single forward-moving cursor.

    The scanner lays every directory's children out as one contiguous id range, with the
    blocks themselves in depth-first order, so an unpruned walk consumes this cursor
    strictly in order and never re-queries. That is the whole point: it turns a walk into
    one sequential scan of the database file instead of a seek per directory.

    A prune breaks the sequence, and the reader re-seeks past the skipped subtree rather
    than streaming rows it is going to discard -- which is what makes pruning a large
    subtree actually cheap rather than merely correct.
    """

    __slots__ = ('_cursor', '_hi', '_pos', '_iter', 'seeks')

    def __init__(self, conn, hi):
        self._cursor = conn.cursor()
        self._cursor.arraysize = 1024
        self._hi = hi
        self._pos = None
        self._iter = None
        #: Number of re-seeks performed. Zero for an unpruned walk.
        self.seeks = 0

    def block(self, start, end):
        if self._pos != start:
            self._cursor.execute(_BLOCK_SQL, (start, self._hi))
            self._iter = iter(self._cursor)
            self._pos = start
            self.seeks += 1
        rows = list(islice(self._iter, end - start + 1))
        self._pos += len(rows)
        return rows

    def close(self):
        self._cursor.close()


class cakewalk:
    def __init__(self, db_location: str):
        self.db_location = db_location
        self.conn = None
        self._blocks_available = None

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
            # Backfill on databases written before idx_parent_id existed. Without it every
            # read-path query falls back to a full table scan.
            try:
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_parent_id ON fs_nodes(parent_id);")
            except sqlite3.Error:
                pass
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

    def walk(self, top: str, topdown: bool = True, onerror = None, followlinks: bool = False,
             validate: str = VALIDATE_ROOT):
        conn = self._get_conn()
        cursor = conn.cursor()

        current_id = self._get_node_id(cursor, top)
        if current_id is None:
            return

        yield from self._walk_cached(top, current_id, topdown, onerror, followlinks, validate)

    def _node_mtime(self, node_id):
        row = self.conn.execute(
            "SELECT last_modified FROM fs_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return row[0] if row else None

    def _has_blocks(self):
        """Whether this database carries the contiguous-child-block layout.

        Databases written before the layout existed still read correctly, just via the
        slower seek-per-directory path, so this is a capability check rather than a
        version gate.
        """
        if self._blocks_available is None:
            try:
                cols = {r[1] for r in self._get_conn().execute("PRAGMA table_info(fs_nodes)")}
            except sqlite3.Error:
                cols = set()
            self._blocks_available = {'child_start', 'child_end', 'subtree_last'} <= cols
        return self._blocks_available

    def _block_extent(self, node_id):
        row = self._get_conn().execute(
            "SELECT child_start, child_end, subtree_last FROM fs_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        return row

    def _walk_blocks(self, top, extent, topdown):
        """Walk out of the block layout: one forward pass, O(depth) memory.

        Blocks are always *read* in depth-first pre-order, because that is how they are
        stored. `topdown` only changes when each directory's tuple is handed to the caller,
        so bottom-up costs the same sequential scan and still holds only the directories on
        the current path.
        """
        child_start, child_end, subtree_last = extent
        reader = _BlockReader(self._get_conn(), subtree_last)
        join = os.path.join
        try:
            # frame: [path, dirnames, filenames, subdirs, next_child_index]
            root = [top, None, None, None, 0]
            root[1:4] = self._read_block(reader, child_start, child_end)
            stack = [root]

            while stack:
                frame = stack[-1]
                path, dirnames, filenames, subdirs, idx = frame

                if topdown and idx == 0:
                    yield path, dirnames, filenames
                    # os.walk contract: the caller may prune dirnames in place after the
                    # yield. Anything dropped here is skipped with a seek, so its blocks
                    # are never transferred.
                    if len(dirnames) != len(subdirs):
                        kept = set(dirnames)
                        subdirs = [d for d in subdirs if d[0] in kept]
                        frame[3] = subdirs

                if idx < len(subdirs):
                    frame[4] = idx + 1
                    name, cs, ce = subdirs[idx]
                    child = [join(path, name), None, None, None, 0]
                    child[1:4] = self._read_block(reader, cs, ce)
                    stack.append(child)
                    continue

                stack.pop()
                if not topdown:
                    yield path, dirnames, filenames
        finally:
            reader.close()

    @staticmethod
    def _read_block(reader, child_start, child_end):
        """Split one directory's block into (dirnames, filenames, subdir extents)."""
        if child_end < child_start:
            return [], [], []
        dirnames, filenames, subdirs = [], [], []
        for name, is_dir, cs, ce in reader.block(child_start, child_end):
            if is_dir:
                dirnames.append(name)
                subdirs.append((name, cs, ce))
            else:
                filenames.append(name)
        return dirnames, filenames, subdirs

    def _children(self, cursor, node_id):
        """Fetch one directory's children.

        Deliberately *not* a bulk subtree load. Holding a whole tree in Python objects is
        fine for a project directory and fatal for a multi-terabyte share, so the walk
        streams: one indexed seek per directory, and only for directories the caller
        actually descends into. Pruning therefore costs nothing instead of being paid for
        up front.
        """
        cursor.execute(
            "SELECT id, name, is_dir, last_modified FROM fs_nodes WHERE parent_id = ?",
            (node_id,),
        )
        dirs = []
        files = []
        for child_id, name, is_dir, last_modified in cursor.fetchall():
            if is_dir:
                dirs.append((child_id, name, last_modified))
            else:
                files.append(name)
        return dirs, files

    def _check_fresh(self, path, cached_mtime, onerror):
        """Return 'ok', 'stale' or 'gone' for a cached directory.

        Directory mtimes are stored truncated to whole seconds by the Rust scanner, so
        compare at the same resolution.
        """
        try:
            live = os.stat(path).st_mtime_ns // 1_000_000_000
        except OSError as err:
            if onerror is not None:
                onerror(err)
            return 'gone'
        if cached_mtime is None or live != cached_mtime:
            return 'stale'
        return 'ok'

    def _walk_cached(self, top, node_id, topdown, onerror, followlinks,
                     validate=VALIDATE_ROOT):
        """Stream a walk out of the index.

        Memory is proportional to the traversal frontier, not to the size of the tree, so
        this runs the same on a source checkout and on a shared drive with tens of millions
        of nodes.

        See VALIDATE_NONE / VALIDATE_ROOT / VALIDATE_FULL for the freshness policy.
        """
        if validate not in (VALIDATE_NONE, VALIDATE_ROOT, VALIDATE_FULL):
            raise ValueError(
                f"validate must be one of {VALIDATE_NONE!r}, {VALIDATE_ROOT!r}, "
                f"{VALIDATE_FULL!r}; got {validate!r}"
            )

        # One stat, then trust everything beneath it.
        if validate == VALIDATE_ROOT:
            status = self._check_fresh(top, self._node_mtime(node_id), onerror)
            if status == 'gone':
                return
            if status == 'stale':
                yield from os.walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks)
                return

        per_dir = validate == VALIDATE_FULL

        # The fast path. VALIDATE_FULL is deliberately excluded: it stats every directory as
        # it goes, so it is bounded by syscalls rather than by query cost, and it needs to
        # be able to drop into a live os.walk part-way down -- neither of which the single
        # forward cursor is built for.
        if not per_dir and self._has_blocks():
            extent = self._block_extent(node_id)
            if extent is not None:
                yield from self._walk_blocks(top, extent, topdown)
                return

        cursor = self._get_conn().cursor()

        if topdown:
            stack = [(top, node_id, None)]
            while stack:
                path, nid, mtime = stack.pop()

                if per_dir:
                    status = self._check_fresh(path, mtime, onerror)
                    if status == 'gone':
                        continue
                    if status == 'stale':
                        yield from os.walk(path, topdown=True, onerror=onerror, followlinks=followlinks)
                        continue

                dirs, filenames = self._children(cursor, nid)
                dirnames = [name for _, name, _ in dirs]

                yield path, dirnames, filenames

                # os.walk contract: the caller may prune dirnames in place after the yield.
                # Because children are fetched lazily, a pruned subtree is never queried.
                kept = set(dirnames)
                for child_id, name, child_mtime in reversed(dirs):
                    if name in kept:
                        stack.append((os.path.join(path, name), child_id, child_mtime))
        else:
            stack = [(top, node_id, None, False)]
            while stack:
                path, nid, mtime, expanded = stack.pop()

                if expanded:
                    dirs, filenames = self._children(cursor, nid)
                    yield path, [name for _, name, _ in dirs], filenames
                    continue

                if per_dir:
                    status = self._check_fresh(path, mtime, onerror)
                    if status == 'gone':
                        continue
                    if status == 'stale':
                        yield from os.walk(path, topdown=False, onerror=onerror, followlinks=followlinks)
                        continue

                dirs, _ = self._children(cursor, nid)
                stack.append((path, nid, mtime, True))
                for child_id, name, child_mtime in reversed(dirs):
                    stack.append((os.path.join(path, name), child_id, child_mtime, False))

    def connect(self):
        """A read-only :mod:`sqlite3` connection to the index.

        The index is an ordinary SQLite database and the schema is documented, so
        anything you can express in SQL you can ask of it directly. That is usually the
        better tool: an aggregate answered inside SQLite never materialises a Python
        object per node, which is the dominant cost of :meth:`walk`.

        Read-only by URI, so a query cannot corrupt the cache or block a scan.
        """
        if not os.path.exists(self.db_location):
            raise FileNotFoundError(
                f"Database not found at {self.db_location}. Call start_scan() first."
            )
        uri = 'file:{}?mode=ro'.format(
            os.path.abspath(self.db_location).replace('?', '%3f').replace('#', '%23')
        )
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA cache_size = -64000;")
        return conn

    def subtree_range(self, path: str):
        """``(lo, hi)`` id range holding everything *beneath* ``path``.

        The relayout gives every directory's descendants one contiguous run of row ids,
        so a whole-subtree question is a range scan over the primary key rather than a
        recursive CTE::

            lo, hi = scanner.subtree_range(r"D:\\share\\projects")
            conn.execute(
                "SELECT sum(size) FROM fs_nodes WHERE id BETWEEN ? AND ? AND is_dir = 0",
                (lo, hi),
            )

        ``path`` itself is *not* in the range -- its own rollups are on its row, and
        :meth:`du` returns them. A directory with no children yields ``(0, -1)``, which
        matches nothing, so callers do not need to special-case it.

        Returns None if ``path`` is not indexed, or if the index predates the layout.
        """
        if not self._has_blocks():
            return None
        conn = self._get_conn()
        node_id = self._get_node_id(conn.cursor(), path)
        if node_id is None:
            return None
        row = conn.execute(
            "SELECT child_start, subtree_last FROM fs_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        child_start, subtree_last = row
        # An empty directory carries child_start 0 / child_end -1; subtree_last is its own
        # id, which would otherwise describe a range containing unrelated rows.
        return (0, -1) if child_start == 0 else (child_start, subtree_last)

    def path_of(self, node_id: int):
        """Absolute path of a row id, or None if there is no such row.

        Rows store only a name, so a query result is not usable as a path until it is
        joined back up the tree. Costs one lookup per level.
        """
        conn = self._get_conn()
        parts = []
        current = node_id
        while True:
            row = conn.execute(
                "SELECT parent_id, name FROM fs_nodes WHERE id = ?", (current,)
            ).fetchone()
            if row is None:
                return None
            parent_id, name = row
            if parent_id is None:
                # Root rows hold the full path they were scanned as.
                return os.path.join(name, *reversed(parts)) if parts else name
            parts.append(name)
            current = parent_id

    def du(self, path: str):
        """Total bytes, file count and directory count beneath ``path``.

        A single indexed lookup against rollups computed during the scan, so this is O(1)
        regardless of how large the subtree is. Returns None if the path is not indexed.
        """
        conn = self._get_conn()
        node_id = self._get_node_id(conn.cursor(), path)
        if node_id is None:
            return None
        row = conn.execute(
            "SELECT size, file_count, dir_count FROM fs_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return {'size': row[0], 'files': row[1], 'dirs': row[2]}

    def cache_info(self, path: str):
        """What the index knows about ``path``: rollups, and how old the scan is."""
        import time as _time

        conn = self._get_conn()
        node_id = self._get_node_id(conn.cursor(), path)
        if node_id is None:
            return None

        row = conn.execute(
            "SELECT size, file_count, dir_count, last_modified FROM fs_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None

        # scan_meta is keyed by the root that was swept, which may be an ancestor of `path`.
        scanned_at = None
        try:
            meta = conn.execute("SELECT max(scanned_at) FROM scan_meta").fetchone()
            scanned_at = meta[0] if meta else None
        except sqlite3.Error:
            pass

        return {
            'size': row[0],
            'files': row[1],
            'dirs': row[2],
            'mtime': row[3],
            'scanned_at': scanned_at,
            'age_seconds': None if scanned_at is None else int(_time.time()) - scanned_at,
        }

    def scandir(self, path: str = '.'):
        if path == '.':
            path = os.getcwd()
            
        conn = self._get_conn()
        cursor = conn.cursor()
        
        current_id = self._get_node_id(cursor, path)
        if current_id is None:
            raise FileNotFoundError(f"Path not found in cakewalk: {path}")
            
        cursor.execute("SELECT name, is_dir, size FROM fs_nodes WHERE parent_id = ?", (current_id,))
        for name, is_dir, size in cursor.fetchall():
            yield cakewalkDirEntry(name, os.path.join(path, name), is_dir, size)

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

def walk(top: str, topdown: bool = True, onerror = None, followlinks: bool = False,
         validate: str = VALIDATE_ROOT):
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
        # Cache hit: stream the walk out of the index.
        yield from scanner._walk_cached(top, current_id, topdown, onerror, followlinks, validate)
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


def du(path: str):
    """Total bytes, file count and directory count beneath ``path``, from the index.

    O(1): the totals are rolled up during the scan, so this costs one indexed lookup no
    matter how large the subtree is. Returns None if ``path`` has not been scanned.
    """
    scanner = _get_default_scanner(path)
    try:
        return scanner.du(os.path.abspath(path))
    except (FileNotFoundError, sqlite3.Error):
        return None


def cache_info(path: str):
    """Rollups for ``path`` plus how old the scan is, or None if it is not indexed."""
    scanner = _get_default_scanner(path)
    try:
        return scanner.cache_info(os.path.abspath(path))
    except (FileNotFoundError, sqlite3.Error):
        return None


def connect():
    """A read-only :mod:`sqlite3` connection to the default index.

    The index is a plain SQLite database with a documented schema. Questions about a
    whole subtree -- total size, largest files, what changed, what matches a pattern --
    are answered far faster in SQL than by walking, because the answer never becomes one
    Python object per node. See :meth:`cakewalk.connect`.
    """
    return _get_default_scanner("ROOT").connect()


def subtree_range(path: str):
    """``(lo, hi)`` id range holding everything beneath ``path``, or None.

    Lets a whole-subtree query be a primary-key range scan. See
    :meth:`cakewalk.subtree_range`.
    """
    scanner = _get_default_scanner(path)
    try:
        return scanner.subtree_range(os.path.abspath(path))
    except (FileNotFoundError, sqlite3.Error):
        return None


def path_of(node_id: int):
    """Absolute path for a row id from a SQL result, or None.

    See :meth:`cakewalk.path_of`.
    """
    scanner = _get_default_scanner("ROOT")
    try:
        return scanner.path_of(node_id)
    except (FileNotFoundError, sqlite3.Error):
        return None
