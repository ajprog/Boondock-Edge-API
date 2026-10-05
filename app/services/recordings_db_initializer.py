from config import Config
from app.utils.sqlite_utils import connect_sqlite

# Get database path from centralized config
# Get recordings database path (will use event_name from settings if available)
DB_PATH = Config.get_recordings_db_path()

def initialize_db():
    """Initialize the SQLite database and create tables."""
    # Ensure database directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = connect_sqlite(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            filename TEXT,
            timestamp INTEGER,
            transcription TEXT,
            status TEXT DEFAULT 'new',
            backed_up BOOLEAN DEFAULT FALSE,
            is_duplicate BOOLEAN DEFAULT FALSE,
            filesize INTEGER DEFAULT 0,
            duration REAL,
            crc TEXT,
            is_hallucination BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at INTEGER NOT NULL DEFAULT
                (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        )
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_recordings_timestamp 
        ON recordings(timestamp DESC)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_channel_timestamp 
        ON recordings(channel_id, timestamp DESC)
    ''')
    
    
     # Create the tag_relation table to associate tags with recordings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tag_relation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            FOREIGN KEY(recording_id) REFERENCES recordings(id)
        )
    ''')
    
    # Create the recording_history table for version control
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recording_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            transcription TEXT,
            audio_filename TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT,
            FOREIGN KEY(recording_id) REFERENCES recordings(id),
            UNIQUE(recording_id, version_number)
        )
    ''')
        
    # Create the maintenance_history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS maintenance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            description TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            duration_seconds REAL,
            status TEXT NOT NULL,
            details JSON,
            error_message TEXT
        )
    ''')
    
    # Create index on task_id and started_at for faster queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_maintenance_history_task_id 
        ON maintenance_history(task_id)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_maintenance_history_started_at 
        ON maintenance_history(started_at DESC)
    ''')
    
    # Create the system_usage table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calculated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            application_size_bytes INTEGER,
            audio_files_size_bytes INTEGER,
            logs_size_bytes INTEGER,
            database_size_bytes INTEGER,
            per_channel_usage JSON,
            total_disk_used_bytes INTEGER,
            total_disk_available_bytes INTEGER
        )
    ''')
    
    # Create index on calculated_at for faster queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_system_usage_calculated_at 
        ON system_usage(calculated_at DESC)
    ''')
    
    conn.commit()
    conn.close()
