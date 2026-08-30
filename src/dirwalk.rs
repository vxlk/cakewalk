//! Reading `dir_blocks` from Rust instead of through Python's `sqlite3` module.
//!
//! The projection already removed most of the boundary crossings by giving each directory
//! one row. What is left is the cost of that row: `sqlite3` charges roughly 470 ns per row
//! plus 215 ns per column value, and a walk needs six columns. Here rusqlite steps the
//! rows and the names become `PyString`s straight from the packed blob -- no per-row tuple,
//! no intermediate whole-blob `str` that is then split and thrown away.
//!
//! The floor is the string objects themselves, about 95 ns each; this lands within roughly
//! 3x of it. Everything below exists to preserve `os.walk` semantics exactly while doing
//! that, which is why pruning gets as much code as reading does.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use rusqlite::Connection;
use std::collections::HashSet;
use std::collections::VecDeque;

/// Rows fetched per query. The statement cannot outlive the borrow of the connection
/// inside a `#[pyclass]`, so reading happens in batches rather than on one long-lived
/// cursor. A restart is a primary-key seek, so at this size the cost disappears.
const CHUNK: i64 = 4096;

/// A directory, with its child names already turned into Python lists.
///
/// The conversion happens at fetch time so the packed blobs can be read straight out of
/// SQLite's own buffer: no intermediate Rust `String` per row, and no second pass to split
/// one. `name` stays owned because it outlives the statement -- it is needed for path
/// building and for the prune check.
struct Row {
    dfs: i64,
    parent: i64,
    name: String,
    dirnames: Py<PyList>,
    filenames: Py<PyList>,
    ndirs: usize,
    subtree_end: i64,
}

/// A directory whose subtree is still being walked.
struct Frame {
    dfs: i64,
    path: String,
    subtree_end: i64,
    /// For topdown: the `dirnames` list handed to the caller, who may prune it in place.
    /// For bottom-up: the lists this directory will be yielded with once its subtree ends.
    dirnames: Py<PyList>,
    filenames: Option<Py<PyList>>,
    /// Length of `dirnames` at the moment it was handed out. A shorter list means a prune.
    handed_out: usize,
    /// Names still to be descended into. `None` until the list has been checked, and stays
    /// `None` when nothing was pruned -- which keeps the common path free of hashing.
    kept: Option<HashSet<String>>,
    checked: bool,
}

#[pyclass(unsendable)]
pub struct DirBlockWalk {
    conn: Connection,
    root: String,
    sep: String,
    /// Next `dfs` to fetch, and the last one belonging to this walk.
    next_dfs: i64,
    hi: i64,
    topdown: bool,
    buf: VecDeque<Row>,
    drained: bool,
    stack: Vec<Frame>,
    /// Tuples ready to hand out. Bottom-up completes several directories at once.
    ready: VecDeque<Py<PyAny>>,
    /// Query restarts. One for an unpruned walk, matching the Python reader.
    seeks: u64,
}

