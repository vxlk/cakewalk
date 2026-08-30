use pyo3::prelude::*;
use rusqlite::Connection;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use crossbeam_channel::bounded;
use rayon::prelude::*;
use std::time::UNIX_EPOCH;
use xxhash_rust::xxh64::xxh64;
use std::collections::HashMap;

fn get_drives() -> Vec<String> {
    let mut drives = Vec::new();
    for c in b'A'..=b'Z' {
        let path = format!("{}:\\", c as char);
        if std::path::Path::new(&path).exists() {
            drives.push(path);
        }
    }
    drives
}

struct Node {
    id: usize,
    parent_id: Option<usize>,
    name: String,
    is_dir: bool,
    node_hash: i64,
    last_modified: u64,
    /// Files: own size in bytes. Directories: rolled-up size of the whole subtree.
    size: u64,
    /// Directories: number of files anywhere beneath this node. Files: 0.
    file_count: u64,
    /// Directories: number of directories anywhere beneath this node. Files: 0.
    dir_count: u64,
}

/// What a subtree traversal reports back to its parent.
#[derive(Default, Clone, Copy)]
struct Rollup {
    hash: u64,
    size: u64,
    files: u64,
    dirs: u64,
}

enum DbMessage {
    DeleteNode(usize),
    ReplaceFiles(usize, Vec<Node>),
    UpsertNodes(Vec<Node>),
}

/// Add columns introduced after the first release. SQLite has no `ADD COLUMN IF NOT EXISTS`,
/// and re-adding an existing column is an error, so the failures are expected and ignored.
///
/// Returns true if any column was actually added, meaning the existing rows carry default
/// zeros for it. The caller must then force a full re-write: the differential scan skips
/// directories whose hash is unchanged, so a migrated database would otherwise keep its
/// zeroed rollups forever.
fn migrate_db(conn: &Connection) -> bool {
    let mut added = false;
    for stmt in [
        "ALTER TABLE fs_nodes ADD COLUMN size INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fs_nodes ADD COLUMN file_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fs_nodes ADD COLUMN dir_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fs_nodes ADD COLUMN child_start INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fs_nodes ADD COLUMN child_end INTEGER NOT NULL DEFAULT -1",
        "ALTER TABLE fs_nodes ADD COLUMN subtree_last INTEGER NOT NULL DEFAULT 0",
    ] {
        if conn.execute(stmt, []).is_ok() {
            added = true;
        }
    }
    added
}

/// Column list shared by the live table and the relayout scratch table.
const NODE_COLUMNS: &str = "
        id INTEGER PRIMARY KEY,
        parent_id INTEGER,
        name TEXT NOT NULL,
        is_dir BOOLEAN NOT NULL,
        node_hash INTEGER NOT NULL,
        last_modified INTEGER NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        file_count INTEGER NOT NULL DEFAULT 0,
        dir_count INTEGER NOT NULL DEFAULT 0,
        -- Physical layout of this directory's immediate children: they occupy the
        -- contiguous id range [child_start, child_end]. Empty directories and files
        -- carry an empty range (start 0, end -1).
        child_start INTEGER NOT NULL DEFAULT 0,
        child_end INTEGER NOT NULL DEFAULT -1,
        -- Highest id anywhere in this node's subtree. Lets a reader skip a pruned
        -- subtree with a single seek instead of streaming past it.
        subtree_last INTEGER NOT NULL DEFAULT 0";

