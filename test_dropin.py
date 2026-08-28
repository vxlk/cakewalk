import cakewalk
import os

print("--- cakewalk.walk ---")
for i, (root, dirs, files) in enumerate(cakewalk.walk("src")):
    print(f"root: {root}, dirs: {dirs}, files: {files}")
    if i > 2:
        break

print("\n--- cakewalk.scandir ---")
for entry in cakewalk.scandir("src"):
    print(f"{entry.name}, is_dir={entry.is_dir()}, is_file={entry.is_file()}")