impl DirBlockWalk {
    /// Refill `buf`, or mark the stream drained.
    fn fetch(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.drained || self.next_dfs > self.hi {
            self.drained = true;
            return Ok(());
        }
        let sql = |e: rusqlite::Error| pyo3::exceptions::PyRuntimeError::new_err(e.to_string());
        let mut stmt = self
            .conn
            .prepare_cached(
                "SELECT dfs, parent, name, dnames, fnames, subtree_end FROM dir_blocks \
                 WHERE dfs BETWEEN ? AND ? ORDER BY dfs LIMIT ?",
            )
            .map_err(sql)?;
        let mut rows = stmt
            .query(rusqlite::params![self.next_dfs, self.hi, CHUNK])
            .map_err(sql)?;
        let mut n = 0;
        let mut next_dfs = self.next_dfs;
        while let Some(r) = rows.next().map_err(sql)? {
            let dfs: i64 = r.get(0).map_err(sql)?;
            let parent: i64 = r.get(1).map_err(sql)?;
            let name: String = r.get(2).map_err(sql)?;
            let subtree_end: i64 = r.get(5).map_err(sql)?;
            // Borrowed from the statement's own buffer and consumed before the next step.
            let cast = |e: rusqlite::types::FromSqlError| {
                pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
            };
            let dirnames = names(py, r.get_ref(3).map_err(sql)?.as_str().map_err(cast)?)?;
            let filenames = names(py, r.get_ref(4).map_err(sql)?.as_str().map_err(cast)?)?;
            next_dfs = dfs + 1;
            let ndirs = dirnames.len();
            self.buf.push_back(Row {
                dfs,
                parent,
                name,
                dirnames: dirnames.unbind(),
                filenames: filenames.unbind(),
                ndirs,
                subtree_end,
            });
            n += 1;
        }
        self.next_dfs = next_dfs;
        self.seeks += 1;
        if n < CHUNK {
            self.drained = true;
        }
        Ok(())
    }

    /// Abandon the current position and resume at `dfs`, discarding anything buffered.
    fn seek(&mut self, dfs: i64) {
        self.buf.clear();
        self.next_dfs = dfs;
        self.drained = false;
    }

    fn next_row(&mut self, py: Python<'_>) -> PyResult<Option<Row>> {
        if self.buf.is_empty() {
            self.fetch(py)?;
        }
        Ok(self.buf.pop_front())
    }

    /// Work out what the caller pruned, the first time it matters.
    ///
    /// Deferred rather than done at yield time because the caller edits the list *after*
    /// receiving it; the earliest safe moment to look is when a child of that directory
    /// comes up. Only a shortened list counts as a prune, which is the same rule the
    /// Python reader uses.
    fn resolve_prune(frame: &mut Frame, py: Python<'_>) -> PyResult<()> {
        if frame.checked {
            return Ok(());
        }
        frame.checked = true;
        let list = frame.dirnames.bind(py);
        if list.len() == frame.handed_out {
            return Ok(());
        }
        let mut kept = HashSet::with_capacity(list.len());
        for item in list.iter() {
            kept.insert(item.extract::<String>()?);
        }
        frame.kept = Some(kept);
        Ok(())
    }
}

/// One `PyString` per name, straight from the packed blob.
fn names<'py>(py: Python<'py>, blob: &str) -> PyResult<Bound<'py, PyList>> {
    if blob.is_empty() {
        return Ok(PyList::empty(py));
    }
    PyList::new(py, blob.split('\0').map(|s| PyString::new(py, s)))
}

