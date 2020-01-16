# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from unittest.mock import Mock

from synapse.api.constants import Membership
from synapse.rest.admin import register_servlets_for_client_rest_resource
from synapse.rest.client.v1 import login, room
from synapse.types import Requester, UserID

from tests import unittest


class RoomMemberStoreTestCase(unittest.HomeserverTestCase):

    servlets = [
        login.register_servlets,
        register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        hs = self.setup_test_homeserver(
            resource_for_federation=Mock(), http_client=None
        )
        return hs

    def prepare(self, reactor, clock, hs):

        # We can't test the RoomMemberStore on its own without the other event
        # storage logic
        self.store = hs.get_datastore()

        self.u_alice = self.register_user("alice", "pass")
        self.t_alice = self.login("alice", "pass")
        self.u_bob = self.register_user("bob", "pass")

        # User elsewhere on another host
        self.u_charlie = UserID.from_string("@charlie:elsewhere")

    def test_one_member(self):

        # Alice creates the room, and is automatically joined
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)

        rooms_for_user = self.get_success(
            self.store.get_rooms_for_local_user_where_membership_is(
                self.u_alice, [Membership.JOIN]
            )
        )

        self.assertEquals([self.room], [m.room_id for m in rooms_for_user])

    def test_count_known_servers(self):
        """
        _count_known_servers will calculate how many servers are in a room.
        """
        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        servers = self.get_success(self.store._count_known_servers())
        self.assertEqual(servers, 2)

    def test_count_known_servers_stat_counter_disabled(self):
        """
        If enabled, the metrics for how many servers are known will be counted.
        """
        self.assertTrue("_known_servers_count" not in self.store.__dict__.keys())

        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        self.pump(20)

        self.assertTrue("_known_servers_count" not in self.store.__dict__.keys())

    @unittest.override_config(
        {"enable_metrics": True, "metrics_flags": {"known_servers": True}}
    )
    def test_count_known_servers_stat_counter_enabled(self):
        """
        If enabled, the metrics for how many servers are known will be counted.
        """
        # Initialises to 1 -- itself
        self.assertEqual(self.store._known_servers_count, 1)

        self.pump(20)

        # No rooms have been joined, so technically the SQL returns 0, but it
        # will still say it knows about itself.
        self.assertEqual(self.store._known_servers_count, 1)

        self.room = self.helper.create_room_as(self.u_alice, tok=self.t_alice)
        self.inject_room_member(self.room, self.u_bob, Membership.JOIN)
        self.inject_room_member(self.room, self.u_charlie.to_string(), Membership.JOIN)

        self.pump(20)

        # It now knows about Charlie's server.
        self.assertEqual(self.store._known_servers_count, 2)


class CurrentStateMembershipUpdateTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor, clock, homeserver):
        self.store = homeserver.get_datastore()
        self.room_creator = homeserver.get_room_creation_handler()

    def test_can_rerun_update(self):
        # First make sure we have completed all updates.
        while not self.get_success(
            self.store.db.updates.has_completed_background_updates()
        ):
            self.get_success(
                self.store.db.updates.do_next_background_update(100), by=0.1
            )

        # Now let's create a room, which will insert a membership
        user = UserID("alice", "test")
        requester = Requester(user, None, False, None, None)
        self.get_success(self.room_creator.create_room(requester, {}))

        # Register the background update to run again.
        self.get_success(
            self.store.db.simple_insert(
                table="background_updates",
                values={
                    "update_name": "current_state_events_membership",
                    "progress_json": "{}",
                    "depends_on": None,
                },
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.store.db.updates._all_done = False

        # Now let's actually drive the updates to completion
        while not self.get_success(
            self.store.db.updates.has_completed_background_updates()
        ):
            self.get_success(
                self.store.db.updates.do_next_background_update(100), by=0.1
            )
