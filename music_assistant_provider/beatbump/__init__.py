"""Beatbump music provider for Music Assistant."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

CONF_BASE_URL = "base_url"
SUPPORTED_FEATURES = {ProviderFeature.SEARCH}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    return BeatbumpProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BeatbumpProvider(MusicProvider):
    """Music Assistant provider backed by a self-hosted Beatbump server."""

    @property
    def is_streaming_provider(self) -> bool:
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        return {MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST}

    @property
    def base_url(self) -> str:
        return str(self.get_setup_value(CONF_BASE_URL)).rstrip("/")

    async def _get_json(self, path: str, **params: str) -> dict[str, Any]:
        async with self.mass.http_session.get(
            f"{self.base_url}{path}", params=params, timeout=30
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("Beatbump returned an unexpected response")
            return data

    async def _search_filter(self, query: str, filter_name: str) -> list[dict[str, Any]]:
        payload = await self._get_json("/api/v1/search.json", q=query, filter=filter_name)
        items: list[dict[str, Any]] = []
        for shelf in payload.get("results") or []:
            if not isinstance(shelf, dict):
                continue
            for candidate in shelf.get("contents") or []:
                if isinstance(candidate, dict):
                    items.append(candidate)
        return items

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search each Beatbump filter separately; Beatbump's unfiltered search is unreliable."""
        result = SearchResults()
        requested = set(media_types)

        if MediaType.TRACK in requested:
            items = await self._search_filter(search_query, "songs")
            result.tracks = [
                track
                for item in items[:limit]
                if (track := self._parse_track(item)) is not None
            ]

        if MediaType.ARTIST in requested:
            items = await self._search_filter(search_query, "artists")
            result.artists = [
                artist
                for item in items[:limit]
                if (artist := self._parse_artist_search(item)) is not None
            ]

        if MediaType.ALBUM in requested:
            items = await self._search_filter(search_query, "albums")
            result.albums = [
                album
                for item in items[:limit]
                if (album := self._parse_album_search(item)) is not None
            ]

        if MediaType.PLAYLIST in requested:
            items = await self._search_filter(search_query, "all_playlists")
            result.playlists = [
                playlist
                for item in items[:limit]
                if (playlist := self._parse_playlist_search(item)) is not None
            ]
        return result

    def _mapping(self, media_type: MediaType, item_id: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=item_id,
            provider=self.instance_id,
            name=name,
        )

    def _provider_mapping(self, item_id: str, audio: bool = False) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=True,
            audio_format=AudioFormat(content_type=ContentType.M4A) if audio else None,
        )

    def _parse_track(self, data: dict[str, Any], position: int = 0) -> Track | None:
        video_id = _first_string(data, "videoId")
        title = _title(data)
        if not video_id or not title:
            return None
        artist_name, artist_id = _extract_artist(data)
        if not artist_name:
            # Music Assistant expects tracks to have an artist. YouTube occasionally omits it.
            artist_name = "YouTube Music"
            artist_id = "youtube-music"
        track = Track(
            item_id=video_id,
            provider=self.instance_id,
            name=title,
            duration=_extract_duration(data),
            artists=UniqueList([
                self._mapping(MediaType.ARTIST, artist_id or artist_name, artist_name)
            ]),
            provider_mappings={self._provider_mapping(video_id, audio=True)},
            position=position or None,
        )
        album = data.get("album")
        if isinstance(album, dict):
            album_name = _first_string(album, "text", "title", "name")
            album_id = _first_string(album, "browseId", "id")
            if album_name and album_id:
                track.album = self._mapping(MediaType.ALBUM, album_id, album_name)
        return track

    def _parse_artist_search(self, data: dict[str, Any]) -> Artist | None:
        name = _title(data)
        artist_id = _browse_id(data)
        if not name or not artist_id:
            return None
        return Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={self._provider_mapping(artist_id)},
        )

    def _parse_album_search(self, data: dict[str, Any]) -> Album | None:
        name = _title(data)
        album_id = _browse_id(data)
        if not name or not album_id:
            return None
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={self._provider_mapping(album_id)},
        )
        artist_name, artist_id = _extract_artist(data)
        if artist_name:
            album.artists = UniqueList([
                self._mapping(MediaType.ARTIST, artist_id or artist_name, artist_name)
            ])
        return album

    def _parse_playlist_search(self, data: dict[str, Any]) -> Playlist | None:
        name = _title(data)
        playlist_id = _first_string(data, "playlistId") or _browse_id(data)
        if not name or not playlist_id:
            return None
        return Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={self._provider_mapping(playlist_id)},
            is_editable=False,
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Resolve an artist mapping created by search/track parsing."""
        if prov_artist_id == "youtube-music":
            return Artist(
                item_id=prov_artist_id,
                provider=self.instance_id,
                name="YouTube Music",
                provider_mappings={self._provider_mapping(prov_artist_id)},
            )
        payload = await self._get_json(f"/api/v1/artist/{prov_artist_id}")
        header = payload.get("header") or {}
        name = _first_string(header, "name") or prov_artist_id
        artist = Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={self._provider_mapping(prov_artist_id)},
        )
        if isinstance(header, dict) and isinstance(header.get("description"), str):
            artist.metadata.description = header["description"]
        return artist

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        payload = await self._get_json(f"/api/v1/artist/{prov_artist_id}")
        candidates: list[dict[str, Any]] = []
        songs = payload.get("songs")
        if isinstance(songs, dict):
            candidates.extend(x for x in songs.get("contents") or [] if isinstance(x, dict))
        for carousel in payload.get("carousels") or []:
            if not isinstance(carousel, dict):
                continue
            header = carousel.get("header") or {}
            if "song" not in str(header.get("title", "")).lower():
                continue
            candidates.extend(x for x in carousel.get("contents") or [] if isinstance(x, dict))
        return [track for item in candidates if (track := self._parse_track(item)) is not None][:25]

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        payload = await self._get_json(f"/api/v1/artist/{prov_artist_id}")
        albums: list[Album] = []
        for carousel in payload.get("carousels") or []:
            if not isinstance(carousel, dict):
                continue
            header = carousel.get("header") or {}
            title = str(header.get("title", "")).lower()
            if "album" not in title and "single" not in title:
                continue
            for item in carousel.get("contents") or []:
                if isinstance(item, dict) and (album := self._parse_album_search(item)):
                    albums.append(album)
        return albums

    async def _playlist_payload(self, item_id: str) -> dict[str, Any]:
        return await self._get_json("/api/v1/playlist.json", list=item_id)

    async def get_album(self, prov_album_id: str) -> Album:
        payload = await self._playlist_payload(prov_album_id)
        header = payload.get("header") or {}
        title = _header_title(header) or prov_album_id
        album = Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={self._provider_mapping(prov_album_id)},
        )
        tracks = payload.get("tracks") or []
        for item in tracks:
            if isinstance(item, dict):
                artist_name, artist_id = _extract_artist(item)
                if artist_name:
                    album.artists = UniqueList([
                        self._mapping(MediaType.ARTIST, artist_id or artist_name, artist_name)
                    ])
                    break
        return album

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        payload = await self._playlist_payload(prov_album_id)
        result: list[Track] = []
        for index, item in enumerate(payload.get("tracks") or [], 1):
            if isinstance(item, dict) and (track := self._parse_track(item, index)):
                track.album = self._mapping(
                    MediaType.ALBUM,
                    prov_album_id,
                    _header_title(payload.get("header") or {}) or prov_album_id,
                )
                result.append(track)
        return result

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        payload = await self._playlist_payload(prov_playlist_id)
        header = payload.get("header") or {}
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=_header_title(header) or prov_playlist_id,
            provider_mappings={self._provider_mapping(prov_playlist_id)},
            is_editable=False,
        )

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        if page > 0:
            return []
        payload = await self._playlist_payload(prov_playlist_id)
        return [
            track
            for index, item in enumerate(payload.get("tracks") or [], 1)
            if isinstance(item, dict) and (track := self._parse_track(item, index)) is not None
        ]

    async def get_track(self, prov_track_id: str) -> Track:
        payload = await self._get_json("/api/v1/player.json", videoId=prov_track_id)
        details = payload.get("videoDetails") or {}
        title = str(details.get("title") or prov_track_id)
        artist = str(details.get("author") or "YouTube Music")
        duration = _as_int(details.get("lengthSeconds"))
        return self._parse_track({
            "videoId": prov_track_id,
            "title": title,
            "artist": artist,
            "duration": duration,
        }) or Track(
            item_id=prov_track_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={self._provider_mapping(prov_track_id, audio=True)},
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a fresh signed YouTube audio URL through Beatbump for every playback."""
        payload = await self._get_json("/api/v1/player.json", videoId=item_id)
        status = (payload.get("playabilityStatus") or {}).get("status")
        if status and status != "OK":
            raise ValueError(f"Beatbump reports {item_id} as not playable: {status}")

        formats = (payload.get("streamingData") or {}).get("adaptiveFormats") or []
        audio_formats = [
            item for item in formats
            if isinstance(item, dict)
            and item.get("url")
            and "audio" in str(item.get("mimeType", "")).lower()
        ]
        if not audio_formats:
            raise ValueError(f"Beatbump returned no audio stream for {item_id}")

        # Prefer AAC-LC/M4A itag 140 for maximum MA/player compatibility.
        aac_140 = [item for item in audio_formats if _as_int(item.get("itag")) == 140]
        selected = aac_140[0] if aac_140 else max(
            audio_formats, key=lambda item: _as_int(item.get("bitrate")) or 0
        )
        url = str(selected["url"])
        mime = str(selected.get("mimeType", "")).lower()
        if "audio/mp4" in mime or "m4a" in mime:
            content_type = ContentType.M4A
        elif "audio/webm" in mime:
            content_type = ContentType.WEBM
        else:
            content_type = ContentType.UNKNOWN

        expiration = 3600
        query = parse_qs(urlparse(url).query)
        if expire := query.get("expire", [None])[0]:
            try:
                expiration = max(60, int(expire) - int(time.time()))
            except (TypeError, ValueError):
                pass
        duration_ms = _as_int(selected.get("approxDurationMs"))
        self.logger.info(
            "Beatbump playback %s -> itag=%s mime=%s bitrate=%s expires_in=%ss",
            item_id,
            selected.get("itag"),
            selected.get("mimeType"),
            selected.get("bitrate"),
            expiration,
        )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=url,
            audio_format=AudioFormat(content_type=content_type),
            duration=(duration_ms / 1000) if duration_ms else None,
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
            # YouTube serves adaptive streams at approximately playback pace.
            is_realtime=True,
        )