const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS fs_nodes (
        id INTEGER PRIMARY KEY,
        parent_id INTEGER,
        name TEXT NOT NULL,
        is_dir BOOLEAN NOT NULL,
        node_hash INTEGER NOT NULL,
        last_modified INTEGER NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        file_count INTEGER NOT NULL DEFAULT 0,
        dir_count INTEGER NOT NULL DEFAULT 0,
        child_start INTEGER NOT NULL DEFAULT 0,
        child_end INTEGER NOT NULL DEFAULT -1,
        subtree_last INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(parent_id) REFERENCES fs_nodes(id) ON DELETE CASCADE
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_name ON fs_nodes(ifnull(parent_id, -1), name);
    -- idx_parent_name is an *expression* index on ifnull(parent_id, -1); SQLite cannot use it
    -- to satisfy a plain `WHERE parent_id = ?`, so without this second index every read-path
    -- query (and every ON DELETE CASCADE) degrades to a full table scan.
    CREATE INDEX IF NOT EXISTS idx_parent_id ON fs_nodes(parent_id);
    -- Records when each root was last swept, so readers can report cache age and decide
    -- whether to trust it.
    CREATE TABLE IF NOT EXISTS scan_meta (
        root_id INTEGER PRIMARY KEY,
        scanned_at INTEGER NOT NULL
    );
";

fn setup_db(db_path: &str) -> Connection {
    let conn = Connection::open(db_path).unwrap();
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA synchronous = NORMAL;
         PRAGMA foreign_keys = ON;
         PRAGMA cache_size = -10000;
         PRAGMA temp_store = MEMORY;"
    ).unwrap();
    conn.execute_batch(SCHEMA).unwrap();
    let _ = migrate_db(&conn);
    conn
}

