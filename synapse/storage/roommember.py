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

from twisted.internet import defer

from collections import namedtuple

from ._base import SQLBaseStore
from synapse.util.caches.descriptors import cached, cachedInlineCallbacks

from synapse.api.constants import Membership
from synapse.types import get_domain_from_id

import logging

logger = logging.getLogger(__name__)


RoomsForUser = namedtuple(
    "RoomsForUser",
    ("room_id", "sender", "membership", "event_id", "stream_ordering")
)


class RoomMemberStore(SQLBaseStore):

    def _store_room_members_txn(self, txn, events, backfilled):
        """Store a room member in the database.
        """
        self._simple_insert_many_txn(
            txn,
            table="room_memberships",
            values=[
                {
                    "event_id": event.event_id,
                    "user_id": event.state_key,
                    "sender": event.user_id,
                    "room_id": event.room_id,
                    "membership": event.membership,
                }
                for event in events
            ]
        )

        for event in events:
            txn.call_after(self.get_rooms_for_user.invalidate, (event.state_key,))
            txn.call_after(self.get_joined_hosts_for_room.invalidate, (event.room_id,))
            txn.call_after(self.get_users_in_room.invalidate, (event.room_id,))
            txn.call_after(
                self._membership_stream_cache.entity_has_changed,
                event.state_key, event.internal_metadata.stream_ordering
            )
            txn.call_after(
                self.get_invited_rooms_for_user.invalidate, (event.state_key,)
            )

            # We update the local_invites table only if the event is "current",
            # i.e., its something that has just happened.
            # The only current event that can also be an outlier is if its an
            # invite that has come in across federation.
            is_new_state = not backfilled and (
                not event.internal_metadata.is_outlier()
                or event.internal_metadata.is_invite_from_remote()
            )
            is_mine = self.hs.is_mine_id(event.state_key)
            if is_new_state and is_mine:
                if event.membership == Membership.INVITE:
                    self._simple_insert_txn(
                        txn,
                        table="local_invites",
                        values={
                            "event_id": event.event_id,
                            "invitee": event.state_key,
                            "inviter": event.sender,
                            "room_id": event.room_id,
                            "stream_id": event.internal_metadata.stream_ordering,
                        }
                    )
                else:
                    sql = (
                        "UPDATE local_invites SET stream_id = ?, replaced_by = ? WHERE"
                        " room_id = ? AND invitee = ? AND locally_rejected is NULL"
                        " AND replaced_by is NULL"
                    )

                    txn.execute(sql, (
                        event.internal_metadata.stream_ordering,
                        event.event_id,
                        event.room_id,
                        event.state_key,
                    ))

    @defer.inlineCallbacks
    def locally_reject_invite(self, user_id, room_id):
        sql = (
            "UPDATE local_invites SET stream_id = ?, locally_rejected = ? WHERE"
            " room_id = ? AND invitee = ? AND locally_rejected is NULL"
            " AND replaced_by is NULL"
        )

        def f(txn, stream_ordering):
            txn.execute(sql, (
                stream_ordering,
                True,
                room_id,
                user_id,
            ))

        with self._stream_id_gen.get_next() as stream_ordering:
            yield self.runInteraction("locally_reject_invite", f, stream_ordering)

    @cached(max_entries=5000)
    def get_users_in_room(self, room_id):
        def f(txn):

            rows = self._get_members_rows_txn(
                txn,
                room_id=room_id,
                membership=Membership.JOIN,
            )

            return [r["user_id"] for r in rows]
        return self.runInteraction("get_users_in_room", f)

    @cached()
    def get_invited_rooms_for_user(self, user_id):
        """ Get all the rooms the user is invited to
        Args:
            user_id (str): The user ID.
        Returns:
            A deferred list of RoomsForUser.
        """

        return self.get_rooms_for_user_where_membership_is(
            user_id, [Membership.INVITE]
        )

    @defer.inlineCallbacks
    def get_invite_for_user_in_room(self, user_id, room_id):
        """Gets the invite for the given user and room

        Args:
            user_id (str)
            room_id (str)

        Returns:
            Deferred: Resolves to either a RoomsForUser or None if no invite was
                found.
        """
        invites = yield self.get_invited_rooms_for_user(user_id)
        for invite in invites:
            if invite.room_id == room_id:
                defer.returnValue(invite)
        defer.returnValue(None)

    def get_rooms_for_user_where_membership_is(self, user_id, membership_list):
        """ Get all the rooms for this user where the membership for this user
        matches one in the membership list.

        Args:
            user_id (str): The user ID.
            membership_list (list): A list of synapse.api.constants.Membership
            values which the user must be in.
        Returns:
            A list of dictionary objects, with room_id, membership and sender
            defined.
        """
        if not membership_list:
            return defer.succeed(None)

        return self.runInteraction(
            "get_rooms_for_user_where_membership_is",
            self._get_rooms_for_user_where_membership_is_txn,
            user_id, membership_list
        )

    def _get_rooms_for_user_where_membership_is_txn(self, txn, user_id,
                                                    membership_list):

        do_invite = Membership.INVITE in membership_list
        membership_list = [m for m in membership_list if m != Membership.INVITE]

        results = []
        if membership_list:
            where_clause = "user_id = ? AND (%s) AND forgotten = 0" % (
                " OR ".join(["membership = ?" for _ in membership_list]),
            )

            args = [user_id]
            args.extend(membership_list)

            sql = (
                "SELECT m.room_id, m.sender, m.membership, m.event_id, e.stream_ordering"
                " FROM current_state_events as c"
                " INNER JOIN room_memberships as m"
                " ON m.event_id = c.event_id"
                " INNER JOIN events as e"
                " ON e.event_id = c.event_id"
                " AND m.room_id = c.room_id"
                " AND m.user_id = c.state_key"
                " WHERE %s"
            ) % (where_clause,)

            txn.execute(sql, args)
            results = [
                RoomsForUser(**r) for r in self.cursor_to_dict(txn)
            ]

        if do_invite:
            sql = (
                "SELECT i.room_id, inviter, i.event_id, e.stream_ordering"
                " FROM local_invites as i"
                " INNER JOIN events as e USING (event_id)"
                " WHERE invitee = ? AND locally_rejected is NULL"
                " AND replaced_by is NULL"
            )

            txn.execute(sql, (user_id,))
            results.extend(RoomsForUser(
                room_id=r["room_id"],
                sender=r["inviter"],
                event_id=r["event_id"],
                stream_ordering=r["stream_ordering"],
                membership=Membership.INVITE,
            ) for r in self.cursor_to_dict(txn))

        return results

    @cachedInlineCallbacks(max_entries=5000)
    def get_joined_hosts_for_room(self, room_id):
        user_ids = yield self.get_users_in_room(room_id)
        defer.returnValue(set(get_domain_from_id(uid) for uid in user_ids))

    def _get_members_rows_txn(self, txn, room_id, membership=None, user_id=None):
        where_clause = "c.room_id = ?"
        where_values = [room_id]

        if membership:
            where_clause += " AND m.membership = ?"
            where_values.append(membership)

        if user_id:
            where_clause += " AND m.user_id = ?"
            where_values.append(user_id)

        sql = (
            "SELECT m.* FROM room_memberships as m"
            " INNER JOIN current_state_events as c"
            " ON m.event_id = c.event_id "
            " AND m.room_id = c.room_id "
            " AND m.user_id = c.state_key"
            " WHERE %(where)s"
        ) % {
            "where": where_clause,
        }

        txn.execute(sql, where_values)
        rows = self.cursor_to_dict(txn)

        return rows

    @cached(max_entries=5000)
    def get_rooms_for_user(self, user_id):
        return self.get_rooms_for_user_where_membership_is(
            user_id, membership_list=[Membership.JOIN],
        )

    @defer.inlineCallbacks
    def forget(self, user_id, room_id):
        """Indicate that user_id wishes to discard history for room_id."""
        def f(txn):
            sql = (
                "UPDATE"
                "  room_memberships"
                " SET"
                "  forgotten = 1"
                " WHERE"
                "  user_id = ?"
                " AND"
                "  room_id = ?"
            )
            txn.execute(sql, (user_id, room_id))
        yield self.runInteraction("forget_membership", f)
        self.was_forgotten_at.invalidate_all()
        self.who_forgot_in_room.invalidate_all()
        self.did_forget.invalidate((user_id, room_id))

    @cachedInlineCallbacks(num_args=2)
    def did_forget(self, user_id, room_id):
        """Returns whether user_id has elected to discard history for room_id.

        Returns False if they have since re-joined."""
        def f(txn):
            sql = (
                "SELECT"
                "  COUNT(*)"
                " FROM"
                "  room_memberships"
                " WHERE"
                "  user_id = ?"
                " AND"
                "  room_id = ?"
                " AND"
                "  forgotten = 0"
            )
            txn.execute(sql, (user_id, room_id))
            rows = txn.fetchall()
            return rows[0][0]
        count = yield self.runInteraction("did_forget_membership", f)
        defer.returnValue(count == 0)

    @cachedInlineCallbacks(num_args=3)
    def was_forgotten_at(self, user_id, room_id, event_id):
        """Returns whether user_id has elected to discard history for room_id at event_id.

        event_id must be a membership event."""
        def f(txn):
            sql = (
                "SELECT"
                "  forgotten"
                " FROM"
                "  room_memberships"
                " WHERE"
                "  user_id = ?"
                " AND"
                "  room_id = ?"
                " AND"
                "  event_id = ?"
            )
            txn.execute(sql, (user_id, room_id, event_id))
            rows = txn.fetchall()
            return rows[0][0]
        forgot = yield self.runInteraction("did_forget_membership_at", f)
        defer.returnValue(forgot == 1)

    @cached()
    def who_forgot_in_room(self, room_id):
        return self._simple_select_list(
            table="room_memberships",
            retcols=("user_id", "event_id"),
            keyvalues={
                "room_id": room_id,
                "forgotten": 1,
            },
            desc="who_forgot"
        )