def _title(data: dict[str, Any]) -> str | None:
    return _first_string(data, "title", "name", "text")


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _browse_id(data: dict[str, Any]) -> str | None:
    if value := _first_string(data, "browseId", "id"):
        return value
    endpoint = data.get("endpoint")
    if isinstance(endpoint, dict):
        return _first_string(endpoint, "browseId", "browseID")
    return None


def _extract_artist(data: dict[str, Any]) -> tuple[str, str | None]:
    artist_info = data.get("artistInfo")
    if isinstance(artist_info, dict):
        artists = artist_info.get("artist")
        if isinstance(artists, list) and artists and isinstance(artists[0], dict):
            first = artists[0]
            name = _first_string(first, "text", "name")
            if name:
                return name, _first_string(first, "browseId", "id")
    for key in ("artist", "author"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, None
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in subtitle:
            if not isinstance(entry, dict):
                continue
            if "ARTIST" in str(entry.get("pageType", "")):
                name = _first_string(entry, "text", "name")
                if name:
                    return name, _first_string(entry, "browseId", "id")
    return "", None


def _extract_duration(data: dict[str, Any]) -> int | None:
    for key in ("duration", "lengthSeconds"):
        value = _as_int(data.get(key))
        if value is not None:
            return value
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in reversed(subtitle):
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            parts = text.split(":")
            if len(parts) in (2, 3) and all(part.isdigit() for part in parts):
                seconds = 0
                for part in parts:
                    seconds = seconds * 60 + int(part)
                return seconds
    return None


def _header_title(header: Any) -> str | None:
    if not isinstance(header, dict):
        return None
    value = header.get("title")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((x for x in value if isinstance(x, str) and x), None)
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
