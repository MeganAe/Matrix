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


import json

from mock import ANY, Mock, call

from twisted.internet import defer

from synapse.api.errors import AuthError
from synapse.types import UserID

from tests import unittest
from tests.utils import register_federation_servlets

# Some local users to test with
U_APPLE = UserID.from_string("@apple:test")
U_BANANA = UserID.from_string("@banana:test")

# Remote user
U_ONION = UserID.from_string("@onion:farm")

# Test room id
ROOM_ID = "a-room"


def _expect_edu_transaction(edu_type, content, origin="test"):
    return {
        "origin": origin,
        "origin_server_ts": 1000000,
        "pdus": [],
        "edus": [{"edu_type": edu_type, "content": content}],
    }


def _make_edu_transaction_json(edu_type, content):
    return json.dumps(_expect_edu_transaction(edu_type, content)).encode('utf8')


class TypingNotificationsTestCase(unittest.HomeserverTestCase):
    servlets = [register_federation_servlets]

    def make_homeserver(self, reactor, clock):
        # we mock out the keyring so as to skip the authentication check on the
        # federation API call.
        mock_keyring = Mock(spec=["verify_json_for_server"])
        mock_keyring.verify_json_for_server.return_value = defer.succeed(True)

        # we mock out the federation client too
        mock_federation_client = Mock(spec=["put_json"])
        mock_federation_client.put_json.return_value = defer.succeed((200, "OK"))

        hs = self.setup_test_homeserver(
            datastore=(Mock(
                spec=[
                    # Bits that Federation needs
                    "prep_send_transaction",
                    "delivered_txn",
                    "get_received_txn_response",
                    "set_received_txn_response",
                    "get_destination_retry_timings",
                    "get_devices_by_remote",
                    # Bits that user_directory needs
                    "get_user_directory_stream_pos",
                    "get_current_state_deltas",
                ]
            )),
            notifier=Mock(),
            http_client=mock_federation_client,
            keyring=mock_keyring,
        )

        return hs

    def prepare(self, reactor, clock, hs):
        # the tests assume that we are starting at unix time 1000
        reactor.pump((1000, ))

        mock_notifier = hs.get_notifier()
        self.on_new_event = mock_notifier.on_new_event

        self.handler = hs.get_typing_handler()

        self.event_source = hs.get_event_sources().sources["typing"]

        self.datastore = hs.get_datastore()
        retry_timings_res = {"destination": "", "retry_last_ts": 0, "retry_interval": 0}
        self.datastore.get_destination_retry_timings.return_value = defer.succeed(
            retry_timings_res
        )

        self.datastore.get_devices_by_remote.return_value = (0, [])

        def get_received_txn_response(*args):
            return defer.succeed(None)

        self.datastore.get_received_txn_response = get_received_txn_response

        self.room_members = []

        def check_joined_room(room_id, user_id):
            if user_id not in [u.to_string() for u in self.room_members]:
                raise AuthError(401, "User is not in the room")
        hs.get_auth().check_joined_room = check_joined_room

        def get_joined_hosts_for_room(room_id):
            return set(member.domain for member in self.room_members)

        self.datastore.get_joined_hosts_for_room = get_joined_hosts_for_room

        def get_current_user_in_room(room_id):
            return set(str(u) for u in self.room_members)
        hs.get_state_handler().get_current_user_in_room = get_current_user_in_room

        self.datastore.get_user_directory_stream_pos.return_value = (
            # we deliberately return a non-None stream pos to avoid doing an initial_spam
            defer.succeed(1)
        )

        self.datastore.get_current_state_deltas.return_value = None

        self.datastore.get_to_device_stream_token = lambda: 0
        self.datastore.get_new_device_msgs_for_remote = lambda *args, **kargs: ([], 0)
        self.datastore.delete_device_msgs_for_remote = lambda *args, **kargs: None

    def test_started_typing_local(self):
        self.room_members = [U_APPLE, U_BANANA]

        self.assertEquals(self.event_source.get_current_key(), 0)

        self.successResultOf(self.handler.started_typing(
            target_user=U_APPLE,
            auth_user=U_APPLE,
            room_id=ROOM_ID,
            timeout=20000,
        ))

        self.on_new_event.assert_has_calls(
            [call('typing_key', 1, rooms=[ROOM_ID])]
        )

        self.assertEquals(self.event_source.get_current_key(), 1)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=0
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )

    def test_started_typing_remote_send(self):
        self.room_members = [U_APPLE, U_ONION]

        self.successResultOf(self.handler.started_typing(
            target_user=U_APPLE,
            auth_user=U_APPLE,
            room_id=ROOM_ID,
            timeout=20000,
        ))

        put_json = self.hs.get_http_client().put_json
        put_json.assert_called_once_with(
            "farm",
            path="/_matrix/federation/v1/send/1000000",
            data=_expect_edu_transaction(
                "m.typing",
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_APPLE.to_string(),
                    "typing": True,
                },
            ),
            json_data_callback=ANY,
            long_retries=True,
            backoff_on_404=True,
            try_trailing_slash_on_400=True,
        )

    def test_started_typing_remote_recv(self):
        self.room_members = [U_APPLE, U_ONION]

        self.assertEquals(self.event_source.get_current_key(), 0)

        (request, channel) = self.make_request(
            "PUT",
            "/_matrix/federation/v1/send/1000000",
            _make_edu_transaction_json(
                "m.typing",
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_ONION.to_string(),
                    "typing": True,
                },
            ),
            federation_auth_origin=b'farm',
        )
        self.render(request)
        self.assertEqual(channel.code, 200)

        self.on_new_event.assert_has_calls(
            [call('typing_key', 1, rooms=[ROOM_ID])]
        )

        self.assertEquals(self.event_source.get_current_key(), 1)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=0
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_ONION.to_string()]},
                }
            ],
        )

    def test_stopped_typing(self):
        self.room_members = [U_APPLE, U_BANANA, U_ONION]

        # Gut-wrenching
        from synapse.handlers.typing import RoomMember

        member = RoomMember(ROOM_ID, U_APPLE.to_string())
        self.handler._member_typing_until[member] = 1002000
        self.handler._room_typing[ROOM_ID] = set([U_APPLE.to_string()])

        self.assertEquals(self.event_source.get_current_key(), 0)

        self.successResultOf(self.handler.stopped_typing(
            target_user=U_APPLE, auth_user=U_APPLE, room_id=ROOM_ID
        ))

        self.on_new_event.assert_has_calls(
            [call('typing_key', 1, rooms=[ROOM_ID])]
        )

        put_json = self.hs.get_http_client().put_json
        put_json.assert_called_once_with(
            "farm",
            path="/_matrix/federation/v1/send/1000000",
            data=_expect_edu_transaction(
                "m.typing",
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_APPLE.to_string(),
                    "typing": False,
                },
            ),
            json_data_callback=ANY,
            long_retries=True,
            backoff_on_404=True,
            try_trailing_slash_on_400=True,
        )

        self.assertEquals(self.event_source.get_current_key(), 1)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=0
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": []},
                }
            ],
        )

    def test_typing_timeout(self):
        self.room_members = [U_APPLE, U_BANANA]

        self.assertEquals(self.event_source.get_current_key(), 0)

        self.successResultOf(self.handler.started_typing(
            target_user=U_APPLE,
            auth_user=U_APPLE,
            room_id=ROOM_ID,
            timeout=10000,
        ))

        self.on_new_event.assert_has_calls(
            [call('typing_key', 1, rooms=[ROOM_ID])]
        )
        self.on_new_event.reset_mock()

        self.assertEquals(self.event_source.get_current_key(), 1)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=0
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )

        self.reactor.pump([16, ])

        self.on_new_event.assert_has_calls(
            [call('typing_key', 2, rooms=[ROOM_ID])]
        )

        self.assertEquals(self.event_source.get_current_key(), 2)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=1
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": []},
                }
            ],
        )

        # SYN-230 - see if we can still set after timeout

        self.successResultOf(self.handler.started_typing(
            target_user=U_APPLE,
            auth_user=U_APPLE,
            room_id=ROOM_ID,
            timeout=10000,
        ))

        self.on_new_event.assert_has_calls(
            [call('typing_key', 3, rooms=[ROOM_ID])]
        )
        self.on_new_event.reset_mock()

        self.assertEquals(self.event_source.get_current_key(), 3)
        events = self.event_source.get_new_events(
            room_ids=[ROOM_ID], from_key=0
        )
        self.assertEquals(
            events[0],
            [
                {
                    "type": "m.typing",
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )
