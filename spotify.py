"""Spotify API layer — thin wrappers around spotipy for the TUI."""

import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "spotify-tui"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

from dotenv import load_dotenv

load_dotenv(CONFIG_DIR / ".env")

import requests.adapters
import spotipy
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth
from urllib3.util.retry import Retry

SCOPE = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-library-read "
    "user-library-modify"
)


def get_spotify() -> spotipy.Spotify:
    cache_handler = CacheFileHandler(cache_path=str(CONFIG_DIR / ".cache"))
    auth = SpotifyOAuth(scope=SCOPE, open_browser=True, cache_handler=cache_handler)
    sp = spotipy.Spotify(auth_manager=auth, retries=0)
    no_retry = Retry(total=0, status_forcelist=[])
    adapter = requests.adapters.HTTPAdapter(max_retries=no_retry)
    sp._session.mount("https://", adapter)
    sp._session.mount("http://", adapter)
    return sp


def pick_device(sp: spotipy.Spotify) -> tuple[str, str]:
    """List Spotify Connect devices and let the user choose. Returns (device_id, device_name)."""
    devices = []
    for attempt in range(6):
        try:
            devices = sp.devices().get("devices", [])
        except Exception as e:
            if attempt == 5:
                print(f"ERROR: Could not fetch devices: {e}")
                raise SystemExit(1)
        if devices:
            break
        if attempt < 5:
            print(f"  Waiting for devices... ({attempt + 1}/5)")
            time.sleep(2)

    if not devices:
        print("ERROR: No Spotify Connect devices found.")
        print("Make sure you have Spotify open on a device (phone, desktop, web player, spotifyd, etc.)")
        raise SystemExit(1)

    if len(devices) == 1:
        d = devices[0]
        print(f"Using device: {d['name']}")
        return d["id"], d["name"]

    print("\nAvailable devices:")
    for i, d in enumerate(devices, 1):
        active = " (active)" if d.get("is_active") else ""
        print(f"  {i}. {d['name']} — {d['type']}{active}")

    while True:
        try:
            choice = input(f"\nSelect device [1-{len(devices)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                d = devices[idx]
                return d["id"], d["name"]
        except (ValueError, EOFError):
            pass
        print(f"Please enter a number between 1 and {len(devices)}")


def api_call(fn, *args, max_retries: int = 4, **kwargs):
    """Call a Spotify API function with rate-limit retry loop."""
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status == 429 and attempt < max_retries:
                wait = int(e.headers.get("Retry-After", 5)) if e.headers else 5
                time.sleep(wait)
                continue
            raise


def parse_tracks(items: list, wrapper_key: str | None = "track") -> list[dict]:
    tracks = []
    for item in items:
        t = item.get(wrapper_key) if wrapper_key else item
        if not t:
            continue
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        tracks.append({
            "name": t.get("name", "Unknown"),
            "artist": artists,
            "uri": t.get("uri", ""),
            "id": t.get("id", ""),
            "duration_ms": t.get("duration_ms", 0),
        })
    return tracks


def fmt_time(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


# ── Convenience wrappers (blocking, called from Textual workers) ─────


def fetch_playlists(sp: spotipy.Spotify) -> list[dict]:
    results = api_call(sp.current_user_playlists, limit=50)
    all_pl = list(results.get("items", []))
    while results.get("next"):
        results = api_call(sp.next, results)
        if results:
            all_pl.extend(results.get("items", []))
    return [
        {
            "name": pl["name"],
            "id": pl["id"],
            "total": pl.get("tracks", {}).get("total", 0),
        }
        for pl in all_pl
    ]


def fetch_playlist_tracks(
    sp: spotipy.Spotify, playlist_id: str, on_page=None,
) -> list[dict]:
    first_page = api_call(sp.playlist_tracks, playlist_id, limit=50)
    return _stream_pages(sp, first_page, "track", on_page)


def fetch_liked_tracks(sp: spotipy.Spotify, on_page=None) -> list[dict]:
    first_page = api_call(sp.current_user_saved_tracks, limit=50)
    return _stream_pages(sp, first_page, "track", on_page)


def _stream_pages(sp, first_page, wrapper_key, on_page=None) -> list[dict]:
    """Paginate and optionally call on_page(batch) after each page."""
    all_tracks = []
    batch = parse_tracks(first_page.get("items", []), wrapper_key=wrapper_key)
    all_tracks.extend(batch)
    if on_page and batch:
        on_page(batch)
    results = first_page
    while results.get("next"):
        results = api_call(sp.next, results)
        if results:
            batch = parse_tracks(results.get("items", []), wrapper_key=wrapper_key)
            all_tracks.extend(batch)
            if on_page and batch:
                on_page(batch)
    return all_tracks


def search_tracks(sp: spotipy.Spotify, query: str, limit: int = 25) -> list[dict]:
    results = api_call(sp.search, q=query, type="track", limit=limit)
    items = results.get("tracks", {}).get("items", [])
    return parse_tracks(items, wrapper_key=None)


def fetch_playback(sp: spotipy.Spotify) -> dict | None:
    try:
        pb = api_call(sp.current_playback)
        if pb and pb.get("item"):
            return pb
    except Exception:
        pass
    return None
