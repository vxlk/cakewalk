import fastfs
import os

print("--- fastfs.walk ---")
for i, (root, dirs, files) in enumerate(fastfs.walk("src")):
    print(f"root: {root}, dirs: {dirs}, files: {files}")
    if i > 2:
        break

print("\n--- fastfs.scandir ---")
for entry in fastfs.scandir("src"):
    print(f"{entry.name}, is_dir={entry.is_dir()}, is_file={entry.is_file()}")
