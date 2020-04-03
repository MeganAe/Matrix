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
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_boolean,
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
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()
        self.admin_handler = hs.get_handlers().admin_handler

    async def on_GET(self, request, user_id):
        target_user = UserID.from_string(user_id)
        await assert_requester_is_admin(self.auth, request)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only users a local user")

        ret = await self.store.get_users()

        return 200, ret


class UsersRestServletV2(RestServlet):
    PATTERNS = (re.compile("^/_synapse/admin/v2/users$"),)

    """Get request to list all local users.
    This needs user to have administrator access in Synapse.

    GET /_synapse/admin/v2/users?from=0&limit=10&guests=false

    returns:
        200 OK with list of users if success otherwise an error.

    The parameters `from` and `limit` are required only for pagination.
    By default, a `limit` of 100 is used.
    The parameter `user_id` can be used to filter by user id.
    The parameter `guests` can be used to exclude guest users.
    The parameter `deactivated` can be used to include deactivated users.
    """

    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()
        self.admin_handler = hs.get_handlers().admin_handler

    async def on_GET(self, request):
        await assert_requester_is_admin(self.auth, request)

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)
        user_id = parse_string(request, "user_id", default=None)
        guests = parse_boolean(request, "guests", default=True)
        deactivated = parse_boolean(request, "deactivated", default=False)

        users = await self.store.get_users_paginate(
            start, limit, user_id, guests, deactivated
        )
        ret = {"users": users}
        if len(users) >= limit:
            ret["next_token"] = str(start + len(users))

        return 200, ret


