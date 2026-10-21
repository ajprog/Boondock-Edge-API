# app/services/maintenance_service.py
"""
Maintenance Service - Handles overnight maintenance tasks.

This module provides a centralized, modular system for maintenance operations:
- Data backup (based on Backup & Restore settings)
- Logs cleanup (removes logs older than 30 days)
- Disk usage calculations (tracks system resource usage)

All maintenance tasks are logged to the maintenance_history table for audit purposes.
"""
import json
import sqlite3
import logging
import shutil
from config import Config, DATA_ROOT
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, List

from .s3_backup_service import run_backup_job
from .log_cleanup_service import cleanup_old_logs

log = logging.getLogger(__name__)

# Database path from centralized config

DB_PATH = Config.get_recordings_db_path()

# Task IDs
TASK_ID_BACKUP = 'data_backup'
TASK_ID_LOGS_CLEANUP = 'logs_cleanup'
TASK_ID_HEALTH_CHECKS = 'health_checks'

# Maintenance task descriptions
TASK_DESCRIPTIONS = {
    TASK_ID_BACKUP: 'Data backup based on Backup & Restore settings',
    TASK_ID_LOGS_CLEANUP: 'Logs cleanup (removes logs older than 30 days)',
    TASK_ID_HEALTH_CHECKS: 'Health checks (database sizes, top tables, disk usage)'
}


