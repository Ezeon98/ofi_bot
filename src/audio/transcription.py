"""Audio transcription protocol.

Defines the interface that any transcription provider must implement.
No concrete provider is implemented here — wire in OpenAI Whisper,
AssemblyAI, Deepgram, etc. as needed.

Usage:
    class WhisperProvider(TranscriptionProvider):
        async def transcribe(self, audio_path: str) -> str:
            ...

    # In AIOrchestrator or bot handler:
    text = await transcription_provider.transcribe(audio_path)
    response = await orchestrator.process(user_id=..., message=text, ...)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Any object that can turn an audio file path into text."""

    async def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file at `audio_path` and return the text.

        Args:
            audio_path: Absolute path to the audio file (OGG, MP3, WAV, etc.)

        Returns:
            Transcribed text string.

        Raises:
            TranscriptionError: on provider failure.
        """
        ...


class TranscriptionError(Exception):
    """Raised when a transcription provider fails."""