class UserRestServletV2(RestServlet):
    PATTERNS = (re.compile("^/_synapse/admin/v2/users/(?P<user_id>[^/]+)$"),)

    """Get request to list user details.
    This needs user to have administrator access in Synapse.

    GET /_synapse/admin/v2/users/<user_id>

    returns:
        200 OK with user details if success otherwise an error.

    Put request to allow an administrator to add or modify a user.
    This needs user to have administrator access in Synapse.
    We use PUT instead of POST since we already know the id of the user
    object to create. POST could be used to create guests.

    PUT /_synapse/admin/v2/users/<user_id>
    {
        "password": "secret",
        "displayname": "User"
    }

    returns:
        201 OK with new user object if user was created or
        200 OK with modified user object if user was modified
        otherwise an error.
    """

    def __init__(self, hs):
        self.hs = hs
        self.auth = hs.get_auth()
        self.admin_handler = hs.get_handlers().admin_handler
        self.store = hs.get_datastore()
        self.auth_handler = hs.get_auth_handler()
        self.profile_handler = hs.get_profile_handler()
        self.set_password_handler = hs.get_set_password_handler()
        self.deactivate_account_handler = hs.get_deactivate_account_handler()
        self.registration_handler = hs.get_registration_handler()

    async def on_GET(self, request, user_id):
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Can only lookup local users")

        ret = await self.admin_handler.get_user(target_user)

        if not ret:
            raise NotFoundError("User not found")

        return 200, ret

    async def on_PUT(self, request, user_id):
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester.user)

        target_user = UserID.from_string(user_id)
        body = parse_json_object_from_request(request)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "This endpoint can only be used with local users")

        user = await self.admin_handler.get_user(target_user)
        user_id = target_user.to_string()

        if user:  # modify user
            if "displayname" in body:
                await self.profile_handler.set_displayname(
                    target_user, requester, body["displayname"], True
                )

            if "threepids" in body:
                # check for required parameters for each threepid
                for threepid in body["threepids"]:
                    assert_params_in_dict(threepid, ["medium", "address"])

                # remove old threepids from user
                threepids = await self.store.user_get_threepids(user_id)
                for threepid in threepids:
                    try:
                        await self.auth_handler.delete_threepid(
                            user_id, threepid["medium"], threepid["address"], None
                        )
                    except Exception:
                        logger.exception("Failed to remove threepids")
                        raise SynapseError(500, "Failed to remove threepids")

                # add new threepids to user
                current_time = self.hs.get_clock().time_msec()
                for threepid in body["threepids"]:
                    await self.auth_handler.add_threepid(
                        user_id, threepid["medium"], threepid["address"], current_time
                    )

            if "avatar_url" in body:
                await self.profile_handler.set_avatar_url(
                    target_user, requester, body["avatar_url"], True
                )

            if "admin" in body:
                set_admin_to = bool(body["admin"])
                if set_admin_to != user["admin"]:
                    auth_user = requester.user
                    if target_user == auth_user and not set_admin_to:
                        raise SynapseError(400, "You may not demote yourself.")

                    await self.store.set_server_admin(target_user, set_admin_to)

            if "password" in body:
                if (
                    not isinstance(body["password"], text_type)
                    or len(body["password"]) > 512
                ):
                    raise SynapseError(400, "Invalid password")
                else:
                    new_password = body["password"]
                    logout_devices = True
                    await self.set_password_handler.set_password(
                        target_user.to_string(), new_password, logout_devices, requester
                    )

            if "deactivated" in body:
                deactivate = body["deactivated"]
                if not isinstance(deactivate, bool):
                    raise SynapseError(
                        400, "'deactivated' parameter is not of type boolean"
                    )

                if deactivate and not user["deactivated"]:
                    await self.deactivate_account_handler.deactivate_account(
                        target_user.to_string(), False
                    )

            user = await self.admin_handler.get_user(target_user)
            return 200, user

        else:  # create user
            password = body.get("password")
            if password is not None and (
                not isinstance(body["password"], text_type)
                or len(body["password"]) > 512
            ):
                raise SynapseError(400, "Invalid password")

            admin = body.get("admin", None)
            user_type = body.get("user_type", None)
            displayname = body.get("displayname", None)
            threepids = body.get("threepids", None)

            if user_type is not None and user_type not in UserTypes.ALL_USER_TYPES:
                raise SynapseError(400, "Invalid user type")

            user_id = await self.registration_handler.register_user(
                localpart=target_user.localpart,
                password=password,
                admin=bool(admin),
                default_display_name=displayname,
                user_type=user_type,
            )

            if "threepids" in body:
                # check for required parameters for each threepid
                for threepid in body["threepids"]:
                    assert_params_in_dict(threepid, ["medium", "address"])

                current_time = self.hs.get_clock().time_msec()
                for threepid in body["threepids"]:
                    await self.auth_handler.add_threepid(
                        user_id, threepid["medium"], threepid["address"], current_time
                    )

            if "avatar_url" in body:
                await self.profile_handler.set_avatar_url(
                    user_id, requester, body["avatar_url"], True
                )

            ret = await self.admin_handler.get_user(target_user)

            return 201, ret


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

        want_mac_builder = hmac.new(
            key=self.hs.config.registration_shared_secret.encode(),
            digestmod=hashlib.sha1,
        )
        want_mac_builder.update(nonce.encode("utf8"))
        want_mac_builder.update(b"\x00")
        want_mac_builder.update(username)
        want_mac_builder.update(b"\x00")
        want_mac_builder.update(password)
        want_mac_builder.update(b"\x00")
        want_mac_builder.update(b"admin" if admin else b"notadmin")
        if user_type:
            want_mac_builder.update(b"\x00")
            want_mac_builder.update(user_type.encode("utf8"))

        want_mac = want_mac_builder.hexdigest()

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
        logout_devices = params.get("logout_devices", True)

        await self._set_password_handler.set_password(
            target_user_id, new_password, logout_devices, requester
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
        self.hs = hs
        self.store = hs.get_datastore()
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

        ret = await self.handlers.store.search_users(term)
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

    PATTERNS = (re.compile("^/_synapse/admin/v1/users/(?P<user_id>[^/]*)/admin$"),)

    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()
        self.auth = hs.get_auth()

    async def on_GET(self, request, user_id):
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)

        if not self.hs.is_mine(target_user):
            raise SynapseError(400, "Only local users can be admins of this homeserver")

        is_admin = await self.store.is_server_admin(target_user)

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

        await self.store.set_server_admin(target_user, set_admin_to)

        return 200, {}
