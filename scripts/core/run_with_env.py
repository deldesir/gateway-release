#!/usr/bin/env python3
"""Load /opt/iiab/ai-gateway/.env robustly into os.environ, then run a target
script as __main__. Avoids shell `source` choking on values with colons/commas
(AUTHORIZED_USERS, URLs, etc.). Usage: run_with_env.py <script.py> [args...]"""
import os
import sys
import runpy

ENV = "/opt/iiab/ai-gateway/.env"
for line in open(ENV):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    v = v.strip()
    if v[:1] in "\"'" and v[-1:] == v[:1]:
        v = v[1:-1]
    os.environ[k.strip()] = v

os.environ.setdefault("RAPIDPRO_API_BASE", "http://localhost")
# The setup scripts use RAPIDPRO_HOST as their API base, but the live .env value
# is http://localhost:8080 (= courier, no /api/v2). Force the correct nginx API
# host for provisioning (script context only — the gateway service .env is untouched).
os.environ["RAPIDPRO_HOST"] = os.environ["RAPIDPRO_API_BASE"]

# Absolutize so the scripts' Path(__file__)-relative file paths (export JSON, etc.)
# resolve correctly even though the RapidPro subprocess runs with a different cwd.
target = os.path.abspath(sys.argv[1])
sys.argv = [target] + sys.argv[2:]
runpy.run_path(target, run_name="__main__")
