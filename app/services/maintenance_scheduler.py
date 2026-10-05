# app/services/maintenance_scheduler.py
"""
Maintenance Scheduler - Runs scheduled maintenance tasks at configured time.

This scheduler coordinates all maintenance tasks and runs them at the user-configured
maintenance time (default: 3:00 AM).
"""
import logging
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, time

from .maintenance_service import get_maintenance_service, TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS

log = logging.getLogger(__name__)

from .settings_manager import get_settings_manager

_settings_manager = get_settings_manager()
scheduler = None


def get_maintenance_time():
    """Get maintenance time from settings, default to 3:00 AM."""
    try:
        settings = _settings_manager.get_all_settings()
        maintenance_time_str = settings.get('scheduled_time', '03:00')
        # Parse time string (HH:MM format)
        try:
            hour, minute = map(int, maintenance_time_str.split(':'))
            return time(hour, minute)
        except (ValueError, AttributeError):
            log.warning(f"Invalid maintenance time format: {maintenance_time_str}, using default 3:00 AM")
            return time(3, 0)
    except Exception as e:
        log.warning(f"Error reading maintenance time from settings: {e}, using default 3:00 AM")
    
    return time(3, 0)  # Default: 3:00 AM


def get_enabled_maintenance_tasks():
    """Get list of enabled maintenance tasks from settings."""
    try:
        settings = _settings_manager.get_all_settings()
        # Get enabled tasks (default: all enabled)
        enabled_tasks = settings.get('enabled_tasks', [
            TASK_ID_BACKUP,
            TASK_ID_LOGS_CLEANUP,
            TASK_ID_HEALTH_CHECKS
        ])
        # Ensure it's a list
        if isinstance(enabled_tasks, str):
            # If it's a JSON string, parse it
            try:
                enabled_tasks = json.loads(enabled_tasks)
            except json.JSONDecodeError:
                enabled_tasks = [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]
        return enabled_tasks
    except Exception as e:
        log.warning(f"Error reading enabled maintenance tasks from settings: {e}, using defaults")
    
    # Default: all tasks enabled
    return [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]


def run_maintenance_job():
    """Main maintenance job that runs all enabled maintenance tasks."""
    log.info("=" * 60)
    log.info("SCHEDULED MAINTENANCE JOB STARTING")
    log.info("=" * 60)
    
    try:
        maintenance_service = get_maintenance_service()
        enabled_tasks = get_enabled_maintenance_tasks()
        
        log.info(f"Running maintenance tasks: {', '.join(enabled_tasks)}")
        
        results = maintenance_service.run_maintenance_tasks(enabled_tasks)
        
        log.info("=" * 60)
        log.info("SCHEDULED MAINTENANCE JOB COMPLETED")
        log.info(f"Overall status: {results.get('overall_status')}")
        log.info(f"Total duration: {results.get('overall_duration_seconds', 0):.2f} seconds")
        log.info("=" * 60)
        
    except Exception as e:
        log.error(f"Error running scheduled maintenance job: {str(e)}", exc_info=True)


def start_scheduler():
    """Start the background scheduler for maintenance tasks."""
    global scheduler
    
    if scheduler is not None:
        log.warning("Maintenance scheduler already running")
        return
    
    try:
        scheduler = BackgroundScheduler(daemon=True)
        
        # Get maintenance time from settings
        maintenance_time = get_maintenance_time()
        
        # Schedule maintenance job to run daily at the configured time
        scheduler.add_job(
            run_maintenance_job,
            trigger=CronTrigger(hour=maintenance_time.hour, minute=maintenance_time.minute),
            id='maintenance_job',
            name='Maintenance Job',
            replace_existing=True
        )
        
        scheduler.start()
        log.info(f"Maintenance scheduler started - tasks will run daily at {maintenance_time.strftime('%H:%M')}")
        
    except Exception as e:
        log.error(f"Failed to start maintenance scheduler: {str(e)}")
        # Don't raise - scheduler failure shouldn't crash the app


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    
    if scheduler is not None:
        try:
            scheduler.shutdown()
            scheduler = None
            log.info("Maintenance scheduler stopped")
        except Exception as e:
            log.error(f"Error stopping maintenance scheduler: {str(e)}")


def restart_scheduler():
    """Restart the scheduler (useful when maintenance time or enabled tasks change)."""
    stop_scheduler()
    start_scheduler()
