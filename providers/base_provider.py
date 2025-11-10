"""
Base provider class defining the interface for AI realtime providers.
"""

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional


class BaseRealtimeClient(ABC):
    """
    Abstract base class for realtime AI providers.

    All providers must implement these methods to ensure feature parity
    and seamless switching between providers.
    """

    @abstractmethod
    async def connect(
        self,
        instructions: Optional[str] = None,
        voice: Optional[str] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        max_output_tokens: Optional[Any] = None,
        agent_name: Optional[str] = None,
        company_name: Optional[str] = None,
        tool_definitions: Optional[List[Dict[str, Any]]] = None,
        tool_instructions: Optional[str] = None,
        tool_choice: Optional[Any] = None
    ):
        """
        Establish connection to the AI provider's realtime API.

        :param instructions: System instructions/prompt
        :param voice: Voice identifier for speech synthesis
        :param temperature: Sampling temperature
        :param model: Model identifier
        :param max_output_tokens: Maximum output tokens
        :param agent_name: Name of the AI agent
        :param company_name: Company name for personalization
        :param tool_definitions: Function/tool definitions for calling
        :param tool_instructions: Additional instructions for tool usage
        :param tool_choice: Tool choice strategy
        """
        pass

    @abstractmethod
    async def send_audio(self, audio_bytes: bytes):
        """
        Send audio data to the AI provider.

        :param audio_bytes: Raw audio bytes (format varies by provider)
        """
        pass

    @abstractmethod
    async def start_receiving(self, on_audio_callback: Callable[[bytes], None]):
        """
        Start receiving audio responses from the AI provider.

        :param on_audio_callback: Callback function to handle received audio
        """
        pass

    @abstractmethod
    async def close(self):
        """Close the connection to the AI provider."""
        pass

    @abstractmethod
    async def terminate_session(self, reason: str = "completed", final_message: Optional[str] = None):
        """
        Terminate the session gracefully.

        :param reason: Termination reason
        :param final_message: Optional final message to send
        """
        pass

    @abstractmethod
    def register_genesys_tool_handlers(
        self,
        handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]]
    ):
        """
        Register handler functions for Genesys data actions.

        :param handlers: Dictionary mapping tool names to handler functions
        """
        pass

    @abstractmethod
    async def disconnect_session(self, reason: str = "completed", info: str = ""):
        """
        Disconnect the session.

        :param reason: Disconnect reason
        :param info: Additional information
        """
        pass

    @abstractmethod
    async def await_summary(self, timeout: float = 10.0):
        """
        Wait for and return a conversation summary.

        :param timeout: Maximum time to wait for summary
        :return: Summary data
        """
        pass
