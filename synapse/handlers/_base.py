# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

from twisted.internet import defer

from synapse.api.errors import LimitExceededError, SynapseError, AuthError
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.api.constants import Membership, EventTypes
from synapse.types import UserID, RoomAlias

from synapse.util.logcontext import PreserveLoggingContext
from synapse.util import third_party_invites

import logging


logger = logging.getLogger(__name__)


class BaseHandler(object):

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()
        self.notifier = hs.get_notifier()
        self.state_handler = hs.get_state_handler()
        self.distributor = hs.get_distributor()
        self.ratelimiter = hs.get_ratelimiter()
        self.clock = hs.get_clock()
        self.hs = hs

        self.signing_key = hs.config.signing_key[0]
        self.server_name = hs.hostname

        self.event_builder_factory = hs.get_event_builder_factory()

    @defer.inlineCallbacks
    def _filter_events_for_client(self, user_id, events, is_guest=False,
                                  require_all_visible_for_guests=True):
        exclude = 0
        include = 1
        include_if_later_joined = 2

        def allowed(event, membership, visibility):
            if visibility == "world_readable":
                return include

            if membership == Membership.JOIN:
                return include

            if event.type == EventTypes.RoomHistoryVisibility:
                return include_if_later_joined

            if visibility == "public":
                return include_if_later_joined
            elif visibility == "shared":
                return include_if_later_joined
            elif visibility == "joined":
                if membership == Membership.JOIN:
                    return include
                return exclude
            elif visibility == "invited":
                if membership == Membership.INVITE:
                    return include_if_later_joined
                return exclude

            return include_if_later_joined

        event_id_to_state = yield self.store.get_state_for_events(
            frozenset(e.event_id for e in events),
            types=(
                (EventTypes.RoomHistoryVisibility, ""),
                (EventTypes.Member, user_id),
            )
        )

        events_to_return = []
        events_to_return_if_later_joined = []
        for event in events:
            state = event_id_to_state[event.event_id]

            membership_event = state.get((EventTypes.Member, user_id), None)
            if membership_event:
                membership = membership_event.membership
            else:
                membership = None

            if membership == Membership.JOIN:
                events_to_return.extend(events_to_return_if_later_joined)
                events_to_return_if_later_joined = []

            visibility_event = state.get((EventTypes.RoomHistoryVisibility, ""), None)
            if visibility_event:
                visibility = visibility_event.content.get("history_visibility", "shared")
            else:
                visibility = "shared"

            should_include = allowed(event, membership, visibility)
            if should_include == include:
                events_to_return.append(event)
            elif should_include == include_if_later_joined:
                events_to_return_if_later_joined.append(event)

        if (require_all_visible_for_guests
                and is_guest
                and len(events_to_return) < len(events)):
            raise AuthError(403, "User %s does not have permission" % (
                user_id
            ))

        defer.returnValue(events_to_return)

    def ratelimit(self, user_id):
        time_now = self.clock.time()
        allowed, time_allowed = self.ratelimiter.send_message(
            user_id, time_now,
            msg_rate_hz=self.hs.config.rc_messages_per_second,
            burst_count=self.hs.config.rc_message_burst_count,
        )
        if not allowed:
            raise LimitExceededError(
                retry_after_ms=int(1000*(time_allowed - time_now)),
            )

    @defer.inlineCallbacks
    def _create_new_client_event(self, builder):
        latest_ret = yield self.store.get_latest_events_in_room(
            builder.room_id,
        )

        if latest_ret:
            depth = max([d for _, _, d in latest_ret]) + 1
        else:
            depth = 1

        prev_events = [(e, h) for e, h, _ in latest_ret]

        builder.prev_events = prev_events
        builder.depth = depth

        state_handler = self.state_handler

        context = yield state_handler.compute_event_context(builder)

        if builder.is_state():
            builder.prev_state = yield self.store.add_event_hashes(
                context.prev_state_events
            )

        yield self.auth.add_auth_events(builder, context)

        add_hashes_and_signatures(
            builder, self.server_name, self.signing_key
        )

        event = builder.build()

        logger.debug(
            "Created event %s with current state: %s",
            event.event_id, context.current_state,
        )

        defer.returnValue(
            (event, context,)
        )

    @defer.inlineCallbacks
    def handle_new_client_event(self, event, context, extra_destinations=[],
                                extra_users=[], suppress_auth=False):
        # We now need to go and hit out to wherever we need to hit out to.

        if not suppress_auth:
            self.auth.check(event, auth_events=context.current_state)

        if event.type == EventTypes.CanonicalAlias:
            # Check the alias is acually valid (at this time at least)
            room_alias_str = event.content.get("alias", None)
            if room_alias_str:
                room_alias = RoomAlias.from_string(room_alias_str)
                directory_handler = self.hs.get_handlers().directory_handler
                mapping = yield directory_handler.get_association(room_alias)

                if mapping["room_id"] != event.room_id:
                    raise SynapseError(
                        400,
                        "Room alias %s does not point to the room" % (
                            room_alias_str,
                        )
                    )

        if (
            event.type == EventTypes.Member and
            event.content["membership"] == Membership.JOIN and
            third_party_invites.join_has_third_party_invite(event.content)
        ):
            yield third_party_invites.check_key_valid(
                self.hs.get_simple_http_client(),
                event
            )

        federation_handler = self.hs.get_handlers().federation_handler

        if event.type == EventTypes.Member:
            if event.content["membership"] == Membership.INVITE:
                event.unsigned["invite_room_state"] = [
                    {
                        "type": e.type,
                        "state_key": e.state_key,
                        "content": e.content,
                        "sender": e.sender,
                    }
                    for k, e in context.current_state.items()
                    if e.type in (
                        EventTypes.JoinRules,
                        EventTypes.CanonicalAlias,
                        EventTypes.RoomAvatar,
                        EventTypes.Name,
                    )
                ]

                invitee = UserID.from_string(event.state_key)
                if not self.hs.is_mine(invitee):
                    # TODO: Can we add signature from remote server in a nicer
                    # way? If we have been invited by a remote server, we need
                    # to get them to sign the event.

                    returned_invite = yield federation_handler.send_invite(
                        invitee.domain,
                        event,
                    )

                    event.unsigned.pop("room_state", None)

                    # TODO: Make sure the signatures actually are correct.
                    event.signatures.update(
                        returned_invite.signatures
                    )

        if event.type == EventTypes.Redaction:
            if self.auth.check_redaction(event, auth_events=context.current_state):
                original_event = yield self.store.get_event(
                    event.redacts,
                    check_redacted=False,
                    get_prev_content=False,
                    allow_rejected=False,
                    allow_none=False
                )
                if event.user_id != original_event.user_id:
                    raise AuthError(
                        403,
                        "You don't have permission to redact events"
                    )

        (event_stream_id, max_stream_id) = yield self.store.persist_event(
            event, context=context
        )

        destinations = set(extra_destinations)
        for k, s in context.current_state.items():
            try:
                if k[0] == EventTypes.Member:
                    if s.content["membership"] == Membership.JOIN:
                        destinations.add(
                            UserID.from_string(s.state_key).domain
                        )
            except SynapseError:
                logger.warn(
                    "Failed to get destination from event %s", s.event_id
                )

        with PreserveLoggingContext():
            # Don't block waiting on waking up all the listeners.
            notify_d = self.notifier.on_new_room_event(
                event, event_stream_id, max_stream_id,
                extra_users=extra_users
            )

        def log_failure(f):
            logger.warn(
                "Failed to notify about %s: %s",
                event.event_id, f.value
            )

        notify_d.addErrback(log_failure)

        # If invite, remove room_state from unsigned before sending.
        event.unsigned.pop("invite_room_state", None)

        federation_handler.handle_new_event(
            event, destinations=destinations,
        )
