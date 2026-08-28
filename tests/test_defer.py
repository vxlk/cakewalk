import sqlite3

def test_defer_foreign_keys():
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

    with conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        # Insert child before parent
        conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (2, 1, 'Windows')")
        # Insert parent
        conn.execute("INSERT INTO fs_nodes (id, parent_id, name) VALUES (1, NULL, 'C:\\')")

    # Verify both exist
    rows = conn.execute("SELECT id, parent_id, name FROM fs_nodes ORDER BY id").fetchall()
    assert rows == [(1, None, 'C:\\'), (2, 1, 'Windows')]
