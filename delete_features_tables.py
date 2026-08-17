import sqlite3

conn = sqlite3.connect("school.db")
cursor = conn.cursor()
cursor.execute("DROP TABLE IF EXISTS fee_payments")
conn.commit()
conn.close()
print("Table 'fee_payments' deleted.")