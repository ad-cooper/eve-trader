import io
import bz2
import csv
import re
import requests
import duckdb
from concurrent.futures import ThreadPoolExecutor, as_completed

REGION_ID = 10000002  # The Forge (Jita)
TARGET_YEAR = 2025
MAX_WORKERS = 8       # Parallel download threads

con = duckdb.connect("data/eve_analytics.duckdb")

con.execute("""
CREATE TABLE IF NOT EXISTS forge_market_history (
    date DATE,
    type_id INTEGER,
    average DOUBLE,
    highest DOUBLE,
    lowest DOUBLE,
    volume BIGINT,
    order_count INTEGER,
    PRIMARY KEY (date, type_id)
);
""")

# 1. Fetch dates already stored in DuckDB so we can skip them
existing_rows = con.execute("SELECT DISTINCT date FROM forge_market_history").fetchall()
existing_dates = set(str(r[0]) for r in existing_rows)
print(f"Database already contains {len(existing_dates)} days of market history.")

# 2. Fetch remote manifest
index_url = f"https://data.everef.net/market-history/{TARGET_YEAR}/index.json"
print(f"Fetching archive manifest for {TARGET_YEAR}...")
manifest = requests.get(index_url, timeout=15).json()
files = manifest.get("files", [])

# 3. Filter out files whose dates are already in our database
date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")

files_to_process = []
for f in files:
    match = date_pattern.search(f["name"])
    if match:
        file_date = match.group(1)
        if file_date not in existing_dates:
            files_to_process.append(f)

print(f"Skipping {len(files) - len(files_to_process)} already-ingested days.")
print(f"Remaining files to download: {len(files_to_process)}")

if not files_to_process:
    print("All files for this year are already ingested!")
    exit(0)

def process_file(file_info):
    url = file_info["url"]
    records = []
    try:
        r = requests.get(url, stream=True, timeout=30)
        decompressed = bz2.decompress(r.content).decode("utf-8")
        reader = csv.DictReader(io.StringIO(decompressed))
        for row in reader:
            if int(row["region_id"]) == REGION_ID:
                records.append((
                    row["date"],
                    int(row["type_id"]),
                    float(row["average"]),
                    float(row["highest"]),
                    float(row["lowest"]),
                    int(row["volume"]),
                    int(row["order_count"])
                ))
    except Exception as e:
        print(f"Error on {file_info['name']}: {e}")
    return records

completed = 0
buffer = []

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [executor.submit(process_file, f) for f in files_to_process]
    for future in as_completed(futures):
        res = future.result()
        if res:
            buffer.extend(res)
        completed += 1
        
        # Flush to disk every 25 files to keep memory usage low
        if len(buffer) >= 100000:
            con.executemany("""
                INSERT OR IGNORE INTO forge_market_history 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, buffer)
            con.commit()
            buffer = []
            
        if completed % 25 == 0 or completed == len(files_to_process):
            print(f"Progress: [{completed}/{len(files_to_process)}] files processed...")

# Flush remainder
if buffer:
    con.executemany("""
        INSERT OR IGNORE INTO forge_market_history 
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, buffer)
    con.commit()

total_rows = con.execute("SELECT count(*) FROM forge_market_history").fetchone()[0]
print(f"\nIngestion Complete! Total records in DuckDB: {total_rows:,}")