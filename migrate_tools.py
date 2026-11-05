
import sqlite3
import os

DB_PATH = "checkpoints.sqlite"

def migrate():
    print(f"Checking {DB_PATH} for schema updates...")
    
    if not os.path.exists(DB_PATH):
        print("Database does not exist yet. It will be created by the app.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if column exists
        cursor.execute("PRAGMA table_info(konex_personas)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "allowed_tools" not in columns:
            print("Adding 'allowed_tools' column to 'konex_personas'...")
            # SQLite doesn't support adding JSON column type explicitly, TEXT is fine for JSON
            cursor.execute("ALTER TABLE konex_personas ADD COLUMN allowed_tools TEXT DEFAULT '[]'")
            conn.commit()
            print("Migration successful.")
        else:
            print("Column 'allowed_tools' already exists.")
            
    except Exception as e:
        print(f"Migration failed: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
