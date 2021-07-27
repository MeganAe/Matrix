# Copyright 2021 Callum Brown
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
import random
import string
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.servlet import (
    RestServlet,
    parse_boolean,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ListRegistrationTokensRestServlet(RestServlet):
    """List registration tokens.

    To list all tokens:

        GET /_synapse/admin/v1/registration_tokens

        200 OK

        {
            "registration_tokens": [
                {
                    "token": "abcd",
                    "uses_allowed": 3,
                    "pending": 0,
                    "completed": 1,
                    "expiry_time": null
                },
                {
                    "token": "wxyz",
                    "uses_allowed": null,
                    "pending": 0,
                    "completed": 9,
                    "expiry_time": 1625394937000
                }
            ]
        }

    The optional query parameter `valid` is used to the response to only
    valid or invalid tokens. It can be `true` or `false`.
    """

    PATTERNS = admin_patterns("/registration_tokens$")

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        valid = parse_boolean(request, "valid")
        token_list = await self.store.get_registration_tokens(valid)
        return 200, {"registration_tokens": token_list}


class NewRegistrationTokenRestServlet(RestServlet):
    """Create a new registration token.

    For example, to create a token specifying some fields:

        POST /_synapse/admin/v1/registration_tokens/new

        {
            "token": "defg",
            "uses_allowed": 1
        }

        200 OK

        {
            "token": "defg",
            "uses_allowed": 1,
            "pending": 0,
            "completed": 0,
            "expiry_time": null
        }

    Defaults are used for any fields not specified.
    """

    PATTERNS = admin_patterns("/registration_tokens/new$")

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        # A string of all the characters allowed to be in a registration_token
        self.allowed_chars = string.ascii_letters + string.digits + "-_"
        self.allowed_chars_set = set(self.allowed_chars)

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        body = parse_json_object_from_request(request)

        if "token" in body:
            token = body["token"]
            if not isinstance(token, str):
                raise SynapseError(400, "token must be a string", Codes.INVALID_PARAM)
            if not (0 < len(token) <= 64):
                raise SynapseError(
                    400,
                    "token must not be empty and must not be longer than 64 characters",
                    Codes.INVALID_PARAM,
                )
            if not set(token).issubset(self.allowed_chars_set):
                raise SynapseError(
                    400,
                    "token must consist only of characters matched by the regex [A-Za-z0-9-_]",
                    Codes.INVALID_PARAM,
                )

        else:
            # Get length of token to generate (default is 16)
            length = body.get("length", 16)
            if not isinstance(length, int):
                raise SynapseError(
                    400, "length must be an integer", Codes.INVALID_PARAM
                )
            if not (0 < length <= 64):
                raise SynapseError(
                    400,
                    "length must be greater than zero and not greater than 64",
                    Codes.INVALID_PARAM,
                )

            # Generate token
            token = "".join(random.choices(self.allowed_chars, k=length))

        uses_allowed = body.get("uses_allowed", None)
        if not (
            uses_allowed is None
            or (isinstance(uses_allowed, int) and uses_allowed >= 0)
        ):
            raise SynapseError(
                400,
                "uses_allowed must be a non-negative integer or null",
                Codes.INVALID_PARAM,
            )

        expiry_time = body.get("expiry_time", None)
        if not isinstance(expiry_time, (int, type(None))):
            raise SynapseError(
                400, "expiry_time must be an integer or null", Codes.INVALID_PARAM
            )
        if isinstance(expiry_time, int) and expiry_time < self.clock.time_msec():
            raise SynapseError(
                400, "expiry_time must not be in the past", Codes.INVALID_PARAM
            )

        res = await self.store.create_registration_token(
            token, uses_allowed, expiry_time
        )
        if not res:
            # Creation failed, probably the token already exists
            raise SynapseError(
                400, f"Token already exists: {token}", Codes.INVALID_PARAM
            )

        resp = {
            "token": token,
            "uses_allowed": uses_allowed,
            "pending": 0,
            "completed": 0,
            "expiry_time": expiry_time,
        }
        return 200, resp


class RegistrationTokenRestServlet(RestServlet):
    """Retrieve, update, or delete the given token.

    For example,

    to retrieve a token:

        GET /_synapse/admin/v1/registration_tokens/abcd

        200 OK

        {
            "token": "abcd",
            "uses_allowed": 3,
            "pending": 0,
            "completed": 1,
            "expiry_time": null
        }


    to update a token:

        PUT /_synapse/admin/v1/registration_tokens/defg

        {
            "expiry_time": 4781243146000
        }

        200 OK

        {
            "token": "defg",
            "uses_allowed": 1,
            "pending": 0,
            "completed": 0,
            "expiry_time": 4781243146000
        }


    to delete a token:

        DELETE /_synapse/admin/v1/registration_tokens/wxyz

        200 OK

        {}
    """

    PATTERNS = admin_patterns("/registration_tokens/(?P<token>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()
        self.store = hs.get_datastore()

    async def on_GET(self, request: SynapseRequest, token: str) -> Tuple[int, JsonDict]:
        """Retrieve a registration token."""
        await assert_requester_is_admin(self.auth, request)
        token_info = await self.store.get_one_registration_token(token)

        # If no result return a 404
        if token_info is None:
            raise NotFoundError(f"No such registration token: {token}")

        return 200, token_info

    async def on_PUT(self, request: SynapseRequest, token: str) -> Tuple[int, JsonDict]:
        """Update a registration token."""
        await assert_requester_is_admin(self.auth, request)
        body = parse_json_object_from_request(request)
        new_attributes = {}

        # Only add uses_allowed to new_attributes if it is present and valid
        if "uses_allowed" in body:
            ua = body["uses_allowed"]
            if not (ua is None or (isinstance(ua, int) and ua >= 0)):
                raise SynapseError(
                    400,
                    "uses_allowed must be a non-negative integer or null",
                    Codes.INVALID_PARAM,
                )
            new_attributes["uses_allowed"] = ua

        if "expiry_time" in body:
            et = body["expiry_time"]
            if not isinstance(et, (int, type(None))):
                raise SynapseError(
                    400, "expiry_time must be an integer or null", Codes.INVALID_PARAM
                )
            if isinstance(et, int) and et < self.clock.time_msec():
                raise SynapseError(
                    400, "expiry_time must not be in the past", Codes.INVALID_PARAM
                )
            new_attributes["expiry_time"] = et

        if len(new_attributes) == 0:
            # Nothing to update, get token info to return
            token_info = await self.store.get_one_registration_token(token)
        else:
            token_info = await self.store.update_registration_token(
                token, new_attributes
            )

        # If no result return a 404
        if token_info is None:
            raise NotFoundError(f"No such registration token: {token}")

        return 200, token_info

    async def on_DELETE(
        self, request: SynapseRequest, token: str
    ) -> Tuple[int, JsonDict]:
        """Delete a registration token."""
        await assert_requester_is_admin(self.auth, request)

        if await self.store.delete_registration_token(token):
            return 200, {}

        raise NotFoundError(f"No such registration token: {token}")
