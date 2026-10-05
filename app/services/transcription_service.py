# app/services/audio_handler.py
import os
import socket
import wave
import threading
import time
import select
import errno
import signal
import subprocess
import sys
from pathlib import Path
from ..utils.logging_setup import error_logger, transcription_logger
from ..services.settings_manager import get_settings_manager
import requests

_local_whisper_compatibility = None


class TranscriptionAuthenticationError(Exception):
    """The remote transcription credential is missing, expired, or rejected."""


def _ensure_local_whisper_compatible():
    """Raise without crashing the API if faster-whisper cannot run on this CPU."""
    global _local_whisper_compatibility
    if _local_whisper_compatibility is True:
        return
    if isinstance(_local_whisper_compatibility, str):
        raise RuntimeError(_local_whisper_compatibility)

    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import ctranslate2; import faster_whisper"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        message = f"Could not verify local transcription compatibility: {exc}"
        _local_whisper_compatibility = message
        raise RuntimeError(message) from exc

    if probe.returncode == 0:
        _local_whisper_compatibility = True
        return

    if probe.returncode < 0:
        reason = signal.Signals(-probe.returncode).name
        message = (
            f"Local transcription native libraries terminated with {reason}. "
            "Use API transcription or install CPU-compatible ctranslate2/faster-whisper wheels."
        )
    else:
        detail = (probe.stderr or "native dependency import failed").strip().splitlines()[-1]
        message = f"Local transcription is unavailable: {detail}"
    _local_whisper_compatibility = message
    raise RuntimeError(message)

