# Beatbump provider for Music Assistant

Experimental Music Assistant music provider backed by this Beatbump server.

## Current MVP

- Search Beatbump's YouTube Music catalogue for tracks.
- Return tracks to Music Assistant.
- Resolve a selected track through Beatbump `/api/v1/player.json`.
- Hand Music Assistant the highest-bitrate audio-only HTTP stream returned by Beatbump.

## Install for development

Copy the `beatbump` directory into the Music Assistant server source tree as:

`music_assistant/providers/beatbump/`

Restart Music Assistant, then add **Beatbump** under **Settings -> Music Sources**. Enter the URL that the Music Assistant server/container can use to reach Beatbump, for example:

`http://192.168.1.50:8080`

or, if both containers share a Docker network:

`http://beatbump:8080`

## Notes

This is intentionally a small first implementation. It currently exposes tracks only. Albums, artists, playlists, browse, recommendations and library/favourites can be layered on after search/playback is proven against the live Beatbump and Music Assistant instances.

The player endpoint returns signed/temporary upstream stream URLs. Music Assistant therefore resolves the URL at playback time rather than storing it from search results.
