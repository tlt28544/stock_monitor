#!/usr/bin/env python3
"""Check COHR and POWL status."""
import sqlite3
import os
import sys

# Check database
db_path = '/home/admin/.openclaw/workspace/repos/stock_monitor/pipeline.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("=== Database Status ===")
cursor.execute("SELECT ticker, status, stage, error FROM state WHERE ticker IN ('COHR', 'POWL') ORDER BY ticker")
rows = cursor.fetchall()
for row in rows:
    print(f"Ticker: {row[0]}, Status: {row[1]}, Stage: {row[2]}, Error: {row[3]}")

conn.close()

# Check files
print("\n=== File Status ===")
report_dir = '/home/admin/.openclaw/workspace/repos/stock_monitor/reports/2026-10-13'
if os.path.exists(report_dir):
    files = os.listdir(report_dir)
    pdfs = [f for f in files if f.endswith('.pdf')]
    print(f"Total PDFs: {len(pdfs)}")
    print(f"COHR.pdf exists: {os.path.exists(os.path.join(report_dir, 'COHR.pdf'))}")
    print(f"POWL.pdf exists: {os.path.exists(os.path.join(report_dir, 'POWL.pdf'))}")
    
    # List all PDFs
    print("\n=== All PDFs ===")
    for pdf in sorted(pdfs):
        print(pdf)
else:
    print(f"Report directory does not exist: {report_dir}")

sys.exit(0)