/// Write one directory's children as a single contiguous block of ids, and report back the
/// subdirectories among them so the caller can descend.
fn write_child_block(
    sel_children: &mut rusqlite::Statement<'_>,
    ins: &mut rusqlite::Statement<'_>,
    upd_block: &mut rusqlite::Statement<'_>,
    new_id: i64,
    old_id: i64,
    next_id: &mut i64,
) -> rusqlite::Result<Vec<(i64, i64)>> {
    // Materialised before any insert runs: the select reads the old table and the insert
    // writes the new one, but interleaving a read and a write on one connection is asking
    // for trouble, and a single directory's entries are a bounded amount of memory.
    let rows = sel_children
        .query_map([old_id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, bool>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, i64>(4)?,
                r.get::<_, i64>(5)?,
                r.get::<_, i64>(6)?,
                r.get::<_, i64>(7)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    if rows.is_empty() {
        return Ok(Vec::new());
    }

    let child_start = *next_id;
    let child_end = child_start + rows.len() as i64 - 1;
    *next_id = child_end + 1;

    let mut subdirs = Vec::new();
    for (i, (cid, name, is_dir, hash, lm, size, fc, dc)) in rows.into_iter().enumerate() {
        let cnew = child_start + i as i64;
        // subtree_last starts as the node's own id, which is already correct for files and
        // for empty directories; directories get theirs rewritten when the walk unwinds.
        ins.execute(rusqlite::params![
            cnew, new_id, name, is_dir, hash, lm, size, fc, dc, 0i64, -1i64, cnew
        ])?;
        if is_dir {
            subdirs.push((cnew, cid));
        }
    }
    upd_block.execute(rusqlite::params![child_start, child_end, new_id])?;
    Ok(subdirs)
}

/// Rewrite the node table so every directory's immediate children occupy one contiguous id
/// range, with the directory blocks themselves laid out depth-first.
///
/// This is the whole reason the read path is cheap. Ids handed out by the parallel scanner
/// follow discovery order, so a walk hops around the rowid space -- measured at 0.141
/// backward page seeks per node, which is invisible while the database fits in RAM and
/// ruinous once it does not. After this pass a full walk reads blocks strictly forward, so
/// the access pattern is sequential and readahead works: the difference between seconds and
/// hours on a multi-terabyte share backed by spinning media.
///
/// Laying children out contiguously (rather than simply numbering every node depth-first)
/// is what preserves `os.walk` semantics. A directory's complete `dirnames` list is one
/// block read, so it can be yielded *before* descending, and a caller that prunes it skips
/// a contiguous run of blocks with a single seek.
///
/// Runs in O(depth x fanout) memory -- only the directories on the current path are held --
/// and costs a full rewrite of the table. That trade is deliberate: the scan side is allowed
/// to be slow so the walk side can be fast.
///
/// Returns the old-id -> new-id mapping for the roots.
fn relayout(db_path: &str) -> rusqlite::Result<HashMap<usize, i64>> {
    let mut conn = Connection::open(db_path)?;
    conn.execute_batch(
        "PRAGMA foreign_keys = OFF;
         PRAGMA temp_store = MEMORY;
         PRAGMA cache_size = -20000;",
    )?;
    conn.execute("DROP TABLE IF EXISTS fs_nodes_relayout", [])?;
    // The self-reference is written against the scratch name; ALTER TABLE ... RENAME
    // rewrites it to point at fs_nodes once the swap happens.
    conn.execute_batch(&format!(
        "CREATE TABLE fs_nodes_relayout ({},
            FOREIGN KEY(parent_id) REFERENCES fs_nodes_relayout(id) ON DELETE CASCADE
        );",
        NODE_COLUMNS
    ))?;

    // Every root in the database, not just the ones this scan touched: a scan of one drive
    // must not orphan the nodes belonging to another.
    let old_roots: Vec<usize> = {
        let mut stmt = conn.prepare("SELECT id FROM fs_nodes WHERE parent_id IS NULL")?;
        let ids = stmt
            .query_map([], |r| r.get::<_, i64>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        ids.into_iter().map(|i| i as usize).collect()
    };

    let mut root_map: HashMap<usize, i64> = HashMap::new();

    struct Frame {
        new_id: i64,
        /// Value of the id counter when this directory was entered. Everything allocated
        /// from here on belongs to its subtree, which is how subtree_last is derived
        /// without a second pass.
        entry_next: i64,
        subdirs: Vec<(i64, i64)>,
        idx: usize,
    }

    let tx = conn.transaction()?;
    {
        let mut sel_children = tx.prepare(
            "SELECT id, name, is_dir, node_hash, last_modified, size, file_count, dir_count
             FROM fs_nodes WHERE parent_id = ? ORDER BY name",
        )?;
        let mut sel_root = tx.prepare(
            "SELECT name, is_dir, node_hash, last_modified, size, file_count, dir_count
             FROM fs_nodes WHERE id = ?",
        )?;
        let mut ins = tx.prepare(
            "INSERT INTO fs_nodes_relayout
               (id, parent_id, name, is_dir, node_hash, last_modified, size, file_count,
                dir_count, child_start, child_end, subtree_last)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )?;
        let mut upd_block =
            tx.prepare("UPDATE fs_nodes_relayout SET child_start = ?, child_end = ? WHERE id = ?")?;
        let mut upd_last =
            tx.prepare("UPDATE fs_nodes_relayout SET subtree_last = ? WHERE id = ?")?;

        let mut next_id: i64 = 1;

        for old_root in old_roots {
            let root = sel_root.query_row([old_root as i64], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, bool>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, i64>(6)?,
                ))
            });
            let (name, is_dir, hash, lm, size, fc, dc) = match root {
                Ok(v) => v,
                Err(_) => continue,
            };

            let root_new = next_id;
            next_id += 1;
            ins.execute(rusqlite::params![
                root_new,
                Option::<i64>::None,
                name,
                is_dir,
                hash,
                lm,
                size,
                fc,
                dc,
                0i64,
                -1i64,
                root_new
            ])?;
            root_map.insert(old_root, root_new);

            let entry_next = next_id;
            let subdirs = write_child_block(
                &mut sel_children,
                &mut ins,
                &mut upd_block,
                root_new,
                old_root as i64,
                &mut next_id,
            )?;
            let mut stack = vec![Frame {
                new_id: root_new,
                entry_next,
                subdirs,
                idx: 0,
            }];

            while !stack.is_empty() {
                let descend = {
                    let top = stack.last_mut().unwrap();
                    if top.idx < top.subdirs.len() {
                        let v = top.subdirs[top.idx];
                        top.idx += 1;
                        Some(v)
                    } else {
                        None
                    }
                };
                match descend {
                    Some((child_new, child_old)) => {
                        let entry_next = next_id;
                        let subdirs = write_child_block(
                            &mut sel_children,
                            &mut ins,
                            &mut upd_block,
                            child_new,
                            child_old,
                            &mut next_id,
                        )?;
                        stack.push(Frame {
                            new_id: child_new,
                            entry_next,
                            subdirs,
                            idx: 0,
                        });
                    }
                    None => {
                        let f = stack.pop().unwrap();
                        // Nothing allocated beneath it means an empty directory, whose
                        // subtree is just itself.
                        let last = if next_id == f.entry_next {
                            f.new_id
                        } else {
                            next_id - 1
                        };
                        upd_last.execute(rusqlite::params![last, f.new_id])?;
                    }
                }
            }
        }
    }

    // scan_meta is keyed by root id, and every root id just changed.
    let stale: Vec<(usize, i64)> = {
        let mut stmt = tx.prepare("SELECT root_id, scanned_at FROM scan_meta")?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)? as usize, r.get::<_, i64>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };

    // The swap stays inside the transaction: a failure between dropping the old table and
    // renaming the new one would otherwise leave no cache at all.
    tx.execute("DROP TABLE fs_nodes", [])?;
    tx.execute("ALTER TABLE fs_nodes_relayout RENAME TO fs_nodes", [])?;
    tx.execute_batch(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_name ON fs_nodes(ifnull(parent_id, -1), name);
         CREATE INDEX IF NOT EXISTS idx_parent_id ON fs_nodes(parent_id);",
    )?;

    tx.execute("DELETE FROM scan_meta", [])?;
    for (old_id, at) in stale {
        if let Some(&new_id) = root_map.get(&old_id) {
            tx.execute(
                "INSERT INTO scan_meta (root_id, scanned_at) VALUES (?, ?)
                 ON CONFLICT(root_id) DO UPDATE SET scanned_at = excluded.scanned_at",
                rusqlite::params![new_id, at],
            )?;
        }
    }

    tx.commit()?;

    // The old table's pages are free space now. SQLite would reuse them on the next scan,
    // but the file would sit at roughly twice the size it needs for ever, and this cache is
    // meant to be read off disk -- a file twice as large is twice as much sequential read.
    // VACUUM also rewrites the table in id order, so the physical layout matches the
    // logical one that the whole design depends on.
    conn.execute_batch("VACUUM; PRAGMA wal_checkpoint(TRUNCATE);")?;
    Ok(root_map)
}

