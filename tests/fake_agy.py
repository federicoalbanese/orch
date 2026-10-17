"""A stand-in for the agy binary, driven by environment variables (used by the adapter and failure tests).

FAKE_AGY_FIXTURE  NDJSON file to replay on stdout, byte for byte ({WORKDIR} is replaced with the cwd)
FAKE_AGY_STDERR   text to print on stderr
FAKE_AGY_EXIT     exit code (default 0)
FAKE_AGY_EDITS    JSON object {relative path: content} written before the result line
FAKE_AGY_PARTIAL  JSON object {relative path: content} written at once, before any output (work in progress)
FAKE_AGY_DELAY    seconds to sleep before each line
FAKE_AGY_ARGV     file to write the received argv to (JSON)
FAKE_AGY_PIDFILE  file to write this process's pid to
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
if os.environ.get("FAKE_AGY_PIDFILE"):
    Path(os.environ["FAKE_AGY_PIDFILE"]).write_text(str(os.getpid()))
cwd = os.getcwd()
for rel, content in json.loads(os.environ.get("FAKE_AGY_PARTIAL", "{}")).items():
    Path(cwd, rel).write_text(content)
delay = float(os.environ.get("FAKE_AGY_DELAY", "0"))
lines = []
if os.environ.get("FAKE_AGY_FIXTURE"):
    lines = Path(os.environ["FAKE_AGY_FIXTURE"]).read_bytes().replace(b"{WORKDIR}", cwd.encode()).splitlines()
edits = json.loads(os.environ.get("FAKE_AGY_EDITS", "{}"))
for line in lines:
    if delay:
        time.sleep(delay)
    if b'"event": "result"' in line or b'"event":"result"' in line:
        for rel, content in edits.items():
            Path(cwd, rel).write_text(content)
    sys.stdout.buffer.write(line + b"\n")
    sys.stdout.flush()
if os.environ.get("FAKE_AGY_STDERR"):
    print(os.environ["FAKE_AGY_STDERR"], file=sys.stderr, flush=True)
sys.exit(int(os.environ.get("FAKE_AGY_EXIT", "0")))
