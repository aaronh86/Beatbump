"""Beatbump music provider for Music Assistant."""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from music_assistant_models.enums import ContentType, ImageType, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import (
    Artist, AudioFormat, BrowseFolder, ItemMapping, MediaItemImage, MediaItemType,
    ProviderMapping, SearchResults, Track, UniqueList,
)
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

    async def _get_json_any(self, path: str, **params: str) -> Any:
        async with self.mass.http_session.get(f"{self.base_url}{path}", params=params, timeout=30) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _get_json(self, path: str, **params: str) -> dict[str, Any]:
        data = await self._get_json_any(path, **params)
        if not isinstance(data, dict):
            raise ValueError("Beatbump returned an unexpected response")
        return data

    async def _search_items(self, query: str, filter_name: str) -> list[dict[str, Any]]:
        payload = await self._get_json("/api/v1/search.json", q=query, filter=filter_name)
        items: list[dict[str, Any]] = []
        for shelf in payload.get("results") or []:
            if isinstance(shelf, dict):
                items.extend(x for x in (shelf.get("contents") or shelf.get("items") or []) if isinstance(x, dict))
        return items

    async def _search_songs(self, query: str) -> list[dict[str, Any]]:
        return await self._search_items(query, "songs")

    async def search(self, search_query: str, media_types: list[MediaType], limit: int = 5) -> SearchResults:
        result = SearchResults()
        if MediaType.TRACK in media_types:
            items = await self._search_songs(search_query)
            result.tracks = [track for item in items[:limit] if (track := self._parse_track(item))]
        return result

    def _image_from_data(self, data: dict[str, Any]) -> MediaItemImage | None:
        thumbs = data.get("thumbnails") or data.get("foregroundThumbnails") or []
        if not isinstance(thumbs, list) or not thumbs:
            return None
        valid = [x for x in thumbs if isinstance(x, dict) and isinstance(x.get("url"), str) and x.get("url")]
        if not valid:
            return None
        best = max(valid, key=lambda x: (_as_int(x.get("width")) or 0) * (_as_int(x.get("height")) or 0))
        return MediaItemImage(type=ImageType.THUMB, path=best["url"], provider=self.instance_id, remotely_accessible=True)

    def _browse_folder(self, item_id: str, path: str, name: str, data: dict[str, Any] | None = None) -> BrowseFolder:
        image = self._image_from_data(data) if data else None
        return BrowseFolder(
            item_id=item_id,
            provider=self.instance_id,
            path=f"{self.instance_id}://{path}",
            name=name,
            image=image,
        )

    def _add_images(self, media_item: Any, data: dict[str, Any]) -> None:
        image = self._image_from_data(data)
        if image is not None:
            media_item.metadata.images = UniqueList([image])

    def _carousel_folders(self, payload: dict[str, Any], prefix: str) -> list[BrowseFolder]:
        result: list[BrowseFolder] = []
        for index, carousel in enumerate(payload.get("carousels") or []):
            if not isinstance(carousel, dict):
                continue
            items = carousel.get("items") or carousel.get("contents") or []
            if items:
                result.append(self._browse_folder(f"{prefix}-{index}", f"{prefix}/section/{index}", _carousel_name(carousel) or f"Section {index + 1}", carousel))
        return result

    def _carousel_items(self, payload: dict[str, Any], index: int, prefix: str) -> list[MediaItemType | BrowseFolder]:
        carousels = payload.get("carousels") or []
        if index < 0 or index >= len(carousels) or not isinstance(carousels[index], dict):
            return []
        result: list[MediaItemType | BrowseFolder] = []
        seen: set[str] = set()
        items = carousels[index].get("items") or carousels[index].get("contents") or []
        for position, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            video_id = _first_string(item, "videoId")
            if video_id:
                if video_id not in seen and (track := self._parse_track(item, position)):
                    seen.add(video_id); result.append(track)
                continue
            endpoint = item.get("endpoint") or {}
            browse_id = _first_string(endpoint, "browseId") if isinstance(endpoint, dict) else None
            page_type = _first_string(endpoint, "pageType") if isinstance(endpoint, dict) else ""
            playlist_id = _first_string(item, "playlistId")
            name = _first_string(item, "title", "name", "text")
            if not name:
                continue
            if browse_id and "ARTIST" in page_type:
                key = f"artist:{browse_id}"
                if key not in seen:
                    seen.add(key)
                    result.append(self._browse_folder(key, f"artist/{quote(browse_id, safe='')}", name, item))
                continue
            list_id = browse_id or playlist_id
            if list_id and list_id not in seen:
                seen.add(list_id)
                result.append(self._browse_folder(f"item-{list_id}", f"{prefix}/item/{quote(list_id, safe='')}", name, item))
        return result

    async def _browse_playlist_tracks(self, list_id: str) -> list[Track]:
        payload = await self._get_json("/api/v1/playlist.json", list=list_id)
        return self._tracks_from_items(payload.get("tracks") or [])

    def _tracks_from_items(self, items: Any) -> list[Track]:
        result: list[Track] = []
        seen: set[str] = set()
        if not isinstance(items, list):
            return result
        for position, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            video_id = _first_string(item, "videoId")
            if video_id and video_id not in seen and (track := self._parse_track(item, position)):
                seen.add(video_id); result.append(track)
        return result

    async def _browse_artist(self, artist_id: str) -> list[MediaItemType | BrowseFolder]:
        payload = await self._get_json(f"/api/v1/artist/{artist_id}")
        result: list[MediaItemType | BrowseFolder] = []
        songs = payload.get("songs") or {}
        if isinstance(songs, dict):
            result.extend(self._tracks_from_items(songs.get("contents") or songs.get("items") or []))
        result.extend(self._carousel_folders(payload, f"artist/{quote(artist_id, safe='')}"))
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        subpath = path.split("://", 1)[1].strip("/") if "://" in path else ""
        if not subpath:
            return [self._browse_folder("home", "home", "Home"), self._browse_folder("trending", "trending", "Trending"), self._browse_folder("explore", "explore", "Explore")]
        parts = subpath.split("/")
        root = parts[0]
        if root == "artist" and len(parts) >= 2:
            artist_id = unquote(parts[1])
            payload = await self._get_json(f"/api/v1/artist/{artist_id}")
            if len(parts) == 2:
                result: list[MediaItemType | BrowseFolder] = []
                songs = payload.get("songs") or {}
                if isinstance(songs, dict): result.extend(self._tracks_from_items(songs.get("contents") or songs.get("items") or []))
                result.extend(self._carousel_folders(payload, f"artist/{parts[1]}"))
                return result
            if len(parts) == 4 and parts[2] == "section":
                try: return self._carousel_items(payload, int(parts[3]), f"artist/{parts[1]}")
                except ValueError: return []
            if len(parts) == 4 and parts[2] == "item":
                return await self._browse_playlist_tracks(unquote(parts[3]))
            return []
        if root in ("home", "trending"):
            payload = await self._get_json("/api/v1/home.json" if root == "home" else "/api/v1/trending")
            if len(parts) == 1: return self._carousel_folders(payload, root)
            if len(parts) == 3 and parts[1] == "section":
                try: return self._carousel_items(payload, int(parts[2]), root)
                except ValueError: return []
            if len(parts) == 3 and parts[1] == "item": return await self._browse_playlist_tracks(unquote(parts[2]))
            return []
        if root == "explore":
            if len(parts) == 1:
                payload = await self._get_json_any("/api/v1/explore")
                if not isinstance(payload, list): return []
                result: list[BrowseFolder] = []
                n = 0
                for section in payload:
                    if not isinstance(section, dict): continue
                    for category in section.get("section") or []:
                        if not isinstance(category, dict): continue
                        name = _first_string(category, "text", "name"); endpoint = category.get("endpoint") or {}
                        params = _first_string(endpoint, "params") if isinstance(endpoint, dict) else None
                        if name and params:
                            token = quote(params, safe="")
                            result.append(self._browse_folder(f"explore-{n}", f"explore/category/{token}", name)); n += 1
                return result
            if len(parts) >= 3 and parts[1] == "category":
                token = parts[2]; category = unquote(token)
                payload = await self._get_json(f"/api/v1/explore/{quote(category, safe='')}")
                prefix = f"explore/category/{token}"
                if len(parts) == 3: return self._carousel_folders(payload, prefix)
                if len(parts) == 5 and parts[3] == "section":
                    try: return self._carousel_items(payload, int(parts[4]), prefix)
                    except ValueError: return []
                if len(parts) == 5 and parts[3] == "item": return await self._browse_playlist_tracks(unquote(parts[4]))
            return []
        return []

    def _provider_mapping(self, item_id: str, audio: bool = False) -> ProviderMapping:
        return ProviderMapping(item_id=item_id, provider_domain=self.domain, provider_instance=self.instance_id, available=True, audio_format=AudioFormat(content_type=ContentType.M4A) if audio else None)

    def _artist_mapping(self, name: str, artist_id: str | None) -> ItemMapping | None:
        return ItemMapping(media_type=MediaType.ARTIST, item_id=artist_id, provider=self.instance_id, name=name) if artist_id else None

    def _parse_track(self, data: dict[str, Any], position: int = 0) -> Track | None:
        video_id = _first_string(data, "videoId"); title = _first_string(data, "title", "name", "text")
        if not video_id or not title: return None
        artist_name, artist_id = _extract_artist(data); artists = UniqueList()
        if artist_name and (mapping := self._artist_mapping(artist_name, artist_id)): artists.append(mapping)
        track = Track(item_id=video_id, provider=self.instance_id, name=title, duration=_extract_duration(data), provider_mappings={self._provider_mapping(video_id, True)}, position=position or None)
        if artists: track.artists = artists
        self._add_images(track, data)
        return track

    async def get_artist(self, prov_artist_id: str) -> Artist:
        payload = await self._get_json(f"/api/v1/artist/{prov_artist_id}"); header = payload.get("header") or {}
        return Artist(item_id=prov_artist_id, provider=self.instance_id, name=_first_string(header, "name", "title") or prov_artist_id, provider_mappings={self._provider_mapping(prov_artist_id)})

    async def get_track(self, prov_track_id: str) -> Track:
        payload = await self._get_json("/api/v1/player.json", videoId=prov_track_id); details = payload.get("videoDetails") or {}
        title = str(details.get("title") or prov_track_id); duration = _as_int(details.get("lengthSeconds")); artist_mapping = None; source_item = None
        try:
            for candidate in await self._search_songs(title):
                if _first_string(candidate, "videoId") == prov_track_id:
                    source_item = candidate; artist_name, artist_id = _extract_artist(candidate)
                    artist_mapping = self._artist_mapping(artist_name, artist_id) if artist_name else None; break
        except Exception as err: self.logger.debug("Could not enrich Beatbump track %s: %s", prov_track_id, err)
        track = Track(item_id=prov_track_id, provider=self.instance_id, name=title, duration=duration, provider_mappings={self._provider_mapping(prov_track_id, True)})
        if artist_mapping: track.artists = UniqueList([artist_mapping])
        if source_item: self._add_images(track, source_item)
        return track

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        payload = await self._get_json("/api/v1/player.json", videoId=item_id); status = (payload.get("playabilityStatus") or {}).get("status")
        if status and status != "OK": raise ValueError(f"Beatbump reports {item_id} as not playable: {status}")
        formats = (payload.get("streamingData") or {}).get("adaptiveFormats") or []
        audio = [x for x in formats if isinstance(x, dict) and x.get("url") and "audio" in str(x.get("mimeType", "")).lower()]
        if not audio: raise ValueError(f"Beatbump returned no audio stream for {item_id}")
        aac = [x for x in audio if _as_int(x.get("itag")) == 140]; selected = aac[0] if aac else max(audio, key=lambda x: _as_int(x.get("bitrate")) or 0)
        url = str(selected["url"]); mime = str(selected.get("mimeType", "")).lower(); content_type = ContentType.M4A if "audio/mp4" in mime else ContentType.WEBM if "audio/webm" in mime else ContentType.UNKNOWN
        expiration = 3600; expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire:
            try: expiration = max(60, int(expire) - int(time.time()))
            except (TypeError, ValueError): pass
        duration_ms = _as_int(selected.get("approxDurationMs"))
        return StreamDetails(provider=self.instance_id, item_id=item_id, media_type=MediaType.TRACK, stream_type=StreamType.HTTP, path=url, audio_format=AudioFormat(content_type=content_type), duration=(duration_ms / 1000) if duration_ms else None, can_seek=True, allow_seek=True, expiration=expiration, is_realtime=True)

