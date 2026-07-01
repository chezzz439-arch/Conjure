#!/usr/bin/env python3
"""
Run this once after adding Supabase credentials to .env.
Creates the storage bucket and verifies the connection.
Usage: .venv/bin/python3 setup_supabase.py
"""
import os
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_BUCKET   = os.getenv("SUPABASE_BUCKET", "conjure-models")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_ANON_KEY in .env first")
    exit(1)

from supabase import create_client

print(f"Connecting to Supabase at {SUPABASE_URL}...")
client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
print("Connected.")

try:
    buckets = client.storage.list_buckets()
    existing = [b.name for b in buckets]
    print(f"Existing buckets: {existing}")
except Exception as e:
    print(f"Could not list buckets: {e}")
    existing = []

if SUPABASE_BUCKET not in existing:
    try:
        client.storage.create_bucket(SUPABASE_BUCKET, options={"public": True})
        print(f"Created bucket: {SUPABASE_BUCKET}")
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(f"Bucket {SUPABASE_BUCKET} already exists — OK")
        elif "403" in str(e) or "Unauthorized" in str(e) or "security policy" in str(e).lower():
            print(f"Bucket creation requires service_role key — assuming '{SUPABASE_BUCKET}' already exists in dashboard")
        else:
            print(f"Bucket creation error: {e}")
            exit(1)
else:
    print(f"Bucket {SUPABASE_BUCKET} already exists — OK")

test_content = b"conjure storage test"
test_path = f"test/conjure_test_{int(time.time())}.txt"

try:
    client.storage.from_(SUPABASE_BUCKET).upload(
        path=test_path,
        file=test_content,
        file_options={"content-type": "text/plain", "upsert": "true"}
    )
    public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(test_path)
    print(f"Test upload OK")
    print(f"Public URL: {public_url}")
except Exception as e:
    print(f"Test upload failed: {e}")
    print(f"NOTE: Create the '{SUPABASE_BUCKET}' bucket manually in the Supabase dashboard (Storage tab) and set it to Public.")
    exit(1)

print("")
print("Supabase setup complete.")
print("Restart the server and run: curl http://localhost:8001/api/supabase/status")
