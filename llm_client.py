"""LLM client — OpenAI-compatible wrapper for LLM providers."""

import openai


class LLMClient:
    """Thin wrapper around openai.OpenAI for any OpenAI-compatible API.

    Works with Groq, OpenRouter, Ollama (via OpenAI-compatible endpoint),
    and any other provider that implements the OpenAI API spec.
    """

    def __init__(self, base_url: str, api_key: str, timeout: int = 60) -> None:
        """Initialize the client.

        Args:
            base_url: Base URL of the API endpoint (e.g.
                "https://api.groq.com/openai/v1").
            api_key: API key for the provider.
            timeout: Request timeout in seconds.
        """
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip('/'),
            timeout=timeout,
        )
        self.chat = self._client.chat
