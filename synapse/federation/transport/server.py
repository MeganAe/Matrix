# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

from synapse.api.urls import FEDERATION_PREFIX as PREFIX
from synapse.api.errors import Codes, SynapseError
from synapse.util.logutils import log_function

import functools
import logging
import simplejson as json
import re


logger = logging.getLogger(__name__)


class TransportLayerServer(object):
    """Handles incoming federation HTTP requests"""

    # A method just so we can pass 'self' as the authenticator to the Servlets
    @defer.inlineCallbacks
    def authenticate_request(self, request):
        json_request = {
            "method": request.method,
            "uri": request.uri,
            "destination": self.server_name,
            "signatures": {},
        }

        content = None
        origin = None

        if request.method in ["PUT", "POST"]:
            # TODO: Handle other method types? other content types?
            try:
                content_bytes = request.content.read()
                content = json.loads(content_bytes)
                json_request["content"] = content
            except:
                raise SynapseError(400, "Unable to parse JSON", Codes.BAD_JSON)

        def parse_auth_header(header_str):
            try:
                params = auth.split(" ")[1].split(",")
                param_dict = dict(kv.split("=") for kv in params)

                def strip_quotes(value):
                    if value.startswith("\""):
                        return value[1:-1]
                    else:
                        return value

                origin = strip_quotes(param_dict["origin"])
                key = strip_quotes(param_dict["key"])
                sig = strip_quotes(param_dict["sig"])
                return (origin, key, sig)
            except:
                raise SynapseError(
                    400, "Malformed Authorization header", Codes.UNAUTHORIZED
                )

        auth_headers = request.requestHeaders.getRawHeaders(b"Authorization")

        if not auth_headers:
            raise SynapseError(
                401, "Missing Authorization headers", Codes.UNAUTHORIZED,
            )

        for auth in auth_headers:
            if auth.startswith("X-Matrix"):
                (origin, key, sig) = parse_auth_header(auth)
                json_request["origin"] = origin
                json_request["signatures"].setdefault(origin, {})[key] = sig

        if not json_request["signatures"]:
            raise SynapseError(
                401, "Missing Authorization headers", Codes.UNAUTHORIZED,
            )

        yield self.keyring.verify_json_for_server(origin, json_request)

        logger.info("Request from %s", origin)
        request.authenticated_entity = origin

        defer.returnValue((origin, content))

    @log_function
    def register_received_handler(self, handler):
        """ Register a handler that will be fired when we receive data.

        Args:
            handler (TransportReceivedHandler)
        """
        FederationSendServlet(
            handler,
            authenticator=self,
            ratelimiter=self.ratelimiter,
            server_name=self.server_name,
        ).register(self.server)

    @log_function
    def register_request_handler(self, handler):
        """ Register a handler that will be fired when we get asked for data.

        Args:
            handler (TransportRequestHandler)
        """
        for servletclass in SERVLET_CLASSES:
            servletclass(
                handler,
                authenticator=self,
                ratelimiter=self.ratelimiter,
            ).register(self.server)


class BaseFederationServlet(object):
    def __init__(self, handler, authenticator, ratelimiter):
        self.handler = handler
        self.authenticator = authenticator
        self.ratelimiter = ratelimiter

    def _wrap(self, code):
        authenticator = self.authenticator
        ratelimiter = self.ratelimiter

        @defer.inlineCallbacks
        @functools.wraps(code)
        def new_code(request, *args, **kwargs):
            try:
                (origin, content) = yield authenticator.authenticate_request(request)
                with ratelimiter.ratelimit(origin) as d:
                    yield d
                    response = yield code(
                        origin, content, request.args, *args, **kwargs
                    )
            except:
                logger.exception("authenticate_request failed")
                raise
            defer.returnValue(response)

        # Extra logic that functools.wraps() doesn't finish
        new_code.__self__ = code.__self__

        return new_code

    def register(self, server):
        pattern = re.compile("^" + PREFIX + self.PATH + "$")

        for method in ("GET", "PUT", "POST"):
            code = getattr(self, "on_%s" % (method), None)
            if code is None:
                continue

            server.register_path(method, pattern, self._wrap(code))


