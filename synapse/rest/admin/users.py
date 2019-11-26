# -*- coding: utf-8 -*-
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
import hashlib
import hmac
import logging
import re

from six import text_type
from six.moves import http_client

from synapse.api.constants import UserTypes
from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.rest.admin._base import (
    assert_requester_is_admin,
    assert_user_is_admin,
    historical_admin_path_patterns,
)
from synapse.types import UserID

logger = logging.getLogger(__name__)


class UsersRestServlet(RestServlet):
    PATTERNS = historical_admin_path_patterns("/users/(?P<user_id>[^/]*)$")

    def __init__(self, hs):
        self.hs = hs
        self.auth = hs.get_auth()
        self.admin_handler = hs.get_handlers().admin_handler

    async def on_GET(self, request, user_id):
        target_user = UserID.from_string(user_id)
        await assert_requester_is_admin(self.auth, request)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only users a local user")

        ret = await self.admin_handler.get_users()

        return 200, ret


class GetUsersPaginatedRestServlet(RestServlet):
    """Get request to get specific number of users from Synapse.
    This needs user to have administrator access in Synapse.
        Example:
            http://localhost:8008/_synapse/admin/v1/users_paginate/
            @admin:user?access_token=admin_access_token&start=0&limit=10
        Returns:
            200 OK with json object {list[dict[str, Any]], count} or empty object.
        """

    PATTERNS = historical_admin_path_patterns(
        "/users_paginate/(?P<target_user_id>[^/]*)"
    )

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.hs = hs
        self.auth = hs.get_auth()
        self.handlers = hs.get_handlers()

    async def on_GET(self, request, target_user_id):
        """Get request to get specific number of users from Synapse.
        This needs user to have administrator access in Synapse.
        """
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(target_user_id)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only users a local user")

        order = "name"  # order by name in user table
        start = parse_integer(request, "start", required=True)
        limit = parse_integer(request, "limit", required=True)

        logger.info("limit: %s, start: %s", limit, start)

        ret = await self.handlers.admin_handler.get_users_paginate(order, start, limit)
        return 200, ret

    async def on_POST(self, request, target_user_id):
        """Post request to get specific number of users from Synapse..
        This needs user to have administrator access in Synapse.
        Example:
            http://localhost:8008/_synapse/admin/v1/users_paginate/
            @admin:user?access_token=admin_access_token
        JsonBodyToSend:
            {
                "start": "0",
                "limit": "10
            }
        Returns:
            200 OK with json object {list[dict[str, Any]], count} or empty object.
        """
        await assert_requester_is_admin(self.auth, request)
        UserID.from_string(target_user_id)

        order = "name"  # order by name in user table
        params = parse_json_object_from_request(request)
        assert_params_in_dict(params, ["limit", "start"])
        limit = params["limit"]
        start = params["start"]
        logger.info("limit: %s, start: %s", limit, start)

        ret = await self.handlers.admin_handler.get_users_paginate(order, start, limit)
        return 200, ret


