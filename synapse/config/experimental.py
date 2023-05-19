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

from typing import Any, Optional

import attr

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config import ConfigError
from synapse.config._base import Config
from synapse.types import JsonDict


@attr.s(auto_attribs=True, frozen=True, slots=True)
class MSC3866Config:
    """Configuration for MSC3866 (mandating approval for new users)"""

    # Whether the base support for the approval process is enabled. This includes the
    # ability for administrators to check and update the approval of users, even if no
    # approval is currently required.
    enabled: bool = False
    # Whether to require that new users are approved by an admin before their account
    # can be used. Note that this setting is ignored if 'enabled' is false.
    require_approval_for_new_accounts: bool = False


class ExperimentalConfig(Config):
    """Config section for enabling experimental features"""

    section = "experimental"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        experimental = config.get("experimental_features") or {}

        # MSC3026 (busy presence state)
        self.msc3026_enabled: bool = experimental.get("msc3026_enabled", False)

        # MSC2716 (importing historical messages)
        self.msc2716_enabled: bool = experimental.get("msc2716_enabled", False)

        # MSC3244 (room version capabilities)
        self.msc3244_enabled: bool = experimental.get("msc3244_enabled", True)

        # MSC3266 (room summary api)
        self.msc3266_enabled: bool = experimental.get("msc3266_enabled", False)

        # MSC2409 (this setting only relates to optionally sending to-device messages).
        # Presence, typing and read receipt EDUs are already sent to application services that
        # have opted in to receive them. If enabled, this adds to-device messages to that list.
        self.msc2409_to_device_messages_enabled: bool = experimental.get(
            "msc2409_to_device_messages_enabled", False
        )

        # The portion of MSC3202 which is related to device masquerading.
        self.msc3202_device_masquerading_enabled: bool = experimental.get(
            "msc3202_device_masquerading", False
        )

        # The portion of MSC3202 related to transaction extensions:
        # sending device list changes, one-time key counts and fallback key
        # usage to application services.
        self.msc3202_transaction_extensions: bool = experimental.get(
            "msc3202_transaction_extensions", False
        )

        # MSC3983: Proxying OTK claim requests to exclusive ASes.
        self.msc3983_appservice_otk_claims: bool = experimental.get(
            "msc3983_appservice_otk_claims", False
        )

        # MSC3984: Proxying key queries to exclusive ASes.
        self.msc3984_appservice_key_query: bool = experimental.get(
            "msc3984_appservice_key_query", False
        )

        # MSC3720 (Account status endpoint)
        self.msc3720_enabled: bool = experimental.get("msc3720_enabled", False)

        # MSC2654: Unread counts
        #
        # Note that enabling this will result in an incorrect unread count for
        # previously calculated push actions.
        self.msc2654_enabled: bool = experimental.get("msc2654_enabled", False)

        # MSC2815 (allow room moderators to view redacted event content)
        self.msc2815_enabled: bool = experimental.get("msc2815_enabled", False)

        # MSC3391: Removing account data.
        self.msc3391_enabled = experimental.get("msc3391_enabled", False)

        # MSC3773: Thread notifications
        self.msc3773_enabled: bool = experimental.get("msc3773_enabled", False)

        # MSC3664: Pushrules to match on related events
        self.msc3664_enabled: bool = experimental.get("msc3664_enabled", False)

        # MSC3848: Introduce errcodes for specific event sending failures
        self.msc3848_enabled: bool = experimental.get("msc3848_enabled", False)

        # MSC3852: Expose last seen user agent field on /_matrix/client/v3/devices.
        self.msc3852_enabled: bool = experimental.get("msc3852_enabled", False)

        # MSC3866: M_USER_AWAITING_APPROVAL error code
        raw_msc3866_config = experimental.get("msc3866", {})
        self.msc3866 = MSC3866Config(**raw_msc3866_config)

        # MSC3881: Remotely toggle push notifications for another client
        self.msc3881_enabled: bool = experimental.get("msc3881_enabled", False)

        # MSC3882: Allow an existing session to sign in a new session
        self.msc3882_enabled: bool = experimental.get("msc3882_enabled", False)
        self.msc3882_ui_auth: bool = experimental.get("msc3882_ui_auth", True)
        self.msc3882_token_timeout = self.parse_duration(
            experimental.get("msc3882_token_timeout", "5m")
        )

        # MSC3874: Filtering /messages with rel_types / not_rel_types.
        self.msc3874_enabled: bool = experimental.get("msc3874_enabled", False)

        # MSC3886: Simple client rendezvous capability
        self.msc3886_endpoint: Optional[str] = experimental.get(
            "msc3886_endpoint", None
        )

        # MSC3890: Remotely silence local notifications
        # Note: This option requires "experimental_features.msc3391_enabled" to be
        # set to "true", in order to communicate account data deletions to clients.
        self.msc3890_enabled: bool = experimental.get("msc3890_enabled", False)
        if self.msc3890_enabled and not self.msc3391_enabled:
            raise ConfigError(
                "Option 'experimental_features.msc3391' must be set to 'true' to "
                "enable 'experimental_features.msc3890'. MSC3391 functionality is "
                "required to communicate account data deletions to clients."
            )

        # MSC3381: Polls.
        # In practice, supporting polls in Synapse only requires an implementation of
        # MSC3930: Push rules for MSC3391 polls; which is what this option enables.
        self.msc3381_polls_enabled: bool = experimental.get(
            "msc3381_polls_enabled", False
        )

        # MSC3912: Relation-based redactions.
        self.msc3912_enabled: bool = experimental.get("msc3912_enabled", False)

        # MSC1767 and friends: Extensible Events
        self.msc1767_enabled: bool = experimental.get("msc1767_enabled", False)
        if self.msc1767_enabled:
            # Enable room version (and thus applicable push rules from MSC3931/3932)
            version_id = RoomVersions.MSC1767v10.identifier
            KNOWN_ROOM_VERSIONS[version_id] = RoomVersions.MSC1767v10

        # MSC3391: Removing account data.
        self.msc3391_enabled = experimental.get("msc3391_enabled", False)

        # MSC3952: Intentional mentions, this depends on MSC3966.
        self.msc3952_intentional_mentions = experimental.get(
            "msc3952_intentional_mentions", False
        )

        # MSC3959: Do not generate notifications for edits.
        self.msc3958_supress_edit_notifs = experimental.get(
            "msc3958_supress_edit_notifs", False
        )

        # MSC3967: Do not require UIA when first uploading cross signing keys
        self.msc3967_enabled = experimental.get("msc3967_enabled", False)

        # MSC3981: Recurse relations
        self.msc3981_recurse_relations = experimental.get(
            "msc3981_recurse_relations", False
        )

        # MSC3970: Scope transaction IDs to devices
        self.msc3970_enabled = experimental.get("msc3970_enabled", False)

        # MSC4009: E.164 Matrix IDs
        self.msc4009_e164_mxids = experimental.get("msc4009_e164_mxids", False)

        # MSC4010: Do not allow setting m.push_rules account data.
        self.msc4010_push_rules_account_data = experimental.get(
            "msc4010_push_rules_account_data", False
        )
