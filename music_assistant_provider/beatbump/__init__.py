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
        for candidate in _walk_dicts(payload.get("results", payload)):
            video_id = _find_string(candidate, "videoId", "video_id")
            if not video_id or video_id in seen:
                continue
            title = _extract_title(candidate)
            if not title:
                continue
            seen.add(video_id)
            tracks.append(self._track_from_search(video_id, title, candidate))
            if len(tracks) >= limit:
                break
        return SearchResults(tracks=UniqueList(tracks))

    def _track_from_search(
        self, video_id: str, title: str, data: dict[str, Any]
    ) -> Track:
        artist = _extract_artist(data)
        duration = _extract_duration(data)
        artists = UniqueList(
            [ItemMapping(media_type=MediaType.ARTIST, item_id=artist, provider=self.instance_id, name=artist)]
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


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _find_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    for value in data.values():
        if isinstance(value, dict):
            found = _find_string(value, *keys)
            if found:
                return found
    return None


def _extract_title(data: dict[str, Any]) -> str | None:
    for key in ("title", "name"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            runs = value.get("runs")
            if isinstance(runs, list) and runs and isinstance(runs[0], dict):
                text = runs[0].get("text")
                if isinstance(text, str) and text:
                    return text
    return _find_string(data, "title")


def _extract_artist(data: dict[str, Any]) -> str:
    for key in ("artist", "author"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    artists = data.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return str(first.get("name") or first.get("text") or "")
    return ""


def _extract_duration(data: dict[str, Any]) -> int | None:
    for key in ("duration", "lengthSeconds"):
        value = _as_int(data.get(key))
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
