"""Beatbump music provider for Music Assistant.

Initial MVP: catalogue search, track lookup and playback through Beatbump's
/api/v1/search.json and /api/v1/player.json endpoints.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
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
    """Music Assistant provider backed by a Beatbump server."""

    @property
    def is_streaming_provider(self) -> bool:
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        return {MediaType.TRACK}

    @property
    def base_url(self) -> str:
        return str(self.get_setup_value(CONF_BASE_URL)).rstrip("/")

    async def _get_json(self, path: str, **params: str) -> dict[str, Any]:
        async with self.mass.http_session.get(
            f"{self.base_url}{path}", params=params, timeout=20
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("Beatbump returned an unexpected response")
            return data

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        if MediaType.TRACK not in media_types:
            return SearchResults()
        payload = await self._get_json(
            "/api/v1/search.json", q=search_query, filter="songs"
        )
        tracks: list[Track] = []
        seen: set[str] = set()

        # Beatbump's filtered search response is a list of shelves:
        # {"results": [{"header": {"title": "Songs"}, "contents": [...] }]}
        # Parse the normalized contents directly instead of recursively walking the
        # raw YouTube response, which is also included elsewhere in the payload.
        for shelf in payload.get("results") or []:
            if not isinstance(shelf, dict):
                continue
            for candidate in shelf.get("contents") or []:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("type") not in (None, "songs"):
                    continue
                video_id = candidate.get("videoId")
                title = candidate.get("title")
                if not isinstance(video_id, str) or not video_id or video_id in seen:
                    continue
                if not isinstance(title, str) or not title:
                    continue
                seen.add(video_id)
                tracks.append(self._track_from_search(video_id, title, candidate))
                if len(tracks) >= limit:
                    return SearchResults(tracks=UniqueList(tracks))
        return SearchResults(tracks=UniqueList(tracks))

    def _track_from_search(
        self, video_id: str, title: str, data: dict[str, Any]
    ) -> Track:
        artist, artist_id = _extract_artist(data)
        duration = _extract_duration(data)
        artists = UniqueList(
            [ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=artist_id or artist,
                provider=self.instance_id,
                name=artist,
            )]
        ) if artist else UniqueList()
        return Track(
            item_id=video_id,
            provider=self.instance_id,
            name=title,
            duration=duration,
            artists=artists,
            provider_mappings={
                ProviderMapping(
                    item_id=video_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                )
            },
        )

    async def get_track(self, prov_track_id: str) -> Track:
        payload = await self._get_json("/api/v1/player.json", videoId=prov_track_id)
        details = payload.get("videoDetails") or {}
        title = str(details.get("title") or prov_track_id)
        artist = str(details.get("author") or "")
        duration = _as_int(details.get("lengthSeconds"))
        return self._track_from_search(
            prov_track_id,
            title,
            {"artist": artist, "duration": duration},
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        payload = await self._get_json("/api/v1/player.json", videoId=item_id)
        formats = (payload.get("streamingData") or {}).get("adaptiveFormats") or []
        audio_formats = [
            item for item in formats
            if isinstance(item, dict)
            and item.get("url")
            and "audio" in str(item.get("mimeType", "")).lower()
        ]
        if not audio_formats:
            raise ValueError(f"Beatbump returned no audio stream for {item_id}")
        selected = max(audio_formats, key=lambda item: _as_int(item.get("bitrate")) or 0)
        mime = str(selected.get("mimeType", "")).lower()
        content_type = ContentType.UNKNOWN
        if "audio/mp4" in mime or "m4a" in mime:
            content_type = ContentType.M4A
        elif "audio/webm" in mime:
            content_type = ContentType.WEBM
        duration_ms = _as_int(selected.get("approxDurationMs"))
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=str(selected["url"]),
            audio_format=AudioFormat(content_type=content_type),
            duration=(duration_ms / 1000) if duration_ms else None,
            can_seek=True,
            allow_seek=True,
        )


def _extract_artist(data: dict[str, Any]) -> tuple[str, str | None]:
    # Beatbump exposes normalized artist metadata here.
    artist_info = data.get("artistInfo")
    if isinstance(artist_info, dict):
        artists = artist_info.get("artist")
        if isinstance(artists, list) and artists and isinstance(artists[0], dict):
            first = artists[0]
            name = first.get("text")
            browse_id = first.get("browseId")
            if isinstance(name, str) and name:
                return name, browse_id if isinstance(browse_id, str) else None

    for key in ("artist", "author"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, None
    return "", None


def _extract_duration(data: dict[str, Any]) -> int | None:
    for key in ("duration", "lengthSeconds"):
        value = _as_int(data.get(key))
        if value is not None:
            return value

    # Search results expose duration as the final subtitle entry, e.g. "8:26".
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


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
