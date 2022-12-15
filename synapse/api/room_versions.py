# Copyright 2019 New Vector Ltd
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

from typing import Callable, Dict, List, Optional

import attr


class EventFormatVersions:
    """This is an internal enum for tracking the version of the event format,
    independently of the room version.

    To reduce confusion, the event format versions are named after the room
    versions that they were used or introduced in.
    The concept of an 'event format version' is specific to Synapse (the
    specification does not mention this term.)
    """

    ROOM_V1_V2 = 1  # $id:server event id format: used for room v1 and v2
    ROOM_V3 = 2  # MSC1659-style $hash event id format: used for room v3
    ROOM_V4_PLUS = 3  # MSC1884-style $hash format: introduced for room v4


KNOWN_EVENT_FORMAT_VERSIONS = {
    EventFormatVersions.ROOM_V1_V2,
    EventFormatVersions.ROOM_V3,
    EventFormatVersions.ROOM_V4_PLUS,
}


class StateResolutionVersions:
    """Enum to identify the state resolution algorithms"""

    V1 = 1  # room v1 state res
    V2 = 2  # MSC1442 state res: room v2 and later


class RoomDisposition:
    STABLE = "stable"
    UNSTABLE = "unstable"


class PushRuleRoomFlag:
    """Enum for listing possible MSC3931 room version feature flags, for push rules"""

    # MSC3932: Room version supports MSC1767 Extensible Events.
    EXTENSIBLE_EVENTS = "org.matrix.msc3932.extensible_events"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomVersion:
    """An object which describes the unique attributes of a room version."""

    identifier: str  # the identifier for this version
    disposition: str  # one of the RoomDispositions
    event_format: int  # one of the EventFormatVersions
    state_res: int  # one of the StateResolutionVersions
    enforce_key_validity: bool

    # Before MSC2432, m.room.aliases had special auth rules and redaction rules
    special_case_aliases_auth: bool
    # Strictly enforce canonicaljson, do not allow:
    # * Integers outside the range of [-2 ^ 53 + 1, 2 ^ 53 - 1]
    # * Floats
    # * NaN, Infinity, -Infinity
    strict_canonicaljson: bool
    # MSC2209: Check 'notifications' key while verifying
    # m.room.power_levels auth rules.
    limit_notifications_power_levels: bool
    # MSC2174/MSC2176: Apply updated redaction rules algorithm.
    msc2176_redaction_rules: bool
    # MSC3083: Support the 'restricted' join_rule.
    msc3083_join_rules: bool
    # MSC3375: Support for the proper redaction rules for MSC3083. This mustn't
    #          be enabled if MSC3083 is not.
    msc3375_redaction_rules: bool
    # MSC2403: Allows join_rules to be set to 'knock', changes auth rules to allow sending
    # m.room.membership event with membership 'knock'.
    msc2403_knocking: bool
    # MSC2716: Adds m.room.power_levels -> content.historical field to control
    # whether "insertion", "chunk", "marker" events can be sent
    msc2716_historical: bool
    # MSC2716: Adds support for redacting "insertion", "chunk", and "marker" events
    msc2716_redactions: bool
    # MSC3787: Adds support for a `knock_restricted` join rule, mixing concepts of
    # knocks and restricted join rules into the same join condition.
    msc3787_knock_restricted_join_rule: bool
    # MSC3667: Enforce integer power levels
    msc3667_int_only_power_levels: bool
    # MSC3931: Adds a push rule condition for "room version feature flags", making
    # some push rules room version dependent. Note that adding a flag to this list
    # is not enough to mark it "supported": the push rule evaluator also needs to
    # support the flag. Unknown flags are ignored by the evaluator, making conditions
    # fail if used.
    msc3931_push_features: List[str]  # values from PushRuleRoomFlag


class RoomVersions:
    V1 = RoomVersion(
        "1",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V1_V2,
        StateResolutionVersions.V1,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V2 = RoomVersion(
        "2",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V1_V2,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V3 = RoomVersion(
        "3",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V3,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V4 = RoomVersion(
        "4",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=False,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V5 = RoomVersion(
        "5",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=True,
        strict_canonicaljson=False,
        limit_notifications_power_levels=False,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V6 = RoomVersion(
        "6",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    MSC2176 = RoomVersion(
        "org.matrix.msc2176",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=True,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=False,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V7 = RoomVersion(
        "7",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V8 = RoomVersion(
        "8",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=True,
        msc3375_redaction_rules=False,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V9 = RoomVersion(
        "9",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=True,
        msc3375_redaction_rules=True,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    MSC3787 = RoomVersion(
        "org.matrix.msc3787",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=True,
        msc3375_redaction_rules=True,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=True,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    V10 = RoomVersion(
        "10",
        RoomDisposition.STABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=True,
        msc3375_redaction_rules=True,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=True,
        msc3667_int_only_power_levels=True,
        msc3931_push_features=[],
    )
    MSC2716v4 = RoomVersion(
        "org.matrix.msc2716v4",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=False,
        msc3375_redaction_rules=False,
        msc2403_knocking=True,
        msc2716_historical=True,
        msc2716_redactions=True,
        msc3787_knock_restricted_join_rule=False,
        msc3667_int_only_power_levels=False,
        msc3931_push_features=[],
    )
    MSC1767v10 = RoomVersion(
        # MSC1767 (Extensible Events) based on room version "10"
        "org.matrix.msc1767.10",
        RoomDisposition.UNSTABLE,
        EventFormatVersions.ROOM_V4_PLUS,
        StateResolutionVersions.V2,
        enforce_key_validity=True,
        special_case_aliases_auth=False,
        strict_canonicaljson=True,
        limit_notifications_power_levels=True,
        msc2176_redaction_rules=False,
        msc3083_join_rules=True,
        msc3375_redaction_rules=True,
        msc2403_knocking=True,
        msc2716_historical=False,
        msc2716_redactions=False,
        msc3787_knock_restricted_join_rule=True,
        msc3667_int_only_power_levels=True,
        msc3931_push_features=[PushRuleRoomFlag.EXTENSIBLE_EVENTS],
    )


KNOWN_ROOM_VERSIONS: Dict[str, RoomVersion] = {
    v.identifier: v
    for v in (
        RoomVersions.V1,
        RoomVersions.V2,
        RoomVersions.V3,
        RoomVersions.V4,
        RoomVersions.V5,
        RoomVersions.V6,
        RoomVersions.MSC2176,
        RoomVersions.V7,
        RoomVersions.V8,
        RoomVersions.V9,
        RoomVersions.MSC3787,
        RoomVersions.V10,
        RoomVersions.MSC2716v4,
    )
}


@attr.s(slots=True, frozen=True, auto_attribs=True)
class RoomVersionCapability:
    """An object which describes the unique attributes of a room version."""

    identifier: str  # the identifier for this capability
    preferred_version: Optional[RoomVersion]
    support_check_lambda: Callable[[RoomVersion], bool]


MSC3244_CAPABILITIES = {
    cap.identifier: {
        "preferred": cap.preferred_version.identifier
        if cap.preferred_version is not None
        else None,
        "support": [
            v.identifier
            for v in KNOWN_ROOM_VERSIONS.values()
            if cap.support_check_lambda(v)
        ],
    }
    for cap in (
        RoomVersionCapability(
            "knock",
            RoomVersions.V7,
            lambda room_version: room_version.msc2403_knocking,
        ),
        RoomVersionCapability(
            "restricted",
            RoomVersions.V9,
            lambda room_version: room_version.msc3083_join_rules,
        ),
    )
}
