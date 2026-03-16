"""CLI entry point for spotify-tui."""

import argparse
import sys

def setup():
    """Interactive first-time configuration wizard."""
    from pathlib import Path
    config_dir = Path.home() / ".config" / "spotify-tui"
    env_file = config_dir / ".env"

    print()
    print("  spotify-tui setup")
    print("  ─────────────────")
    print()
    print("  You'll need a Spotify app to use this.")
    print("  Create one at: https://developer.spotify.com/dashboard")
    print("  Set the redirect URI to: http://127.0.0.1:8888/callback")
    print()

    client_id = input("  Client ID: ").strip()
    if not client_id:
        print("  Client ID is required.")
        sys.exit(1)

    client_secret = input("  Client Secret: ").strip()
    if not client_secret:
        print("  Client Secret is required.")
        sys.exit(1)

    config_dir.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"SPOTIPY_CLIENT_ID={client_id}\n"
        f"SPOTIPY_CLIENT_SECRET={client_secret}\n"
        f"SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback\n"
    )
    print()
    print(f"  Config saved to {env_file}")
    print()

    # Load env BEFORE importing spotify (which runs load_dotenv at module level)
    print("  Opening browser for Spotify authorization...")
    from dotenv import load_dotenv
    load_dotenv(env_file, override=True)
    import spotify
    spotify.get_spotify()

    print()
    print("  Setup complete! Run `spotify-tui` to start.")
    print("  Tip: alias music='spotify-tui'")
    print()


def cli():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="spotify-tui",
        description="Terminal UI for Spotify",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Run the setup wizard")
    args = parser.parse_args()

    if args.command == "setup":
        setup()
        return

    # Check if config exists, auto-run setup if missing
    from pathlib import Path
    env_file = Path.home() / ".config" / "spotify-tui" / ".env"
    if not env_file.exists():
        print("  No config found. Running first-time setup...\n")
        setup()

    import spotify
    from app import SpotifyApp

    sp = spotify.get_spotify()
    device_id, device_name = spotify.pick_device(sp)
    SpotifyApp(sp, device_id, device_name).run()


if __name__ == "__main__":
    cli()
