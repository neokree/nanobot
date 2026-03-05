"""
Anthropic OAuth provider — uses Claude Code OAuth credentials with auto-refresh.

Reads OAuth credentials from ~/.claude/.credentials.json and automatically refreshes
tokens before expiry. Requires Claude Code CLI to be installed and authenticated.

Usage in config.json:
    {
        "providers": {
            "anthropic": {
                "authMethod": "claude-code"
            }
        }
    }

Setup:
    1. Install Claude Code CLI
    2. Run: claude auth login
    3. Configure nanobot to use claude-code auth method
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)

# Claude Code OAuth constants
CLAUDE_CODE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_CODE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CODE_SCOPES = "user:inference user:sessions:claude_code user:mcp_servers"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
REFRESH_MARGIN_SECONDS = 300  # Refresh 5 minutes before expiry


def read_claude_code_credentials() -> dict[str, Any] | None:
    """
    Read Claude Code OAuth credentials from ~/.claude/.credentials.json.

    Returns the claudeAiOauth dict with keys:
    - accessToken (str)
    - refreshToken (str)
    - expiresAt (int, timestamp in milliseconds)
    - scopes (list[str])
    - subscriptionType (str | None)
    - rateLimitTier (str | None)

    Returns None if file doesn't exist or claudeAiOauth is missing.
    """
    if not CLAUDE_CODE_CREDENTIALS_PATH.exists():
        logger.warning(
            "Claude Code credentials file not found at %s. "
            "Run `claude auth login` first.",
            CLAUDE_CODE_CREDENTIALS_PATH,
        )
        return None

    try:
        with open(CLAUDE_CODE_CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        oauth_creds = data.get("claudeAiOauth")
        if not oauth_creds:
            logger.warning("No claudeAiOauth field found in credentials file")
            return None

        # Validate required fields
        required = ["accessToken", "refreshToken", "expiresAt"]
        missing = [k for k in required if k not in oauth_creds]
        if missing:
            logger.error("Missing required fields in claudeAiOauth: %s", missing)
            return None

        return oauth_creds

    except json.JSONDecodeError as e:
        logger.error("Failed to parse credentials file: %s", e)
        return None
    except Exception as e:
        logger.error("Error reading credentials file: %s", e)
        return None


def needs_refresh(expires_at_ms: int) -> bool:
    """Check if access token needs refresh (< 5 minutes until expiry)."""
    now_ms = int(time.time() * 1000)
    margin_ms = REFRESH_MARGIN_SECONDS * 1000
    return (expires_at_ms - now_ms) < margin_ms


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """
    Refresh OAuth access token using Claude Code's refresh_token.

    Args:
        refresh_token: The refresh token from claudeAiOauth

    Returns:
        Dict with new tokens:
        - access_token (str)
        - refresh_token (str) - may be same or new
        - expires_in (int) - seconds until expiry

    Raises:
        httpx.HTTPStatusError: If refresh request fails
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
        "scope": CLAUDE_CODE_SCOPES,
    }

    logger.info("Refreshing Claude OAuth access token...")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            CLAUDE_CODE_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

    # Validate response
    required = ["access_token", "expires_in"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Token refresh response missing fields: {missing}")

    logger.info("OAuth access token refreshed successfully")
    return data


def save_credentials(
    access_token: str,
    refresh_token: str,
    expires_in: int,
    existing_creds: dict[str, Any],
) -> None:
    """
    Update ~/.claude/.credentials.json with new tokens.

    Args:
        access_token: New access token
        refresh_token: New refresh token (may be same as old)
        expires_in: Seconds until expiry
        existing_creds: Current claudeAiOauth dict (for preserving other fields)
    """
    expires_at_ms = int((time.time() + expires_in) * 1000)

    # Update credentials
    updated_oauth = {
        **existing_creds,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }

    # Read full file to preserve other fields
    try:
        if CLAUDE_CODE_CREDENTIALS_PATH.exists():
            with open(CLAUDE_CODE_CREDENTIALS_PATH, "r", encoding="utf-8") as f:
                full_data = json.load(f)
        else:
            full_data = {}

        full_data["claudeAiOauth"] = updated_oauth

        # Write atomically (write to temp, then rename)
        temp_path = CLAUDE_CODE_CREDENTIALS_PATH.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(full_data, f, indent=2)

        temp_path.replace(CLAUDE_CODE_CREDENTIALS_PATH)

        # Ensure file is readable only by user (security)
        CLAUDE_CODE_CREDENTIALS_PATH.chmod(0o600)

        logger.info("Updated credentials file with new tokens")

    except Exception as e:
        logger.error("Failed to save credentials: %s", e)
        raise


async def get_valid_access_token() -> str:
    """
    Get a valid access token, refreshing if needed.

    Returns:
        Valid access token ready to use

    Raises:
        ValueError: If credentials not found or refresh fails
    """
    creds = read_claude_code_credentials()
    if not creds:
        raise ValueError(
            "No Claude Code credentials found. Run `claude auth login` first."
        )

    access_token = creds["accessToken"]
    refresh_token = creds["refreshToken"]
    expires_at = creds["expiresAt"]

    # Check if token needs refresh
    if needs_refresh(expires_at):
        logger.info("Access token expires soon, refreshing...")

        try:
            refresh_data = await refresh_access_token(refresh_token)

            # Save new tokens
            save_credentials(
                access_token=refresh_data["access_token"],
                refresh_token=refresh_data.get("refresh_token", refresh_token),
                expires_in=refresh_data["expires_in"],
                existing_creds=creds,
            )

            # Return new token
            return refresh_data["access_token"]

        except Exception as e:
            logger.error("Token refresh failed: %s", e)
            logger.warning("Falling back to potentially expired token")
            # Try to use old token anyway (might still work)
            return access_token

    # Token is still valid
    return access_token


class AnthropicOAuthProvider(LLMProvider):
    """
    LLM provider using Anthropic API with Claude Code OAuth credentials.

    Automatically refreshes tokens before expiry using credentials from
    ~/.claude/.credentials.json. Requires Claude Code CLI authenticated.
    """

    def __init__(
        self,
        default_model: str = "claude-sonnet-4-5-20250929",
    ):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

        # Validate that credentials exist
        creds = read_claude_code_credentials()
        if not creds:
            raise ValueError(
                "No Claude Code credentials found. "
                "Run `claude auth login` first."
            )

        # Client will be created dynamically in chat() with fresh token
        self.client = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request via Anthropic API with OAuth token."""

        # Get fresh token with auto-refresh
        try:
            access_token = await get_valid_access_token()
            # Create client with fresh token
            self.client = anthropic.AsyncAnthropic(
                auth_token=access_token,
                default_headers={
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )
        except Exception as e:
            logger.error("Failed to get valid access token: %s", e)
            return LLMResponse(
                content=f"Authentication failed: {e}. Run `claude auth login`.",
                finish_reason="error",
            )

        model = model or self.default_model
        # Strip provider prefix if present (e.g. "anthropic/claude-sonnet-4-5")
        if "/" in model:
            model = model.split("/", 1)[1]

        system_parts = []
        api_messages = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_parts.append(content)
            elif role == "user":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    api_messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id,
                                "content": content,
                            }
                        ],
                    })
                else:
                    api_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    blocks: list[dict[str, Any]] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        # Extract function details from OpenAI-format tool_call
                        func = tc.get("function", {})
                        func_name = func.get("name", "")
                        func_args = func.get("arguments", "{}")

                        # Parse arguments if they're a JSON string
                        if isinstance(func_args, str):
                            try:
                                func_args = json.loads(func_args)
                            except json.JSONDecodeError:
                                func_args = {}

                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func_name,
                            "input": func_args,
                        })
                    api_messages.append({"role": "assistant", "content": blocks})
                else:
                    api_messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                api_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": content,
                        }
                    ],
                })

        # Build kwargs
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        try:
            response = await self.client.messages.create(**kwargs)
            return self._parse_response(response)
        except anthropic.AuthenticationError as e:
            logger.error("Setup-token authentication failed: %s", e)
            return LLMResponse(
                content=f"Authentication failed (setup-token may be expired): {e}",
                finish_reason="error",
            )
        except anthropic.PermissionDeniedError as e:
            logger.error("Setup-token permission denied: %s", e)
            return LLMResponse(
                content=f"Permission denied (setup-token may be blocked): {e}",
                finish_reason="error",
            )
        except anthropic.RateLimitError as e:
            logger.error("Anthropic rate limit: %s", e)
            return LLMResponse(
                content=f"Rate limit exceeded: {e}",
                finish_reason="error",
            )
        except anthropic.APIError as e:
            logger.error("Anthropic API error: %s", e)
            return LLMResponse(
                content=f"Error calling Anthropic API: {e}",
                finish_reason="error",
            )

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tools to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        """Parse Anthropic API response into standard LLMResponse."""
        content_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))

        finish_reason = "stop"
        if response.stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif response.stop_reason == "max_tokens":
            finish_reason = "length"

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }

        return LLMResponse(
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def get_default_model(self) -> str:
        return self.default_model
