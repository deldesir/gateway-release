#!/usr/bin/env python3
"""
ADR-013: Gateway Bootstrap Script

Validates and provisions the minimum viable configuration for the
AI Gateway + RiveBot + RapidPro pipeline. Intended for first-time
setup or disaster recovery.

Steps:
  1. Validate required env vars (GEMINI_API_KEY, RAPIDPRO_API_TOKEN, etc.)
  2. Ensure GATEWAY_INTERNAL_KEY exists (generate if missing)
  3. Initialize SQLite databases (checkpoints, audit)
  4. Verify RapidPro Admins group exists (create if missing)
  5. Add ADMIN_PHONE to Admins group
  6. Verify RiveBot ↔ Gateway connectivity
  7. Sync .env files between services
  8. Print diagnostic summary

Usage:
    cd /opt/iiab/ai-gateway
    source .env
    python scripts/setup_gateway.py
"""

import os
import sys
import secrets
import sqlite3
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ requests not installed. Run: pip install requests")
    sys.exit(1)


# ── Configuration ────────────────────────────────────────────────────────────

GATEWAY_DIR = Path(__file__).parent.parent
RIVEBOT_DIR = Path("/opt/iiab/rivebot")
GATEWAY_ENV = GATEWAY_DIR / ".env"
RIVEBOT_ENV = RIVEBOT_DIR / ".env"

RAPIDPRO_HOST = os.getenv("RAPIDPRO_API_BASE", "http://localhost")  # nginx /api/v2 (NOT :8080 = courier)
API_TOKEN = os.getenv("RAPIDPRO_API_TOKEN", "")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")
GATEWAY_URL = "http://127.0.0.1:8086"
RIVEBOT_URL = "http://127.0.0.1:8087"

API_URL = f"{RAPIDPRO_HOST}/api/v2"


def _env_get(key: str) -> str:
    return os.getenv(key, "")


def _persist_env(env_path: Path, key: str, value: str, comment: str = ""):
    """Add or update a key in an .env file."""
    if not env_path.exists():
        env_path.write_text("")

    content = env_path.read_text()
    if key in content:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        env_path.write_text("\n".join(lines) + "\n")
        return "updated"
    else:
        with open(env_path, "a") as f:
            if comment:
                f.write(f"\n# {comment}\n")
            f.write(f"{key}={value}\n")
        return "created"


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 1: Validate Required Environment
# ═══════════════════════════════════════════════════════════════════════════

def step_validate_env():
    print("\n── Step 1: Validate Environment ──")
    required = {
        "GEMINI_API_KEY": "LLM provider key (Gemini)",
        "RAPIDPRO_API_TOKEN": "RapidPro API access",
        "ADMIN_PHONE": "Primary admin WhatsApp number",
        "HERMES_PROVIDER": "LLM routing (should be 'gemini')",
        "LLM_MODEL": "Default LLM model name",
    }
    recommended = {
        "GATEWAY_API_KEY": "Public API key for /v1/chat/completions",
        "GATEWAY_INTERNAL_KEY": "Internal service auth (RiveBot → Gateway)",
        "AUTHORIZED_USERS": "Admin phone:name pairs for persona auto-upgrade",
        "POSTGRES_URI": "PostgreSQL connection for async session storage",
    }

    ok = True
    for key, desc in required.items():
        val = _env_get(key)
        if val:
            display = f"{val[:8]}..." if len(val) > 12 else val
            print(f"   ✅ {key} = {display} ({desc})")
        else:
            print(f"   ❌ {key} NOT SET ({desc})")
            ok = False

    for key, desc in recommended.items():
        val = _env_get(key)
        if val:
            print(f"   ✅ {key} = set ({desc})")
        else:
            print(f"   ⚠️  {key} not set ({desc})")

    return ok


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 2: Ensure GATEWAY_INTERNAL_KEY
# ═══════════════════════════════════════════════════════════════════════════

