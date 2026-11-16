"""Stands in for the fastbrowse CLI in tests: sleeps, then reports FAKE_FINAL_URL as where it ended."""
import json
import os
import sys
import time

time.sleep(float(os.environ.get("FAKE_NAV_SECONDS", "1.5")))
print(json.dumps({"status": "complete", "answer": "done", "final_url": os.environ.get("FAKE_FINAL_URL"),
                  "steps": [], "cost": {"lines": []}, "argv": sys.argv[1:]}))
