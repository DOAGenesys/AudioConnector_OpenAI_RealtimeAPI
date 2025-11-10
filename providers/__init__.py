"""
AI Provider abstraction layer for AudioConnector.

Supports multiple AI providers (OpenAI, Gemini) with a unified interface.
"""

from typing import Optional
from config import logger


def create_client(
    provider: str,
    session_id: str,
    on_speech_started_callback=None
):
    """
    Factory function to create the appropriate AI client based on provider.

    :param provider: Provider name ('openai' or 'gemini')
    :param session_id: Session identifier
    :param on_speech_started_callback: Callback for speech started events
    :return: Provider-specific client instance
    """
    provider = provider.lower()

    if provider == 'openai':
        from providers.openai_provider import OpenAIRealtimeClient
        return OpenAIRealtimeClient(session_id, on_speech_started_callback)
    elif provider == 'gemini':
        from providers.gemini_provider import GeminiLiveClient
        return GeminiLiveClient(session_id, on_speech_started_callback)
    else:
        raise ValueError(f"Unsupported AI provider: {provider}. Must be 'openai' or 'gemini'.")


__all__ = ['create_client']
