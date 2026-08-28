# FastFS

[![Tests](https://github.com/USER/fastfs/actions/workflows/test.yml/badge.svg)](https://github.com/USER/fastfs/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**FastFS** is a blisteringly fast, drop-in replacement for standard Python file system traversal (`os.walk` and `os.scandir`). It is built as a native Python C-Extension using **Rust**, **PyO3**, **jwalk**, and **SQLite**.

## How it works

FastFS uses a dual-architecture fallback mechanism to give you the absolute fastest possible filesystem traversal:

1. **Cache Miss (`jwalk` Cold Start)**: If the directory hasn't been cached, `fastfs.walk()` instantly drops down into a native Rust thread utilizing `jwalk` (and `rayon`). It sweeps the filesystem using parallel thread-pools and blasts `(root, dirs, files)` batches across a lock-free crossbeam channel directly to Python. This is significantly faster than standard `os.walk`.
2. **Cache Hit (0-IO SQLite)**: You can explicitly trigger a cache build via `fastfs.update_cache()`. This calculates a bottom-up hierarchical Merkle Tree of your directory using `xxHash64`, storing it in an optimized SQLite database. Future calls to `fastfs.walk()` will hit the cache and traverse the SQLite index purely in memory, requiring **0 disk I/O**, returning massive folders in milliseconds.

Because it calculates a Merkle Tree state hash, future synchronizations of the drive are nearly instantaneous. If a directory's hash matches the database, FastFS skips the entire subtree, completely bypassing deep filesystem traversal on subsequent runs.

## Usage

FastFS is designed to be a 1-to-1 drop-in replacement for standard Python library functions.

```python
import fastfs
import time

# ---------------------------------------------------------
# 1. COLD START (Cache Miss)
# ---------------------------------------------------------
# If the cache doesn't exist, this automatically drops into 
# the blazing-fast Rust `jwalk` engine.
print("Cold Start Traversal:")
for root, dirs, files in fastfs.walk("C:\\Windows\\System32"):
    pass 

# ---------------------------------------------------------
# 2. BUILDING THE CACHE
# ---------------------------------------------------------
# Explicitly warm up the SQLite differential cache.
print("Building Cache...")
fastfs.update_cache("C:\\Windows\\System32")

# ---------------------------------------------------------
# 3. WARM START (Cache Hit)
# ---------------------------------------------------------
# This call now hits the SQLite index. It requires 0 disk IO 
# and returns the entire tree in milliseconds.
print("Warm Start Traversal:")
start = time.time()
for root, dirs, files in fastfs.walk("C:\\Windows\\System32"):
    pass 
print(f"Time: {time.time() - start:.4f} seconds")
```

You can also use `fastfs.scandir()` as a drop-in replacement for `os.scandir()`.

## PyInstaller Compatibility
FastFS is built with `maturin`. It compiles to a `.pyd` module, which PyInstaller's dependency analyzer automatically discovers and bundles. There is no need for `sys._MEIPASS` extractions or custom `datas` hooks!

## FastFS vs. Voidtools "Everything"

If you've researched fast file search on Windows, you might wonder why we didn't just use the Voidtools "Everything" SDK. 

**Use Voidtools Everything when:**
- You are building a system-wide search utility where you can guarantee the user has **Administrator Privileges**.
- You want the absolute maximum speed physically possible (Everything reads the raw NTFS Master File Table / USN Journal at the disk-sector level).

**Use FastFS when:**
- You are distributing a portable Python application (like a PyInstaller `.exe`) to everyday users and **cannot ask for UAC Administrator privileges**.
- You want zero background services. FastFS runs entirely in user-space inside your Python process.
- You need to index **Network Drives** (SMB). Everything relies on the local NTFS MFT and struggles with network shares, while FastFS gracefully sweeps network drives using standard Windows APIs.

## Building from Source

```bash
pip install maturin
pip install -e .
```
