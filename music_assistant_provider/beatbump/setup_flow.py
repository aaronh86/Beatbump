from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

CONF_BASE_URL = "base_url"
DEFAULT_BASE_URL = "http://beatbump:8080"


async def run_setup(session: SetupSession) -> None:
    """Collect the URL of the Beatbump server."""
    prefill = (
        session.context.setup_data.get(CONF_BASE_URL)
        or session.context.values.get(CONF_BASE_URL)
        or DEFAULT_BASE_URL
    )
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=DEFAULT_BASE_URL,
                value=str(prefill),
            )
        ],
        step_id="user",
        last_step=True,
    )
    await session.finish({CONF_BASE_URL: str(values[CONF_BASE_URL]).rstrip("/")})
