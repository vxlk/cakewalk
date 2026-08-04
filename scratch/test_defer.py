import sqlite3

conn = sqlite3.connect(":memory:")
conn.execute("PRAGMA foreign_keys = ON")
conn.executescript("""
CREATE TABLE fs_nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    name TEXT NOT NULL,
    FOREIGN KEY(parent_id) REFERENCES fs_nodes(id) ON DELETE CASCADE
);
""")

try:
    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        # Insert child before parent
        conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (2, 1, 'Windows')")
        # Insert parent
        conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (1, NULL, 'C:\\')")
    print("Success with defer_foreign_keys!")
except Exception as e:
    print(f"Error: {e}")
