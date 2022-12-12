# Copyright 2015-2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Any, Iterable, Optional, Tuple

from synapse.api.constants import EventTypes
from synapse.config._base import Config, ConfigError
from synapse.config._util import validate_config
from synapse.types import JsonDict
from synapse.types.state import StateFilter

logger = logging.getLogger(__name__)


class ApiConfig(Config):
    section = "api"

    room_prejoin_state: StateFilter
    track_puppetted_users_ips: bool

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        validate_config(_MAIN_SCHEMA, config, ())
        self.room_prejoin_state = StateFilter.from_types(
            self._get_prejoin_state_entries(config)
        )
        self.track_puppeted_user_ips = config.get("track_puppeted_user_ips", False)

    def _get_prejoin_state_entries(
        self, config: JsonDict
    ) -> Iterable[Tuple[str, Optional[str]]]:
        """Get the event types and state keys to include in the prejoin state."""
        room_prejoin_state_config = config.get("room_prejoin_state") or {}

        # backwards-compatibility support for room_invite_state_types
        if "room_invite_state_types" in config:
            # if both "room_invite_state_types" and "room_prejoin_state" are set, then
            # we don't really know what to do.
            if room_prejoin_state_config:
                raise ConfigError(
                    "Can't specify both 'room_invite_state_types' and 'room_prejoin_state' "
                    "in config"
                )

            logger.warning(_ROOM_INVITE_STATE_TYPES_WARNING)

            for event_type in config["room_invite_state_types"]:
                yield event_type, None
            return

        if not room_prejoin_state_config.get("disable_default_event_types"):
            yield from _DEFAULT_PREJOIN_STATE_TYPES_AND_STATE_KEYS

        for entry in room_prejoin_state_config.get("additional_event_types", []):
            if isinstance(entry, str):
                yield entry, None
            else:
                yield entry


_ROOM_INVITE_STATE_TYPES_WARNING = """\
WARNING: The 'room_invite_state_types' configuration setting is now deprecated,
and replaced with 'room_prejoin_state'. New features may not work correctly
unless 'room_invite_state_types' is removed. See the config documentation at
    https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html#room_prejoin_state
for details of 'room_prejoin_state'.
--------------------------------------------------------------------------------
"""

_DEFAULT_PREJOIN_STATE_TYPES_AND_STATE_KEYS = [
    (EventTypes.JoinRules, ""),
    (EventTypes.CanonicalAlias, ""),
    (EventTypes.RoomAvatar, ""),
    (EventTypes.RoomEncryption, ""),
    (EventTypes.Name, ""),
    # Per MSC1772.
    (EventTypes.Create, ""),
    # Per MSC3173.
    (EventTypes.Topic, ""),
]


# room_prejoin_state can either be None (as it is in the default config), or
# an object containing other config settings
_ROOM_PREJOIN_STATE_CONFIG_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "disable_default_event_types": {"type": "boolean"},
                "additional_event_types": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                        ],
                    },
                },
            },
        },
        {"type": "null"},
    ]
}

# the legacy room_invite_state_types setting
_ROOM_INVITE_STATE_TYPES_SCHEMA = {"type": "array", "items": {"type": "string"}}

_MAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "room_prejoin_state": _ROOM_PREJOIN_STATE_CONFIG_SCHEMA,
        "room_invite_state_types": _ROOM_INVITE_STATE_TYPES_SCHEMA,
        "track_puppeted_user_ips": {
            "type": "boolean",
        },
    },
}