class UserRegisterServlet(RestServlet):
    """
    Attributes:
         NONCE_TIMEOUT (int): Seconds until a generated nonce won't be accepted
         nonces (dict[str, int]): The nonces that we will accept. A dict of
             nonce to the time it was generated, in int seconds.
    """

    PATTERNS = historical_admin_path_patterns("/register")
    NONCE_TIMEOUT = 60

    def __init__(self, hs):
        self.handlers = hs.get_handlers()
        self.reactor = hs.get_reactor()
        self.nonces = {}
        self.hs = hs

    def _clear_old_nonces(self):
        """
        Clear out old nonces that are older than NONCE_TIMEOUT.
        """
        now = int(self.reactor.seconds())

        for k, v in list(self.nonces.items()):
            if now - v > self.NONCE_TIMEOUT:
                del self.nonces[k]

    def on_GET(self, request):
        """
        Generate a new nonce.
        """
        self._clear_old_nonces()

        nonce = self.hs.get_secrets().token_hex(64)
        self.nonces[nonce] = int(self.reactor.seconds())
        return 200, {"nonce": nonce}

    async def on_POST(self, request):
        self._clear_old_nonces()

        if not self.hs.config.registration_shared_secret:
            raise SynapseError(400, "Shared secret registration is not enabled")

        body = parse_json_object_from_request(request)

        if "nonce" not in body:
            raise SynapseError(400, "nonce must be specified", errcode=Codes.BAD_JSON)

        nonce = body["nonce"]

        if nonce not in self.nonces:
            raise SynapseError(400, "unrecognised nonce")

        # Delete the nonce, so it can't be reused, even if it's invalid
        del self.nonces[nonce]

        if "username" not in body:
            raise SynapseError(
                400, "username must be specified", errcode=Codes.BAD_JSON
            )
        else:
            if (
                not isinstance(body["username"], text_type)
                or len(body["username"]) > 512
            ):
                raise SynapseError(400, "Invalid username")

            username = body["username"].encode("utf-8")
            if b"\x00" in username:
                raise SynapseError(400, "Invalid username")

        if "password" not in body:
            raise SynapseError(
                400, "password must be specified", errcode=Codes.BAD_JSON
            )
        else:
            if (
                not isinstance(body["password"], text_type)
                or len(body["password"]) > 512
            ):
                raise SynapseError(400, "Invalid password")

            password = body["password"].encode("utf-8")
            if b"\x00" in password:
                raise SynapseError(400, "Invalid password")

        admin = body.get("admin", None)
        user_type = body.get("user_type", None)

        if user_type is not None and user_type not in UserTypes.ALL_USER_TYPES:
            raise SynapseError(400, "Invalid user type")

        got_mac = body["mac"]

        want_mac = hmac.new(
            key=self.hs.config.registration_shared_secret.encode(),
            digestmod=hashlib.sha1,
        )
        want_mac.update(nonce.encode("utf8"))
        want_mac.update(b"\x00")
        want_mac.update(username)
        want_mac.update(b"\x00")
        want_mac.update(password)
        want_mac.update(b"\x00")
        want_mac.update(b"admin" if admin else b"notadmin")
        if user_type:
            want_mac.update(b"\x00")
            want_mac.update(user_type.encode("utf8"))
        want_mac = want_mac.hexdigest()

        if not hmac.compare_digest(want_mac.encode("ascii"), got_mac.encode("ascii")):
            raise SynapseError(403, "HMAC incorrect")

        # Reuse the parts of RegisterRestServlet to reduce code duplication
        from synapse.rest.client.v2_alpha.register import RegisterRestServlet

        register = RegisterRestServlet(self.hs)

        user_id = await register.registration_handler.register_user(
            localpart=body["username"].lower(),
            password=body["password"],
            admin=bool(admin),
            user_type=user_type,
        )

        result = await register._create_registration_details(user_id, body)
        return 200, result


class WhoisRestServlet(RestServlet):
    PATTERNS = historical_admin_path_patterns("/whois/(?P<user_id>[^/]*)")

    def __init__(self, hs):
        self.hs = hs
        self.auth = hs.get_auth()
        self.handlers = hs.get_handlers()

    async def on_GET(self, request, user_id):
        target_user = UserID.from_string(user_id)
        requester = await self.auth.get_user_by_req(request)
        auth_user = requester.user

        if target_user != auth_user:
            await assert_user_is_admin(self.auth, auth_user)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only whois a local user")

        ret = await self.handlers.admin_handler.get_whois(target_user)

        return 200, ret


class DeactivateAccountRestServlet(RestServlet):
    PATTERNS = historical_admin_path_patterns("/deactivate/(?P<target_user_id>[^/]*)")

    def __init__(self, hs):
        self._deactivate_account_handler = hs.get_deactivate_account_handler()
        self.auth = hs.get_auth()

    async def on_POST(self, request, target_user_id):
        await assert_requester_is_admin(self.auth, request)
        body = parse_json_object_from_request(request, allow_empty_body=True)
        erase = body.get("erase", False)
        if not isinstance(erase, bool):
            raise SynapseError(
                http_client.BAD_REQUEST,
                "Param 'erase' must be a boolean, if given",
                Codes.BAD_JSON,
            )

        UserID.from_string(target_user_id)

        result = await self._deactivate_account_handler.deactivate_account(
            target_user_id, erase
        )
        if result:
            id_server_unbind_result = "success"
        else:
            id_server_unbind_result = "no-support"

        return 200, {"id_server_unbind_result": id_server_unbind_result}