#[pymethods]
impl DirBlockWalk {
    #[new]
    #[pyo3(signature = (db_path, root, sep, lo, hi, topdown))]
    fn new(db_path: &str, root: &str, sep: &str, lo: i64, hi: i64, topdown: bool) -> PyResult<Self> {
        let conn = Connection::open_with_flags(
            db_path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        let _ = conn.execute_batch("PRAGMA cache_size = -64000;");
        Ok(DirBlockWalk {
            conn,
            root: root.to_string(),
            sep: sep.to_string(),
            next_dfs: lo,
            hi,
            topdown,
            buf: VecDeque::new(),
            drained: false,
            stack: Vec::new(),
            ready: VecDeque::new(),
            seeks: 0,
        })
    }

    /// Query restarts so far. Exposed so a test can assert an unpruned walk does not seek.
    #[getter]
    fn seeks(&self) -> u64 {
        self.seeks
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        loop {
            if let Some(item) = slf.ready.pop_front() {
                return Ok(Some(item));
            }

            let row = match slf.next_row(py)? {
                Some(row) => row,
                None => {
                    if slf.topdown {
                        return Ok(None);
                    }
                    // Bottom-up: whatever is still open completes now, deepest first.
                    while let Some(frame) = slf.stack.pop() {
                        let item = (
                            PyString::new(py, &frame.path),
                            frame.dirnames.bind(py).clone(),
                            frame.filenames.unwrap().bind(py).clone(),
                        )
                            .into_pyobject(py)?
                            .unbind()
                            .into();
                        slf.ready.push_back(item);
                    }
                    if slf.ready.is_empty() {
                        return Ok(None);
                    }
                    continue;
                }
            };

            if slf.topdown {
                // Borrowing through PyRefMut's Deref locks the whole struct, so take a
                // plain &mut once and let the compiler see `stack`, `sep` and `root` as
                // the disjoint fields they are.
                let this = &mut *slf;

                // Unwind to this row's parent, then decide whether it was pruned away.
                let mut skip_to: Option<i64> = None;
                let mut retire_parent = false;
                let path;
                while this
                    .stack
                    .last()
                    .map(|f| f.dfs != row.parent)
                    .unwrap_or(false)
                {
                    this.stack.pop();
                }
                match this.stack.last_mut() {
                    None => path = this.root.clone(),
                    Some(frame) => {
                        Self::resolve_prune(frame, py)?;
                        if let Some(kept) = frame.kept.as_mut() {
                            if kept.is_empty() {
                                // Every subdirectory of this parent has been walked or
                                // pruned, so the whole remainder of its subtree is dead:
                                // one seek clears it however many siblings are left.
                                skip_to = Some(frame.subtree_end + 1);
                                retire_parent = true;
                            } else if !kept.contains(&row.name) {
                                skip_to = Some(row.subtree_end + 1);
                            } else {
                                kept.remove(&row.name);
                            }
                        }
                        if skip_to.is_some() {
                            path = String::new();
                        } else {
                            let mut p = String::with_capacity(
                                frame.path.len() + this.sep.len() + row.name.len(),
                            );
                            p.push_str(&frame.path);
                            p.push_str(&this.sep);
                            p.push_str(&row.name);
                            path = p;
                        }
                    }
                }

                if let Some(target) = skip_to {
                    if retire_parent {
                        slf.stack.pop();
                    }
                    slf.seek(target);
                    continue;
                }

                let count = row.ndirs;
                let item: Py<PyAny> = (
                    PyString::new(py, &path),
                    row.dirnames.bind(py).clone(),
                    row.filenames.bind(py).clone(),
                )
                    .into_pyobject(py)?
                    .unbind()
                    .into();

                if count > 0 {
                    slf.stack.push(Frame {
                        dfs: row.dfs,
                        path,
                        subtree_end: row.subtree_end,
                        dirnames: row.dirnames,
                        filenames: None,
                        handed_out: count,
                        kept: None,
                        checked: false,
                    });
                }
                return Ok(Some(item));
            }

            // Bottom-up. Rows still arrive pre-order, so a directory is held until a row
            // turns up that is not inside its subtree.
            while slf
                .stack
                .last()
                .map(|f| f.dfs != row.parent)
                .unwrap_or(false)
            {
                let frame = slf.stack.pop().unwrap();
                let item = (
                    PyString::new(py, &frame.path),
                    frame.dirnames.bind(py).clone(),
                    frame.filenames.unwrap().bind(py).clone(),
                )
                    .into_pyobject(py)?
                    .unbind()
                    .into();
                slf.ready.push_back(item);
            }

            let path = match slf.stack.last() {
                None => slf.root.clone(),
                Some(frame) => {
                    let mut p = String::with_capacity(
                        frame.path.len() + slf.sep.len() + row.name.len(),
                    );
                    p.push_str(&frame.path);
                    p.push_str(&slf.sep);
                    p.push_str(&row.name);
                    p
                }
            };
            slf.stack.push(Frame {
                dfs: row.dfs,
                path,
                subtree_end: row.subtree_end,
                dirnames: row.dirnames,
                filenames: Some(row.filenames),
                handed_out: 0,
                kept: None,
                checked: true,
            });
        }
    }
}
