"""Spotify TUI — Textual-based terminal controller."""

import json
import time

import spotify

import spotipy

_PREFS_FILE = spotify.CONFIG_DIR / "prefs.json"


def _load_prefs() -> dict:
    try:
        return json.loads(_PREFS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_prefs(prefs: dict) -> None:
    _PREFS_FILE.write_text(json.dumps(prefs))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, ListView, ListItem, Static


class NowPlaying(Static):
    """Bottom bar showing current track, progress, and playback state."""

    DEFAULT_CSS = """
    NowPlaying {
        dock: bottom;
        height: 3;
        background: $surface;
        border-top: solid $primary-background;
        padding: 0 1;
    }
    """


class SpotifyApp(App):
    CSS = """
    #main-layout {
        height: 1fr;
    }
    #sidebar {
        width: 30;
        border-right: solid $primary-background;
    }
    #sidebar-header, #track-header {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $text;
        background: $surface;
    }
    #playlist-list {
        height: 1fr;
    }
    #content {
        width: 1fr;
    }
    #search-input {
        display: none;
    }
    #search-input.visible {
        display: block;
    }
    #track-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_play", "Play/Pause", show=True, priority=True),
        Binding("n", "next_track", "Next", show=True),
        Binding("p", "prev_track", "Prev", show=True),
        Binding("plus,equal", "vol_up", "+Vol", show=True),
        Binding("minus", "vol_down", "-Vol", show=True),
        Binding("l", "toggle_like", "Like", show=True),
        Binding("s", "toggle_shuffle", "Shuffle", show=True),
        Binding("r", "cycle_repeat", "Repeat", show=True),
        Binding("t", "next_theme", "Theme", show=True),
        Binding("slash", "open_search", "Search", show=True),
        Binding("tab", "switch_focus", "Focus", show=False),
        Binding("escape", "close_search", "Back", show=False),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    def __init__(self, sp: spotipy.Spotify, device_id: str, device_name: str):
        super().__init__()
        self._sp = sp
        self._device_id = device_id
        self._device_name = device_name
        self._playlists: list[dict] = []
        self._tracks: list[dict] = []
        self._context_uri: str | None = None
        # Now-playing state
        self._current: dict = {}
        self._progress_ms: int = 0
        self._duration_ms: int = 0
        self._is_playing: bool = False
        self._volume: int = 50
        self._shuffle: bool = False
        self._repeat: str = "off"

    def compose(self) -> ComposeResult:
        yield Static(
            f"  [green]♫[/] spotify-tui            {self._device_name}",
            id="app-header",
        )
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                yield Static("PLAYLISTS", id="sidebar-header")
                yield ListView(id="playlist-list")
            with Vertical(id="content"):
                yield Input(placeholder="Search tracks...", id="search-input")
                yield Static("Select a playlist", id="track-header")
                yield DataTable(id="track-table", cursor_type="row", zebra_stripes=True)
        yield NowPlaying(id="now-playing")
        yield Footer()

    def on_mount(self) -> None:
        saved_theme = _load_prefs().get("theme")
        if saved_theme and saved_theme in self.available_themes:
            self.theme = saved_theme
        table = self.query_one("#track-table", DataTable)
        table.add_columns("#", "Title", "Artist", "Duration")
        self._load_playlists()
        self._poll_playback()
        self.set_interval(1, self._tick)
        self.set_interval(5, self._poll_playback)

    # ── Playlist loading ──────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="api")
    def _load_playlists(self) -> None:
        try:
            playlists = spotify.fetch_playlists(self._sp)
            self.call_from_thread(self._on_playlists_loaded, playlists)
        except Exception as e:
            self.call_from_thread(self.notify, f"Failed to load playlists: {e}", severity="error")

    def _on_playlists_loaded(self, playlists: list[dict]) -> None:
        self._playlists = playlists
        lv = self.query_one("#playlist-list", ListView)
        lv.clear()
        lv.append(ListItem(Static("♥ Liked Songs"), name="liked"))
        for pl in playlists:
            lv.append(ListItem(Static(pl["name"][:28]), name=pl["id"]))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        if idx == 0:
            self._load_tracks("Liked Songs", None, spotify.fetch_liked_tracks)
        else:
            pl = self._playlists[idx - 1]
            self._load_tracks(
                pl["name"],
                f"spotify:playlist:{pl['id']}",
                spotify.fetch_playlist_tracks,
                playlist_id=pl["id"],
            )

    # ── Track loading ─────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="api")
    def _load_tracks(self, name: str, context_uri: str | None, fetch_fn, **kw) -> None:
        self.call_from_thread(self._begin_track_load, name, context_uri)
        try:
            fetch_fn(
                self._sp,
                on_page=lambda batch: self.call_from_thread(self._append_tracks, batch),
                **kw,
            )
            self.call_from_thread(self._finish_track_load, name)
        except Exception as e:
            self.call_from_thread(self.notify, f"Failed to load tracks: {e}", severity="error")

    def _begin_track_load(self, name: str, context_uri: str | None) -> None:
        self._tracks = []
        self._context_uri = context_uri
        self.query_one("#track-header", Static).update(f"{name} · loading...")
        table = self.query_one("#track-table", DataTable)
        table.clear()
        table.focus()

    def _append_tracks(self, batch: list[dict]) -> None:
        table = self.query_one("#track-table", DataTable)
        offset = len(self._tracks)
        self._tracks.extend(batch)
        for i, t in enumerate(batch, offset + 1):
            dur = spotify.fmt_time(t["duration_ms"])
            table.add_row(str(i), t["name"], t["artist"], dur, key=str(i))

    def _finish_track_load(self, name: str) -> None:
        self.query_one("#track-header", Static).update(
            f"{name} · {len(self._tracks)} tracks"
        )

    # ── Track playback ────────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = int(str(event.row_key.value)) - 1
        except (ValueError, TypeError):
            return
        if idx < 0 or idx >= len(self._tracks):
            return
        self._play_track(idx)

    @work(thread=True, exclusive=True, group="api")
    def _play_track(self, index: int) -> None:
        track = self._tracks[index]
        try:
            self._start_playback_for_track(track)
            self.call_from_thread(self._on_track_started, track)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def _start_playback_for_track(self, track: dict) -> None:
        if self._context_uri:
            spotify.api_call(
                self._sp.start_playback,
                device_id=self._device_id,
                context_uri=self._context_uri,
                offset={"uri": track["uri"]},
            )
        else:
            # No context_uri (liked songs / search results) — send a window
            # of URIs so next/prev/shuffle work, capped to avoid 413
            MAX_QUEUE = 200
            all_uris = [t["uri"] for t in self._tracks]
            try:
                idx = all_uris.index(track["uri"])
            except ValueError:
                idx = 0
            half = MAX_QUEUE // 2
            start = max(0, idx - half)
            end = min(len(all_uris), start + MAX_QUEUE)
            start = max(0, end - MAX_QUEUE)  # adjust if near end
            window = all_uris[start:end]
            offset_in_window = idx - start
            spotify.api_call(
                self._sp.start_playback,
                device_id=self._device_id,
                uris=window,
                offset={"position": offset_in_window},
            )

    def _on_track_started(self, track: dict) -> None:
        self._current = track
        self._progress_ms = 0
        self._duration_ms = track["duration_ms"]
        self._is_playing = True
        self._render_now_playing()

    # ── NowPlaying + progress ─────────────────────────────────────────

    def _tick(self) -> None:
        if self._is_playing and self._duration_ms > 0:
            self._progress_ms = min(self._progress_ms + 1000, self._duration_ms)
            self._render_now_playing()

    @work(thread=True, exclusive=True, group="playback")
    def _poll_playback(self) -> None:
        try:
            pb = spotify.fetch_playback(self._sp)
            if pb:
                self.call_from_thread(self._on_playback_polled, pb)
        except Exception:
            pass  # Silent — poll will retry in 5s

    def _on_playback_polled(self, pb: dict) -> None:
        self._current = spotify.parse_tracks([pb["item"]], wrapper_key=None)[0]
        self._progress_ms = pb.get("progress_ms", 0)
        self._duration_ms = self._current.get("duration_ms", 0)
        self._is_playing = pb.get("is_playing", False)
        self._volume = pb.get("device", {}).get("volume_percent", 50)
        self._shuffle = pb.get("shuffle_state", False)
        self._repeat = pb.get("repeat_state", "off")
        self._render_now_playing()

    def _render_now_playing(self) -> None:
        if not self._current.get("name"):
            self.query_one("#now-playing", NowPlaying).update(
                "[dim]No track playing[/]"
            )
            return

        icon = "[green]▶[/]" if self._is_playing else "[dim]⏸[/]"
        elapsed = spotify.fmt_time(self._progress_ms)
        total = spotify.fmt_time(self._duration_ms)

        bar_w = 35
        pct = self._progress_ms / self._duration_ms if self._duration_ms else 0
        filled = min(int(pct * bar_w), bar_w - 1)
        empty = bar_w - filled - 1
        bar = f"[green]{'━' * filled}[/]╸[dim]{'─' * empty}[/]"

        shuf = "[green bold]🔀 ON[/]" if self._shuffle else "[dim]🔀[/]"
        rep_map = {"off": "[dim]↻[/]", "context": "[green]↻ all[/]", "track": "[green]↻ one[/]"}
        rep = rep_map.get(self._repeat, "[dim]↻[/]")

        line1 = f" {icon} [bold]{self._current['name']}[/] — {self._current['artist']}"
        line2 = f" [dim]{elapsed}[/] {bar} [dim]{total}[/]   Vol {self._volume}%  {shuf}  {rep}"
        self.query_one("#now-playing", NowPlaying).update(f"{line1}\n{line2}")

    # ── Key bindings ──────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="transport")
    def action_toggle_play(self) -> None:
        try:
            if self._is_playing:
                spotify.api_call(
                    self._sp.pause_playback, device_id=self._device_id
                )
                self.call_from_thread(self._set_playing, False)
            else:
                spotify.api_call(
                    self._sp.start_playback, device_id=self._device_id
                )
                self.call_from_thread(self._set_playing, True)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def _set_playing(self, state: bool) -> None:
        self._is_playing = state
        self._render_now_playing()

    def action_next_track(self) -> None:
        self._do_skip(self._sp.next_track)

    def action_prev_track(self) -> None:
        self._do_skip(self._sp.previous_track)

    @work(thread=True, exclusive=True, group="transport")
    def _do_skip(self, fn) -> None:
        try:
            spotify.api_call(fn, device_id=self._device_id)
            time.sleep(0.5)
            pb = spotify.fetch_playback(self._sp)
            if pb:
                self.call_from_thread(self._on_playback_polled, pb)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def action_vol_up(self) -> None:
        self._do_vol_change(5)

    def action_vol_down(self) -> None:
        self._do_vol_change(-5)

    @work(thread=True, exclusive=True, group="api")
    def _do_vol_change(self, delta: int) -> None:
        new_vol = max(0, min(100, self._volume + delta))
        try:
            spotify.api_call(
                self._sp.volume, new_vol, device_id=self._device_id
            )
            self.call_from_thread(self._set_volume, new_vol)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def _set_volume(self, vol: int) -> None:
        self._volume = vol
        self._render_now_playing()

    @work(thread=True, exclusive=True, group="api")
    def action_toggle_like(self) -> None:
        if not self._current.get("id"):
            return
        track_id = self._current["id"]
        try:
            liked = spotify.api_call(
                self._sp.current_user_saved_tracks_contains,
                tracks=[track_id],
            )
            if liked and liked[0]:
                spotify.api_call(
                    self._sp.current_user_saved_tracks_delete,
                    tracks=[track_id],
                )
                self.call_from_thread(
                    self.notify, "Removed from library", severity="information"
                )
            else:
                spotify.api_call(
                    self._sp.current_user_saved_tracks_add,
                    tracks=[track_id],
                )
                self.call_from_thread(
                    self.notify, "Saved to library ♥", severity="information"
                )
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    @work(thread=True, exclusive=True, group="api")
    def action_toggle_shuffle(self) -> None:
        new_state = not self._shuffle
        try:
            spotify.api_call(
                self._sp.shuffle, new_state, device_id=self._device_id
            )
            self.call_from_thread(self._set_shuffle, new_state)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def _set_shuffle(self, state: bool) -> None:
        self._shuffle = state
        label = "on" if state else "off"
        self.notify(f"Shuffle {label}")
        self._render_now_playing()

    @work(thread=True, exclusive=True, group="api")
    def action_cycle_repeat(self) -> None:
        cycle = {"off": "context", "context": "track", "track": "off"}
        new_state = cycle.get(self._repeat, "off")
        try:
            spotify.api_call(
                self._sp.repeat, new_state, device_id=self._device_id
            )
            self.call_from_thread(self._set_repeat, new_state)
        except spotipy.SpotifyException as e:
            self.call_from_thread(self._handle_api_error, e)

    def _set_repeat(self, state: str) -> None:
        self._repeat = state
        label = {"off": "off", "context": "all", "track": "one"}.get(state, state)
        self.notify(f"Repeat {label}")
        self._render_now_playing()

    # ── Theme ─────────────────────────────────────────────────────────

    def action_next_theme(self) -> None:
        themes = sorted(self.available_themes.keys())
        idx = themes.index(self.theme) if self.theme in themes else -1
        self.theme = themes[(idx + 1) % len(themes)]
        prefs = _load_prefs()
        prefs["theme"] = self.theme
        _save_prefs(prefs)
        t = self.available_themes[self.theme]
        colors = [f"[{val}]██[/] {name}" for name, val in [("pri", t.primary), ("sec", t.secondary), ("acc", t.accent), ("suc", t.success), ("warn", t.warning), ("err", t.error)] if val]
        swatch = "  ".join(colors) if colors else ""
        mode = "dark" if t.dark else "light"
        self.notify(f"{self.theme} ({mode})\n{swatch}", timeout=3)

    # ── Search ────────────────────────────────────────────────────────

    def action_open_search(self) -> None:
        search = self.query_one("#search-input", Input)
        search.add_class("visible")
        search.value = ""
        search.focus()

    def action_close_search(self) -> None:
        search = self.query_one("#search-input", Input)
        if search.has_class("visible"):
            search.remove_class("visible")
            search.value = ""
            self.query_one("#track-table", DataTable).focus()
        else:
            self.query_one("#playlist-list", ListView).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        self._search_tracks(query)

    @work(thread=True, exclusive=True, group="api")
    def _search_tracks(self, query: str) -> None:
        try:
            tracks = spotify.search_tracks(self._sp, query)
            self.call_from_thread(self._begin_track_load, f'Search: "{query}"', None)
            self.call_from_thread(self._append_tracks, tracks)
            self.call_from_thread(self._finish_track_load, f'Search: "{query}"')
            self.call_from_thread(self._close_search_input)
        except Exception as e:
            self.call_from_thread(self.notify, f"Search failed: {e}", severity="error")

    def _close_search_input(self) -> None:
        search = self.query_one("#search-input", Input)
        search.remove_class("visible")
        search.value = ""

    # ── Focus ─────────────────────────────────────────────────────────

    def action_switch_focus(self) -> None:
        focused = self.focused
        if isinstance(focused, (ListView, ListItem)):
            self.query_one("#track-table", DataTable).focus()
        else:
            self.query_one("#playlist-list", ListView).focus()

    def action_quit_app(self) -> None:
        self.exit()

    # ── Error handling ────────────────────────────────────────────────

    def _handle_api_error(self, e: spotipy.SpotifyException) -> None:
        if e.http_status == 404:
            self.notify("Device lost. Is your Spotify device still running?", severity="error")
            self._reconnect_device()
        elif e.http_status == 403:
            self.notify("Spotify Premium required for this action", severity="error")
        else:
            reason = getattr(e, "reason", None) or ""
            self.notify(
                f"Spotify {e.http_status}: {reason or 'request failed'}",
                severity="error",
            )

    @work(thread=True, exclusive=True, group="reconnect")
    def _reconnect_device(self) -> None:
        for _ in range(8):
            try:
                devices = self._sp.devices().get("devices", [])
                for d in devices:
                    if d["id"] == self._device_id:
                        self.call_from_thread(
                            self.notify,
                            "Device reconnected!",
                            severity="information",
                        )
                        return
            except Exception:
                pass
            time.sleep(5)
        self.call_from_thread(
            self.notify, "Could not reconnect device", severity="error"
        )
