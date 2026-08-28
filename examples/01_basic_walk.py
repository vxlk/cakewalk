import time
from cakewalk import cakewalk

def main():
    db_path = "cache_full.db"
    scanner = cakewalk(db_path)
    
    print("Initiating full multi-drive sweep (A:\\ through Z:\\)")
    print("This will take 10-20 seconds on the first run, but future runs will be near-instant.")
    
    start = time.time()
    scanner.start_scan()
    print(f"Sweep complete in {time.time() - start:.2f} seconds!\n")
    
    target = "C:\\Windows\\System32"
    print(f"Instantly recreating {target}...")
    start = time.time()
    
    total_files = 0
    total_dirs = 0
    
    for root, dirs, files in scanner.walk(target):
        total_dirs += len(dirs)
        total_files += len(files)
        
    print(f"Found {total_dirs:,} directories and {total_files:,} files in {time.time() - start:.4f} seconds!")
    scanner.close()

if __name__ == "__main__":
    main()