class FederationSendServlet(BaseFederationServlet):
    PATH = "/send/([^/]*)/"

    def __init__(self, handler, server_name, **kwargs):
        super(FederationSendServlet, self).__init__(handler, **kwargs)
        self.server_name = server_name

    # This is when someone is trying to send us a bunch of data.
    @defer.inlineCallbacks
    def on_PUT(self, origin, content, query, transaction_id):
        """ Called on PUT /send/<transaction_id>/

        Args:
            request (twisted.web.http.Request): The HTTP request.
            transaction_id (str): The transaction_id associated with this
                request. This is *not* None.

        Returns:
            Deferred: Results in a tuple of `(code, response)`, where
            `response` is a python dict to be converted into JSON that is
            used as the response body.
        """
        # Parse the request
        try:
            transaction_data = content

            logger.debug(
                "Decoded %s: %s",
                transaction_id, str(transaction_data)
            )

            logger.info(
                "Received txn %s from %s. (PDUs: %d, EDUs: %d, failures: %d)",
                transaction_id, origin,
                len(transaction_data.get("pdus", [])),
                len(transaction_data.get("edus", [])),
                len(transaction_data.get("failures", [])),
            )

            # We should ideally be getting this from the security layer.
            # origin = body["origin"]

            # Add some extra data to the transaction dict that isn't included
            # in the request body.
            transaction_data.update(
                transaction_id=transaction_id,
                destination=self.server_name
            )

        except Exception as e:
            logger.exception(e)
            defer.returnValue((400, {"error": "Invalid transaction"}))
            return

        try:
            code, response = yield self.handler.on_incoming_transaction(
                transaction_data
            )
        except:
            logger.exception("on_incoming_transaction failed")
            raise

        defer.returnValue((code, response))


class FederationPullServlet(BaseFederationServlet):
    PATH = "/pull/"

    # This is for when someone asks us for everything since version X
    def on_GET(self, origin, content, query):
        return self.handler.on_pull_request(query["origin"][0], query["v"])


class FederationEventServlet(BaseFederationServlet):
    PATH = "/event/([^/]*)/"

    # This is when someone asks for a data item for a given server data_id pair.
    def on_GET(self, origin, content, query, event_id):
        return self.handler.on_pdu_request(origin, event_id)


class FederationStateServlet(BaseFederationServlet):
    PATH = "/state/([^/]*)/"

    # This is when someone asks for all data for a given context.
    def on_GET(self, origin, content, query, context):
        return self.handler.on_context_state_request(
            origin,
            context,
            query.get("event_id", [None])[0],
        )


class FederationBackfillServlet(BaseFederationServlet):
    PATH = "/backfill/([^/]*)/"

    def on_GET(self, origin, content, query, context):
        versions = query["v"]
        limits = query["limit"]

        if not limits:
            return defer.succeed((400, {"error": "Did not include limit param"}))

        limit = int(limits[-1])

        return self.handler.on_backfill_request(origin, context, versions, limit)


class FederationQueryServlet(BaseFederationServlet):
    PATH = "/query/([^/]*)"

    # This is when we receive a server-server Query
    def on_GET(self, origin, content, query, query_type):
        return self.handler.on_query_request(
            query_type,
            {k: v[0].decode("utf-8") for k, v in query.items()}
        )


class FederationMakeJoinServlet(BaseFederationServlet):
    PATH = "/make_join/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_GET(self, origin, content, query, context, user_id):
        content = yield self.handler.on_make_join_request(context, user_id)
        defer.returnValue((200, content))


class FederationMakeLeaveServlet(BaseFederationServlet):
    PATH = "/make_leave/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_GET(self, origin, content, query, context, user_id):
        content = yield self.handler.on_make_leave_request(context, user_id)
        defer.returnValue((200, content))


class FederationSendLeaveServlet(BaseFederationServlet):
    PATH = "/send_leave/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_PUT(self, origin, content, query, room_id, txid):
        content = yield self.handler.on_send_leave_request(origin, content)
        defer.returnValue((200, content))


