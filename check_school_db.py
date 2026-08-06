import sqlite3

conn = sqlite3.connect("school.db")
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
for (table_name,) in cur.fetchall():
    print(table_name)
conn.close()