# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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

from synapse.api.constants import Membership
from synapse.types import RoomStreamToken
from synapse.visibility import filter_events_for_client

from ._base import BaseHandler

logger = logging.getLogger(__name__)


class AdminHandler(BaseHandler):
    def __init__(self, hs):
        super(AdminHandler, self).__init__(hs)

        self.storage = hs.get_storage()
        self.state_store = self.storage.state

    async def get_whois(self, user):
        connections = []

        sessions = await self.store.get_user_ip_and_agents(user)
        for session in sessions:
            connections.append(
                {
                    "ip": session["ip"],
                    "last_seen": session["last_seen"],
                    "user_agent": session["user_agent"],
                }
            )

        ret = {
            "user_id": user.to_string(),
            "devices": {"": {"sessions": [{"connections": connections}]}},
        }

        return ret

    async def get_users(self):
        """Function to retrieve a list of users in users table.

        Args:
        Returns:
            defer.Deferred: resolves to list[dict[str, Any]]
        """
        ret = await self.store.get_users()

        return ret

    async def get_users_paginate(self, start, limit, name, guests, deactivated):
        """Function to retrieve a paginated list of users from
        users list. This will return a json list of users.

        Args:
            start (int): start number to begin the query from
            limit (int): number of rows to retrieve
            name (string): filter for user names
            guests (bool): whether to in include guest users
            deactivated (bool): whether to include deactivated users
        Returns:
            defer.Deferred: resolves to json list[dict[str, Any]]
        """
        ret = await self.store.get_users_paginate(
            start, limit, name, guests, deactivated
        )

        return ret

    async def search_users(self, term):
        """Function to search users list for one or more users with
        the matched term.

        Args:
            term (str): search term
        Returns:
            defer.Deferred: resolves to list[dict[str, Any]]
        """
        ret = await self.store.search_users(term)

        return ret

    def get_user_server_admin(self, user):
        """
        Get the admin bit on a user.

        Args:
            user_id (UserID): the (necessarily local) user to manipulate
        """
        return self.store.is_server_admin(user)

    def set_user_server_admin(self, user, admin):
        """
        Set the admin bit on a user.

        Args:
            user_id (UserID): the (necessarily local) user to manipulate
            admin (bool): whether or not the user should be an admin of this server
        """
        return self.store.set_server_admin(user, admin)

    async def export_user_data(self, user_id, writer):
        """Write all data we have on the user to the given writer.

        Args:
            user_id (str)
            writer (ExfiltrationWriter)

        Returns:
            defer.Deferred: Resolves when all data for a user has been written.
            The returned value is that returned by `writer.finished()`.
        """
        # Get all rooms the user is in or has been in
        rooms = await self.store.get_rooms_for_user_where_membership_is(
            user_id,
            membership_list=(
                Membership.JOIN,
                Membership.LEAVE,
                Membership.BAN,
                Membership.INVITE,
            ),
        )

        # We only try and fetch events for rooms the user has been in. If
        # they've been e.g. invited to a room without joining then we handle
        # those seperately.
        rooms_user_has_been_in = await self.store.get_rooms_user_has_been_in(user_id)

        for index, room in enumerate(rooms):
            room_id = room.room_id

            logger.info(
                "[%s] Handling room %s, %d/%d", user_id, room_id, index + 1, len(rooms)
            )

            forgotten = await self.store.did_forget(user_id, room_id)
            if forgotten:
                logger.info("[%s] User forgot room %d, ignoring", user_id, room_id)
                continue

            if room_id not in rooms_user_has_been_in:
                # If we haven't been in the rooms then the filtering code below
                # won't return anything, so we need to handle these cases
                # explicitly.

                if room.membership == Membership.INVITE:
                    event_id = room.event_id
                    invite = await self.store.get_event(event_id, allow_none=True)
                    if invite:
                        invited_state = invite.unsigned["invite_room_state"]
                        writer.write_invite(room_id, invite, invited_state)

                continue

            # We only want to bother fetching events up to the last time they
            # were joined. We estimate that point by looking at the
            # stream_ordering of the last membership if it wasn't a join.
            if room.membership == Membership.JOIN:
                stream_ordering = self.store.get_room_max_stream_ordering()
            else:
                stream_ordering = room.stream_ordering

            from_key = str(RoomStreamToken(0, 0))
            to_key = str(RoomStreamToken(None, stream_ordering))

            written_events = set()  # Events that we've processed in this room

            # We need to track gaps in the events stream so that we can then
            # write out the state at those events. We do this by keeping track
            # of events whose prev events we haven't seen.

            # Map from event ID to prev events that haven't been processed,
            # dict[str, set[str]].
            event_to_unseen_prevs = {}

            # The reverse mapping to above, i.e. map from unseen event to events
            # that have the unseen event in their prev_events, i.e. the unseen
            # events "children". dict[str, set[str]]
            unseen_to_child_events = {}

            # We fetch events in the room the user could see by fetching *all*
            # events that we have and then filtering, this isn't the most
            # efficient method perhaps but it does guarantee we get everything.
            while True:
                events, _ = await self.store.paginate_room_events(
                    room_id, from_key, to_key, limit=100, direction="f"
                )
                if not events:
                    break

                from_key = events[-1].internal_metadata.after

                events = await filter_events_for_client(self.storage, user_id, events)

                writer.write_events(room_id, events)

                # Update the extremity tracking dicts
                for event in events:
                    # Check if we have any prev events that haven't been
                    # processed yet, and add those to the appropriate dicts.
                    unseen_events = set(event.prev_event_ids()) - written_events
                    if unseen_events:
                        event_to_unseen_prevs[event.event_id] = unseen_events
                        for unseen in unseen_events:
                            unseen_to_child_events.setdefault(unseen, set()).add(
                                event.event_id
                            )

                    # Now check if this event is an unseen prev event, if so
                    # then we remove this event from the appropriate dicts.
                    for child_id in unseen_to_child_events.pop(event.event_id, []):
                        event_to_unseen_prevs[child_id].discard(event.event_id)

                    written_events.add(event.event_id)

                logger.info(
                    "Written %d events in room %s", len(written_events), room_id
                )

            # Extremities are the events who have at least one unseen prev event.
            extremities = (
                event_id
                for event_id, unseen_prevs in event_to_unseen_prevs.items()
                if unseen_prevs
            )
            for event_id in extremities:
                if not event_to_unseen_prevs[event_id]:
                    continue
                state = await self.state_store.get_state_for_event(event_id)
                writer.write_state(room_id, event_id, state)

        return writer.finished()


class ExfiltrationWriter(object):
    """Interface used to specify how to write exported data.
    """

    def write_events(self, room_id, events):
        """Write a batch of events for a room.

        Args:
            room_id (str)
            events (list[FrozenEvent])
        """
        pass

    def write_state(self, room_id, event_id, state):
        """Write the state at the given event in the room.

        This only gets called for backward extremities rather than for each
        event.

        Args:
            room_id (str)
            event_id (str)
            state (dict[tuple[str, str], FrozenEvent])
        """
        pass

    def write_invite(self, room_id, event, state):
        """Write an invite for the room, with associated invite state.

        Args:
            room_id (str)
            event (FrozenEvent)
            state (dict[tuple[str, str], dict]): A subset of the state at the
                invite, with a subset of the event keys (type, state_key
                content and sender)
        """

    def finished(self):
        """Called when all data has succesfully been exported and written.

        This functions return value is passed to the caller of
        `export_user_data`.
        """
        pass
