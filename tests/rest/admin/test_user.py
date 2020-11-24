# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
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

import hashlib
import hmac
import json
import urllib.parse
from binascii import unhexlify

from mock import Mock

import synapse.rest.admin
from synapse.api.constants import UserTypes
from synapse.api.errors import Codes, HttpResponseException, ResourceLimitError
from synapse.rest.client.v1 import login, logout, profile, room
from synapse.rest.client.v2_alpha import devices, sync

from tests import unittest
from tests.test_utils import make_awaitable
from tests.unittest import override_config


class UserRegisterTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        profile.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):

        self.url = "/_synapse/admin/v1/register"

        self.registration_handler = Mock()
        self.identity_handler = Mock()
        self.login_handler = Mock()
        self.device_handler = Mock()
        self.device_handler.check_device_registered = Mock(return_value="FAKE")

        self.datastore = Mock(return_value=Mock())
        self.datastore.get_current_state_deltas = Mock(return_value=(0, []))

        self.secrets = Mock()

        self.hs = self.setup_test_homeserver()

        self.hs.config.registration_shared_secret = "shared"

        self.hs.get_media_repository = Mock()
        self.hs.get_deactivate_account_handler = Mock()

        return self.hs

    def test_disabled(self):
        """
        If there is no shared secret, registration through this method will be
        prevented.
        """
        self.hs.config.registration_shared_secret = None

        request, channel = self.make_request("POST", self.url, b"{}")

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(
            "Shared secret registration is not enabled", channel.json_body["error"]
        )

    def test_get_nonce(self):
        """
        Calling GET on the endpoint will return a randomised nonce, using the
        homeserver's secrets provider.
        """
        secrets = Mock()
        secrets.token_hex = Mock(return_value="abcd")

        self.hs.get_secrets = Mock(return_value=secrets)

        request, channel = self.make_request("GET", self.url)

        self.assertEqual(channel.json_body, {"nonce": "abcd"})

    def test_expired_nonce(self):
        """
        Calling GET on the endpoint will return a randomised nonce, which will
        only last for SALT_TIMEOUT (60s).
        """
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        # 59 seconds
        self.reactor.advance(59)

        body = json.dumps({"nonce": nonce})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("username must be specified", channel.json_body["error"])

        # 61 seconds
        self.reactor.advance(2)

        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("unrecognised nonce", channel.json_body["error"])

    def test_register_incorrect_nonce(self):
        """
        Only the provided nonce can be used, as it's checked in the MAC.
        """
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(b"notthenonce\x00bob\x00abc123\x00admin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob",
                "password": "abc123",
                "admin": True,
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("HMAC incorrect", channel.json_body["error"])

    def test_register_correct_nonce(self):
        """
        When the correct nonce is provided, and the right key is provided, the
        user is registered.
        """
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(
            nonce.encode("ascii") + b"\x00bob\x00abc123\x00admin\x00support"
        )
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob",
                "password": "abc123",
                "admin": True,
                "user_type": UserTypes.SUPPORT,
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["user_id"])

    def test_nonce_reuse(self):
        """
        A valid unrecognised nonce.
        """
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(nonce.encode("ascii") + b"\x00bob\x00abc123\x00admin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob",
                "password": "abc123",
                "admin": True,
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["user_id"])

        # Now, try and reuse it
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("unrecognised nonce", channel.json_body["error"])

    def test_missing_parts(self):
        """
        Synapse will complain if you don't give nonce, username, password, and
        mac.  Admin and user_types are optional.  Additional checks are done for length
        and type.
        """

        def nonce():
            request, channel = self.make_request("GET", self.url)
            return channel.json_body["nonce"]

        #
        # Nonce check
        #

        # Must be present
        body = json.dumps({})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("nonce must be specified", channel.json_body["error"])

        #
        # Username checks
        #

        # Must be present
        body = json.dumps({"nonce": nonce()})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("username must be specified", channel.json_body["error"])

        # Must be a string
        body = json.dumps({"nonce": nonce(), "username": 1234})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid username", channel.json_body["error"])

        # Must not have null bytes
        body = json.dumps({"nonce": nonce(), "username": "abcd\u0000"})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid username", channel.json_body["error"])

        # Must not have null bytes
        body = json.dumps({"nonce": nonce(), "username": "a" * 1000})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid username", channel.json_body["error"])

        #
        # Password checks
        #

        # Must be present
        body = json.dumps({"nonce": nonce(), "username": "a"})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("password must be specified", channel.json_body["error"])

        # Must be a string
        body = json.dumps({"nonce": nonce(), "username": "a", "password": 1234})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid password", channel.json_body["error"])

        # Must not have null bytes
        body = json.dumps({"nonce": nonce(), "username": "a", "password": "abcd\u0000"})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid password", channel.json_body["error"])

        # Super long
        body = json.dumps({"nonce": nonce(), "username": "a", "password": "A" * 1000})
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid password", channel.json_body["error"])

        #
        # user_type check
        #

        # Invalid user_type
        body = json.dumps(
            {
                "nonce": nonce(),
                "username": "a",
                "password": "1234",
                "user_type": "invalid",
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Invalid user type", channel.json_body["error"])

    def test_displayname(self):
        """
        Test that displayname of new user is set
        """

        # set no displayname
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(nonce.encode("ascii") + b"\x00bob1\x00abc123\x00notadmin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {"nonce": nonce, "username": "bob1", "password": "abc123", "mac": want_mac}
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob1:test", channel.json_body["user_id"])

        request, channel = self.make_request("GET", "/profile/@bob1:test/displayname")
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("bob1", channel.json_body["displayname"])

        # displayname is None
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(nonce.encode("ascii") + b"\x00bob2\x00abc123\x00notadmin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob2",
                "displayname": None,
                "password": "abc123",
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob2:test", channel.json_body["user_id"])

        request, channel = self.make_request("GET", "/profile/@bob2:test/displayname")
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("bob2", channel.json_body["displayname"])

        # displayname is empty
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(nonce.encode("ascii") + b"\x00bob3\x00abc123\x00notadmin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob3",
                "displayname": "",
                "password": "abc123",
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob3:test", channel.json_body["user_id"])

        request, channel = self.make_request("GET", "/profile/@bob3:test/displayname")
        self.assertEqual(404, int(channel.result["code"]), msg=channel.result["body"])

        # set displayname
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(nonce.encode("ascii") + b"\x00bob4\x00abc123\x00notadmin")
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob4",
                "displayname": "Bob's Name",
                "password": "abc123",
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob4:test", channel.json_body["user_id"])

        request, channel = self.make_request("GET", "/profile/@bob4:test/displayname")
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("Bob's Name", channel.json_body["displayname"])

    @override_config(
        {"limit_usage_by_mau": True, "max_mau_value": 2, "mau_trial_days": 0}
    )
    def test_register_mau_limit_reached(self):
        """
        Check we can register a user via the shared secret registration API
        even if the MAU limit is reached.
        """
        handler = self.hs.get_registration_handler()
        store = self.hs.get_datastore()

        # Set monthly active users to the limit
        store.get_monthly_active_count = Mock(
            return_value=make_awaitable(self.hs.config.max_mau_value)
        )
        # Check that the blocking of monthly active users is working as expected
        # The registration of a new user fails due to the limit
        self.get_failure(
            handler.register_user(localpart="local_part"), ResourceLimitError
        )

        # Register new user with admin API
        request, channel = self.make_request("GET", self.url)
        nonce = channel.json_body["nonce"]

        want_mac = hmac.new(key=b"shared", digestmod=hashlib.sha1)
        want_mac.update(
            nonce.encode("ascii") + b"\x00bob\x00abc123\x00admin\x00support"
        )
        want_mac = want_mac.hexdigest()

        body = json.dumps(
            {
                "nonce": nonce,
                "username": "bob",
                "password": "abc123",
                "admin": True,
                "user_type": UserTypes.SUPPORT,
                "mac": want_mac,
            }
        )
        request, channel = self.make_request("POST", self.url, body.encode("utf8"))

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["user_id"])


class UsersListTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]
    url = "/_synapse/admin/v2/users"

    def prepare(self, reactor, clock, hs):
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.register_user("user1", "pass1", admin=False)
        self.register_user("user2", "pass2", admin=False)

    def test_no_auth(self):
        """
        Try to list users without authentication.
        """
        request, channel = self.make_request("GET", self.url, b"{}")

        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("M_MISSING_TOKEN", channel.json_body["errcode"])

    def test_all_users(self):
        """
        List all users, including deactivated users.
        """
        request, channel = self.make_request(
            "GET",
            self.url + "?deactivated=true",
            b"{}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(3, len(channel.json_body["users"]))
        self.assertEqual(3, channel.json_body["total"])


class UserRestTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_token = self.login("user", "pass")
        self.url_other_user = "/_synapse/admin/v2/users/%s" % urllib.parse.quote(
            self.other_user
        )

    def test_requester_is_no_admin(self):
        """
        If the user is not a server admin, an error is returned.
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        request, channel = self.make_request(
            "GET", url, access_token=self.other_user_token,
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("You are not a server admin", channel.json_body["error"])

        request, channel = self.make_request(
            "PUT", url, access_token=self.other_user_token, content=b"{}",
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("You are not a server admin", channel.json_body["error"])

    def test_user_does_not_exist(self):
        """
        Tests that a lookup for a user that does not exist returns a 404
        """

        request, channel = self.make_request(
            "GET",
            "/_synapse/admin/v2/users/@unknown_person:test",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual("M_NOT_FOUND", channel.json_body["errcode"])

    def test_create_server_admin(self):
        """
        Check that a new admin user is created successfully.
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user (server admin)
        body = json.dumps(
            {
                "password": "abc123",
                "admin": True,
                "displayname": "Bob's name",
                "threepids": [{"medium": "email", "address": "bob@bob.bob"}],
                "avatar_url": None,
            }
        )

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("Bob's name", channel.json_body["displayname"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])
        self.assertEqual(True, channel.json_body["admin"])

        # Get user
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("Bob's name", channel.json_body["displayname"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])
        self.assertEqual(True, channel.json_body["admin"])
        self.assertEqual(False, channel.json_body["is_guest"])
        self.assertEqual(False, channel.json_body["deactivated"])

    def test_create_user(self):
        """
        Check that a new regular user is created successfully.
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps(
            {
                "password": "abc123",
                "admin": False,
                "displayname": "Bob's name",
                "threepids": [{"medium": "email", "address": "bob@bob.bob"}],
            }
        )

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("Bob's name", channel.json_body["displayname"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])
        self.assertEqual(False, channel.json_body["admin"])

        # Get user
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("Bob's name", channel.json_body["displayname"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])
        self.assertEqual(False, channel.json_body["admin"])
        self.assertEqual(False, channel.json_body["is_guest"])
        self.assertEqual(False, channel.json_body["deactivated"])

    @override_config(
        {"limit_usage_by_mau": True, "max_mau_value": 2, "mau_trial_days": 0}
    )
    def test_create_user_mau_limit_reached_active_admin(self):
        """
        Check that an admin can register a new user via the admin API
        even if the MAU limit is reached.
        Admin user was active before creating user.
        """

        handler = self.hs.get_registration_handler()

        # Sync to set admin user to active
        # before limit of monthly active users is reached
        request, channel = self.make_request(
            "GET", "/sync", access_token=self.admin_user_tok
        )

        if channel.code != 200:
            raise HttpResponseException(
                channel.code, channel.result["reason"], channel.result["body"]
            )

        # Set monthly active users to the limit
        self.store.get_monthly_active_count = Mock(
            return_value=make_awaitable(self.hs.config.max_mau_value)
        )
        # Check that the blocking of monthly active users is working as expected
        # The registration of a new user fails due to the limit
        self.get_failure(
            handler.register_user(localpart="local_part"), ResourceLimitError
        )

        # Register new user with admin API
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps({"password": "abc123", "admin": False})

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual(False, channel.json_body["admin"])

    @override_config(
        {"limit_usage_by_mau": True, "max_mau_value": 2, "mau_trial_days": 0}
    )
    def test_create_user_mau_limit_reached_passive_admin(self):
        """
        Check that an admin can register a new user via the admin API
        even if the MAU limit is reached.
        Admin user was not active before creating user.
        """

        handler = self.hs.get_registration_handler()

        # Set monthly active users to the limit
        self.store.get_monthly_active_count = Mock(
            return_value=make_awaitable(self.hs.config.max_mau_value)
        )
        # Check that the blocking of monthly active users is working as expected
        # The registration of a new user fails due to the limit
        self.get_failure(
            handler.register_user(localpart="local_part"), ResourceLimitError
        )

        # Register new user with admin API
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps({"password": "abc123", "admin": False})

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        # Admin user is not blocked by mau anymore
        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual(False, channel.json_body["admin"])

    @override_config(
        {
            "email": {
                "enable_notifs": True,
                "notif_for_new_users": True,
                "notif_from": "test@example.com",
            },
            "public_baseurl": "https://example.com",
        }
    )
    def test_create_user_email_notif_for_new_users(self):
        """
        Check that a new regular user is created successfully and
        got an email pusher.
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps(
            {
                "password": "abc123",
                "threepids": [{"medium": "email", "address": "bob@bob.bob"}],
            }
        )

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])

        pushers = self.get_success(
            self.store.get_pushers_by({"user_name": "@bob:test"})
        )
        pushers = list(pushers)
        self.assertEqual(len(pushers), 1)
        self.assertEqual("@bob:test", pushers[0]["user_name"])

    @override_config(
        {
            "email": {
                "enable_notifs": False,
                "notif_for_new_users": False,
                "notif_from": "test@example.com",
            },
            "public_baseurl": "https://example.com",
        }
    )
    def test_create_user_email_no_notif_for_new_users(self):
        """
        Check that a new regular user is created successfully and
        got not an email pusher.
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps(
            {
                "password": "abc123",
                "threepids": [{"medium": "email", "address": "bob@bob.bob"}],
            }
        )

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob@bob.bob", channel.json_body["threepids"][0]["address"])

        pushers = self.get_success(
            self.store.get_pushers_by({"user_name": "@bob:test"})
        )
        pushers = list(pushers)
        self.assertEqual(len(pushers), 0)

    def test_set_password(self):
        """
        Test setting a new password for another user.
        """

        # Change password
        body = json.dumps({"password": "hahaha"})

        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

    def test_set_displayname(self):
        """
        Test setting the displayname of another user.
        """

        # Modify user
        body = json.dumps({"displayname": "foobar"})

        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual("foobar", channel.json_body["displayname"])

        # Get user
        request, channel = self.make_request(
            "GET", self.url_other_user, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual("foobar", channel.json_body["displayname"])

    def test_set_threepid(self):
        """
        Test setting threepid for an other user.
        """

        # Delete old and add new threepid to user
        body = json.dumps(
            {"threepids": [{"medium": "email", "address": "bob3@bob.bob"}]}
        )

        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob3@bob.bob", channel.json_body["threepids"][0]["address"])

        # Get user
        request, channel = self.make_request(
            "GET", self.url_other_user, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual("email", channel.json_body["threepids"][0]["medium"])
        self.assertEqual("bob3@bob.bob", channel.json_body["threepids"][0]["address"])

    def test_deactivate_user(self):
        """
        Test deactivating another user.
        """

        # Deactivate user
        body = json.dumps({"deactivated": True})

        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual(True, channel.json_body["deactivated"])
        # the user is deactivated, the threepid will be deleted

        # Get user
        request, channel = self.make_request(
            "GET", self.url_other_user, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual(True, channel.json_body["deactivated"])

    def test_reactivate_user(self):
        """
        Test reactivating another user.
        """

        # Deactivate the user.
        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=json.dumps({"deactivated": True}).encode(encoding="utf_8"),
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self._is_erased("@user:test", False)
        d = self.store.mark_user_erased("@user:test")
        self.assertIsNone(self.get_success(d))
        self._is_erased("@user:test", True)

        # Attempt to reactivate the user (without a password).
        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=json.dumps({"deactivated": False}).encode(encoding="utf_8"),
        )
        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])

        # Reactivate the user.
        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=json.dumps({"deactivated": False, "password": "foo"}).encode(
                encoding="utf_8"
            ),
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # Get user
        request, channel = self.make_request(
            "GET", self.url_other_user, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual(False, channel.json_body["deactivated"])
        self._is_erased("@user:test", False)

    def test_set_user_as_admin(self):
        """
        Test setting the admin flag on a user.
        """

        # Set a user as an admin
        body = json.dumps({"admin": True})

        request, channel = self.make_request(
            "PUT",
            self.url_other_user,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual(True, channel.json_body["admin"])

        # Get user
        request, channel = self.make_request(
            "GET", self.url_other_user, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@user:test", channel.json_body["name"])
        self.assertEqual(True, channel.json_body["admin"])

    def test_accidental_deactivation_prevention(self):
        """
        Ensure an account can't accidentally be deactivated by using a str value
        for the deactivated body parameter
        """
        url = "/_synapse/admin/v2/users/@bob:test"

        # Create user
        body = json.dumps({"password": "abc123"})

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(201, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("bob", channel.json_body["displayname"])

        # Get user
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("bob", channel.json_body["displayname"])
        self.assertEqual(0, channel.json_body["deactivated"])

        # Change password (and use a str for deactivate instead of a bool)
        body = json.dumps({"password": "abc123", "deactivated": "false"})  # oops!

        request, channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
            content=body.encode(encoding="utf_8"),
        )

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])

        # Check user is not deactivated
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual("@bob:test", channel.json_body["name"])
        self.assertEqual("bob", channel.json_body["displayname"])

        # Ensure they're still alive
        self.assertEqual(0, channel.json_body["deactivated"])

    def _is_erased(self, user_id, expect):
        """Assert that the user is erased or not
        """
        d = self.store.is_user_erased(user_id)
        if expect:
            self.assertTrue(self.get_success(d))
        else:
            self.assertFalse(self.get_success(d))


class UserMembershipRestTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.url = "/_synapse/admin/v1/users/%s/joined_rooms" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self):
        """
        Try to list rooms of an user without authentication.
        """
        request, channel = self.make_request("GET", self.url, b"{}")

        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self):
        """
        If the user is not a server admin, an error is returned.
        """
        other_user_token = self.login("user", "pass")

        request, channel = self.make_request(
            "GET", self.url, access_token=other_user_token,
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_does_not_exist(self):
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = "/_synapse/admin/v1/users/@unknown_person:test/joined_rooms"
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_user_is_not_local(self):
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = "/_synapse/admin/v1/users/@unknown_person:unknown_domain/joined_rooms"

        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_no_memberships(self):
        """
        Tests that a normal lookup for rooms is successfully
        if user has no memberships
        """
        # Get rooms
        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(0, channel.json_body["total"])
        self.assertEqual(0, len(channel.json_body["joined_rooms"]))

    def test_get_rooms(self):
        """
        Tests that a normal lookup for rooms is successfully
        """
        # Create rooms and join
        other_user_tok = self.login("user", "pass")
        number_rooms = 5
        for n in range(number_rooms):
            self.helper.create_room_as(self.other_user, tok=other_user_tok)

        # Get rooms
        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(number_rooms, channel.json_body["total"])
        self.assertEqual(number_rooms, len(channel.json_body["joined_rooms"]))


class PushersRestTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.url = "/_synapse/admin/v1/users/%s/pushers" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self):
        """
        Try to list pushers of an user without authentication.
        """
        request, channel = self.make_request("GET", self.url, b"{}")

        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self):
        """
        If the user is not a server admin, an error is returned.
        """
        other_user_token = self.login("user", "pass")

        request, channel = self.make_request(
            "GET", self.url, access_token=other_user_token,
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_does_not_exist(self):
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = "/_synapse/admin/v1/users/@unknown_person:test/pushers"
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_user_is_not_local(self):
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = "/_synapse/admin/v1/users/@unknown_person:unknown_domain/pushers"

        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_get_pushers(self):
        """
        Tests that a normal lookup for pushers is successfully
        """

        # Get pushers
        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(0, channel.json_body["total"])

        # Register the pusher
        other_user_token = self.login("user", "pass")
        user_tuple = self.get_success(
            self.store.get_user_by_access_token(other_user_token)
        )
        token_id = user_tuple.token_id

        self.get_success(
            self.hs.get_pusherpool().add_pusher(
                user_id=self.other_user,
                access_token=token_id,
                kind="http",
                app_id="m.http",
                app_display_name="HTTP Push Notifications",
                device_display_name="pushy push",
                pushkey="a@example.com",
                lang=None,
                data={"url": "example.com"},
            )
        )

        # Get pushers
        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(1, channel.json_body["total"])

        for p in channel.json_body["pushers"]:
            self.assertIn("pushkey", p)
            self.assertIn("kind", p)
            self.assertIn("app_id", p)
            self.assertIn("app_display_name", p)
            self.assertIn("device_display_name", p)
            self.assertIn("profile_tag", p)
            self.assertIn("lang", p)
            self.assertIn("url", p["data"])


class UserMediaRestTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()
        self.media_repo = hs.get_media_repository_resource()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.url = "/_synapse/admin/v1/users/%s/media" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self):
        """
        Try to list media of an user without authentication.
        """
        request, channel = self.make_request("GET", self.url, b"{}")

        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self):
        """
        If the user is not a server admin, an error is returned.
        """
        other_user_token = self.login("user", "pass")

        request, channel = self.make_request(
            "GET", self.url, access_token=other_user_token,
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_does_not_exist(self):
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = "/_synapse/admin/v1/users/@unknown_person:test/media"
        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_user_is_not_local(self):
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = "/_synapse/admin/v1/users/@unknown_person:unknown_domain/media"

        request, channel = self.make_request(
            "GET", url, access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_limit(self):
        """
        Testing list of media with limit
        """

        number_media = 20
        other_user_tok = self.login("user", "pass")
        self._create_media(other_user_tok, number_media)

        request, channel = self.make_request(
            "GET", self.url + "?limit=5", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), 5)
        self.assertEqual(channel.json_body["next_token"], 5)
        self._check_fields(channel.json_body["media"])

    def test_from(self):
        """
        Testing list of media with a defined starting point (from)
        """

        number_media = 20
        other_user_tok = self.login("user", "pass")
        self._create_media(other_user_tok, number_media)

        request, channel = self.make_request(
            "GET", self.url + "?from=5", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), 15)
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["media"])

    def test_limit_and_from(self):
        """
        Testing list of media with a defined starting point and limit
        """

        number_media = 20
        other_user_tok = self.login("user", "pass")
        self._create_media(other_user_tok, number_media)

        request, channel = self.make_request(
            "GET", self.url + "?from=5&limit=10", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(channel.json_body["next_token"], 15)
        self.assertEqual(len(channel.json_body["media"]), 10)
        self._check_fields(channel.json_body["media"])

    def test_limit_is_negative(self):
        """
        Testing that a negative limit parameter returns a 400
        """

        request, channel = self.make_request(
            "GET", self.url + "?limit=-5", access_token=self.admin_user_tok,
        )

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def test_from_is_negative(self):
        """
        Testing that a negative from parameter returns a 400
        """

        request, channel = self.make_request(
            "GET", self.url + "?from=-5", access_token=self.admin_user_tok,
        )

        self.assertEqual(400, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.INVALID_PARAM, channel.json_body["errcode"])

    def test_next_token(self):
        """
        Testing that `next_token` appears at the right place
        """

        number_media = 20
        other_user_tok = self.login("user", "pass")
        self._create_media(other_user_tok, number_media)

        #  `next_token` does not appear
        # Number of results is the number of entries
        request, channel = self.make_request(
            "GET", self.url + "?limit=20", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), number_media)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does not appear
        # Number of max results is larger than the number of entries
        request, channel = self.make_request(
            "GET", self.url + "?limit=21", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), number_media)
        self.assertNotIn("next_token", channel.json_body)

        #  `next_token` does appear
        # Number of max results is smaller than the number of entries
        request, channel = self.make_request(
            "GET", self.url + "?limit=19", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), 19)
        self.assertEqual(channel.json_body["next_token"], 19)

        # Check
        # Set `from` to value of `next_token` for request remaining entries
        #  `next_token` does not appear
        request, channel = self.make_request(
            "GET", self.url + "?from=19", access_token=self.admin_user_tok,
        )

        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(channel.json_body["total"], number_media)
        self.assertEqual(len(channel.json_body["media"]), 1)
        self.assertNotIn("next_token", channel.json_body)

    def test_user_has_no_media(self):
        """
        Tests that a normal lookup for media is successfully
        if user has no media created
        """

        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(0, channel.json_body["total"])
        self.assertEqual(0, len(channel.json_body["media"]))

    def test_get_media(self):
        """
        Tests that a normal lookup for media is successfully
        """

        number_media = 5
        other_user_tok = self.login("user", "pass")
        self._create_media(other_user_tok, number_media)

        request, channel = self.make_request(
            "GET", self.url, access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(number_media, channel.json_body["total"])
        self.assertEqual(number_media, len(channel.json_body["media"]))
        self.assertNotIn("next_token", channel.json_body)
        self._check_fields(channel.json_body["media"])

    def _create_media(self, user_token, number_media):
        """
        Create a number of media for a specific user
        """
        upload_resource = self.media_repo.children[b"upload"]
        for i in range(number_media):
            # file size is 67 Byte
            image_data = unhexlify(
                b"89504e470d0a1a0a0000000d4948445200000001000000010806"
                b"0000001f15c4890000000a49444154789c63000100000500010d"
                b"0a2db40000000049454e44ae426082"
            )

            # Upload some media into the room
            self.helper.upload_media(
                upload_resource, image_data, tok=user_token, expect_code=200
            )

    def _check_fields(self, content):
        """Checks that all attributes are present in content
        """
        for m in content:
            self.assertIn("media_id", m)
            self.assertIn("media_type", m)
            self.assertIn("media_length", m)
            self.assertIn("upload_name", m)
            self.assertIn("created_ts", m)
            self.assertIn("last_access_ts", m)
            self.assertIn("quarantined_by", m)
            self.assertIn("safe_from_quarantine", m)


class UserTokenRestTestCase(unittest.HomeserverTestCase):
    """Test for /_synapse/admin/v1/users/<user>/login
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        room.register_servlets,
        devices.register_servlets,
        logout.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_tok = self.login("user", "pass")
        self.url = "/_synapse/admin/v1/users/%s/login" % urllib.parse.quote(
            self.other_user
        )

    def _get_token(self) -> str:
        request, channel = self.make_request(
            "POST", self.url, b"{}", access_token=self.admin_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])
        return channel.json_body["access_token"]

    def test_no_auth(self):
        """Try to login as a user without authentication.
        """
        request, channel = self.make_request("POST", self.url, b"{}")

        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_not_admin(self):
        """Try to login as a user as a non-admin user.
        """
        request, channel = self.make_request(
            "POST", self.url, b"{}", access_token=self.other_user_tok
        )

        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])

    def test_send_event(self):
        """Test that sending event as a user works.
        """
        # Create a room.
        room_id = self.helper.create_room_as(self.other_user, tok=self.other_user_tok)

        # Login in as the user
        puppet_token = self._get_token()

        # Test that sending works, and generates the event as the right user.
        resp = self.helper.send_event(room_id, "com.example.test", tok=puppet_token)
        event_id = resp["event_id"]
        event = self.get_success(self.store.get_event(event_id))
        self.assertEqual(event.sender, self.other_user)

    def test_devices(self):
        """Tests that logging in as a user doesn't create a new device for them.
        """
        # Login in as the user
        self._get_token()

        # Check that we don't see a new device in our devices list
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=self.other_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # We should only see the one device (from the login in `prepare`)
        self.assertEqual(len(channel.json_body["devices"]), 1)

    def test_logout(self):
        """Test that calling `/logout` with the token works.
        """
        # Login in as the user
        puppet_token = self._get_token()

        # Test that we can successfully make a request
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # Logout with the puppet token
        request, channel = self.make_request(
            "POST", "logout", b"{}", access_token=puppet_token
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # The puppet token should no longer work
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])

        # .. but the real user's tokens should still work
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=self.other_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

    def test_user_logout_all(self):
        """Tests that the target user calling `/logout/all` does *not* expire
        the token.
        """
        # Login in as the user
        puppet_token = self._get_token()

        # Test that we can successfully make a request
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # Logout all with the real user token
        request, channel = self.make_request(
            "POST", "logout/all", b"{}", access_token=self.other_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # The puppet token should still work
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # .. but the real user's tokens shouldn't
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=self.other_user_tok
        )
        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])

    def test_admin_logout_all(self):
        """Tests that the admin user calling `/logout/all` does expire the
        token.
        """
        # Login in as the user
        puppet_token = self._get_token()

        # Test that we can successfully make a request
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # Logout all with the admin user token
        request, channel = self.make_request(
            "POST", "logout/all", b"{}", access_token=self.admin_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

        # The puppet token should no longer work
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=puppet_token
        )
        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])

        # .. but the real user's tokens should still work
        request, channel = self.make_request(
            "GET", "devices", b"{}", access_token=self.other_user_tok
        )
        self.assertEqual(200, int(channel.result["code"]), msg=channel.result["body"])

    @unittest.override_config(
        {
            "public_baseurl": "https://example.org/",
            "user_consent": {
                "version": "1.0",
                "policy_name": "My Cool Privacy Policy",
                "template_dir": "/",
                "require_at_registration": True,
                "block_events_error": "You should accept the policy",
            },
            "form_secret": "123secret",
        }
    )
    def test_consent(self):
        """Test that sending a message is not subject to the privacy policies.
        """
        # Have the admin user accept the terms.
        self.get_success(self.store.user_set_consent_version(self.admin_user, "1.0"))

        # First, cheekily accept the terms and create a room
        self.get_success(self.store.user_set_consent_version(self.other_user, "1.0"))
        room_id = self.helper.create_room_as(self.other_user, tok=self.other_user_tok)
        self.helper.send_event(room_id, "com.example.test", tok=self.other_user_tok)

        # Now unaccept it and check that we can't send an event
        self.get_success(self.store.user_set_consent_version(self.other_user, "0.0"))
        self.helper.send_event(
            room_id, "com.example.test", tok=self.other_user_tok, expect_code=403
        )

        # Login in as the user
        puppet_token = self._get_token()

        # Sending an event on their behalf should work fine
        self.helper.send_event(room_id, "com.example.test", tok=puppet_token)

    @override_config(
        {"limit_usage_by_mau": True, "max_mau_value": 1, "mau_trial_days": 0}
    )
    def test_mau_limit(self):
        # Create a room as the admin user. This will bump the monthly active users to 1.
        room_id = self.helper.create_room_as(self.admin_user, tok=self.admin_user_tok)

        # Trying to join as the other user should fail due to reaching MAU limit.
        self.helper.join(
            room_id, user=self.other_user, tok=self.other_user_tok, expect_code=403
        )

        # Logging in as the other user and joining a room should work, even
        # though the MAU limit would stop the user doing so.
        puppet_token = self._get_token()
        self.helper.join(room_id, user=self.other_user, tok=puppet_token)


