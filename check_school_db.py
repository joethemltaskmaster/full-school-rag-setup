import sqlite3

conn = sqlite3.connect("school.db")
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
for (table_name,) in cur.fetchall():
    print(table_name)
conn.close()

"""import sqlite3

conn = sqlite3.connect("school.db")
cur = conn.cursor()

# Get all table names
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = [row[0] for row in cur.fetchall()]

empty_tables = []
non_empty_tables = []

for table_name in tables:
    cur.execute(f"SELECT COUNT(*) FROM {table_name};")
    count = cur.fetchone()[0]
    if count == 0:
        empty_tables.append(table_name)
    else:
        non_empty_tables.append((table_name, count))

conn.close()

print(f"Total tables: {len(tables)}\n")

print(f"Empty tables ({len(empty_tables)}):")
for t in empty_tables:
    print(f"  - {t}")

print(f"\nNon-empty tables ({len(non_empty_tables)}):")
for t, count in non_empty_tables:
    print(f"  - {t}: {count} rows")"""

'''import sqlite3

conn = sqlite3.connect("school.db")
cur = conn.cursor()

# Get all table names
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = [row[0] for row in cur.fetchall()]

output = []

for table_name in tables:
    output.append(f"## Table: `{table_name}`\n")

    # --- Schema info ---
    cur.execute(f"PRAGMA table_info({table_name});")
    columns = cur.fetchall()  # (cid, name, type, notnull, dflt_value, pk)

    output.append("**Columns:**\n")
    output.append("| # | Name | Type | Not Null | Default | Primary Key |")
    output.append("|---|------|------|----------|---------|--------------|")
    for cid, name, col_type, notnull, dflt, pk in columns:
        output.append(f"| {cid} | {name} | {col_type} | {'Yes' if notnull else 'No'} | {dflt if dflt is not None else '-'} | {'Yes' if pk else 'No'} |")

    # --- Foreign keys ---
    cur.execute(f"PRAGMA foreign_key_list({table_name});")
    fks = cur.fetchall()
    if fks:
        output.append("\n**Foreign Keys:**\n")
        output.append("| From Column | References Table | To Column |")
        output.append("|-------------|-------------------|-----------|")
        for fk in fks:
            output.append(f"| {fk[3]} | {fk[2]} | {fk[4]} |")

    # --- Row count ---
    cur.execute(f"SELECT COUNT(*) FROM {table_name};")
    row_count = cur.fetchone()[0]
    output.append(f"\n**Row count:** {row_count}\n")

    # --- Data ---
    col_names = [c[1] for c in columns]
    cur.execute(f"SELECT * FROM {table_name};")
    rows = cur.fetchall()

    if rows:
        output.append("**Data:**\n")
        output.append("| " + " | ".join(col_names) + " |")
        output.append("|" + "---|" * len(col_names))
        for row in rows:
            row_str = [str(v) if v is not None else "NULL" for v in row]
            output.append("| " + " | ".join(row_str) + " |")
    else:
        output.append("_No data in this table._")

    output.append("\n---\n")

conn.close()

markup = "\n".join(output)

# Print to console
print(markup)

# Save to file
with open("school_db_markup.md", "w", encoding="utf-8") as f:
    f.write(markup)

print("\nFull database markup saved to school_db_markup.md") '''