fn traverse(
    path: std::path::PathBuf,
    this_dir_id: usize,
    this_dir_name: &str,
    this_dir_modified: u64,
    cached_old_hash: Option<u64>,
    tx: crossbeam_channel::Sender<DbMessage>,
    next_id: Arc<AtomicUsize>,
    cache_map: Arc<HashMap<(usize, String), (usize, u64, u64)>>,
    parent_to_children: Arc<HashMap<usize, Vec<String>>>,
    force_full: bool,
) -> Rollup {
    // A schema migration invalidates every cached hash comparison: rows exist but their new
    // columns are zeroed, so we must re-write them even where nothing on disk changed.
    let cached_old_hash = if force_full { None } else { cached_old_hash };
    let mut child_hash_sum = 0u64;
    let mut total_size = 0u64;
    let mut total_files = 0u64;
    let mut total_dirs = 0u64;
    let mut batch = Vec::new();
    let mut sub_dirs = Vec::new();

    let entries = match std::fs::read_dir(&path) {
        Ok(e) => e,
        Err(_) => {
            // Unreadable (permissions, vanished mid-scan). Report the cached hash so the
            // parent does not churn the DB, and contribute nothing to the rollups.
            return Rollup { hash: cached_old_hash.unwrap_or(0), ..Default::default() };
        }
    };

    for entry in entries.flatten() {
        let mut child_path = path.clone();
        child_path.push(entry.file_name());
        
        if let Ok(meta) = std::fs::symlink_metadata(&child_path) {
            let is_dir = meta.is_dir();
            let name = match entry.file_name().into_string() {
                Ok(s) => s,
                Err(os_str) => os_str.to_string_lossy().into_owned(),
            };
            
            let size = meta.len();

            if is_dir {
                // Truncate directory mtime to seconds to avoid NTFS lazy flush invalidation storms
                let dir_modified = meta.modified().unwrap_or(UNIX_EPOCH).duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
                let cached_id = cache_map.get(&(this_dir_id, name.clone())).map(|c| c.0);
                let id = cached_id.unwrap_or_else(|| next_id.fetch_add(1, Ordering::Relaxed));
                sub_dirs.push((name, dir_modified, id));
            } else {
                // Keep nanosecond precision for files
                let modified = meta.modified().unwrap_or(UNIX_EPOCH).duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos() as u64;
                let id = next_id.fetch_add(1, Ordering::Relaxed);
                let mut hash_data = Vec::with_capacity(name.len() + 16);
                hash_data.extend_from_slice(name.as_bytes());
                hash_data.extend_from_slice(&size.to_le_bytes());
                hash_data.extend_from_slice(&modified.to_le_bytes());
                
                let hash = xxh64(&hash_data, 0);
                child_hash_sum = child_hash_sum.wrapping_add(hash);
                total_size = total_size.saturating_add(size);
                total_files += 1;

                batch.push(Node {
                    id,
                    parent_id: Some(this_dir_id),
                    name,
                    is_dir: false,
                    node_hash: hash as i64,
                    last_modified: modified,
                    size,
                    file_count: 0,
                    dir_count: 0,
                });
            }
        }
    }

    let dir_results: Vec<(Node, Rollup)> = sub_dirs.into_par_iter().map(|(name, modified, id)| {
        let mut child_path = path.clone();
        child_path.push(&name);

        let cached = cache_map.get(&(this_dir_id, name.clone()));
        let old_hash = cached.map(|c| c.2);

        let sub = traverse(child_path, id, &name, modified, old_hash, tx.clone(), next_id.clone(), cache_map.clone(), parent_to_children.clone(), force_full);

        let node = Node {
            id,
            parent_id: Some(this_dir_id),
            name,
            is_dir: true,
            node_hash: sub.hash as i64,
            last_modified: modified,
            size: sub.size,
            file_count: sub.files,
            dir_count: sub.dirs,
        };

        (node, sub)
    }).collect();

    for (_, sub) in &dir_results {
        child_hash_sum = child_hash_sum.wrapping_add(sub.hash);
        total_size = total_size.saturating_add(sub.size);
        total_files += sub.files;
        // The subdirectory itself, plus everything beneath it.
        total_dirs += sub.dirs + 1;
    }

    let mut my_hash_data = Vec::with_capacity(this_dir_name.len() + 16);
    my_hash_data.extend_from_slice(this_dir_name.as_bytes());
    my_hash_data.extend_from_slice(&this_dir_modified.to_le_bytes());
    my_hash_data.extend_from_slice(&child_hash_sum.to_le_bytes());
    let my_final_hash = xxh64(&my_hash_data, 0);

    let rollup = Rollup {
        hash: my_final_hash,
        size: total_size,
        files: total_files,
        dirs: total_dirs,
    };

    if Some(my_final_hash) == cached_old_hash {
        return rollup;
    }

    // Something changed (or it's a new directory). We must synchronize this folder with SQLite.
    let empty_vec = Vec::new();
    let old_children = parent_to_children.get(&this_dir_id).unwrap_or(&empty_vec);

    // Identify deleted subdirectories and clear them
    for old_name in old_children {
        if !dir_results.iter().any(|(n, _)| &n.name == old_name) {
            if let Some(&(old_id, _, _)) = cache_map.get(&(this_dir_id, old_name.clone())) {
                let _ = tx.send(DbMessage::DeleteNode(old_id));
            }
        }
    }

    let _ = tx.send(DbMessage::ReplaceFiles(this_dir_id, batch));

    let mut dir_batch = Vec::new();
    for (node, _) in dir_results {
        dir_batch.push(node);
    }

    if !dir_batch.is_empty() {
        let _ = tx.send(DbMessage::UpsertNodes(dir_batch));
    }

    rollup
}

