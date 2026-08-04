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
}

enum DbMessage {
    DeleteNode(usize),
    ReplaceFiles(usize, Vec<Node>),
    UpsertNodes(Vec<Node>),
}

fn setup_db(db_path: &str) -> Connection {
    let conn = Connection::open(db_path).unwrap();
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA synchronous = NORMAL;
         PRAGMA foreign_keys = ON;
         PRAGMA cache_size = -10000;
         PRAGMA temp_store = MEMORY;
         CREATE TABLE IF NOT EXISTS fs_nodes (
             id INTEGER PRIMARY KEY,
             parent_id INTEGER,
             name TEXT NOT NULL,
             is_dir BOOLEAN NOT NULL,
             node_hash INTEGER NOT NULL,
             last_modified INTEGER NOT NULL,
             FOREIGN KEY(parent_id) REFERENCES fs_nodes(id) ON DELETE CASCADE
         );
         CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_name ON fs_nodes(ifnull(parent_id, -1), name);"
    ).unwrap();
    conn
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
) -> u64 {
    let mut child_hash_sum = 0u64;
    let mut batch = Vec::new();
    let mut sub_dirs = Vec::new();

    if let Ok(entries) = std::fs::read_dir(&path) {
        for entry in entries.flatten() {
            if let Ok(meta) = entry.metadata() {
                let is_dir = meta.is_dir();
                let name = match entry.file_name().into_string() {
                    Ok(s) => s,
                    Err(os_str) => os_str.to_string_lossy().into_owned(),
                };
                
                let modified = meta.modified().unwrap_or(UNIX_EPOCH).duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
                let size = meta.len();

                if is_dir {
                    let cached_id = cache_map.get(&(this_dir_id, name.clone())).map(|c| c.0);
                    let id = cached_id.unwrap_or_else(|| next_id.fetch_add(1, Ordering::Relaxed));
                    sub_dirs.push((name, modified, id));
                } else {
                    let id = next_id.fetch_add(1, Ordering::Relaxed);
                    let mut hash_data = Vec::with_capacity(name.len() + 16);
                    hash_data.extend_from_slice(name.as_bytes());
                    hash_data.extend_from_slice(&size.to_le_bytes());
                    hash_data.extend_from_slice(&modified.to_le_bytes());
                    
                    let hash = xxh64(&hash_data, 0);
                    child_hash_sum = child_hash_sum.wrapping_add(hash);
                    
                    batch.push(Node {
                        id,
                        parent_id: Some(this_dir_id),
                        name,
                        is_dir: false,
                        node_hash: hash as i64,
                        last_modified: modified,
                    });
                }
            }
        }
    }

    let dir_results: Vec<(Node, u64)> = sub_dirs.into_par_iter().map(|(name, modified, id)| {
        let mut child_path = path.clone();
        child_path.push(&name);
        
        let cached = cache_map.get(&(this_dir_id, name.clone()));
        let old_hash = cached.map(|c| c.2);
        
        let sub_hash = traverse(child_path, id, &name, modified, old_hash, tx.clone(), next_id.clone(), cache_map.clone(), parent_to_children.clone());
        
        let node = Node {
            id,
            parent_id: Some(this_dir_id),
            name,
            is_dir: true,
            node_hash: sub_hash as i64,
            last_modified: modified,
        };
        
        (node, sub_hash)
    }).collect();

    for (_, d_hash) in &dir_results {
        child_hash_sum = child_hash_sum.wrapping_add(*d_hash);
    }

    let mut my_hash_data = Vec::with_capacity(this_dir_name.len() + 16);
    my_hash_data.extend_from_slice(this_dir_name.as_bytes());
    my_hash_data.extend_from_slice(&this_dir_modified.to_le_bytes());
    my_hash_data.extend_from_slice(&child_hash_sum.to_le_bytes());
    let my_final_hash = xxh64(&my_hash_data, 0);

    if Some(my_final_hash) == cached_old_hash {
        return my_final_hash;
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

    my_final_hash
}

#[pyfunction]
#[pyo3(signature = (db_path, root=None, background=None))]
fn run_scan(db_path: &str, root: Option<String>, background: Option<bool>) -> PyResult<()> {
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
                "INSERT INTO fs_nodes (id, parent_id, name, is_dir, node_hash, last_modified) 
                 VALUES (?, ?, ?, ?, ?, ?) 
                 ON CONFLICT(id) DO UPDATE SET 
                 parent_id=excluded.parent_id, 
                 name=excluded.name, 
                 is_dir=excluded.is_dir, 
                 node_hash=excluded.node_hash, 
                 last_modified=excluded.last_modified"
            ).unwrap();
            
            for msg in rx {
                match msg {
                    DbMessage::DeleteNode(id) => {
                        stmt_del_node.execute([id]).unwrap();
                    }
                    DbMessage::ReplaceFiles(parent_id, files) => {
                        stmt_del_files.execute([parent_id]).unwrap();
                        for node in files {
                            stmt_upsert.execute(rusqlite::params![node.id, node.parent_id, node.name, node.is_dir, node.node_hash, node.last_modified]).unwrap();
                        }
                    }
                    DbMessage::UpsertNodes(nodes) => {
                        for node in nodes {
                            stmt_upsert.execute(rusqlite::params![node.id, node.parent_id, node.name, node.is_dir, node.node_hash, node.last_modified]).unwrap();
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
        
        let _ = conn.execute(
            "CREATE TABLE IF NOT EXISTS fs_nodes (
                 id INTEGER PRIMARY KEY,
                 parent_id INTEGER,
                 name TEXT NOT NULL,
                 is_dir BOOLEAN NOT NULL,
                 node_hash INTEGER NOT NULL,
                 last_modified INTEGER NOT NULL,
                 FOREIGN KEY(parent_id) REFERENCES fs_nodes(id) ON DELETE CASCADE
             );", []
        );
        
        if let Ok(mut stmt) = conn.prepare("SELECT id, ifnull(parent_id, -1), name, last_modified, node_hash FROM fs_nodes WHERE is_dir = 1") {
            if let Ok(mut rows) = stmt.query([]) {
                while let Some(row) = rows.next().unwrap() {
                    let id: usize = row.get(0).unwrap();
                    let parent_id_raw: i64 = row.get(1).unwrap();
                    let parent_id = if parent_id_raw == -1 { usize::MAX } else { parent_id_raw as usize };
                    let name: String = row.get(2).unwrap();
                    let last_modified: u64 = row.get(3).unwrap();
                    let node_hash: i64 = row.get(4).unwrap();
                    
                    if id > max_id { max_id = id; }
                    cache_map.insert((parent_id, name.clone()), (id, last_modified, node_hash as u64));
                    parent_to_children.entry(parent_id).or_default().push(name);
                }
            }
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

    pool.install(|| {
        let empty_vec = Vec::new();
        let old_roots = parent_to_children.get(&usize::MAX).unwrap_or(&empty_vec);
        for old_root in old_roots {
            if !drives.contains(old_root) {
                if let Some(&(old_id, _, _)) = cache_map.get(&(usize::MAX, old_root.clone())) {
                    let _ = tx.send(DbMessage::DeleteNode(old_id));
                }
            }
        }
    
        for drive in drives {
            let tx_clone = tx.clone();
            let next_id_clone = next_id.clone();
            let root_path = std::path::PathBuf::from(&drive);
            
            let modified = std::fs::metadata(&root_path)
                .map(|m| m.modified().unwrap_or(UNIX_EPOCH).duration_since(UNIX_EPOCH).unwrap_or_default().as_secs())
                .unwrap_or(0);
                
            let cached = cache_map.get(&(usize::MAX, drive.clone()));
            let id = cached.map(|c| c.0).unwrap_or_else(|| next_id.fetch_add(1, Ordering::Relaxed));
            let old_hash = cached.map(|c| c.2);
            
            let root_hash = traverse(root_path, id, &drive, modified, old_hash, tx_clone.clone(), next_id_clone, cache_map.clone(), parent_to_children.clone());
            
            if Some(root_hash) != old_hash {
                let node = Node {
                    id,
                    parent_id: None,
                    name: drive.clone(),
                    is_dir: true,
                    node_hash: root_hash as i64,
                    last_modified: modified,
                };
                let _ = tx_clone.send(DbMessage::UpsertNodes(vec![node]));
            }
        }
    });

    drop(tx);  
    db_thread.join().unwrap();
    
    Ok(())
}

#[pymodule]
fn _fastfs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_scan, m)?)?;
    Ok(())
}
