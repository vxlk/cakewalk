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

conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (1, NULL, 'C:\\')")
conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (2, 1, 'Windows')")
conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (3, 2, 'System32')")
conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (4, 3, 'cmd.exe')")

# Delete Windows's children (System32)
conn.execute("DELETE FROM fs_nodes WHERE parent_id = 2")

for row in conn.execute("SELECT id, name FROM fs_nodes"):
    print(row)
