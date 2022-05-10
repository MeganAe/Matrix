# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import Any, Dict, List
from urllib import parse as urlparse

import yaml
from netaddr import IPSet

from synapse.appservice import ApplicationService
from synapse.types import JsonDict, UserID

from ._base import Config, ConfigError

logger = logging.getLogger(__name__)


class AppServiceConfig(Config):
    section = "appservice"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.app_service_config_files = config.get("app_service_config_files", [])
        self.track_appservice_user_ips = config.get("track_appservice_user_ips", False)
        self.appservice_max_backoff = config.get("appservice_max_backoff", None)

    def generate_config_section(cls, **kwargs: Any) -> str:
        return """\
        # A list of application service config files to use
        #
        #app_service_config_files:
        #  - app_service_1.yaml
        #  - app_service_2.yaml

        # Uncomment to enable tracking of application service IP addresses. Implicitly
        # enables MAU tracking for application service users.
        #
        #track_appservice_user_ips: true

        # Set to establish maximum backoff (in seconds) between HS -> AS connection attempts.
        # Upon failing to push appservice events the homeserver will wait
        # an increasing amount of seconds between retries. This sets an upper limit of that (in seconds)
        #
        #appservice_max_backoff: 60
        """


def load_appservices(
    hostname: str, config_files: List[str]
) -> List[ApplicationService]:
    """Returns a list of Application Services from the config files."""
    if not isinstance(config_files, list):
        # type-ignore: this function gets arbitrary json value; we do use this path.
        logger.warning("Expected %s to be a list of AS config files.", config_files)  # type: ignore[unreachable]
        return []

    # Dicts of value -> filename
    seen_as_tokens: Dict[str, str] = {}
    seen_ids: Dict[str, str] = {}

    appservices = []

    for config_file in config_files:
        try:
            with open(config_file) as f:
                appservice = _load_appservice(hostname, yaml.safe_load(f), config_file)
                if appservice.id in seen_ids:
                    raise ConfigError(
                        "Cannot reuse ID across application services: "
                        "%s (files: %s, %s)"
                        % (appservice.id, config_file, seen_ids[appservice.id])
                    )
                seen_ids[appservice.id] = config_file
                if appservice.token in seen_as_tokens:
                    raise ConfigError(
                        "Cannot reuse as_token across application services: "
                        "%s (files: %s, %s)"
                        % (
                            appservice.token,
                            config_file,
                            seen_as_tokens[appservice.token],
                        )
                    )
                seen_as_tokens[appservice.token] = config_file
                logger.info("Loaded application service: %s", appservice)
                appservices.append(appservice)
        except Exception as e:
            logger.error("Failed to load appservice from '%s'", config_file)
            logger.exception(e)
            raise
    return appservices


def _load_appservice(
    hostname: str, as_info: JsonDict, config_filename: str
) -> ApplicationService:
    required_string_fields = ["id", "as_token", "hs_token", "sender_localpart"]
    for field in required_string_fields:
        if not isinstance(as_info.get(field), str):
            raise KeyError(
                "Required string field: '%s' (%s)" % (field, config_filename)
            )

    # 'url' must either be a string or explicitly null, not missing
    # to avoid accidentally turning off push for ASes.
    if not isinstance(as_info.get("url"), str) and as_info.get("url", "") is not None:
        raise KeyError(
            "Required string field or explicit null: 'url' (%s)" % (config_filename,)
        )

    localpart = as_info["sender_localpart"]
    if urlparse.quote(localpart) != localpart:
        raise ValueError("sender_localpart needs characters which are not URL encoded.")
    user = UserID(localpart, hostname)
    user_id = user.to_string()

    # Rate limiting for users of this AS is on by default (excludes sender)
    rate_limited = as_info.get("rate_limited")
    if not isinstance(rate_limited, bool):
        rate_limited = True

    # namespace checks
    if not isinstance(as_info.get("namespaces"), dict):
        raise KeyError("Requires 'namespaces' object.")
    for ns in ApplicationService.NS_LIST:
        # specific namespaces are optional
        if ns in as_info["namespaces"]:
            # expect a list of dicts with exclusive and regex keys
            for regex_obj in as_info["namespaces"][ns]:
                if not isinstance(regex_obj, dict):
                    raise ValueError(
                        "Expected namespace entry in %s to be an object, but got %s",
                        ns,
                        regex_obj,
                    )
                if not isinstance(regex_obj.get("regex"), str):
                    raise ValueError("Missing/bad type 'regex' key in %s", regex_obj)
                if not isinstance(regex_obj.get("exclusive"), bool):
                    raise ValueError(
                        "Missing/bad type 'exclusive' key in %s", regex_obj
                    )
    # protocols check
    protocols = as_info.get("protocols")
    if protocols:
        if not isinstance(protocols, list):
            raise KeyError("Optional 'protocols' must be a list if present.")
        for p in protocols:
            if not isinstance(p, str):
                raise KeyError("Bad value for 'protocols' item")

    if as_info["url"] is None:
        logger.info(
            "(%s) Explicitly empty 'url' provided. This application service"
            " will not receive events or queries.",
            config_filename,
        )

    ip_range_whitelist = None
    if as_info.get("ip_range_whitelist"):
        ip_range_whitelist = IPSet(as_info.get("ip_range_whitelist"))

    supports_ephemeral = as_info.get("de.sorunome.msc2409.push_ephemeral", False)

    # Opt-in flag for the MSC3202-specific transactional behaviour.
    # When enabled, appservice transactions contain the following information:
    #  - device One-Time Key counts
    #  - device unused fallback key usage states
    #  - device list changes
    msc3202_transaction_extensions = as_info.get("org.matrix.msc3202", False)
    if not isinstance(msc3202_transaction_extensions, bool):
        raise ValueError(
            "The `org.matrix.msc3202` option should be true or false if specified."
        )

    return ApplicationService(
        token=as_info["as_token"],
        hostname=hostname,
        url=as_info["url"],
        namespaces=as_info["namespaces"],
        hs_token=as_info["hs_token"],
        sender=user_id,
        id=as_info["id"],
        protocols=protocols,
        rate_limited=rate_limited,
        ip_range_whitelist=ip_range_whitelist,
        supports_ephemeral=supports_ephemeral,
        msc3202_transaction_extensions=msc3202_transaction_extensions,
    )
