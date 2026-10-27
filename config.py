# config.py
import os
import hashlib
import logging
from pathlib import Path

Logger = logging.getLogger(__name__)

# Application code lives in CODE_ROOT (for example, /api). Persistent data
# lives in DATA_ROOT (for example, /). Override the default parent directory
# with BOONDOCK_DATA_ROOT when needed.
CODE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(
    os.environ.get('BOONDOCK_DATA_ROOT', str(CODE_ROOT.parent))
).expanduser().resolve()

def _stable_secret_key() -> str:
    """
    Return a stable SECRET_KEY that survives server restarts.

    Priority:
      1. SECRET_KEY environment variable (recommended for production).
      2. A key persisted in DATA_ROOT/db/.secret_key (auto-generated once).
      3. An in-process fallback derived from the machine hostname (last resort,
         causes sessions to break across different hosts).
    """
    # 1. Explicit environment variable
    env_key = os.environ.get('SECRET_KEY', '').strip()
    if env_key:
        return env_key

    # 2. File-persisted key (generated once, survives restarts)
    key_file = DATA_ROOT / 'db' / '.secret_key'
    try:
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        if os.path.exists(key_file):
            key = open(key_file, 'r').read().strip()
            if len(key) >= 32:
                return key
        # Generate and persist a new key
        import secrets as _secrets
        key = _secrets.token_hex(32)
        with open(key_file, 'w') as f:
            f.write(key)
        return key
    except Exception as exc:
        Logger.warning(f"Could not persist SECRET_KEY to {key_file}: {exc}")

    # 3. Hostname-based fallback
    import socket
    Logger.warning(
        "Using hostname-derived SECRET_KEY — sessions will NOT survive server restarts. "
        "Set the SECRET_KEY environment variable for production."
    )
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()


class Config:
    # Official release repositories; environment overrides are optional.
    API_REPOSITORY = os.environ.get(
        "BOONDOCK_API_REPOSITORY", "Boondock-Echo/Boondock-Edge-API"
    )
    DASHBOARD_REPOSITORY = os.environ.get(
        "BOONDOCK_DASHBOARD_REPOSITORY", "Boondock-Echo/Boondock-Edge-Dashboard"
    )

    FLASK_HOST = '0.0.0.0'
    FLASK_PORT = int(os.environ.get('FLASK_PORT', '4000'))
    CHANNELS = 6

    # Stable secret key — required for signed cookies/sessions.
    # Override via SECRET_KEY environment variable in production.
    SECRET_KEY = _stable_secret_key()
    
    # Production server type: 'gevent', 'waitress', or 'gunicorn'
    # - 'gevent': Best WebSocket support, works on Windows & Linux (recommended)
    # - 'waitress': Cross-platform, but limited WebSocket support
    # - 'gunicorn': Linux only, best performance with gevent workers
    # Can be overridden by environment variable: PRODUCTION_SERVER=gevent
    PRODUCTION_SERVER = os.environ.get('PRODUCTION_SERVER', 'gevent').lower()
    
    # Production mode: Set to True to use production server
    # Set to False to use Flask development server (for debugging)
    # Can be overridden by environment variable: PRODUCTION_MODE=true
    PRODUCTION_MODE = os.environ.get('PRODUCTION_MODE', 'true').lower() == 'true'
    
    # Database Configuration
    # Base directory for all database files (relative to DATA_ROOT)
    DB_DIR = 'db'
    
    # Database file names
    # Recordings database name is dynamic based on event_name setting
    # Use get_recordings_db_path() method to get the full path
    RECORDINGS_DB_NAME = 'default'  # Default name, can be overridden by event_name
    LOGS_DB_NAME = 'logs.db'
    SETTINGS_DB_NAME = 'settings.db'
    
    # Logs Directory Configuration
    # Logs directory is in the project root (parent of backend/ directory)
    # This is where file-based logs are stored (logs/YYYY/MM/YYYY-MM-DD_*.log)
    LOGS_DIR = 'logs'  # Relative to project root
    
    @staticmethod
    def get_db_dir():
        """
        Get the absolute path to the database directory.
        Returns: Absolute path to DATA_ROOT/db/
        """
        return DATA_ROOT / Config.DB_DIR
    
    @staticmethod
    def get_recordings_db_path(event_name=None):
        """
        Get the path to the recordings database file.
        
        Args:
            event_name: Optional event name. If None, will try to get from settings.
                       Falls back to 'default' if not available.
        
        Returns:
            Absolute path to the recordings database file
        """
        # If event_name not provided, try to get from settings
        if event_name is None:
            try:
                from app.services.settings_manager import get_settings_manager
                settings = get_settings_manager().get_all_settings()
                event_name = settings.get("event_name", "default")
            except Exception:
                event_name = Config.RECORDINGS_DB_NAME
        
        return Config.get_db_dir() / f"{event_name}.db"
    
    @staticmethod
    def get_logs_db_path():
        """
        Get the path to the logs database file.
        
        Returns:
            Absolute path to the logs database file
        """
        return Config.get_db_dir() / Config.LOGS_DB_NAME
    
    @staticmethod
    def get_settings_db_path():
        """
        Get the path to the settings database file.
        
        Returns:
            Absolute path to the settings database file
        """
        return Config.get_db_dir() / Config.SETTINGS_DB_NAME
    
    @staticmethod
    def get_logs_dir():
        """
        Get the absolute path to the logs directory.
        Logs are stored in project root (parent of backend/) in logs/ folder.
        
        Returns:
            Absolute path to the logs directory (project_root/logs/)
        """
        return DATA_ROOT / Config.LOGS_DIR