use pyo3::prelude::*;
use rusqlite::Connection;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use crossbeam_channel::bounded;
use rayon::prelude::*;
use std::time::UNIX_EPOCH;
use xxhash_rust::xxh64::xxh64;

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

fn setup_db(db_path: &str) -> Connection {
    let _ = std::fs::remove_file(db_path);
    let conn = Connection::open(db_path).unwrap();
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA synchronous = 0;
         PRAGMA cache_size = 1000000;
         PRAGMA locking_mode = EXCLUSIVE;
         PRAGMA temp_store = MEMORY;
         CREATE TABLE fs_nodes (
             id INTEGER PRIMARY KEY,
             parent_id INTEGER,
             name TEXT NOT NULL,
             is_dir BOOLEAN NOT NULL,
             node_hash INTEGER NOT NULL,
             last_modified INTEGER NOT NULL
         );
         CREATE UNIQUE INDEX idx_parent_name ON fs_nodes(parent_id, name);"
    ).unwrap();
    conn
}

fn traverse(
    path: std::path::PathBuf,
    parent_id: usize,
    tx: crossbeam_channel::Sender<Vec<Node>>,
    next_id: Arc<AtomicUsize>,
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
                    let id = next_id.fetch_add(1, Ordering::Relaxed);
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
                        parent_id: Some(parent_id),
                        name,
                        is_dir: false,
                        node_hash: hash as i64,
                        last_modified: modified,
                    });
                }
            }
        }
    }

    if !batch.is_empty() {
        let _ = tx.send(batch);
    }

    let dir_results: Vec<(Node, u64)> = sub_dirs.into_par_iter().map(|(name, modified, id)| {
        let mut child_path = path.clone();
        child_path.push(&name);
        
        let sub_hash = traverse(child_path, id, tx.clone(), next_id.clone());
        
        let mut hash_data = Vec::with_capacity(name.len() + 16);
        hash_data.extend_from_slice(name.as_bytes());
        hash_data.extend_from_slice(&modified.to_le_bytes());
        hash_data.extend_from_slice(&sub_hash.to_le_bytes());
        
        let dir_hash = xxh64(&hash_data, 0);
        
        let node = Node {
            id,
            parent_id: Some(parent_id),
            name,
            is_dir: true,
            node_hash: dir_hash as i64,
            last_modified: modified,
        };
        
        (node, dir_hash)
    }).collect();

    let mut dir_batch = Vec::with_capacity(dir_results.len());
    for (node, d_hash) in dir_results {
        child_hash_sum = child_hash_sum.wrapping_add(d_hash);
        dir_batch.push(node);
    }

    if !dir_batch.is_empty() {
        let _ = tx.send(dir_batch);
    }

    child_hash_sum
}

#[pyfunction]
#[pyo3(signature = (db_path, root=None))]
fn run_scan(db_path: &str, root: Option<String>) -> PyResult<()> {
    let (tx, rx) = bounded::<Vec<Node>>(10_000);
    
    // Convert str to String so it can be moved to thread
    let db_path_owned = db_path.to_string();
    
    let db_thread = std::thread::spawn(move || {
        let mut conn = setup_db(&db_path_owned);
        {
            let tx_sql = conn.transaction().unwrap();
            let mut stmt = tx_sql.prepare("INSERT INTO fs_nodes VALUES (?, ?, ?, ?, ?, ?)").unwrap();
            for batch in rx {
                for node in batch {
                    stmt.execute(rusqlite::params![node.id, node.parent_id, node.name, node.is_dir, node.node_hash, node.last_modified]).unwrap();
                }
            }
            drop(stmt);
            tx_sql.commit().unwrap();
        }
    });

    let next_id = Arc::new(AtomicUsize::new(1));
    // It's safe to call build_global multiple times in PyO3 if it's already built. 
    // Just ignore the error.
    let _ = rayon::ThreadPoolBuilder::new().num_threads(64).build_global();

    let drives = match root {
        Some(r) => vec![r],
        None => get_drives(),
    };
    let mut root_nodes = Vec::new();

    for drive in drives {
        let tx_clone = tx.clone();
        let next_id_clone = next_id.clone();
        let root_path = std::path::PathBuf::from(&drive);

        let id = next_id.fetch_add(1, Ordering::Relaxed);
        let root_hash = traverse(root_path, id, tx_clone, next_id_clone);

        root_nodes.push(Node {
            id,
            parent_id: None,
            name: drive,
            is_dir: true,
            node_hash: root_hash as i64,
            last_modified: 0,
        });
    }

    if !root_nodes.is_empty() {
        tx.send(root_nodes).unwrap();
    }

    drop(tx);  
    db_thread.join().unwrap();
    
    Ok(())
}

#[pymodule]
fn _fastfs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_scan, m)?)?;
    Ok(())
}
