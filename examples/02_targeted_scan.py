import os
import time
from cakewalk import cakewalk

def main():
    # Targeted scans are great for isolated directories
    # and take a fraction of a millisecond.
    target_dir = os.environ.get("TEMP", "C:\\Windows\\Temp")
    
    db_path = "cache_targeted.db"
    scanner = cakewalk(db_path)
    
    print(f"Initiating targeted scan on {target_dir}")
    
    start = time.time()
    scanner.start_scan(target_dir)
    print(f"Scan complete in {time.time() - start:.4f} seconds!\n")
    
    print(f"Recreating {target_dir}...")
    start = time.time()
    
    total_files = 0
    for root, dirs, files in scanner.walk(target_dir):
        total_files += len(files)
        
    print(f"Found {total_files:,} files in {time.time() - start:.4f} seconds!")
    scanner.close()

if __name__ == "__main__":
    main()
