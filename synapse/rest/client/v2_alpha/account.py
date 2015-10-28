# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
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

from synapse.api.constants import LoginType
from synapse.api.errors import LoginError, SynapseError, Codes
from synapse.http.servlet import RestServlet
from synapse.util.async import run_on_reactor

from ._base import client_v2_pattern, parse_json_dict_from_request

import logging


logger = logging.getLogger(__name__)


class PasswordRestServlet(RestServlet):
    PATTERN = client_v2_pattern("/account/password")

    def __init__(self, hs):
        super(PasswordRestServlet, self).__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_handlers().auth_handler

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        body = parse_json_dict_from_request(request)

        authed, result, params = yield self.auth_handler.check_auth([
            [LoginType.PASSWORD],
            [LoginType.EMAIL_IDENTITY]
        ], body, self.hs.get_ip_from_request(request))

        if not authed:
            defer.returnValue((401, result))

        user_id = None

        if LoginType.PASSWORD in result:
            # if using password, they should also be logged in
            auth_user, _, _ = yield self.auth.get_user_by_req(request)
            if auth_user.to_string() != result[LoginType.PASSWORD]:
                raise LoginError(400, "", Codes.UNKNOWN)
            user_id = auth_user.to_string()
        elif LoginType.EMAIL_IDENTITY in result:
            threepid = result[LoginType.EMAIL_IDENTITY]
            if 'medium' not in threepid or 'address' not in threepid:
                raise SynapseError(500, "Malformed threepid")
            # if using email, we must know about the email they're authing with!
            threepid_user_id = yield self.hs.get_datastore().get_user_id_by_threepid(
                threepid['medium'], threepid['address']
            )
            if not threepid_user_id:
                raise SynapseError(404, "Email address not found", Codes.NOT_FOUND)
            user_id = threepid_user_id
        else:
            logger.error("Auth succeeded but no known type!", result.keys())
            raise SynapseError(500, "", Codes.UNKNOWN)

        if 'new_password' not in params:
            raise SynapseError(400, "", Codes.MISSING_PARAM)
        new_password = params['new_password']

        yield self.auth_handler.set_password(
            user_id, new_password
        )

        defer.returnValue((200, {}))

    def on_OPTIONS(self, _):
        return 200, {}


class ThreepidRestServlet(RestServlet):
    PATTERN = client_v2_pattern("/account/3pid")

    def __init__(self, hs):
        super(ThreepidRestServlet, self).__init__()
        self.hs = hs
        self.identity_handler = hs.get_handlers().identity_handler
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_handlers().auth_handler

    @defer.inlineCallbacks
    def on_GET(self, request):
        yield run_on_reactor()

        auth_user, _, _ = yield self.auth.get_user_by_req(request)

        threepids = yield self.hs.get_datastore().user_get_threepids(
            auth_user.to_string()
        )

        defer.returnValue((200, {'threepids': threepids}))

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        body = parse_json_dict_from_request(request)

        if 'threePidCreds' not in body:
            raise SynapseError(400, "Missing param", Codes.MISSING_PARAM)
        threePidCreds = body['threePidCreds']

        auth_user, _, _ = yield self.auth.get_user_by_req(request)

        threepid = yield self.identity_handler.threepid_from_creds(threePidCreds)

        if not threepid:
            raise SynapseError(
                400, "Failed to auth 3pid", Codes.THREEPID_AUTH_FAILED
            )

        for reqd in ['medium', 'address', 'validated_at']:
            if reqd not in threepid:
                logger.warn("Couldn't add 3pid: invalid response from ID sevrer")
                raise SynapseError(500, "Invalid response from ID Server")

        yield self.auth_handler.add_threepid(
            auth_user.to_string(),
            threepid['medium'],
            threepid['address'],
            threepid['validated_at'],
        )

        if 'bind' in body and body['bind']:
            logger.debug(
                "Binding emails %s to %s",
                threepid, auth_user.to_string()
            )
            yield self.identity_handler.bind_threepid(
                threePidCreds, auth_user.to_string()
            )

        defer.returnValue((200, {}))


def register_servlets(hs, http_server):
    PasswordRestServlet(hs).register(http_server)
    ThreepidRestServlet(hs).register(http_server)
