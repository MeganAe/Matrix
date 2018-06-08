# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
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

from twisted.test.proto_helpers import MemoryReactorClock

from mock import Mock
from synapse.http.server import JsonResource
from synapse.rest.client.v1.register import register_servlets
from tests import unittest
from tests.server import FakeHomeserver, make_request


class CreateUserServletTestCase(unittest.TestCase):
    def setUp(self):
        self.registration_handler = Mock()

        self.appservice = Mock(sender="@as:test")
        self.datastore = Mock(
            get_app_service_by_token=Mock(return_value=self.appservice)
        )

        handlers = Mock(registration_handler=self.registration_handler)
        self.clock = MemoryReactorClock()

        self.hs = FakeHomeserver(self.clock, "superbig~testing~thing.com")
        self.hs.get_datastore = Mock(return_value=self.datastore)
        self.hs.get_handlers = Mock(return_value=handlers)

    def test_POST_createuser_with_valid_user(self):

        res = JsonResource(self.hs)
        register_servlets(self.hs, res)

        request_data = json.dumps(
            {
                "localpart": "someone",
                "displayname": "someone interesting",
                "duration_seconds": 200,
            }
        )

        url = b'/_matrix/client/api/v1/createUser?access_token=i_am_an_app_service'

        user_id = "@someone:interesting"
        token = "my token"

        self.registration_handler.get_or_create_user = Mock(
            return_value=(user_id, token)
        )

        request, channel = make_request(b"POST", url, request_data)
        request.render(res)

        # Advance the clock because it waits
        self.clock.advance(1)

        self.assertEquals(channel.result["code"], b"200")

        det_data = {
            "user_id": user_id,
            "access_token": token,
            "home_server": self.hs.hostname,
        }
        self.assertDictContainsSubset(det_data, json.loads(channel.result["body"]))