class MaintenanceService:
    """Encapsulated maintenance service for handling overnight maintenance tasks."""
    
    def __init__(self):
        self.db_path = DB_PATH
    
    def _get_db_connection(self):
        """Get database connection."""
        return sqlite3.connect(self.db_path)
    
    def _log_maintenance_task(
        self,
        task_id: str,
        description: str,
        started_at: datetime,
        completed_at: Optional[datetime],
        status: str,
        details: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> int:
        """
        Log a maintenance task to the database.
        
        Args:
            task_id: Unique identifier for the task
            description: Human-readable description
            started_at: When the task started
            completed_at: When the task completed (None if still running)
            status: 'success', 'failed', or 'running'
            details: Optional JSON string with additional details
            error_message: Optional error message if task failed
        
        Returns:
            The ID of the inserted record
        """
        conn = self._get_db_connection()
        cursor = conn.cursor()
        
        duration_seconds = None
        if completed_at:
            duration_seconds = (completed_at - started_at).total_seconds()
        
        cursor.execute('''
            INSERT INTO maintenance_history 
            (task_id, description, started_at, completed_at, duration_seconds, status, details, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id,
            description,
            started_at.isoformat(),
            completed_at.isoformat() if completed_at else None,
            duration_seconds,
            status,
            details,
            error_message
        ))
        
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return record_id
    
    def run_backup_task(self) -> Dict:
        """
        Run data backup task based on Backup & Restore settings.
        
        Returns:
            Dict with task execution details
        """
        task_id = TASK_ID_BACKUP
        description = TASK_DESCRIPTIONS[task_id]
        started_at = datetime.now(timezone.utc)
        
        log.info(f"Starting maintenance task: {description}")
        
        try:
            # Run backup job (incremental, both destinations)
            run_backup_job(manual=False, backup_type='incremental', destination='both')
            
            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()
            
            details = json.dumps({
                'backup_type': 'incremental',
                'destination': 'both'
            })
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='success',
                details=details
            )
            
            log.info(f"Maintenance task completed: {description} (took {duration:.2f} seconds)")
            
            return {
                'task_id': task_id,
                'status': 'success',
                'duration_seconds': duration,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat()
            }
            
        except Exception as e:
            completed_at = datetime.now(timezone.utc)
            error_msg = str(e)
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='failed',
                error_message=error_msg
            )
            
            log.error(f"Maintenance task failed: {description} - {error_msg}", exc_info=True)
            
            return {
                'task_id': task_id,
                'status': 'failed',
                'error': error_msg,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat()
            }
    
    def run_logs_cleanup_task(self) -> Dict:
        """
        Run logs cleanup task (removes logs older than 30 days).
        
        Returns:
            Dict with task execution details
        """
        task_id = TASK_ID_LOGS_CLEANUP
        description = TASK_DESCRIPTIONS[task_id]
        started_at = datetime.now(timezone.utc)
        
        log.info(f"Starting maintenance task: {description}")
        
        try:
            # Run log cleanup
            result = cleanup_old_logs()
            
            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()
            
            details = json.dumps({
                'files_deleted': result.get('files_deleted', 0),
                'bytes_freed': result.get('bytes_freed', 0),
                'cutoff_date': result.get('cutoff_date'),
                'retention_days': result.get('retention_days', 30)
            })
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='success',
                details=details
            )
            
            log.info(f"Maintenance task completed: {description} (took {duration:.2f} seconds)")
            
            return {
                'task_id': task_id,
                'status': 'success',
                'duration_seconds': duration,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat(),
                'files_deleted': result.get('files_deleted', 0),
                'bytes_freed': result.get('bytes_freed', 0)
            }
            
        except Exception as e:
            completed_at = datetime.now(timezone.utc)
            error_msg = str(e)
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='failed',
                error_message=error_msg
            )
            
            log.error(f"Maintenance task failed: {description} - {error_msg}", exc_info=True)
            
            return {
                'task_id': task_id,
                'status': 'failed',
                'error': error_msg,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat()
            }
    
    def _calculate_directory_size(self, directory: Path) -> int:
        """Calculate total size of a directory in bytes."""
        total_size = 0
        try:
            for file_path in directory.rglob("*"):
                if file_path.is_file():
                    try:
                        total_size += file_path.stat().st_size
                    except (OSError, FileNotFoundError):
                        pass
        except Exception as e:
            log.warning("Error calculating size for %s: %s", directory, e)
        
        return total_size

    def _get_top_tables(self, db_path, limit: int = 10) -> List[Dict]:
        """
        Get top N largest tables in a database.
        
        Args:
            db_path: Path to the database file
            limit: Number of top tables to return
        
        Returns:
            List of dicts with table name and size info
        """
        if not db_path.exists():
            return []
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Get all user tables
            cursor.execute('''
                SELECT name 
                FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ''')
            
            tables = []
            for row in cursor.fetchall():
                table_name = row[0]
                try:
                    # Get row count
                    quoted_table = '"' + table_name.replace('"', '""') + '"'
                    cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
                    row_count = cursor.fetchone()[0]
                    
                    # Get table size using SQLite's internal statistics
                    # This query estimates the size based on the number of pages used
                    cursor.execute(f'''
                        SELECT 
                            (SELECT COUNT(*) FROM {table_name}) as row_count,
                            (SELECT page_count FROM pragma_page_count()) as page_count,
                            (SELECT page_size FROM pragma_page_size()) as page_size
                    ''')
                    
                    # Alternative: use a simpler approach - estimate based on row count
                    # For a more accurate size, we'd need to analyze the actual data
                    # For now, we'll use a rough estimate: average row size * row count
                    # This is just an approximation
                    estimated_size = row_count * 500  # Rough estimate: 500 bytes per row average
                    
                    tables.append({
                        'table_name': table_name,
                        'row_count': row_count,
                        'estimated_size_bytes': estimated_size
                    })
                except sqlite3.Error as e:
                    log.warning(f"Error getting info for table {table_name} in {db_path}: {e}")
                    continue
            
            conn.close()
            
            # Sort by estimated size and return top N
            tables.sort(key=lambda x: x['estimated_size_bytes'], reverse=True)
            return tables[:limit]
            
        except Exception as e:
            log.error(f"Error getting top tables from {db_path}: {e}")
            return []
    
    def run_health_checks_task(self) -> Dict:
        """
        Run health checks: database sizes, top 10 largest tables, and disk usage.
        
        Checks:
        - Database sizes for all databases (recordings.db, logs.db, settings.db)
        - Top 10 largest tables in each database
        - Disk usage (total, used, free)
        
        Results are stored in health_stats table in logs.db.
        
        Returns:
            Dict with task execution details
        """
        task_id = TASK_ID_HEALTH_CHECKS
        description = TASK_DESCRIPTIONS[task_id]
        started_at = datetime.now(timezone.utc)
        
        log.info(f"Starting maintenance task: {description}")
        
        try:
            # Get all database paths
            recordings_db_path = Config.get_recordings_db_path()
            logs_db_path = Config.get_logs_db_path()
            settings_db_path = Config.get_settings_db_path()
            
            # Get database sizes
            database_sizes = {}
            # Get top 10 largest tables for each database
            top_tables = {}
            for db_name, db_path in [
                ('recordings', recordings_db_path),
                ('logs', logs_db_path),
                ('settings', settings_db_path)
            ]:
                if db_path.exists():
                    try:
                        top_tables[db_name] = self._get_top_tables(db_path, limit=10)
                        size = db_path.stat().st_size
                        database_sizes[db_name] = {
                            'path': db_path,
                            'size_bytes': size
                        }
                    except (OSError, FileNotFoundError) as e:
                        log.warning(f"Could not get size for {db_name} database: {e}")
                        database_sizes[db_name] = {
                            'path': db_path,
                            'size_bytes': 0,
                            'error': str(e)
                        }
                else:
                    database_sizes[db_name] = {
                        'path': db_path,
                        'size_bytes': 0,
                        'error': 'Database file does not exist'
                    }
                    top_tables[db_name] = []
            
            # Get disk usage
            disk_usage_info = {}
            try:
                disk_usage = shutil.disk_usage(DATA_ROOT)
                disk_usage_info = {
                    'total_bytes': disk_usage.total,
                    'used_bytes': disk_usage.used,
                    'free_bytes': disk_usage.free,
                    'percent_used': (disk_usage.used / disk_usage.total * 100) if disk_usage.total > 0 else 0
                }
            except Exception as e:
                log.warning(f"Could not get disk usage: {e}")
                disk_usage_info = {'error': str(e)}
            
            # Store in health_stats table in logs.db
            logs_db_conn = sqlite3.connect(logs_db_path, check_same_thread=False)
            logs_db_cursor = logs_db_conn.cursor()
            
            logs_db_cursor.execute('''
                INSERT INTO health_stats 
                (checked_at, database_sizes, top_tables, disk_usage, details)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(database_sizes),
                json.dumps(top_tables),
                json.dumps(disk_usage_info),
                json.dumps({
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                    'summary': {
                        'total_databases': len(database_sizes),
                        'total_tables_checked': sum(len(tables) for tables in top_tables.values())
                    }
                })
            ))
            
            logs_db_conn.commit()
            logs_db_conn.close()
            
            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()
            
            details = json.dumps({
                'database_sizes': database_sizes,
                'top_tables': top_tables,
                'disk_usage': disk_usage_info
            })
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='success',
                details=details
            )
            
            log.info(f"Maintenance task completed: {description} (took {duration:.2f} seconds)")
            
            return {
                'task_id': task_id,
                'status': 'success',
                'duration_seconds': duration,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat(),
                'database_sizes': database_sizes,
                'top_tables': top_tables,
                'disk_usage': disk_usage_info
            }
            
        except Exception as e:
            completed_at = datetime.now(timezone.utc)
            error_msg = str(e)
            
            self._log_maintenance_task(
                task_id=task_id,
                description=description,
                started_at=started_at,
                completed_at=completed_at,
                status='failed',
                error_message=error_msg
            )
            
            log.error(f"Maintenance task failed: {description} - {error_msg}", exc_info=True)
            
            return {
                'task_id': task_id,
                'status': 'failed',
                'error': error_msg,
                'started_at': started_at.isoformat(),
                'completed_at': completed_at.isoformat()
            }
    
    def run_maintenance_tasks(self, enabled_tasks: List[str]) -> Dict:
        """
        Run all enabled maintenance tasks.
        
        Tasks will be stopped if they exceed 1 hour total runtime.
        
        Args:
            enabled_tasks: List of task IDs to run (e.g., ['data_backup', 'logs_cleanup', 'health_checks'])
        
        Returns:
            Dict with results for all tasks
        """
        log.info("=" * 60)
        log.info("MAINTENANCE TASKS STARTING")
        log.info(f"Enabled tasks: {', '.join(enabled_tasks)}")
        log.info("Maximum runtime: 1 hour")
        log.info("=" * 60)
        
        results = {}
        overall_started_at = datetime.now(timezone.utc)
        MAX_RUNTIME_SECONDS = 3600  # 1 hour
        
        # Run each enabled task with timeout check
        if TASK_ID_BACKUP in enabled_tasks:
            elapsed = (datetime.now(timezone.utc) - overall_started_at).total_seconds()
            if elapsed >= MAX_RUNTIME_SECONDS:
                log.warning(f"Maintenance timeout reached ({elapsed:.2f}s), skipping remaining tasks")
                results[TASK_ID_BACKUP] = {
                    'task_id': TASK_ID_BACKUP,
                    'status': 'skipped',
                    'error': 'Maintenance timeout reached before task could start'
                }
            else:
                results[TASK_ID_BACKUP] = self.run_backup_task()
                elapsed = (datetime.now(timezone.utc) - overall_started_at).total_seconds()
                if elapsed >= MAX_RUNTIME_SECONDS:
                    log.warning(f"Maintenance timeout reached ({elapsed:.2f}s), stopping remaining tasks")
                    return {
                        'overall_status': 'timeout',
                        'overall_duration_seconds': elapsed,
                        'started_at': overall_started_at.isoformat(),
                        'completed_at': datetime.now(timezone.utc).isoformat(),
                        'tasks': results,
                        'message': 'Maintenance tasks stopped due to 1-hour timeout'
                    }
        
        if TASK_ID_LOGS_CLEANUP in enabled_tasks:
            elapsed = (datetime.now(timezone.utc) - overall_started_at).total_seconds()
            if elapsed >= MAX_RUNTIME_SECONDS:
                log.warning(f"Maintenance timeout reached ({elapsed:.2f}s), skipping remaining tasks")
                results[TASK_ID_LOGS_CLEANUP] = {
                    'task_id': TASK_ID_LOGS_CLEANUP,
                    'status': 'skipped',
                    'error': 'Maintenance timeout reached before task could start'
                }
            else:
                results[TASK_ID_LOGS_CLEANUP] = self.run_logs_cleanup_task()
                elapsed = (datetime.now(timezone.utc) - overall_started_at).total_seconds()
                if elapsed >= MAX_RUNTIME_SECONDS:
                    log.warning(f"Maintenance timeout reached ({elapsed:.2f}s), stopping remaining tasks")
                    return {
                        'overall_status': 'timeout',
                        'overall_duration_seconds': elapsed,
                        'started_at': overall_started_at.isoformat(),
                        'completed_at': datetime.now(timezone.utc).isoformat(),
                        'tasks': results,
                        'message': 'Maintenance tasks stopped due to 1-hour timeout'
                    }
        
        if TASK_ID_HEALTH_CHECKS in enabled_tasks:
            elapsed = (datetime.now(timezone.utc) - overall_started_at).total_seconds()
            if elapsed >= MAX_RUNTIME_SECONDS:
                log.warning(f"Maintenance timeout reached ({elapsed:.2f}s), skipping remaining tasks")
                results[TASK_ID_HEALTH_CHECKS] = {
                    'task_id': TASK_ID_HEALTH_CHECKS,
                    'status': 'skipped',
                    'error': 'Maintenance timeout reached before task could start'
                }
            else:
                results[TASK_ID_HEALTH_CHECKS] = self.run_health_checks_task()
        
        overall_completed_at = datetime.now(timezone.utc)
        overall_duration = (overall_completed_at - overall_started_at).total_seconds()
        
        log.info("=" * 60)
        log.info("MAINTENANCE TASKS COMPLETED")
        log.info(f"Total duration: {overall_duration:.2f} seconds ({overall_duration / 60:.2f} minutes)")
        log.info("=" * 60)
        
        return {
            'overall_status': 'completed',
            'overall_duration_seconds': overall_duration,
            'started_at': overall_started_at.isoformat(),
            'completed_at': overall_completed_at.isoformat(),
            'tasks': results
        }
    
    def get_maintenance_history(self, page: int = 1, per_page: int = 50, task_id: Optional[str] = None) -> Dict:
        """
        Get maintenance history from database.
        
        Args:
            page: Page number (1-indexed)
            per_page: Number of records per page
            task_id: Optional filter by task_id
        
        Returns:
            Dict with history records and pagination info
        """
        conn = self._get_db_connection()
        cursor = conn.cursor()
        
        # Build query
        query = "SELECT * FROM maintenance_history"
        params = []
        
        if task_id:
            query += " WHERE task_id = ?"
            params.append(task_id)
        
        query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([per_page, (page - 1) * per_page])
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Get column names
        column_names = [description[0] for description in cursor.description]
        
        # Convert to list of dicts
        history = []
        for row in rows:
            record = dict(zip(column_names, row))
            # Parse details JSON if present
            if record.get('details'):
                try:
                    record['details'] = json.loads(record['details'])
                except (json.JSONDecodeError, TypeError):
                    pass
            history.append(record)
        
        # Get total count
        count_query = "SELECT COUNT(*) FROM maintenance_history"
        count_params = []
        if task_id:
            count_query += " WHERE task_id = ?"
            count_params.append(task_id)
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'history': history,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        }
    
    def get_latest_system_usage(self) -> Optional[Dict]:
        """Get the latest system usage record."""
        conn = self._get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM system_usage 
            ORDER BY calculated_at DESC 
            LIMIT 1
        ''')
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        column_names = [description[0] for description in cursor.description]
        record = dict(zip(column_names, row))
        
        # Parse per_channel_usage JSON
        if record.get('per_channel_usage'):
            try:
                record['per_channel_usage'] = json.loads(record['per_channel_usage'])
            except (json.JSONDecodeError, TypeError):
                record['per_channel_usage'] = {}
        
        return record


# Singleton instance
_maintenance_service = None

def get_maintenance_service() -> MaintenanceService:
    """Get the singleton maintenance service instance."""
    global _maintenance_service
    if _maintenance_service is None:
        _maintenance_service = MaintenanceService()
    return _maintenance_service

