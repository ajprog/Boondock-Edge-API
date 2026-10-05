# app/__init__.py
from flask import Flask
from flasgger import Swagger
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config
import os

def create_app(config_class=Config):
    app = Flask(__name__, static_folder=None)
    app.config.from_object(config_class)

    # Honor X-Forwarded-* headers from reverse proxies (nginx, Cloudflare, etc.)
    # so request.scheme/is_secure and Swagger UI use HTTPS when the site is served over TLS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    
    # Initialize Swagger
    swagger_config = {
        "headers": [],
        "specs": [
            {
                "endpoint": "apispec",
                "route": "/apispec.json",
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/api-docs"
    }
    
    swagger_template = {
        "swagger": "2.0",
        "info": {
            "title": "Boondock Edge API",
            "description": "API documentation for Boondock Edge server",
            "version": "1.0.0",
            "contact": {
                "name": "Boondock",
            }
        },
        "basePath": "/",
        "securityDefinitions": {
            "BearerAuth": {
                "type": "apiKey",
                "name": "Authorization",
                "in": "header",
                "description": "Admin session token. Format: 'Bearer {token}'"
            },
            "ApiKeyAuth": {
                "type": "apiKey",
                "name": "Authorization",
                "in": "header",
                "description": "External API key. Format: 'Bearer {api_key}'"
            }
        },
        "tags": [
            {
                "name": "Audio",
                "description": "Audio file operations and S3 uploads"
            },
            {
                "name": "Events",
                "description": "Device event handling"
            },
            {
                "name": "Settings",
                "description": "Device settings management"
            },
            {
                "name": "Channels",
                "description": "Channel configuration and management"
            }
        ]
    }
    
    Swagger(app, config=swagger_config, template=swagger_template)
    
    # Import and register blueprints inside create_app to avoid circular imports

    from app.routes.auth_routes import auth_bp, mfa_bp
    from app.routes.channels_routes import channels_bp
    from app.routes.recordings_routes import recordings_bp
    from app.routes.transcription_routes import transcription_bp
    from app.routes.users_routes import users_bp
    from app.routes.groups_routes import groups_bp
    from app.routes.s3_routes import s3_bp
    from app.routes.hotspot_routes import hotspot_bp
    from app.routes.settings_routes import settings_bp
    from app.routes.hallucinations_routes import hallucinations_bp
    from app.routes.logs_routes import logs_bp
    from app.routes.radio_routes import radio_bp
    from app.routes.react import react_bp
    from app.routes.recording_history_routes import history_bp
    from app.routes.device_routes import device_bp
    from app.routes.tags_routes import tags_bp
    from app.routes.frequencies_routes import frequencies_bp
    from app.routes.incident_reports_routes import incident_reports_bp
    from app.routes.maintenance_routes import maintenance_bp
    from app.routes.health_routes import health_bp, notification_bp
    from app.routes.docs_routes import docs_bp
    from app.routes.docusaurus_routes import docusaurus_bp
    from app.routes.release_notes_routes import release_notes_bp
    from app.routes.release_package_routes import release_package_bp
    from app.routes.recorder_routes import recorders_bp
    from app.routes.external_api_routes import external_api_bp

    # Register all blueprints with /api prefix
    # TO-DO non-standard channel_bp routes
    app.register_blueprint(channels_bp, url_prefix='/api')
    # TO-DO non-standard recordings_bp routes
    app.register_blueprint(recordings_bp, url_prefix='/api')
    # TO-DO non-standard transcriptions_bp routes
    app.register_blueprint(transcription_bp, url_prefix='/api')
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(mfa_bp, url_prefix='/api/mfa')
    app.register_blueprint(users_bp, url_prefix='/api')
    app.register_blueprint(groups_bp, url_prefix='/api')
    app.register_blueprint(s3_bp, url_prefix='/api')
    app.register_blueprint(hotspot_bp, url_prefix='/api')
    app.register_blueprint(settings_bp, url_prefix='/api')
    app.register_blueprint(hallucinations_bp, url_prefix='/api')
    app.register_blueprint(history_bp, url_prefix='/api')
    # TO-DO non-standard device_bp routes
    app.register_blueprint(device_bp, url_prefix='/api')
    app.register_blueprint(tags_bp, url_prefix='/api')
    app.register_blueprint(frequencies_bp, url_prefix='/api')
    app.register_blueprint(incident_reports_bp, url_prefix='/api')
    app.register_blueprint(maintenance_bp, url_prefix='/api')
    app.register_blueprint(health_bp, url_prefix='/api/health')
    app.register_blueprint(notification_bp, url_prefix='/api/notifications')
    app.register_blueprint(docs_bp, url_prefix='/api')
    app.register_blueprint(release_notes_bp, url_prefix='/api')
    app.register_blueprint(release_package_bp, url_prefix='/api')
    app.register_blueprint(external_api_bp, url_prefix='/api')
    app.register_blueprint(recorders_bp, url_prefix='/api/recorders')
    app.register_blueprint(logs_bp, url_prefix='/api/')
    app.register_blueprint(radio_bp, url_prefix='/api/radio')


    # Register Docusaurus docs blueprint (no prefix, serves at /docs/)
    app.register_blueprint(docusaurus_bp)
    app.register_blueprint(react_bp)
    
    # Initialize and start the transcription queue engine on application startup.
    # In Werkzeug's reloader mode the module is imported twice (monitor + worker).
    # We must only start background threads in the worker process, identified by
    # WERKZEUG_RUN_MAIN='true', or when the reloader is not active at all.
    _werkzeug_main = os.environ.get('WERKZEUG_RUN_MAIN')
    _reloader_active = _werkzeug_main is not None
    if not _reloader_active or _werkzeug_main == 'true':
        try:
            from app.services.audio_handler import get_audio_handler
            audio_handler = get_audio_handler()
            from app.services.settings_manager import get_settings_manager
            queue_enabled = get_settings_manager().get_setting(
                'transcription_queue_enabled', True
            )
            if queue_enabled and audio_handler and not getattr(audio_handler, "running", False):
                audio_handler.start()
                app.logger.info("Transcription queue processor started on app startup")
            elif not queue_enabled:
                app.logger.info("Transcription queue remains stopped per saved setting")
            else:
                app.logger.info("Transcription queue processor already running on app startup")
        except Exception as e:
            app.logger.error(f"Failed to start transcription queue on app startup: {e}")

    # Ensure settings DB has required fields (e.g. global_live_mode_enabled) on every startup
    try:
        from app.services.db_initializer import initialize_settings_database
        initialize_settings_database()
        app.logger.info("Settings database checked/initialized on app startup")
    except Exception as e:
        app.logger.warning(f"Settings database init check on startup: {e}")

    # Provision default hotspot on first boot when Wi-Fi AP mode is supported.
    try:
        import threading

        def _hotspot_setup_worker():
            try:
                from app.services.hotspot_setup import run_initial_hotspot_setup

                outcome = run_initial_hotspot_setup()
                if outcome.get("success"):
                    app.logger.info("Initial hotspot setup completed: %s", outcome)
                elif not outcome.get("skipped"):
                    app.logger.warning("Initial hotspot setup did not succeed: %s", outcome)
            except Exception as setup_exc:
                app.logger.warning("Initial hotspot setup error: %s", setup_exc)

        threading.Thread(target=_hotspot_setup_worker, daemon=True).start()
    except Exception as e:
        app.logger.warning(f"Could not schedule hotspot setup: {e}")

    return app
