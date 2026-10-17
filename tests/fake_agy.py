"""A stand-in for the agy binary, driven by environment variables (used by test_agy_adapter).

FAKE_AGY_FIXTURE  NDJSON file to replay on stdout ({WORKDIR} is replaced with the cwd)
FAKE_AGY_STDERR   text to print on stderr
FAKE_AGY_EXIT     exit code (default 0)
FAKE_AGY_EDITS    JSON object {relative path: content} written before the result line
FAKE_AGY_DELAY    seconds to sleep before each line
FAKE_AGY_ARGV     file to write the received argv to (JSON)
"""
import json
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("9.9.9-fake")
    sys.exit(0)
if os.environ.get("FAKE_AGY_ARGV"):
    Path(os.environ["FAKE_AGY_ARGV"]).write_text(json.dumps(sys.argv[1:]))
cwd = os.getcwd()
delay = float(os.environ.get("FAKE_AGY_DELAY", "0"))
lines = []
if os.environ.get("FAKE_AGY_FIXTURE"):
    lines = Path(os.environ["FAKE_AGY_FIXTURE"]).read_text().replace("{WORKDIR}", cwd).splitlines()
edits = json.loads(os.environ.get("FAKE_AGY_EDITS", "{}"))
for line in lines:
    if delay:
        time.sleep(delay)
    if '"event": "result"' in line or '"event":"result"' in line:
        for rel, content in edits.items():
            Path(cwd, rel).write_text(content)
    print(line, flush=True)
if os.environ.get("FAKE_AGY_STDERR"):
    print(os.environ["FAKE_AGY_STDERR"], file=sys.stderr, flush=True)
sys.exit(int(os.environ.get("FAKE_AGY_EXIT", "0")))
