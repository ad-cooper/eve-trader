import io
import bz2
import csv
import requests
import duckdb

REGION_ID = 10000002  # The Forge (Jita)
TARGET_YEAR = 2025    # Historical archive year

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

index_url = f"https://data.everef.net/market-history/{TARGET_YEAR}/index.json"
print(f"Fetching archive index from {index_url}...")
resp = requests.get(index_url, timeout=15)
manifest = resp.json()

files = manifest.get("files", [])
if not files:
    print("No files found in index.")
    exit(1)

sample_files = files[-30:]
print(f"Ingesting {len(sample_files)} daily market archives...")

insert_buffer = []
for idx, f_info in enumerate(sample_files):
    file_url = f_info["url"]
    file_name = f_info["name"]
    print(f"[{idx+1}/{len(sample_files)}] Processing {file_name}...")
    
    try:
        r = requests.get(file_url, stream=True, timeout=30)
        decompressed = bz2.decompress(r.content).decode("utf-8")
        reader = csv.DictReader(io.StringIO(decompressed))
        
        for row in reader:
            if int(row["region_id"]) == REGION_ID:
                insert_buffer.append((
                    row["date"],
                    int(row["type_id"]),
                    float(row["average"]),
                    float(row["highest"]),
                    float(row["lowest"]),
                    int(row["volume"]),
                    int(row["order_count"])
                ))
        
        if len(insert_buffer) > 50000:
            con.executemany("""
                INSERT OR IGNORE INTO forge_market_history 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, insert_buffer)
            con.commit()
            insert_buffer = []

    except Exception as e:
        print(f"Error on {file_name}: {e}")

if insert_buffer:
    con.executemany("""
        INSERT OR IGNORE INTO forge_market_history 
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, insert_buffer)
    con.commit()

total = con.execute("SELECT count(*) FROM forge_market_history").fetchone()[0]
print(f"\nDone! Ingested {total:,} total Forge records into data/eve_analytics.duckdb.")