#[pyfunction]
#[pyo3(signature = (db_path, root=None, background=None))]
fn run_scan(py: Python<'_>, db_path: &str, root: Option<String>, background: Option<bool>) -> PyResult<()> {
    // Own every argument before releasing the GIL: `db_path` borrows into Python-owned memory.
    let db_path = db_path.to_string();
    // The scan touches no Python objects, so hold the GIL for none of it. Without this the
    // interpreter is frozen for the whole sweep and `background=True` cannot actually run
    // alongside anything.
    py.detach(move || scan_impl(&db_path, root, background))
}

fn scan_impl(db_path: &str, root: Option<String>, background: Option<bool>) -> PyResult<()> {
    // Detect a schema upgrade *before* spawning the writer, whose setup_db would otherwise
    // perform the migration first and hide the fact that it happened. On an upgraded
    // database every existing row carries zeroed rollup columns, so the differential scan
    // must be forced to re-write them even where the on-disk hash is unchanged.
    let force_full = if std::path::Path::new(db_path).exists() {
        match Connection::open(db_path) {
            Ok(conn) => {
                let _ = conn.execute_batch(SCHEMA);
                migrate_db(&conn)
            }
            Err(_) => false,
        }
    } else {
        false
    };

    let (tx, rx) = bounded::<DbMessage>(10_000);

    let db_path_owned = db_path.to_string();

    let db_thread = std::thread::spawn(move || {
        let mut conn = setup_db(&db_path_owned);
        {
            let tx_sql = conn.transaction().unwrap();
            tx_sql.execute("PRAGMA defer_foreign_keys = ON", []).unwrap();
            
            let mut stmt_del_node = tx_sql.prepare("DELETE FROM fs_nodes WHERE id = ?").unwrap();
            let mut stmt_del_files = tx_sql.prepare("DELETE FROM fs_nodes WHERE parent_id = ? AND is_dir = 0").unwrap();
            let mut stmt_upsert = tx_sql.prepare(
                "INSERT INTO fs_nodes (id, parent_id, name, is_dir, node_hash, last_modified, size, file_count, dir_count)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET
                 parent_id=excluded.parent_id,
                 name=excluded.name,
                 is_dir=excluded.is_dir,
                 node_hash=excluded.node_hash,
                 last_modified=excluded.last_modified,
                 size=excluded.size,
                 file_count=excluded.file_count,
                 dir_count=excluded.dir_count"
            ).unwrap();

            macro_rules! upsert {
                ($stmt:expr, $node:expr) => {
                    $stmt.execute(rusqlite::params![
                        $node.id, $node.parent_id, $node.name, $node.is_dir, $node.node_hash,
                        $node.last_modified, $node.size as i64, $node.file_count as i64,
                        $node.dir_count as i64
                    ]).unwrap()
                };
            }

            for msg in rx {
                match msg {
                    DbMessage::DeleteNode(id) => {
                        stmt_del_node.execute([id]).unwrap();
                    }
                    DbMessage::ReplaceFiles(parent_id, files) => {
                        stmt_del_files.execute([parent_id]).unwrap();
                        for node in files {
                            upsert!(stmt_upsert, node);
                        }
                    }
                    DbMessage::UpsertNodes(nodes) => {
                        for node in nodes {
                            upsert!(stmt_upsert, node);
                        }
                    }
                }
            }
            drop(stmt_del_node);
            drop(stmt_del_files);
            drop(stmt_upsert);
            tx_sql.commit().unwrap();
        }
    });

    let mut cache_map = HashMap::new();
    let mut parent_to_children: HashMap<usize, Vec<String>> = HashMap::new();
    let mut max_id = 0;

    if std::path::Path::new(&db_path).exists() {
        let conn = Connection::open(db_path).unwrap();
        let _ = conn.execute("PRAGMA foreign_keys = ON;", []);

        let _ = conn.execute_batch(SCHEMA);

        if let Ok(mut stmt) = conn.prepare("SELECT id, ifnull(parent_id, -1), name, last_modified, node_hash FROM fs_nodes WHERE is_dir = 1") {
            if let Ok(mut rows) = stmt.query([]) {
                while let Some(row) = rows.next().unwrap() {
                    let id: usize = row.get(0).unwrap();
                    let parent_id_raw: i64 = row.get(1).unwrap();
                    let parent_id = if parent_id_raw == -1 { usize::MAX } else { parent_id_raw as usize };
                    let name: String = row.get(2).unwrap();
                    let last_modified: u64 = row.get(3).unwrap();
                    let node_hash: i64 = row.get(4).unwrap();
                    
                    cache_map.insert((parent_id, name.clone()), (id, last_modified, node_hash as u64));
                    parent_to_children.entry(parent_id).or_default().push(name);
                }
            }
        }

        // Over *every* row, not just the directories loaded above. Files hold ids too, and
        // the block layout interleaves them with directories, so the highest directory id
        // is nowhere near the high-water mark. Seeding the counter from it would hand a new
        // node an id an existing file already owns, and the upsert would silently overwrite
        // that file's row.
        if let Ok(highest) = conn.query_row("SELECT ifnull(max(id), 0) FROM fs_nodes", [], |r| {
            r.get::<_, i64>(0)
        }) {
            max_id = highest as usize;
        }
    }
    
    let cache_map = Arc::new(cache_map);
    let parent_to_children = Arc::new(parent_to_children);
    let next_id = Arc::new(AtomicUsize::new(max_id + 1));
    let bg = background.unwrap_or(false);
    
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(if bg { 4 } else { 64 })
        .start_handler(move |_| {
            if bg {
                #[cfg(target_os = "windows")]
                {
                    unsafe {
                        windows_sys::Win32::System::Threading::SetThreadPriority(
                            windows_sys::Win32::System::Threading::GetCurrentThread(),
                            windows_sys::Win32::System::Threading::THREAD_MODE_BACKGROUND_BEGIN
                        );
                    }
                }
                #[cfg(target_os = "linux")]
                {
                    unsafe {
                        libc::setpriority(libc::PRIO_PROCESS, 0, 19);
                        libc::syscall(libc::SYS_ioprio_set, 1, 0, 3 << 13);
                    }
                }
            }
        })
        .build()
        .unwrap();

    let drives = match root {
        Some(r) => vec![r],
        None => get_drives(),
    };

    let (scanned_roots, tree_changed): (Vec<usize>, bool) = pool.install(|| {
        let mut roots = Vec::new();
        let mut changed = false;

        for drive in drives {
            let tx_clone = tx.clone();
            let next_id_clone = next_id.clone();
            let root_path = std::path::PathBuf::from(&drive);

            let modified = std::fs::metadata(&root_path)
                .map(|m| m.modified().unwrap_or(UNIX_EPOCH).duration_since(UNIX_EPOCH).unwrap_or_default().as_secs())
                .unwrap_or(0);

            let cached = cache_map.get(&(usize::MAX, drive.clone()));
            let id = cached.map(|c| c.0).unwrap_or_else(|| next_id.fetch_add(1, Ordering::Relaxed));
            // Same reasoning as inside traverse: after a migration the root row's rollups are
            // zeroed, so it must be re-written even when its hash is unchanged.
            let old_hash = if force_full { None } else { cached.map(|c| c.2) };

            let root = traverse(root_path, id, &drive, modified, old_hash, tx_clone.clone(), next_id_clone, cache_map.clone(), parent_to_children.clone(), force_full);

            if Some(root.hash) != old_hash {
                // The root hash covers the whole subtree, so this is the exact condition
                // under which any id could have moved and the layout needs rebuilding.
                changed = true;
                let node = Node {
                    id,
                    parent_id: None,
                    name: drive.clone(),
                    is_dir: true,
                    node_hash: root.hash as i64,
                    last_modified: modified,
                    size: root.size,
                    file_count: root.files,
                    dir_count: root.dirs,
                };
                let _ = tx_clone.send(DbMessage::UpsertNodes(vec![node]));
            }

            roots.push(id);
        }

        (roots, changed)
    });

    drop(tx);
    db_thread.join().unwrap();

    // Ids handed out during the scan follow parallel discovery order, which is scattered
    // relative to walk order. Rewrite them into block layout so the read path is one
    // forward sequential pass. Skipped entirely when nothing changed: the existing layout
    // is then still correct, and this is the expensive part of a scan.
    let scanned_roots: Vec<i64> = if tree_changed || force_full {
        match relayout(db_path) {
            Ok(map) => scanned_roots
                .iter()
                .map(|id| map.get(id).copied().unwrap_or(*id as i64))
                .collect(),
            // A failed relayout rolls back, leaving the pre-relayout ids valid. The cache
            // is still correct, just laid out for slower reads.
            Err(_) => scanned_roots.iter().map(|&i| i as i64).collect(),
        }
    } else {
        scanned_roots.iter().map(|&i| i as i64).collect()
    };

    // Stamp scan completion *after* the writer has committed, so a reader that sees a
    // scanned_at row is guaranteed to see the tree that goes with it.
    let now = std::time::SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    if let Ok(conn) = Connection::open(db_path) {
        let _ = conn.execute_batch(SCHEMA);
        for root_id in scanned_roots {
            let _ = conn.execute(
                "INSERT INTO scan_meta (root_id, scanned_at) VALUES (?, ?)
                 ON CONFLICT(root_id) DO UPDATE SET scanned_at = excluded.scanned_at",
                rusqlite::params![root_id, now],
            );
        }
    }

    Ok(())
}

