# -*- coding: utf-8 -*-
# Copyright 2014 - 2016 OpenMarket Ltd
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
import itertools
import logging
import operator

from twisted.internet import defer

from synapse.api.constants import EventTypes, Membership
from synapse.events.utils import prune_event
from synapse.util.logcontext import make_deferred_yieldable, preserve_fn

logger = logging.getLogger(__name__)


VISIBILITY_PRIORITY = (
    "world_readable",
    "shared",
    "invited",
    "joined",
)


MEMBERSHIP_PRIORITY = (
    Membership.JOIN,
    Membership.INVITE,
    Membership.KNOCK,
    Membership.LEAVE,
    Membership.BAN,
)


@defer.inlineCallbacks
def filter_events_for_client(store, user_id, events, is_peeking=False,
                             always_include_ids=frozenset()):
    """
    Check which events a user is allowed to see

    Args:
        store (synapse.storage.DataStore): our datastore (can also be a worker
            store)
        user_id(str): user id to be checked
        events(list[synapse.events.EventBase]): sequence of events to be checked
        is_peeking(bool): should be True if:
          * the user is not currently a member of the room, and:
          * the user has not been a member of the room since the given
            events
        always_include_ids (set(event_id)): set of event ids to specifically
            include (unless sender is ignored)

    Returns:
        Deferred[list[synapse.events.EventBase]]
    """
    types = (
        (EventTypes.RoomHistoryVisibility, ""),
        (EventTypes.Member, user_id),
    )
    event_id_to_state = yield store.get_state_for_events(
        frozenset(e.event_id for e in events),
        types=types,
    )

    forgotten = yield make_deferred_yieldable(defer.gatherResults([
        defer.maybeDeferred(
            preserve_fn(store.who_forgot_in_room),
            room_id,
        )
        for room_id in frozenset(e.room_id for e in events)
    ], consumeErrors=True))

    # Set of membership event_ids that have been forgotten
    event_id_forgotten = frozenset(
        row["event_id"] for rows in forgotten for row in rows
    )

    ignore_dict_content = yield store.get_global_account_data_by_type_for_user(
        "m.ignored_user_list", user_id,
    )

    # FIXME: This will explode if people upload something incorrect.
    ignore_list = frozenset(
        ignore_dict_content.get("ignored_users", {}).keys()
        if ignore_dict_content else []
    )

    erased_senders = yield store.are_users_erased((e.sender for e in events))

    def allowed(event):
        """
        Args:
            event (synapse.events.EventBase): event to check

        Returns:
            None|EventBase:
               None if the user cannot see this event at all

               a redacted copy of the event if they can only see a redacted
               version

               the original event if they can see it as normal.
        """
        if not event.is_state() and event.sender in ignore_list:
            return None

        if event.event_id in always_include_ids:
            return event

        state = event_id_to_state[event.event_id]

        # get the room_visibility at the time of the event.
        visibility_event = state.get((EventTypes.RoomHistoryVisibility, ""), None)
        if visibility_event:
            visibility = visibility_event.content.get("history_visibility", "shared")
        else:
            visibility = "shared"

        if visibility not in VISIBILITY_PRIORITY:
            visibility = "shared"

        # Always allow history visibility events on boundaries. This is done
        # by setting the effective visibility to the least restrictive
        # of the old vs new.
        if event.type == EventTypes.RoomHistoryVisibility:
            prev_content = event.unsigned.get("prev_content", {})
            prev_visibility = prev_content.get("history_visibility", None)

            if prev_visibility not in VISIBILITY_PRIORITY:
                prev_visibility = "shared"

            new_priority = VISIBILITY_PRIORITY.index(visibility)
            old_priority = VISIBILITY_PRIORITY.index(prev_visibility)
            if old_priority < new_priority:
                visibility = prev_visibility

        # likewise, if the event is the user's own membership event, use
        # the 'most joined' membership
        membership = None
        if event.type == EventTypes.Member and event.state_key == user_id:
            membership = event.content.get("membership", None)
            if membership not in MEMBERSHIP_PRIORITY:
                membership = "leave"

            prev_content = event.unsigned.get("prev_content", {})
            prev_membership = prev_content.get("membership", None)
            if prev_membership not in MEMBERSHIP_PRIORITY:
                prev_membership = "leave"

            # Always allow the user to see their own leave events, otherwise
            # they won't see the room disappear if they reject the invite
            if membership == "leave" and (
                prev_membership == "join" or prev_membership == "invite"
            ):
                return event

            new_priority = MEMBERSHIP_PRIORITY.index(membership)
            old_priority = MEMBERSHIP_PRIORITY.index(prev_membership)
            if old_priority < new_priority:
                membership = prev_membership

        # otherwise, get the user's membership at the time of the event.
        if membership is None:
            membership_event = state.get((EventTypes.Member, user_id), None)
            if membership_event:
                # XXX why do we do this?
                # https://github.com/matrix-org/synapse/issues/3350
                if membership_event.event_id not in event_id_forgotten:
                    membership = membership_event.membership

        # if the user was a member of the room at the time of the event,
        # they can see it.
        if membership == Membership.JOIN:
            return event

        # otherwise, it depends on the room visibility.

        if visibility == "joined":
            # we weren't a member at the time of the event, so we can't
            # see this event.
            return None

        elif visibility == "invited":
            # user can also see the event if they were *invited* at the time
            # of the event.
            return (
                event if membership == Membership.INVITE else None
            )

        elif visibility == "shared" and is_peeking:
            # if the visibility is shared, users cannot see the event unless
            # they have *subequently* joined the room (or were members at the
            # time, of course)
            #
            # XXX: if the user has subsequently joined and then left again,
            # ideally we would share history up to the point they left. But
            # we don't know when they left. We just treat it as though they
            # never joined, and restrict access.
            return None

        # the visibility is either shared or world_readable, and the user was
        # not a member at the time. We allow it, provided the original sender
        # has not requested their data to be erased, in which case, we return
        # a redacted version.
        if erased_senders[event.sender]:
            return prune_event(event)

        return event

    # check each event: gives an iterable[None|EventBase]
    filtered_events = itertools.imap(allowed, events)

    # remove the None entries
    filtered_events = filter(operator.truth, filtered_events)

    # we turn it into a list before returning it.
    defer.returnValue(list(filtered_events))
