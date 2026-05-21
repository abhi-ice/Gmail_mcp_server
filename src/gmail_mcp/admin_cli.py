"""
gmail-mcp-admin — operational commands for the v3 OAuth server.

In v3, users self-onboard via Google sign-in (gated by GCC Test Users +
optional env-var allowlist). This CLI is for OPS: listing who's signed up,
revoking access, and cleaning up expired artifacts. No more per-user token
issuance.
"""

from __future__ import annotations

import sys

import click

from . import storage
from .config import get_public_base_url


@click.group()
def main() -> None:
    """Operational commands for the Gmail MCP server."""
    storage.init_db()


@main.command("user-list")
def user_list() -> None:
    """List all users who have ever signed in."""
    users = storage.list_users()
    if not users:
        click.echo("No users yet.")
        return
    click.echo(f"{'EMAIL':<40}  {'STATUS':<10}  {'CREATED':<25}")
    click.echo("-" * 80)
    for u in users:
        status = "revoked" if u["revoked_at"] else ("admin" if u["is_admin"] else "active")
        click.echo(f"{u['email']:<40}  {status:<10}  {u['created_at']}")


@main.command("user-revoke")
@click.option("--email", required=True, help="User's email address.")
def user_revoke(email: str) -> None:
    """Block a user. Existing access tokens are also revoked."""
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    if storage.revoke_user(u["id"]):
        n = storage.revoke_all_user_tokens(u["id"])
        click.echo(f"✓ Revoked user {email} and {n} access token(s).")
    else:
        click.echo(f"User {email} was already revoked.")


@main.command("user-tokens")
@click.option("--email", required=True, help="User's email address.")
def user_tokens(email: str) -> None:
    """List active access tokens for a user."""
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    tokens = storage.list_user_access_tokens(u["id"])
    if not tokens:
        click.echo("No tokens.")
        return
    click.echo(f"{'PREFIX':<14}  {'CLIENT':<40}  {'STATUS':<10}  {'CREATED'}")
    click.echo("-" * 100)
    for t in tokens:
        status = "revoked" if t["revoked_at"] else "active"
        client = t["client_id"] or "(unknown)"
        click.echo(f"{t['token_prefix']:<14}  {client:<40}  {status:<10}  {t['created_at']}")


@main.command("tokens-revoke-user")
@click.option("--email", required=True, help="User's email address.")
def tokens_revoke_user(email: str) -> None:
    """Revoke ALL active access tokens for a user (forces re-sign-in)."""
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    n = storage.revoke_all_user_tokens(u["id"])
    click.echo(f"✓ Revoked {n} access token(s) for {email}.")


@main.command("cleanup")
def cleanup() -> None:
    """Delete expired OAuth states, codes, and tokens."""
    counts = storage.cleanup_expired_oauth_artifacts()
    click.echo(
        f"Removed: {counts['states']} signin states, "
        f"{counts['codes']} authorization codes, "
        f"{counts['tokens']} revoked-and-expired access tokens."
    )


@main.command("config-snippet")
def config_snippet() -> None:
    """
    Print the universal Claude Desktop config snippet. Same snippet for
    everyone in your org — no token needed.
    """
    import json
    snippet = {
        "gmail": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                f"{get_public_base_url()}/mcp",
            ],
        }
    }
    click.echo("Send this to anyone in your org. They paste it under \"mcpServers\":")
    click.echo("")
    click.echo(json.dumps(snippet, indent=2))
    click.echo("")
    click.echo("On first use, Claude Desktop pops up a browser → Sign in with Google → done.")


if __name__ == "__main__":
    main()