class AccountValidityRenewServlet(RestServlet):
    PATTERNS = historical_admin_path_patterns("/account_validity/validity$")

    def __init__(self, hs):
        """
        Args:
            hs (synapse.server.HomeServer): server
        """
        self.hs = hs
        self.account_activity_handler = hs.get_account_validity_handler()
        self.auth = hs.get_auth()

    async def on_POST(self, request):
        await assert_requester_is_admin(self.auth, request)

        body = parse_json_object_from_request(request)

        if "user_id" not in body:
            raise SynapseError(400, "Missing property 'user_id' in the request body")

        expiration_ts = await self.account_activity_handler.renew_account_for_user(
            body["user_id"],
            body.get("expiration_ts"),
            not body.get("enable_renewal_emails", True),
        )

        res = {"expiration_ts": expiration_ts}
        return 200, res


class ResetPasswordRestServlet(RestServlet):
    """Post request to allow an administrator reset password for a user.
    This needs user to have administrator access in Synapse.
        Example:
            http://localhost:8008/_synapse/admin/v1/reset_password/
            @user:to_reset_password?access_token=admin_access_token
        JsonBodyToSend:
            {
                "new_password": "secret"
            }
        Returns:
            200 OK with empty object if success otherwise an error.
        """

    PATTERNS = historical_admin_path_patterns(
        "/reset_password/(?P<target_user_id>[^/]*)"
    )

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.hs = hs
        self.auth = hs.get_auth()
        self._set_password_handler = hs.get_set_password_handler()

    async def on_POST(self, request, target_user_id):
        """Post request to allow an administrator reset password for a user.
        This needs user to have administrator access in Synapse.
        """
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester.user)

        UserID.from_string(target_user_id)

        params = parse_json_object_from_request(request)
        assert_params_in_dict(params, ["new_password"])
        new_password = params["new_password"]

        await self._set_password_handler.set_password(
            target_user_id, new_password, requester
        )
        return 200, {}


class SearchUsersRestServlet(RestServlet):
    """Get request to search user table for specific users according to
    search term.
    This needs user to have administrator access in Synapse.
        Example:
            http://localhost:8008/_synapse/admin/v1/search_users/
            @admin:user?access_token=admin_access_token&term=alice
        Returns:
            200 OK with json object {list[dict[str, Any]], count} or empty object.
    """

    PATTERNS = historical_admin_path_patterns("/search_users/(?P<target_user_id>[^/]*)")

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.hs = hs
        self.auth = hs.get_auth()
        self.handlers = hs.get_handlers()

    async def on_GET(self, request, target_user_id):
        """Get request to search user table for specific users according to
        search term.
        This needs user to have a administrator access in Synapse.
        """
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(target_user_id)

        # To allow all users to get the users list
        # if not is_admin and target_user != auth_user:
        #     raise AuthError(403, "You are not a server admin")

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only users a local user")

        term = parse_string(request, "term", required=True)
        logger.info("term: %s ", term)

        ret = await self.handlers.admin_handler.search_users(term)
        return 200, ret


class UserAdminServlet(RestServlet):
    """
    Get or set whether or not a user is a server administrator.

    Note that only local users can be server administrators, and that an
    administrator may not demote themselves.

    Only server administrators can use this API.

    Examples:
        * Get
            GET /_synapse/admin/v1/users/@nonadmin:example.com/admin
            response on success:
                {
                    "admin": false
                }
        * Set
            PUT /_synapse/admin/v1/users/@reivilibre:librepush.net/admin
            request body:
                {
                    "admin": true
                }
            response on success:
                {}
    """

    PATTERNS = (re.compile("^/_synapse/admin/v1/users/(?P<user_id>@[^/]*)/admin$"),)

    def __init__(self, hs):
        self.hs = hs
        self.auth = hs.get_auth()
        self.handlers = hs.get_handlers()

    async def on_GET(self, request, user_id):
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Only local users can be admins of this homeserver")

        is_admin = await self.handlers.admin_handler.get_user_server_admin(target_user)
        is_admin = bool(is_admin)

        return 200, {"admin": is_admin}

    async def on_PUT(self, request, user_id):
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester.user)
        auth_user = requester.user

        target_user = UserID.from_string(user_id)

        body = parse_json_object_from_request(request)

        assert_params_in_dict(body, ["admin"])

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Only local users can be admins of this homeserver")

        set_admin_to = bool(body["admin"])

        if target_user == auth_user and not set_admin_to:
            raise SynapseError(400, "You may not demote yourself.")

        await self.handlers.admin_handler.set_user_server_admin(
            target_user, set_admin_to
        )

        return 200, {}
