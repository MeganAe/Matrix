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
from synapse.api.errors import SynapseError, Codes, UnrecognizedRequestError
from synapse.http.servlet import RestServlet

from ._base import client_v2_pattern, parse_json_dict_from_request

import logging
import hmac
from hashlib import sha1
from synapse.util.async import run_on_reactor


# We ought to be using hmac.compare_digest() but on older pythons it doesn't
# exist. It's a _really minor_ security flaw to use plain string comparison
# because the timing attack is so obscured by all the other code here it's
# unlikely to make much difference
if hasattr(hmac, "compare_digest"):
    compare_digest = hmac.compare_digest
else:
    compare_digest = lambda a, b: a == b


logger = logging.getLogger(__name__)


class RegisterRestServlet(RestServlet):
    PATTERN = client_v2_pattern("/register")

    def __init__(self, hs):
        super(RegisterRestServlet, self).__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_handlers().auth_handler
        self.registration_handler = hs.get_handlers().registration_handler
        self.identity_handler = hs.get_handlers().identity_handler

    @defer.inlineCallbacks
    def on_POST(self, request):
        yield run_on_reactor()

        kind = "user"
        if "kind" in request.args:
            kind = request.args["kind"][0]

        if kind == "guest":
            ret = yield self._do_guest_registration()
            defer.returnValue(ret)
            return
        elif kind != "user":
            raise UnrecognizedRequestError(
                "Do not understand membership kind: %s" % (kind,)
            )

        if '/register/email/requestToken' in request.path:
            ret = yield self.onEmailTokenRequest(request)
            defer.returnValue(ret)

        body = parse_json_dict_from_request(request)

        # we do basic sanity checks here because the auth layer will store these
        # in sessions. Pull out the username/password provided to us.
        desired_password = None
        if 'password' in body:
            if (not isinstance(body['password'], basestring) or
                    len(body['password']) > 512):
                raise SynapseError(400, "Invalid password")
            desired_password = body["password"]

        desired_username = None
        if 'username' in body:
            if (not isinstance(body['username'], basestring) or
                    len(body['username']) > 512):
                raise SynapseError(400, "Invalid username")
            desired_username = body['username']

        appservice = None
        if 'access_token' in request.args:
            appservice = yield self.auth.get_appservice_by_req(request)

        # fork off as soon as possible for ASes and shared secret auth which
        # have completely different registration flows to normal users

        # == Application Service Registration ==
        if appservice:
            result = yield self._do_appservice_registration(
                desired_username, request.args["access_token"][0]
            )
            defer.returnValue((200, result))  # we throw for non 200 responses
            return

        # == Shared Secret Registration == (e.g. create new user scripts)
        if 'mac' in body:
            # FIXME: Should we really be determining if this is shared secret
            # auth based purely on the 'mac' key?
            result = yield self._do_shared_secret_registration(
                desired_username, desired_password, body["mac"]
            )
            defer.returnValue((200, result))  # we throw for non 200 responses
            return

        # == Normal User Registration == (everyone else)
        if self.hs.config.disable_registration:
            raise SynapseError(403, "Registration has been disabled")

        if desired_username is not None:
            yield self.registration_handler.check_username(desired_username)

        if self.hs.config.enable_registration_captcha:
            flows = [
                [LoginType.RECAPTCHA],
                [LoginType.EMAIL_IDENTITY, LoginType.RECAPTCHA]
            ]
        else:
            flows = [
                [LoginType.DUMMY],
                [LoginType.EMAIL_IDENTITY]
            ]

        authed, result, params = yield self.auth_handler.check_auth(
            flows, body, self.hs.get_ip_from_request(request)
        )

        if not authed:
            defer.returnValue((401, result))
            return

        # NB: This may be from the auth handler and NOT from the POST
        if 'password' not in params:
            raise SynapseError(400, "Missing password.", Codes.MISSING_PARAM)

        desired_username = params.get("username", None)
        new_password = params.get("password", None)

        (user_id, token) = yield self.registration_handler.register(
            localpart=desired_username,
            password=new_password
        )

        if result and LoginType.EMAIL_IDENTITY in result:
            threepid = result[LoginType.EMAIL_IDENTITY]

            for reqd in ['medium', 'address', 'validated_at']:
                if reqd not in threepid:
                    logger.info("Can't add incomplete 3pid")
                else:
                    yield self.auth_handler.add_threepid(
                        user_id,
                        threepid['medium'],
                        threepid['address'],
                        threepid['validated_at'],
                    )

            if 'bind_email' in params and params['bind_email']:
                logger.info("bind_email specified: binding")

                emailThreepid = result[LoginType.EMAIL_IDENTITY]
                threepid_creds = emailThreepid['threepid_creds']
                logger.debug("Binding emails %s to %s" % (
                    emailThreepid, user_id
                ))
                yield self.identity_handler.bind_threepid(threepid_creds, user_id)
            else:
                logger.info("bind_email not specified: not binding email")

        result = self._create_registration_details(user_id, token)
        defer.returnValue((200, result))

    def on_OPTIONS(self, _):
        return 200, {}

    @defer.inlineCallbacks
    def _do_appservice_registration(self, username, as_token):
        (user_id, token) = yield self.registration_handler.appservice_register(
            username, as_token
        )
        defer.returnValue(self._create_registration_details(user_id, token))

    @defer.inlineCallbacks
    def _do_shared_secret_registration(self, username, password, mac):
        if not self.hs.config.registration_shared_secret:
            raise SynapseError(400, "Shared secret registration is not enabled")

        user = username.encode("utf-8")

        # str() because otherwise hmac complains that 'unicode' does not
        # have the buffer interface
        got_mac = str(mac)

        want_mac = hmac.new(
            key=self.hs.config.registration_shared_secret,
            msg=user,
            digestmod=sha1,
        ).hexdigest()

        if not compare_digest(want_mac, got_mac):
            raise SynapseError(
                403, "HMAC incorrect",
            )

        (user_id, token) = yield self.registration_handler.register(
            localpart=username, password=password
        )
        defer.returnValue(self._create_registration_details(user_id, token))

    def _create_registration_details(self, user_id, token):
        return {
            "user_id": user_id,
            "access_token": token,
            "home_server": self.hs.hostname,
        }

    @defer.inlineCallbacks
    def onEmailTokenRequest(self, request):
        body = parse_json_dict_from_request(request)

        required = ['id_server', 'client_secret', 'email', 'send_attempt']
        absent = []
        for k in required:
            if k not in body:
                absent.append(k)

        if len(absent) > 0:
            raise SynapseError(400, "Missing params: %r" % absent, Codes.MISSING_PARAM)

        existingUid = yield self.hs.get_datastore().get_user_id_by_threepid(
            'email', body['email']
        )

        if existingUid is not None:
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        ret = yield self.identity_handler.requestEmailToken(**body)
        defer.returnValue((200, ret))

    @defer.inlineCallbacks
    def _do_guest_registration(self):
        if not self.hs.config.allow_guest_access:
            defer.returnValue((403, "Guest access is disabled"))
        user_id, _ = yield self.registration_handler.register(generate_token=False)
        access_token = self.auth_handler.generate_access_token(user_id, ["guest = true"])
        defer.returnValue((200, {
            "user_id": user_id,
            "access_token": access_token,
            "home_server": self.hs.hostname,
        }))


def register_servlets(hs, http_server):
    RegisterRestServlet(hs).register(http_server)