def request_openai_transcription(audio_file, filename, timeout=60):
    api_key = get_settings_manager().get_setting("transcription_api_key", "")
    if not api_key:
        error_logger.error("Missing Boondock Transcription API Key")
        raise TranscriptionAuthenticationError("Missing Boondock Transcription API Key")

    """Send audio to the configured OpenAI transcription proxy."""
    headers = {
        "User-Agent": "BoondockEdge/2.0-beta.01",
        "Accept": "application/json",
        "X-Boondock-Key": api_key,
    }
    try:
        return requests.post(
            "https://api.boondock.cloud/api/v3/transcribe/openai",
            headers=headers,
            files={"audio_file": (filename, audio_file, "audio/wav")},
            data={"model_id": "whisper-large-v3-turbo"},
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise TimeoutError("OpenAI transcription request timed out") from exc


class TranscriptionService:
    """
    Service for handling audio transcription using local Whisper model.
    Operates offline for maximum privacy and reliability.
    
    The model is kept in memory for 5 minutes after last use to avoid
    repeated loading/unloading, improving performance for frequent transcriptions.
    """
    def __init__(self, model_name="small", model_timeout_seconds=300):
        # Initialize model as None for lazy loading
        self.whisper_model = None
        transcription_logger.info(f"Initializing TranscriptionService with model: {model_name}")
        
        # Store model name for lazy loading the local Whisper model
        self.model_name = model_name
        
        # Model timeout management (5 minutes default)
        self.model_timeout_seconds = model_timeout_seconds
        self.last_used_time = None
        self.model_lock = threading.RLock()
        self.running = True
        
        # Start background thread to unload model after timeout
        self.cleanup_thread = threading.Thread(target=self._model_cleanup_loop, daemon=True)
        self.cleanup_thread.start()
        
        transcription_logger.info("TranscriptionService initialization complete")
        
    def _load_whisper_model(self):
        """
        Lazy load the Whisper model only when needed to conserve memory.
        Updates the last_used_time to keep the model in memory.
        
        Raises:
            Exception: If model loading fails
        """
        with self.model_lock:
            if self.whisper_model is None:
                try:
                    _ensure_local_whisper_compatible()
                    from faster_whisper import WhisperModel
                    # Default to CPU; compute_type kept at int8 for performance unless
                    # overridden by using a different service.
                    self.whisper_model = WhisperModel(
                        self.model_name,
                        device="cpu",
                        compute_type="int8",
                    )
                    transcription_logger.debug(f"Local Whisper model loaded successfully: {self.model_name}")
                except Exception as e:
                    error_logger.error(f"Failed to load Whisper model: {str(e)}")
                    raise
            
            # Update last used time whenever model is accessed
            self.last_used_time = time.time()
            transcription_logger.debug(f"Model last used time updated: {self.model_name}")
    
    def _unload_whisper_model(self):
        """
        Unload the Whisper model to free memory.
        This is called automatically after the timeout period.
        """
        with self.model_lock:
            if self.whisper_model is not None:
                try:
                    # Clear the model reference to allow garbage collection
                    # faster-whisper models don't have an explicit close/cleanup method
                    # but setting to None allows Python GC to free memory
                    self.whisper_model = None
                    self.last_used_time = None
                    transcription_logger.info(f"Whisper model unloaded from memory: {self.model_name}")
                except Exception as e:
                    error_logger.error(f"Error unloading Whisper model: {str(e)}")
    
    def _model_cleanup_loop(self):
        """
        Background thread that periodically checks if the model should be unloaded
        after the timeout period (default 5 minutes).
        """
        while self.running:
            try:
                time.sleep(60)  # Check every minute
                
                with self.model_lock:
                    if self.whisper_model is not None and self.last_used_time is not None:
                        time_since_last_use = time.time() - self.last_used_time
                        if time_since_last_use >= self.model_timeout_seconds:
                            transcription_logger.info(
                                f"Model {self.model_name} has been idle for {time_since_last_use:.1f} seconds "
                                f"(timeout: {self.model_timeout_seconds}s). Unloading to free memory."
                            )
                            self._unload_whisper_model()
            except Exception as e:
                error_logger.error(f"Error in model cleanup loop: {str(e)}")
                # Continue running even if there's an error
    
    def shutdown(self):
        """
        Shutdown the transcription service and cleanup resources.
        """
        self.running = False
        with self.model_lock:
            if self.whisper_model is not None:
                self._unload_whisper_model()
        transcription_logger.info("TranscriptionService shutdown complete")

    def transcribe_audio(self, filepath, use_local=True, use_nodes=False, language=None,
                         **local_kwargs):
        """
        Transcribe audio using either the local Whisper model or the Boondock API.
        Exactly one method is used — there is no fallback between methods.
        If the selected method fails, the transcription is marked as failed immediately.
        
        Args:
            filepath (str): Path to audio file
            use_local (bool): If True, use local Whisper only. If False, use Boondock API only.
            use_nodes (bool): Ignored - nodes service is no longer supported
            language (str, optional): Language code for transcription (e.g., 'en', 'es', 'fr').
                Defaults to 'en' (English) if not provided.
            **local_kwargs: Extra keyword arguments passed through to the local
                Whisper transcription (e.g. beam_size, best_of, etc.).
            
        Returns:
            str: The successfully returned transcription, including an empty string.

        Raises:
            TranscriptionAuthenticationError: The cloud API rejected its credential.
            Exception: The selected transcription method failed.
        """
        # Boondock API path — no fallback to local on failure
        if not use_local:
            return self._transcribe_boondock_api(filepath)

        # Local transcription path
        transcription_logger.info(f"Starting local transcription for file: {filepath}")
        
        # Handle language parameter precedence.
        # Explicit `language` arg wins; if both supplied with different values warn.
        if language is not None:
            if 'language' in local_kwargs and local_kwargs['language'] != language:
                transcription_logger.warning(
                    f"language='{language}' (explicit arg) overrides local_kwargs['language']='{local_kwargs['language']}'"
                )
            local_kwargs['language'] = language
        elif 'language' not in local_kwargs:
            local_kwargs['language'] = 'en'
        
        transcription_logger.debug("Loading Whisper model for local transcription...")
        result = self._transcribe_local(filepath, **local_kwargs)
        transcription_logger.debug(f"Local transcription completed successfully for: {filepath}")
        return result

    def _transcribe_boondock_api(self, filepath):
        """
        Transcribe using Boondock cloud API.
        
        This sends the audio file directly to the configured API endpoint.
        """

        transcription_logger.info(
            f"Starting Boondock API transcription for file: {filepath} -> Boondock Cloud API"
        )
        with open(filepath, "rb") as f:
            response = request_openai_transcription(f, Path(filepath).name)

        transcription_logger.debug(f"Boondock API status: {response.status_code}")
        transcription_logger.debug(f"Boondock API response: {response.text[:500]}")

        if response.status_code == 401:
            error_logger.error("Boondock API rejected the transcription API key")
            raise TranscriptionAuthenticationError(
                "Transcription API authentication failed"
            )
        response.raise_for_status()

        data = response.json()

        # Support different common response shapes
        if isinstance(data, dict):
            if "transcription" in data:
                return data["transcription"]
            if "text" in data:
                return data["text"]

        raise ValueError("Boondock API response did not contain 'transcription' or 'text'")

    def _transcribe_local(self, filepath, **transcribe_kwargs):
        """
        Transcribe using local Whisper model.
        
        Args:
            filepath (str): Path to audio file
            **transcribe_kwargs: Extra keyword arguments forwarded to
                WhisperModel.transcribe for fine-tuning behaviour.
            
        Returns:
            str: Transcription text
            
        Raises:
            Exception: If transcription fails
        """
        try:
            transcription_logger.debug(f"Local transcription starting: {filepath}")
            transcription_logger.debug(f"Model: {self.model_name}, Args: {transcribe_kwargs}")
            
            # Load model if needed and update last used time
            self._load_whisper_model()
            
            # Ensure model is still loaded (might have been unloaded by cleanup thread)
            # Get a reference to the model while holding the lock
            with self.model_lock:
                if self.whisper_model is None:
                    # Model was unloaded, reload it
                    transcription_logger.debug(f"Model was unloaded, reloading: {self.model_name}")
                    _ensure_local_whisper_compatible()
                    from faster_whisper import WhisperModel
                    self.whisper_model = WhisperModel(
                        self.model_name,
                        device="cpu",
                        compute_type="int8",
                    )
                    self.last_used_time = time.time()
                    transcription_logger.debug(f"Whisper model ({self.model_name}) reloaded successfully")
                else:
                    # Update last used time for active model
                    self.last_used_time = time.time()
                
                # Get reference to model for use outside lock
                model_ref = self.whisper_model
            
            if model_ref is None:
                raise Exception("Model is not available for transcription")
            
            transcription_logger.debug(f"Processing audio file: {filepath}")
            # Note: faster-whisper returns generator for segments, must convert to list
            # Use model reference outside lock to avoid blocking
            segments, info = model_ref.transcribe(filepath, **transcribe_kwargs)
            
            # Update last used time after transcription completes
            with self.model_lock:
                if self.whisper_model is not None:
                    self.last_used_time = time.time()
            segments_list = list(segments)
            
            if not segments_list:
                transcription_logger.warning(f"No segments detected in audio file: {filepath}")
                return ""
            
            transcription_logger.debug(f"Extracted {len(segments_list)} segments from audio")
            transcription = " ".join([segment.text for segment in segments_list])
            transcription_logger.debug(f"Raw transcription: {transcription[:100]}..." if len(transcription) > 100 else f"Raw transcription: {transcription}")
            
            transcription_logger.debug("Local transcription completed successfully")
            
            return transcription
        except FileNotFoundError as e:
            error_logger.error(f"Audio file not found: {filepath}")
            raise
        except Exception as e:
            error_logger.error(f"Error in local transcription for {filepath}: {str(e)}", exc_info=True)
            raise