def _first_string(data: Any, *keys: str) -> str | None:
    if not isinstance(data, dict): return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value: return value
    return None

def _carousel_name(carousel: dict[str, Any]) -> str | None:
    header = carousel.get("header")
    if isinstance(header, str) and header: return header
    if isinstance(header, dict): return _first_string(header, "title", "text", "name")
    return _first_string(carousel, "title", "name")

def _extract_artist(data: dict[str, Any]) -> tuple[str, str | None]:
    info = data.get("artistInfo")
    if isinstance(info, dict):
        artists = info.get("artist")
        if isinstance(artists, list) and artists and isinstance(artists[0], dict):
            first = artists[0]; name = _first_string(first, "text", "name")
            if name: return name, _first_string(first, "browseId", "id")
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in subtitle:
            if isinstance(entry, dict) and "ARTIST" in str(entry.get("pageType", "")):
                name = _first_string(entry, "text", "name")
                if name: return name, _first_string(entry, "browseId", "id")
    return "", None

def _extract_duration(data: dict[str, Any]) -> int | None:
    for key in ("duration", "lengthSeconds"):
        value = _as_int(data.get(key))
        if value is not None: return value
    length = data.get("length")
    if isinstance(length, str) and (parsed := _parse_duration_text(length)) is not None: return parsed
    subtitle = data.get("subtitle")
    if isinstance(subtitle, list):
        for entry in reversed(subtitle):
            text = entry.get("text") if isinstance(entry, dict) else None
            if isinstance(text, str) and (parsed := _parse_duration_text(text)) is not None: return parsed
    return None

def _parse_duration_text(text: str) -> int | None:
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(x.isdigit() for x in parts): return None
    seconds = 0
    for part in parts: seconds = seconds * 60 + int(part)
    return seconds

def _as_int(value: Any) -> int | None:
    try: return int(value)
    except (TypeError, ValueError): return None
