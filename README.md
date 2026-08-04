# FastFS

**FastFS** is a blisteringly fast, PyInstaller-friendly file system indexer and traversal library for Windows. It is built as a native Python C-Extension using **Rust**, **PyO3**, and **SQLite**.

## How it works

Standard OS directory traversal (`os.walk` or `os.scandir`) can be incredibly slow on massive drives or network shares because it issues an I/O request for every folder. 

FastFS solves this by:
1. Using **Rayon** to concurrently scan every mounted logical volume on your Windows machine in parallel.
2. Calculating a massive **Merkle Tree** using `xxHash64`. It captures the file name, size, and OS-reported modification time. 
3. Building a highly optimized Adjacency List within an **SQLite** database using Write-Ahead-Logging (WAL) and composite B-Tree indexes.
4. Supplying a Python wrapper that mimics `os.walk`, but queries the SQLite database instead, returning entire massive folders (like `C:\Windows\System32`) in milliseconds.

Because it calculates a Merkle Tree state hash, future synchronizations of the drive are nearly instantaneous. If a directory's hash matches the database, FastFS skips the entire subtree, completely bypassing deep filesystem traversal on subsequent runs.

## Usage

```python
from fastfs import FastFS
import time

# Initialize the scanner with the desired SQLite cache path
scanner = FastFS("multi_drive_cache.db")

# 1. Trigger the Rust engine to parallel-scan all drives (C:\, D:\, Network Drives)
# This will take ~10-20 seconds the first time, and < 5 seconds on subsequent runs
scanner.start_scan() 

# You can also target a specific directory for near-instant scanning:
# scanner.start_scan("C:\\Windows\\System32")

# 2. Reconstruct any folder tree instantly
start = time.time()
for root, dirs, files in scanner.walk("C:\\Windows\\System32"):
    pass # Do something with the files
    
print(f"Time: {time.time() - start:.4f} seconds")
```

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
maturin build --release
```