class FederationEventAuthServlet(BaseFederationServlet):
    PATH = "/event_auth/([^/]*)/([^/]*)"

    def on_GET(self, origin, content, query, context, event_id):
        return self.handler.on_event_auth(origin, context, event_id)


class FederationSendJoinServlet(BaseFederationServlet):
    PATH = "/send_join/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_PUT(self, origin, content, query, context, event_id):
        # TODO(paul): assert that context/event_id parsed from path actually
        #   match those given in content
        content = yield self.handler.on_send_join_request(origin, content)
        defer.returnValue((200, content))


class FederationInviteServlet(BaseFederationServlet):
    PATH = "/invite/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_PUT(self, origin, content, query, context, event_id):
        # TODO(paul): assert that context/event_id parsed from path actually
        #   match those given in content
        content = yield self.handler.on_invite_request(origin, content)
        defer.returnValue((200, content))


class FederationThirdPartyInviteExchangeServlet(BaseFederationServlet):
    PATH = "/exchange_third_party_invite/([^/]*)"

    @defer.inlineCallbacks
    def on_PUT(self, origin, content, query, room_id):
        content = yield self.handler.on_exchange_third_party_invite_request(
            origin, room_id, content
        )
        defer.returnValue((200, content))


class FederationClientKeysQueryServlet(BaseFederationServlet):
    PATH = "/user/keys/query"

    @defer.inlineCallbacks
    def on_POST(self, origin, content, query):
        response = yield self.handler.on_query_client_keys(origin, content)
        defer.returnValue((200, response))


class FederationClientKeysClaimServlet(BaseFederationServlet):
    PATH = "/user/keys/claim"

    @defer.inlineCallbacks
    def on_POST(self, origin, content, query):
        response = yield self.handler.on_claim_client_keys(origin, content)
        defer.returnValue((200, response))


class FederationQueryAuthServlet(BaseFederationServlet):
    PATH = "/query_auth/([^/]*)/([^/]*)"

    @defer.inlineCallbacks
    def on_POST(self, origin, content, query, context, event_id):
        new_content = yield self.handler.on_query_auth_request(
            origin, content, event_id
        )

        defer.returnValue((200, new_content))


class FederationGetMissingEventsServlet(BaseFederationServlet):
    # TODO(paul): Why does this path alone end with "/?" optional?
    PATH = "/get_missing_events/([^/]*)/?"

    @defer.inlineCallbacks
    def on_POST(self, origin, content, query, room_id):
        limit = int(content.get("limit", 10))
        min_depth = int(content.get("min_depth", 0))
        earliest_events = content.get("earliest_events", [])
        latest_events = content.get("latest_events", [])

        content = yield self.handler.on_get_missing_events(
            origin,
            room_id=room_id,
            earliest_events=earliest_events,
            latest_events=latest_events,
            min_depth=min_depth,
            limit=limit,
        )

        defer.returnValue((200, content))


class On3pidBindServlet(BaseFederationServlet):
    PATH = "/3pid/onbind"

    @defer.inlineCallbacks
    def on_POST(self, request):
        content_bytes = request.content.read()
        content = json.loads(content_bytes)
        if "invites" in content:
            last_exception = None
            for invite in content["invites"]:
                try:
                    yield self.handler.exchange_third_party_invite(invite)
                except Exception as e:
                    last_exception = e
            if last_exception:
                raise last_exception
        defer.returnValue((200, {}))

    # Avoid doing remote HS authorization checks which are done by default by
    # BaseFederationServlet.
    def _wrap(self, code):
        return code


SERVLET_CLASSES = (
    FederationPullServlet,
    FederationEventServlet,
    FederationStateServlet,
    FederationBackfillServlet,
    FederationQueryServlet,
    FederationMakeJoinServlet,
    FederationMakeLeaveServlet,
    FederationEventServlet,
    FederationSendJoinServlet,
    FederationSendLeaveServlet,
    FederationInviteServlet,
    FederationQueryAuthServlet,
    FederationGetMissingEventsServlet,
    FederationEventAuthServlet,
    FederationClientKeysQueryServlet,
    FederationClientKeysClaimServlet,
    FederationThirdPartyInviteExchangeServlet,
    On3pidBindServlet,
)
