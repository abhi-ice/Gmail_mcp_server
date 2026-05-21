"""
gmail-mcp-admin — issue and manage user bearer tokens.

Run on the server where the gmail-mcp data DB lives. Reads the same .env as
the MCP server.
"""

from __future__ import annotations

import json
import sys

import click

from . import storage
from .config import get_public_base_url, log


@click.group()
def main() -> None:
    """Admin commands for the Gmail MCP server."""
    storage.init_db()


@main.command("user-create")
@click.option("--email", required=True, help="User's email address.")
@click.option("--admin", is_flag=True, default=False, help="Grant admin flag (reserved for future use).")
def user_create(email: str, admin: bool) -> None:
    """Create a new user and issue a bearer token (shown ONCE)."""
    existing = storage.get_user_by_email(email)
    if existing:
        click.echo(f"User {email} already exists (id={existing['id']}).", err=True)
        click.echo("Use `user-rotate` to issue a new token, or `user-revoke` first.", err=True)
        sys.exit(1)

    user_id, token = storage.create_user(email=email, is_admin=admin)

    click.echo("")
    click.echo(click.style("✓ User created", fg="green", bold=True))
    click.echo(f"  Email:    {email}")
    click.echo(f"  User ID:  {user_id}")
    click.echo(click.style("  Bearer token (SAVE THIS — shown only once):", fg="yellow", bold=True))
    click.echo(f"  {token}")
    click.echo("")
    click.echo("---")
    click.echo("Claude Desktop config snippet (paste under \"mcpServers\"):")
    click.echo("")
    snippet = {
        "gmail": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                f"{get_public_base_url()}/mcp",
                "--header",
                f"Authorization: Bearer {token}",
            ],
        }
    }
    click.echo(json.dumps(snippet, indent=2))
    click.echo("")
    click.echo(
        "Tell the user: open Claude Desktop config (Settings → Developer → Edit Config), "
        "paste the snippet, restart Claude Desktop."
    )


@main.command("user-list")
def user_list() -> None:
    """List all users."""
    users = storage.list_users()
    if not users:
        click.echo("No users yet.")
        return
    for u in users:
        status = "revoked" if u["revoked_at"] else ("admin" if u["is_admin"] else "active")
        click.echo(
            f"{u['email']:<40}  {u['bearer_token_prefix']:<14}  {status:<8}  {u['created_at']}"
        )


@main.command("user-revoke")
@click.option("--email", required=True, help="User's email address.")
def user_revoke(email: str) -> None:
    """Revoke a user's bearer token. Their stored Gmail tokens are kept."""
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    if storage.revoke_user(u["id"]):
        click.echo(f"✓ Revoked user {email}.")
    else:
        click.echo(f"User {email} was already revoked.", err=True)


@main.command("user-rotate")
@click.option("--email", required=True, help="User's email address.")
def user_rotate(email: str) -> None:
    """Issue a new bearer token for an existing user (old one stops working immediately)."""
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    new_token = storage.rotate_user_token(u["id"])
    if not new_token:
        click.echo("Could not rotate token.", err=True)
        sys.exit(1)
    click.echo("")
    click.echo(click.style("✓ Token rotated", fg="green", bold=True))
    click.echo(click.style("  New bearer token (SAVE THIS — shown only once):", fg="yellow", bold=True))
    click.echo(f"  {new_token}")


@main.command("cleanup")
def cleanup() -> None:
    """Delete expired OAuth state tokens."""
    n = storage.cleanup_expired_oauth_states()
    click.echo(f"Removed {n} expired OAuth state tokens.")


@main.command("config-snippet")
@click.option("--email", required=True, help="User's email address.")
def config_snippet(email: str) -> None:
    """
    Re-print the Claude Desktop config snippet for a user. Their bearer token
    is NOT included (we can't recover it). Use user-rotate to get a fresh token.
    """
    u = storage.get_user_by_email(email)
    if not u:
        click.echo(f"No user with email {email}.", err=True)
        sys.exit(1)
    snippet = {
        "gmail": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                f"{get_public_base_url()}/mcp",
                "--header",
                "Authorization: Bearer <PASTE-TOKEN-HERE>",
            ],
        }
    }
    click.echo(json.dumps(snippet, indent=2))
    click.echo("")
    click.echo("Use `user-rotate --email " + email + "` to issue a new token if the old one was lost.")


if __name__ == "__main__":
    main()
