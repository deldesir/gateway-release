#!/usr/bin/env python3
"""
ADR-013 Phase 4: Social-Code Service Bootstrap

Provisions the Social-Code training suite for WhatsApp integration:

  1. Create the social_drill_sessions table (PostgreSQL or SQLite fallback)
  2. Generate a systemd unit for the Social-Code FastAPI server (port 8089)
  3. Wire SOCIAL_CODE_URL into the Gateway and RiveBot .env files
  4. Verify the plugin registration (macro_social in manifest)

Prerequisites:
  - Gateway and RiveBot must be running
  - python scripts/core/setup_gateway.py must have been run first

Usage:
    cd /opt/iiab/ai-gateway
    source .env
    python scripts/setup_social_code.py
"""

import os
import sys
import subprocess
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ requests not installed. Run: pip install requests")
    sys.exit(1)


# ── Configuration ────────────────────────────────────────────────────────────

SOCIAL_CODE_DIR = Path("/opt/iiab/social-code")
SOCIAL_CODE_SRC = SOCIAL_CODE_DIR / "src"
GATEWAY_DIR = Path(__file__).parent.parent
GATEWAY_ENV = GATEWAY_DIR / ".env"
RIVEBOT_ENV = Path("/opt/iiab/rivebot/.env")

SOCIAL_CODE_PORT = 8089
SOCIAL_CODE_URL = f"http://127.0.0.1:{SOCIAL_CODE_PORT}"

POSTGRES_URI = os.getenv("POSTGRES_URI", "")
GATEWAY_URL = os.getenv("GATEWAY_PUBLIC_URL", "http://127.0.0.1:8086")
INTERNAL_KEY = os.getenv("GATEWAY_INTERNAL_KEY", "")

SYSTEMD_UNIT = f"""\
[Unit]
Description=Social-Code Training API
After=network.target ai-gateway.service
Wants=ai-gateway.service

[Service]
Type=simple
User=hermes
WorkingDirectory={SOCIAL_CODE_SRC}
EnvironmentFile={GATEWAY_ENV}
Environment="SOCIAL_CODE_PORT={SOCIAL_CODE_PORT}"

ExecStart=/root/.local/bin/uv run uvicorn social_api.server:app \\
    --host 127.0.0.1 \\
    --port {SOCIAL_CODE_PORT}

Restart=always
RestartSec=5

# Memory budget: Social-Code is lightweight (no LLM in-process)
MemoryMax=200M

[Install]
WantedBy=multi-user.target
"""


def _persist_env(env_path: Path, key: str, value: str, comment: str = ""):
    """Add or update a key in an .env file."""
    if not env_path.exists():
        return
    content = env_path.read_text()
    if key in content:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        env_path.write_text("\n".join(lines) + "\n")
        print(f"   ✅ Updated {key} in {env_path.name}")
    else:
        with open(env_path, "a") as f:
            if comment:
                f.write(f"\n# {comment}\n")
            f.write(f"{key}={value}\n")
        print(f"   ✅ Added {key} to {env_path.name}")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 1: Create Database Tables
# ═══════════════════════════════════════════════════════════════════════════

def step_create_tables():
    print("\n── Step 1: Create social_drill_sessions Table ──")

    if POSTGRES_URI and "postgresql" in POSTGRES_URI:
        # Use psycopg2 directly for DDL
        pg_uri = (POSTGRES_URI
                  .replace("postgresql+asyncpg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
        try:
            import psycopg2
            conn = psycopg2.connect(pg_uri)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS social_drill_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    lang TEXT DEFAULT 'en',
                    current_round INTEGER DEFAULT 1,
                    total_rounds INTEGER DEFAULT 5,
                    current_scenario TEXT,
                    context JSONB DEFAULT '{}',
                    scores JSONB DEFAULT '{}',
                    finished BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_drill_user
                ON social_drill_sessions (user_id)
            """)
            cur.close()
            conn.close()
            print(f"   ✅ Created table in PostgreSQL ({pg_uri.split('@')[1].split('/')[1]})")
        except ImportError:
            print("   ⚠️  psycopg2 not installed — falling back to SQLite")
            _create_sqlite_table()
        except Exception as e:
            print(f"   ❌ PostgreSQL failed: {e}")
            print("   ⚠️  Falling back to SQLite")
            _create_sqlite_table()
    else:
        _create_sqlite_table()


def _create_sqlite_table():
    import sqlite3
    db_path = SOCIAL_CODE_SRC / "social_code.sqlite"
    os.makedirs(SOCIAL_CODE_SRC, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS social_drill_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            mode TEXT NOT NULL,
            lang TEXT DEFAULT 'en',
            current_round INTEGER DEFAULT 1,
            total_rounds INTEGER DEFAULT 5,
            current_scenario TEXT,
            context TEXT DEFAULT '{}',
            scores TEXT DEFAULT '{}',
            finished INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_drill_user
        ON social_drill_sessions (user_id)
    """)
    conn.commit()
    conn.close()
    print(f"   ✅ Created SQLite table: {db_path}")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 2: Create Systemd Unit
# ═══════════════════════════════════════════════════════════════════════════

def step_create_systemd_unit():
    print("\n── Step 2: Systemd Unit ──")
    unit_path = Path("/etc/systemd/system/social-code.service")

    if unit_path.exists():
        print(f"   ✅ {unit_path} already exists")
        return

    try:
        unit_path.write_text(SYSTEMD_UNIT)
        subprocess.run(["systemctl", "daemon-reload"], check=True,
                        capture_output=True, timeout=10)
        print(f"   ✅ Created {unit_path}")
        print(f"   📋 To start: sudo systemctl enable --now social-code")
    except PermissionError:
        print(f"   ⚠️  Permission denied writing to {unit_path}")
        print(f"       Run this script as root, or manually create the unit:")
        fallback = SOCIAL_CODE_DIR / "social-code.service"
        fallback.write_text(SYSTEMD_UNIT)
        print(f"       cp {fallback} {unit_path}")
    except Exception as e:
        print(f"   ❌ Failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 3: Wire .env
# ═══════════════════════════════════════════════════════════════════════════

def step_wire_env():
    print("\n── Step 3: Wire SOCIAL_CODE_URL ──")
    _persist_env(GATEWAY_ENV, "SOCIAL_CODE_URL", SOCIAL_CODE_URL,
                 "ADR-013 Phase 4: Social-Code training API")
    _persist_env(RIVEBOT_ENV, "SOCIAL_CODE_URL", SOCIAL_CODE_URL,
                 "ADR-013 Phase 4: Social-Code training API")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 4: Verify Plugin Registration
# ═══════════════════════════════════════════════════════════════════════════

def step_verify_plugin():
    print("\n── Step 4: Verify Plugin Registration ──")
    try:
        headers = {"X-API-Key": INTERNAL_KEY} if INTERNAL_KEY else {}
        resp = requests.get(f"{GATEWAY_URL}/v1/system/plugins/manifest",
                            headers=headers, timeout=5)
        if resp.status_code == 200:
            plugins = resp.json().get("plugins", [])
            social = [p for p in plugins if p["name"] == "macro_social"]
            if social:
                p = social[0]
                print(f"   ✅ macro_social registered (trigger='{p['trigger']}')")
            else:
                print(f"   ⚠️  macro_social not in manifest ({len(plugins)} plugins loaded)")
                print(f"       Restart ai-gateway to trigger plugin discovery")
        else:
            print(f"   ⚠️  Manifest endpoint returned {resp.status_code}")
    except requests.RequestException as e:
        print(f"   ⚠️  Gateway unreachable: {e}")
        print(f"       This is expected if the gateway isn't running yet")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 5: List Available Apps
# ═══════════════════════════════════════════════════════════════════════════

def step_list_apps():
    print("\n── Step 5: Available Drill Apps ──")
    apps_dir = SOCIAL_CODE_DIR / "apps"
    if not apps_dir.exists():
        apps_dir = SOCIAL_CODE_DIR / "src" / "apps"

    if not apps_dir.exists():
        print("   ⚠️  No apps directory found — drill content not yet provisioned")
        print(f"       Expected at: {SOCIAL_CODE_DIR / 'apps'}")
        return

    apps = [d.name for d in apps_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
    if apps:
        print(f"   📋 {len(apps)} drill apps found:")
        for app in sorted(apps):
            print(f"       • {app}")
    else:
        print("   ⚠️  Apps directory exists but contains no drill modules")


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  ADR-013 Phase 4: Social-Code Bootstrap")
    print("=" * 60)

    step_create_tables()
    step_create_systemd_unit()
    step_wire_env()
    step_verify_plugin()
    step_list_apps()

    print("\n" + "=" * 60)
    print("  Social-Code bootstrap complete!")
    print()
    print("  SERVICES:")
    print(f"  • Social-Code API: {SOCIAL_CODE_URL}")
    print(f"  • Health check:    {SOCIAL_CODE_URL}/health")
    print()
    print("  NEXT STEPS:")
    print("  1. sudo systemctl enable --now social-code")
    print("  2. sudo systemctl restart ai-gateway  (reload plugins)")
    print("  3. Send 'social debate_club steelman' via WhatsApp")
    print("=" * 60)


if __name__ == "__main__":
    main()