#[pyclass]
struct LiveWalk {
    receiver: crossbeam_channel::Receiver<(String, Vec<String>, Vec<String>)>,
}

#[pymethods]
impl LiveWalk {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(slf: PyRefMut<'_, Self>, py: Python) -> Option<(String, Vec<String>, Vec<String>)> {
        let receiver = slf.receiver.clone();
        py.detach(|| receiver.recv().ok())
    }
}

#[pyfunction]
fn live_walk(root: String) -> LiveWalk {
    let (tx, rx) = crossbeam_channel::bounded(100);
    
    std::thread::spawn(move || {
        // jwalk defaults skip_hidden to true, which silently drops every dotfile and
        // dot-directory (.git, .env, .github, ...). os.walk yields them, and so does our
        // SQLite path, so the cold path must too.
        for _ in jwalk::WalkDir::new(&root).skip_hidden(false).process_read_dir(move |_, path, _, children| {
            let mut dirs = Vec::new();
            let mut files = Vec::new();
            
            for child in children.iter().flatten() {
                let name = child.file_name.to_string_lossy().into_owned();
                if child.file_type.is_dir() {
                    dirs.push(name);
                } else {
                    files.push(name);
                }
            }
            
            let root_str = path.to_string_lossy().into_owned();
            
            // jwalk sometimes processes the parent of the root directory to stat the root itself.
            // We should only yield paths that are equal to or inside the root we asked for.
            if root_str.starts_with(&root) {
                let _ = tx.send((root_str, dirs, files));
            }
        }) {}
    });

    LiveWalk { receiver: rx }
}

#[pymodule]
fn _cakewalk(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_scan, m)?)?;
    m.add_function(wrap_pyfunction!(live_walk, m)?)?;
    Ok(())
}
