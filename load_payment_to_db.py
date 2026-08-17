import sqlite3
import pandas as pd

# 1. Connect to / create SQLite database
db_name = 'school.db'
conn = sqlite3.connect(db_name)
cursor = conn.cursor()

# 2. Define schema for fee_payments table matching the features
create_table_sql = """
CREATE TABLE IF NOT EXISTS fee_payments (
    payment_id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    term TEXT NOT NULL,
    amount_due REAL NOT NULL,
    amount_paid REAL NOT NULL,
    cumulative_paid REAL NOT NULL,
    balance_remaining REAL NOT NULL,
    payment_date TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);
"""

cursor.execute(create_table_sql)
conn.commit()

# 3. Load CSV file into pandas DataFrame
csv_file = r'C:\Users\Joseph\Desktop\Database\csv\fees_payment.csv'
df = pd.read_csv(csv_file)

# 4. Insert data into SQLite table
df.to_sql('fees_payment', conn, if_exists='append', index=False)

print(f"Successfully loaded {len(df)} records into '{db_name}' -> 'fee_payments' table!")

# 5. Verify database contents
df_db = pd.read_sql_query("SELECT * FROM fee_payments LIMIT 10;", conn)
print("\nFirst 10 records in 'school.db':")
print(df_db.to_string(index=False))

# Close connection
conn.close()