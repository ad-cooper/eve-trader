import duckdb

con = duckdb.connect("data/eve_analytics.duckdb")

# Automatically detect CSV schema and create the transactions table
con.execute("""
CREATE OR REPLACE TABLE my_transactions AS 
SELECT * FROM read_csv_auto('data/transaction_export.csv');
""")

# Inspect the loaded columns and sample rows
print("Columns imported:")
print(con.execute("DESCRIBE my_transactions").df())

print("\nRecent 5 sales:")
print(con.execute("SELECT * FROM my_transactions LIMIT 5").df())