class WhoisRestTestCase(unittest.HomeserverTestCase):

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.url1 = "/_synapse/admin/v1/whois/%s" % urllib.parse.quote(self.other_user)
        self.url2 = "/_matrix/client/r0/admin/whois/%s" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self):
        """
        Try to get information of an user without authentication.
        """
        request, channel = self.make_request("GET", self.url1, b"{}")
        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

        request, channel = self.make_request("GET", self.url2, b"{}")
        self.assertEqual(401, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_not_admin(self):
        """
        If the user is not a server admin, an error is returned.
        """
        self.register_user("user2", "pass")
        other_user2_token = self.login("user2", "pass")

        request, channel = self.make_request(
            "GET", self.url1, access_token=other_user2_token,
        )
        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

        request, channel = self.make_request(
            "GET", self.url2, access_token=other_user2_token,
        )
        self.assertEqual(403, int(channel.result["code"]), msg=channel.result["body"])
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_is_not_local(self):
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url1 = "/_synapse/admin/v1/whois/@unknown_person:unknown_domain"
        url2 = "/_matrix/client/r0/admin/whois/@unknown_person:unknown_domain"

        request, channel = self.make_request(
            "GET", url1, access_token=self.admin_user_tok,
        )
        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only whois a local user", channel.json_body["error"])

        request, channel = self.make_request(
            "GET", url2, access_token=self.admin_user_tok,
        )
        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only whois a local user", channel.json_body["error"])

    def test_get_whois_admin(self):
        """
        The lookup should succeed for an admin.
        """
        request, channel = self.make_request(
            "GET", self.url1, access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(self.other_user, channel.json_body["user_id"])
        self.assertIn("devices", channel.json_body)

        request, channel = self.make_request(
            "GET", self.url2, access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(self.other_user, channel.json_body["user_id"])
        self.assertIn("devices", channel.json_body)

    def test_get_whois_user(self):
        """
        The lookup should succeed for a normal user looking up their own information.
        """
        other_user_token = self.login("user", "pass")

        request, channel = self.make_request(
            "GET", self.url1, access_token=other_user_token,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(self.other_user, channel.json_body["user_id"])
        self.assertIn("devices", channel.json_body)

        request, channel = self.make_request(
            "GET", self.url2, access_token=other_user_token,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(self.other_user, channel.json_body["user_id"])
        self.assertIn("devices", channel.json_body)
