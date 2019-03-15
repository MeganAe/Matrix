# -*- coding: utf-8 -*-
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

from synapse.api.constants import DEFAULT_ROOM_VERSION, KNOWN_ROOM_VERSIONS
from synapse.rest.client.v1 import admin, login
from synapse.rest.client.v2_alpha import capabilities

from tests import unittest


class CapabilitiesTestCase(unittest.HomeserverTestCase):

    servlets = [
        admin.register_servlets,
        capabilities.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        self.url = b"/_matrix/client/r0/capabilities"
        hs = self.setup_test_homeserver()
        self.store = hs.get_datastore()
        return hs

    def test_check_auth_required(self):
        request, channel = self.make_request("GET", self.url)
        self.render(request)

        self.assertEqual(channel.code, 401)

    def test_get_room_version_capabilities(self):
        self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        request, channel = self.make_request("GET", self.url, access_token=access_token)
        self.render(request)
        capabilities = channel.json_body['capabilities']

        self.assertEqual(channel.code, 200)
        for room_version in capabilities['m.room_versions']['available'].keys():
            self.assertTrue(room_version in KNOWN_ROOM_VERSIONS, "" + room_version)
        self.assertEqual(
            DEFAULT_ROOM_VERSION, capabilities['m.room_versions']['default']
        )

    def test_get_change_password_capabilities(self):
        localpart = "user"
        password = "pass"
        user = self.register_user(localpart, password)
        access_token = self.login(user, password)

        request, channel = self.make_request("GET", self.url, access_token=access_token)
        self.render(request)
        capabilities = channel.json_body['capabilities']

        self.assertEqual(channel.code, 200)

        # Test case where password is handled outside of Synapse
        self.assertTrue(capabilities['m.change_password']['enabled'])
        self.get_success(self.store.user_set_password_hash(user, None))
        request, channel = self.make_request("GET", self.url, access_token=access_token)
        self.render(request)
        capabilities = channel.json_body['capabilities']

        self.assertEqual(channel.code, 200)
        self.assertFalse(capabilities['m.change_password']['enabled'])
