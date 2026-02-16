# Claude OAuth Integration

Nanobot uses Claude Code OAuth credentials for authentication with the Anthropic API, providing automatic token refresh and eliminating the need for manual token management.

## Overview

The Claude OAuth integration:
- Reads credentials from `~/.claude/.credentials.json`
- Automatically refreshes access tokens 5 minutes before expiry
- Shares authentication with Claude Code CLI
- Eliminates the 24-hour token expiry issue of setup-tokens

## Prerequisites

1. **Install Claude Code CLI**
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```

2. **Authenticate with Claude**
   ```bash
   claude auth login
   ```
   Follow the device code flow to authenticate with your Claude account.

3. **Verify authentication**
   ```bash
   claude auth status
   ```
   Should show `"loggedIn": true` and `"authMethod": "oauth"`.

## Configuration

### Anthropic Provider (Recommended)

Add to `~/.nanobot/config.json`:

```json
{
  "providers": {
    "anthropic": {
      "authMethod": "claude-code"
    }
  },
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-5-20250929"
    }
  }
}
```

**Note:** `authMethod` defaults to `"claude-code"` so you can omit it if using the default.

### Minimal Configuration

The simplest config (uses all defaults):

```json
{
  "agents": {
    "defaults": {
      "model": "claude-sonnet-4-5-20250929"
    }
  }
}
```

## How It Works

### Token Lifecycle

1. **Initialization**: Provider validates credentials exist at startup
2. **Before each request**:
   - Check if token expires in < 5 minutes
   - If yes: refresh token via OAuth endpoint
   - If no: use existing token
3. **Token refresh**:
   - POST to `https://platform.claude.com/v1/oauth/token`
   - Atomic update of `~/.claude/.credentials.json`
   - File permissions set to `0o600` (owner read/write only)

### Security

- Credentials file is automatically secured with `0o600` permissions
- Tokens are never logged (masked in debug output)
- Atomic writes prevent file corruption during updates
- Refresh tokens are long-lived but revocable from claude.ai

### Shared Credentials

Nanobot shares OAuth credentials with Claude Code CLI:
- Both read from `~/.claude/.credentials.json`
- Both refresh tokens when needed
- Changes by either tool are visible to the other

## Troubleshooting

### "No Claude Code credentials found"

**Cause:** Credentials file doesn't exist or is missing `claudeAiOauth` field.

**Solution:**
```bash
# Check if file exists
ls -la ~/.claude/.credentials.json

# Check login status
claude auth status

# Re-authenticate if needed
claude auth logout
claude auth login
```

### "Authentication failed: Token refresh failed"

**Cause:** Refresh token expired or revoked.

**Solution:**
```bash
# Re-authenticate to get new tokens
claude auth logout
claude auth login

# Restart Nanobot gateway
pm2 restart nanobot-gateway
```

### "Permission denied" reading credentials file

**Cause:** File permissions too restrictive or file owned by different user.

**Solution:**
```bash
# Check file owner and permissions
ls -la ~/.claude/.credentials.json

# Fix permissions if needed
chmod 600 ~/.claude/.credentials.json
```

### Gateway starts but doesn't respond to messages

**Cause:** Token might be expired or credentials invalid.

**Solution:**
```bash
# Check gateway logs for auth errors
pm2 logs nanobot-gateway --lines 50

# Look for "Failed to get valid access token" messages
# Re-authenticate if needed
claude auth login

# Restart gateway
pm2 restart nanobot-gateway
```

## Benefits

### vs. Setup Tokens

| Feature | Claude OAuth | Setup Tokens (deprecated) |
|---------|-------------|---------------------------|
| **Expiry** | Auto-refresh, no expiry | 24 hours, manual refresh |
| **Setup** | One-time `claude auth login` | Daily `claude setup-token` |
| **Cost** | Uses Claude subscription | Uses Claude subscription |
| **Reliability** | High (auto-refresh) | Low (manual intervention) |
| **Security** | Refresh token revocable | Token harder to revoke |

### vs. API Keys

| Feature | Claude OAuth | API Keys |
|---------|-------------|----------|
| **Cost** | Included in subscription | Per-token pricing |
| **Rate Limits** | Subscription tier limits | API tier limits |
| **Setup** | `claude auth login` | Copy key from console |
| **Management** | Automatic | Manual |

## Migration from Setup Tokens

If you were using setup tokens (deprecated):

### Before
```json
{
  "providers": {
    "anthropic": {
      "setupToken": "sk-ant-oat01-...",
      "authMethod": "setup-token"
    }
  }
}
```

### After
```json
{
  "providers": {
    "anthropic": {
      "authMethod": "claude-code"
    }
  }
}
```

Then authenticate once:
```bash
claude auth login
pm2 restart nanobot-gateway
```

## Advanced

### Custom Credentials Path

The credentials path is hardcoded to `~/.claude/.credentials.json`. To use a different path, modify `CLAUDE_CODE_CREDENTIALS_PATH` in `nanobot/providers/anthropic_oauth_provider.py`.

### Refresh Margin

Tokens are refreshed 5 minutes (300 seconds) before expiry. To adjust, modify `REFRESH_MARGIN_SECONDS` in `anthropic_oauth_provider.py`.

### Manual Token Refresh

For testing or debugging:

```python
from nanobot.providers.anthropic_oauth_provider import get_valid_access_token
import asyncio

token = asyncio.run(get_valid_access_token())
print(f"Token: {token[:20]}...")
```

## Architecture

### Files Modified

- `nanobot/config/schema.py` - Added `auth_method` field
- `nanobot/providers/anthropic_oauth_provider.py` - OAuth refresh logic
- `nanobot/cli/commands.py` - Provider instantiation

### Key Functions

- `read_claude_code_credentials()` - Read credentials file
- `needs_refresh(expires_at_ms)` - Check if token expires soon
- `refresh_access_token(refresh_token)` - Call OAuth endpoint
- `save_credentials(...)` - Atomic update of credentials file
- `get_valid_access_token()` - Orchestrator: read → check → refresh → return

### OAuth Flow

```
┌─────────────────┐
│ chat() called   │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ get_valid_access_token()│
└────────┬────────────────┘
         │
         ▼
   ┌────────────┐
   │ Read creds │
   └────┬───────┘
        │
        ▼
   ┌─────────────┐      No
   │ Needs       ├──────────┐
   │ refresh?    │          │
   └─────┬───────┘          │
        Yes                 │
         │                  │
         ▼                  │
   ┌──────────────┐         │
   │ POST /token  │         │
   └────┬─────────┘         │
        │                   │
        ▼                   │
   ┌──────────────┐         │
   │ Save tokens  │         │
   └────┬─────────┘         │
        │                   │
        └───────┬───────────┘
                │
                ▼
        ┌───────────────┐
        │ Return token  │
        └───────────────┘
```

## Resources

- [Claude Code CLI](https://claude.ai/claude-code)
- [Nanobot Repository](https://github.com/HKUDS/nanobot)
- [Anthropic API Documentation](https://docs.anthropic.com)
