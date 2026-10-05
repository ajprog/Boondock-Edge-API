# app/services/s3_scheduler.py
"""
S3 Backup Scheduler - Runs scheduled backup jobs at configured time.
Also schedules log cleanup to run after backups.
"""
import logging
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, time
from .s3_backup_service import run_backup_job
from .log_cleanup_service import cleanup_old_logs

log = logging.getLogger(__name__)

from .settings_manager import get_settings_manager

_settings_manager = get_settings_manager()
scheduler = None

def get_backup_time():
    """Get backup time from settings, default to 3:00 AM."""
    try:
        settings = _settings_manager.get_all_settings()
        backup_time_str = settings.get('scheduled_time', '03:00')
        # Parse time string (HH:MM format)
        try:
            hour, minute = map(int, backup_time_str.split(':'))
            return time(hour, minute)
        except (ValueError, AttributeError):
            log.warning(f"Invalid backup time format: {backup_time_str}, using default 3:00 AM")
            return time(3, 0)
    except Exception as e:
        log.warning(f"Error reading backup time from settings: {e}, using default 3:00 AM")
    
    return time(3, 0)  # Default: 3:00 AM

def start_scheduler():
    """Start the background scheduler for S3 backups and log cleanup."""
    global scheduler
    
    if scheduler is not None:
        log.warning("Scheduler already running")
        return
    
    try:
        scheduler = BackgroundScheduler(daemon=True)
        
        # Get backup time from settings
        backup_time = get_backup_time()
        
        # Schedule backup job to run daily at the configured time
        scheduler.add_job(
            run_backup_job,
            trigger=CronTrigger(hour=backup_time.hour, minute=backup_time.minute),
            id='s3_backup_job',
            name='S3 Backup Job',
            replace_existing=True
        )
        
        # Schedule log cleanup to run 1 hour after backup time
        # This ensures backups are complete before cleanup
        cleanup_hour = (backup_time.hour + 1) % 24
        scheduler.add_job(
            cleanup_old_logs,
            trigger=CronTrigger(hour=cleanup_hour, minute=backup_time.minute),
            id='log_cleanup_job',
            name='Log Cleanup Job',
            replace_existing=True
        )
        
        scheduler.start()
        log.info(f"S3 backup scheduler started - backups will run daily at {backup_time.strftime('%H:%M')}")
        log.info(f"Log cleanup scheduled to run daily at {cleanup_hour:02d}:{backup_time.minute:02d} (1 hour after backup)")
        
    except Exception as e:
        log.error(f"Failed to start S3 backup scheduler: {str(e)}")
        # Don't raise - scheduler failure shouldn't crash the app

def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    
    if scheduler is not None:
        try:
            scheduler.shutdown()
            scheduler = None
            log.info("S3 backup scheduler stopped")
        except Exception as e:
            log.error(f"Error stopping scheduler: {str(e)}")

def restart_scheduler():
    """Restart the scheduler (useful when backup time changes)."""
    stop_scheduler()
    start_scheduler()



