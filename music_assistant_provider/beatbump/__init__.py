"""Beatbump music provider for Music Assistant."""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import Artist, AudioFormat, BrowseFolder, ItemMapping, MediaItemType, ProviderMapping, SearchResults, Track, UniqueList
from music_assistant_models.streamdetails import StreamDetails
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

CONF_BASE_URL = "base_url"
SUPPORTED_FEATURES = {ProviderFeature.SEARCH, ProviderFeature.BROWSE}

async def setup(mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig) -> ProviderInstanceType:
    return BeatbumpProvider(mass, manifest, config, SUPPORTED_FEATURES)

class BeatbumpProvider(MusicProvider):
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
        async with self.mass.http_session.get(f"{self.base_url}{path}", params=params, timeout=30) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("Beatbump returned an unexpected response")
            return data

    async def _search_songs(self, query: str) -> list[dict[str, Any]]:
        payload = await self._get_json("/api/v1/search.json", q=query, filter="songs")
        items: list[dict[str, Any]] = []
        for shelf in payload.get("results") or []:
            if isinstance(shelf, dict):
                items.extend(x for x in shelf.get("contents") or [] if isinstance(x, dict))
        return items

    async def search(self, search_query: str, media_types: list[MediaType], limit: int = 5) -> SearchResults:
        result = SearchResults()
        if MediaType.TRACK not in media_types:
            return result
        items = await self._search_songs(search_query)
        result.tracks = [track for item in items[:limit] if (track := self._parse_track(item))]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Expose Beatbump as a browsable provider, starting with its native Home feed."""
        base = f"{self.instance_id}://"
        subpath = path.split("://", 1)[1].strip("/") if "://" in path else ""
        if not subpath:
            return [
                BrowseFolder(
                    item_id="home",
                    provider=self.domain,
                    path=f"{base}home",
                    name="Beatbump Home",
                )
            ]
        if subpath != "home":
            return []

        payload = await self._get_json("/api/v1/home.json")
        result: list[MediaItemType | BrowseFolder] = []
        seen: set[str] = set()
        # Beatbump Home is returned as carousels. For the first stable browse version,
        # flatten playable song/video entries into MA rather than exposing unsupported
        # album/playlist objects. This keeps the already-working playback path intact.
        for carousel in payload.get("carousels") or []:
            if not isinstance(carousel, dict):
                continue
            for item in carousel.get("contents") or []:
                if not isinstance(item, dict):
                    continue
                video_id = _first_string(item, "videoId")
                if not video_id or video_id in seen:
                    continue
                track = self._parse_track(item)
                if track:
                    seen.add(video_id)
                    result.append(track)
        return result

    def _provider_mapping(self, item_id: str, audio: bool = False) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=True,
            audio_format=AudioFormat(content_type=ContentType.M4A) if audio else None,
        )

    def _artist_mapping(self, name: str, artist_id: str | None) -> ItemMapping | None:
        # Never manufacture an artist id from a display name. Beatbump's artist route
        # requires a real YouTube Music browse/channel id.
        if not artist_id:
            return None
        return ItemMapping(media_type=MediaType.ARTIST, item_id=artist_id, provider=self.instance_id, name=name)

    def _parse_track(self, data: dict[str, Any], position: int = 0) -> Track | None:
        video_id = _first_string(data, "videoId")
        title = _first_string(data, "title", "name", "text")
        if not video_id or not title:
            return None
        artist_name, artist_id = _extract_artist(data)
        artists = UniqueList()
        if artist_name and (mapping := self._artist_mapping(artist_name, artist_id)):
            artists.append(mapping)
        track = Track(
            item_id=video_id,
            provider=self.instance_id,
            name=title,
            duration=_extract_duration(data),
            provider_mappings={self._provider_mapping(video_id, audio=True)},
            position=position or None,
        )
        if artists:
            track.artists = artists
        return track

    async def get_artist(self, prov_artist_id: str) -> Artist:
        payload = await self._get_json(f"/api/v1/artist/{prov_artist_id}")
        header = payload.get("header") or {}
        name = _first_string(header, "name", "title") or prov_artist_id
        return Artist(item_id=prov_artist_id, provider=self.instance_id, name=name, provider_mappings={self._provider_mapping(prov_artist_id)})

    async def get_track(self, prov_track_id: str) -> Track:
        payload = await self._get_json("/api/v1/player.json", videoId=prov_track_id)
        details = payload.get("videoDetails") or {}
        title = str(details.get("title") or prov_track_id)
        duration = _as_int(details.get("lengthSeconds"))
        artist_mapping: ItemMapping | None = None
        try:
            for candidate in await self._search_songs(title):
                if _first_string(candidate, "videoId") != prov_track_id:
                    continue
                artist_name, artist_id = _extract_artist(candidate)
                if artist_name:
                    artist_mapping = self._artist_mapping(artist_name, artist_id)
                break
        except Exception as err:
            self.logger.debug("Could not enrich Beatbump track %s: %s", prov_track_id, err)
        track = Track(item_id=prov_track_id, provider=self.instance_id, name=title, duration=duration, provider_mappings={self._provider_mapping(prov_track_id, audio=True)})
        if artist_mapping:
            track.artists = UniqueList([artist_mapping])
        return track

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        payload = await self._get_json("/api/v1/player.json", videoId=item_id)
        status = (payload.get("playabilityStatus") or {}).get("status")
        if status and status != "OK":
            raise ValueError(f"Beatbump reports {item_id} as not playable: {status}")
        formats = (payload.get("streamingData") or {}).get("adaptiveFormats") or []
        audio_formats = [x for x in formats if isinstance(x, dict) and x.get("url") and "audio" in str(x.get("mimeType", "")).lower()]
        if not audio_formats:
            raise ValueError(f"Beatbump returned no audio stream for {item_id}")
        aac = [x for x in audio_formats if _as_int(x.get("itag")) == 140]
        selected = aac[0] if aac else max(audio_formats, key=lambda x: _as_int(x.get("bitrate")) or 0)
        url = str(selected["url"])
        mime = str(selected.get("mimeType", "")).lower()
        content_type = ContentType.M4A if "audio/mp4" in mime else ContentType.WEBM if "audio/webm" in mime else ContentType.UNKNOWN
        expiration = 3600
        expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire:
            try:
                expiration = max(60, int(expire) - int(time.time()))
            except (TypeError, ValueError):
                pass
        duration_ms = _as_int(selected.get("approxDurationMs"))
        self.logger.info("Beatbump playback %s -> itag=%s mime=%s", item_id, selected.get("itag"), selected.get("mimeType"))
        return StreamDetails(provider=self.instance_id, item_id=item_id, media_type=MediaType.TRACK, stream_type=StreamType.HTTP, path=url, audio_format=AudioFormat(content_type=content_type), duration=(duration_ms / 1000) if duration_ms else None, can_seek=True, allow_seek=True, expiration=expiration, is_realtime=True)

def _first_string(data: Any, *keys: str) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None

def _extract_artist(data: dict[str, Any]) -> tuple[str, str | None]:
    info = data.get("artistInfo")
    if isinstance(info, dict):
        artists = info.get("artist")
        if isinstance(artists, list) and artists and isinstance(artists[0], dict):
            first = artists[0]
            name = _first_string(first, "text", "name")
            if name:
                return name, _first_string(first, "browseId", "id")
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in subtitle:
            if isinstance(entry, dict) and "ARTIST" in str(entry.get("pageType", "")):
                name = _first_string(entry, "text", "name")
                if name:
                    return name, _first_string(entry, "browseId", "id")
    return "", None

def _extract_duration(data: dict[str, Any]) -> int | None:
    for key in ("duration", "lengthSeconds"):
        if (value := _as_int(data.get(key))) is not None:
            return value
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in reversed(subtitle):
            text = entry.get("text") if isinstance(entry, dict) else None
            if isinstance(text, str):
                parts = text.split(":")
                if len(parts) in (2, 3) and all(x.isdigit() for x in parts):
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
