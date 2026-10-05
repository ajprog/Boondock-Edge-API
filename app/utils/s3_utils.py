# app/utils/s3_utils.py
import os
import boto3
import logging
from botocore.config import Config
from botocore.exceptions import ClientError
from ..services.settings_manager import get_settings_manager

log = logging.getLogger(__name__)

# Get settings manager instance
_settings_manager = get_settings_manager()

def get_s3_settings():
    """Load S3 settings from database."""
    try:
        settings = _settings_manager.get_all_settings()
        return {
            'endpoint_url': settings.get('s3_endpoint_url', os.environ.get('S3_ENDPOINT_URL', 'https://h1u6.ch11.idrivee2-15.com')),
            'aws_access_key_id': settings.get('s3_access_key', os.environ.get('S3_ACCESS_KEY', '')),
            'aws_secret_access_key': settings.get('s3_secret_key', os.environ.get('S3_SECRET_KEY', '')),
            'region_name': settings.get('s3_region', os.environ.get('S3_REGION', 'us-east-1')),
            'bucket_name': settings.get('s3_bucket_name', os.environ.get('S3_BUCKET_NAME', '')),
            'enabled': settings.get('s3_enabled', False)
        }
    except Exception as e:
        log.warning(f"Error loading S3 settings from database: {e}, using environment variables")
    
    # Fallback to environment variables
    return {
        'endpoint_url': os.environ.get('S3_ENDPOINT_URL', 'https://h1u6.ch11.idrivee2-15.com'),
        'aws_access_key_id': os.environ.get('S3_ACCESS_KEY', ''),
        'aws_secret_access_key': os.environ.get('S3_SECRET_KEY', ''),
        'region_name': os.environ.get('S3_REGION', 'us-east-1'),
        'bucket_name': os.environ.get('S3_BUCKET_NAME', ''),
        'enabled': False
    }

def is_s3_enabled():
    """Check if S3 upload is enabled in settings."""
    settings = get_s3_settings()
    return settings['enabled'] and settings['aws_access_key_id'] and settings['aws_secret_access_key'] and settings['bucket_name']

def get_s3_client():
    """Get S3 client with current settings."""
    settings = get_s3_settings()

    if not settings['aws_access_key_id'] or not settings['aws_secret_access_key']:
        log.warning("S3 credentials not configured")
        return None

    return boto3.client(
        's3',
        endpoint_url=settings['endpoint_url'],
        aws_access_key_id=settings['aws_access_key_id'],
        aws_secret_access_key=settings['aws_secret_access_key'],
        region_name=settings['region_name'],
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'}
        )
    )

# Legacy S3_CONFIG for backward compatibility
S3_CONFIG = get_s3_settings()

# Create S3 client (will be None if credentials not configured)
s3_client = get_s3_client()

def ensure_bucket_exists(bucket_name):
    """Ensure an S3 bucket exists, create it if it doesn't."""
    client = get_s3_client()
    if not client:
        raise Exception("S3 client not available - credentials not configured")

    try:
        # Check if bucket exists
        client.head_bucket(Bucket=bucket_name)
        log.debug(f"Bucket {bucket_name} already exists")
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == '404':
            # Bucket doesn't exist, create it
            try:
                client.create_bucket(Bucket=bucket_name)
                log.info(f"Created bucket {bucket_name}")
            except ClientError as create_error:
                log.error(f"Failed to create bucket {bucket_name}: {create_error}")
                raise
        else:
            log.error(f"Error checking bucket {bucket_name}: {e}")
            raise
