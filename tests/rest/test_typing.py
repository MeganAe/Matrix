# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
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

"""Tests REST events for /rooms paths."""

# twisted imports
from twisted.internet import defer

import synapse.rest.room
from synapse.server import HomeServer

from ..utils import MockHttpResource, SQLiteMemoryDbPool, MockKey
from .utils import RestTestCase

from mock import Mock, NonCallableMock


PATH_PREFIX = "/_matrix/client/api/v1"


class RoomTypingTestCase(RestTestCase):
    """ Tests /rooms/$room_id/typing/$user_id REST API. """
    user_id = "@sid:red"

    @defer.inlineCallbacks
    def setUp(self):
        self.mock_resource = MockHttpResource(prefix=PATH_PREFIX)
        self.auth_user_id = self.user_id

        self.mock_config = NonCallableMock()
        self.mock_config.signing_key = [MockKey()]

        db_pool = SQLiteMemoryDbPool()
        yield db_pool.prepare()

        hs = HomeServer(
            "red",
            db_pool=db_pool,
            http_client=None,
            replication_layer=Mock(),
            ratelimiter=NonCallableMock(spec_set=[
                "send_message",
            ]),
            config=self.mock_config,
        )
        self.hs = hs

        self.event_source = hs.get_event_sources().sources["typing"]

        self.ratelimiter = hs.get_ratelimiter()
        self.ratelimiter.send_message.return_value = (True, 0)

        hs.get_handlers().federation_handler = Mock()

        def _get_user_by_token(token=None):
            return {
                "user": hs.parse_userid(self.auth_user_id),
                "admin": False,
                "device_id": None,
            }

        hs.get_auth().get_user_by_token = _get_user_by_token

        def _insert_client_ip(*args, **kwargs):
            return defer.succeed(None)
        hs.get_datastore().insert_client_ip = _insert_client_ip

        synapse.rest.room.register_servlets(hs, self.mock_resource)

        self.room_id = yield self.create_room_as(self.user_id)
        # Need another user to make notifications actually work
        yield self.join(self.room_id, user="@jim:red")

    def tearDown(self):
        self.hs.get_handlers().typing_notification_handler.tearDown()

    @defer.inlineCallbacks
    def test_set_typing(self):
        (code, _) = yield self.mock_resource.trigger("PUT",
            "/rooms/%s/typing/%s" % (self.room_id, self.user_id),
            '{"typing": true, "timeout": 30000}'
        )
        self.assertEquals(200, code)

        self.assertEquals(self.event_source.get_current_key(), 1)
        self.assertEquals(
            self.event_source.get_new_events_for_user(self.user_id, 0, None)[0],
            [
                {"type": "m.typing",
                 "room_id": self.room_id,
                 "content": {
                     "user_ids": [self.user_id],
                }},
            ]
        )

    @defer.inlineCallbacks
    def test_set_not_typing(self):
        (code, _) = yield self.mock_resource.trigger("PUT",
            "/rooms/%s/typing/%s" % (self.room_id, self.user_id),
            '{"typing": false}'
        )
        self.assertEquals(200, code)