def step_ensure_internal_key():
    print("\n── Step 2: Internal Service Key ──")
    key = _env_get("GATEWAY_INTERNAL_KEY")
    if key:
        print(f"   ✅ GATEWAY_INTERNAL_KEY already set ({key[:8]}...)")
        return key

    # Generate a new 32-byte hex key
    key = secrets.token_hex(16)
    _persist_env(GATEWAY_ENV, "GATEWAY_INTERNAL_KEY", key,
                 "ADR-013: Internal service auth (RiveBot → Gateway)")
    _persist_env(RIVEBOT_ENV, "GATEWAY_INTERNAL_KEY", key,
                 "ADR-013: Must match ai-gateway's GATEWAY_INTERNAL_KEY")
    print(f"   🔑 Generated GATEWAY_INTERNAL_KEY: {key[:8]}...")
    print(f"   📁 Written to {GATEWAY_ENV.name} and {RIVEBOT_ENV.name}")
    return key


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 3: Initialize Databases
# ═══════════════════════════════════════════════════════════════════════════

def step_init_databases():
    print("\n── Step 3: Initialize Databases ──")

    # Gateway checkpoints DB
    db_path = _env_get("SQLITE_DB_PATH") or str(GATEWAY_DIR / "checkpoints.sqlite")
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        print(f"   ✅ Gateway DB: {db_path} (WAL mode)")
    except Exception as e:
        print(f"   ❌ Gateway DB failed: {e}")

    # RiveBot audit DB
    audit_path = _env_get("RIVEBOT_AUDIT_DB") or "/opt/iiab/rivebot/data/audit.db"
    try:
        os.makedirs(os.path.dirname(audit_path), exist_ok=True)
        conn = sqlite3.connect(audit_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                user_id TEXT,
                macro TEXT,
                args TEXT,
                status TEXT,
                duration_ms INTEGER,
                auth_method TEXT
            )
        ''')
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()
        print(f"   ✅ Audit DB: {audit_path} (WAL mode, schema verified)")
    except Exception as e:
        print(f"   ❌ Audit DB failed: {e}")

    # Hermes session/palace directories
    hermes_home = _env_get("HERMES_HOME") or str(GATEWAY_DIR / "data/hermes")
    palace_path = _env_get("MEMPALACE_PALACE_PATH") or str(GATEWAY_DIR / "data/palace")
    for d in [hermes_home, palace_path]:
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    print(f"   ✅ Hermes home: {hermes_home}")
    print(f"   ✅ MemPalace:   {palace_path}")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 4: Verify RapidPro Admins Group
# ═══════════════════════════════════════════════════════════════════════════

def step_verify_admins_group():
    print("\n── Step 4: RapidPro Admins Group ──")
    if not API_TOKEN:
        print("   ⚠️  RAPIDPRO_API_TOKEN not set — skipping RapidPro setup")
        return None

    headers = {"Authorization": f"Token {API_TOKEN}"}
    try:
        resp = requests.get(f"{API_URL}/groups.json", params={"name": "Admins"},
                            headers=headers, timeout=10)
        resp.raise_for_status()
        groups = resp.json().get("results", [])
        if groups:
            g = groups[0]
            print(f"   ✅ Admins group exists: {g['uuid']} ({g['count']} members)")
            return g["uuid"]

        # Create it
        resp = requests.post(f"{API_URL}/groups.json", headers=headers,
                             json={"name": "Admins"}, timeout=10)
        resp.raise_for_status()
        uuid = resp.json()["uuid"]
        print(f"   ✅ Created Admins group: {uuid}")
        _persist_env(GATEWAY_ENV, "ADMINS_GROUP_UUID", uuid,
                     "RapidPro Admins group for RBAC")
        return uuid

    except requests.RequestException as e:
        print(f"   ❌ RapidPro unreachable: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 5: Add Admin to Group
# ═══════════════════════════════════════════════════════════════════════════

def _admin_urn(entry: str) -> str:
    """Scheme-aware admin URN: email -> mailto:, already-schemed kept, else whatsapp:."""
    entry = entry.strip()
    if "@" in entry:
        return f"mailto:{entry}"
    if ":" in entry:
        return entry
    return f"whatsapp:{entry}"


def step_add_admin(admins_uuid):
    print("\n── Step 5: Admin Contact ──")
    if not admins_uuid or not ADMIN_PHONE:
        print("   ⚠️  Skipping (no group UUID or ADMIN_PHONE)")
        return

    headers = {"Authorization": f"Token {API_TOKEN}"}
    # ADMIN_PHONE may be a comma-separated list of phones and/or emails.
    for entry in [e for e in ADMIN_PHONE.split(",") if e.strip()]:
        urn = _admin_urn(entry)
        try:
            resp = requests.get(f"{API_URL}/contacts.json", params={"urn": urn},
                                headers=headers, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                print(f"   ⚠️  No contact yet for {urn} (joins on first message)")
                continue
            contact = results[0]
            group_uuids = [g["uuid"] for g in contact.get("groups", [])]
            if admins_uuid in group_uuids:
                print(f"   ✅ {contact.get('name') or urn} already in Admins group")
            else:
                requests.post(f"{API_URL}/contact_actions.json", headers=headers,
                              json={"contacts": [contact["uuid"]], "action": "add",
                                    "group": admins_uuid}, timeout=10)
                print(f"   ✅ Added {contact.get('name') or urn} to Admins group")
        except requests.RequestException as e:
            print(f"   ❌ Contact lookup failed for {urn}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 6: Verify Service Connectivity
# ═══════════════════════════════════════════════════════════════════════════

def step_verify_connectivity():
    print("\n── Step 6: Service Connectivity ──")
    services = {
        "AI Gateway":  f"{GATEWAY_URL}/health",
        "RiveBot":     f"{RIVEBOT_URL}/health",
        "RapidPro":    f"{RAPIDPRO_HOST}/api/v2/org.json",
    }
    for name, url in services.items():
        try:
            headers = {}
            if "rapidpro" in url.lower() or "org.json" in url:
                headers["Authorization"] = f"Token {API_TOKEN}"
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code < 400:
                print(f"   ✅ {name:15s} → {url} ({resp.status_code})")
            else:
                print(f"   ⚠️  {name:15s} → {url} ({resp.status_code})")
        except requests.RequestException:
            print(f"   🔴 {name:15s} → {url} (UNREACHABLE)")


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 7: Sync .env
# ═══════════════════════════════════════════════════════════════════════════

def step_sync_env():
    print("\n── Step 7: Sync .env Files ──")
    # Keys that must match between Gateway and RiveBot
    shared_keys = [
        ("GATEWAY_INTERNAL_KEY", "Internal service auth"),
        ("RAPIDPRO_API_TOKEN", "RapidPro access"),
        ("RAPIDPRO_API_URL", "RapidPro API base URL"),
    ]

    synced = 0
    for key, desc in shared_keys:
        gw_val = _env_get(key)
        if not gw_val:
            continue

        # Check if RiveBot has this key
        rb_content = RIVEBOT_ENV.read_text() if RIVEBOT_ENV.exists() else ""
        if key not in rb_content:
            _persist_env(RIVEBOT_ENV, key, gw_val, f"Synced from ai-gateway ({desc})")
            print(f"   📋 Synced {key} → {RIVEBOT_ENV.name}")
            synced += 1
    if synced == 0:
        print("   ✅ All shared keys already in sync")


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  ADR-013: Gateway Bootstrap")
    print("=" * 60)

    env_ok = step_validate_env()
    if not env_ok:
        print("\n   ⚠️  Some required vars missing — continuing with what we have.")

    step_ensure_internal_key()
    step_init_databases()
    admins_uuid = step_verify_admins_group()
    step_add_admin(admins_uuid)
    step_verify_connectivity()
    step_sync_env()

    print("\n" + "=" * 60)
    print("  Bootstrap complete!")
    print()
    print("  NEXT STEPS:")
    print("  1. sudo systemctl restart ai-gateway rivebot")
    print("  2. python scripts/core/verify_pipeline.py   (smoke test)")
    print("  3. python scripts/flows/setup_crm_ops.py     (CRM flows)")
    print("=" * 60)


if __name__ == "__main__":
    main()
