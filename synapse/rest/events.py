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

"""This module contains REST servlets to do with event streaming, /events."""
from twisted.internet import defer

from synapse.api.errors import SynapseError
from synapse.streams.config import PaginationConfig
from synapse.rest.base import RestServlet, client_path_pattern

import logging


logger = logging.getLogger(__name__)



class EventStreamRestServlet(RestServlet):
    PATTERN = client_path_pattern("/events$")

    DEFAULT_LONGPOLL_TIME_MS = 30000

    @defer.inlineCallbacks
    def on_GET(self, request):
        auth_user = yield self.auth.get_user_by_req(request)
        try:
            handler = self.handlers.event_stream_handler
            pagin_config = PaginationConfig.from_request(request)
            timeout = EventStreamRestServlet.DEFAULT_LONGPOLL_TIME_MS
            if "timeout" in request.args:
                try:
                    timeout = int(request.args["timeout"][0])
                except ValueError:
                    raise SynapseError(400, "timeout must be in milliseconds.")

            chunk = yield handler.get_stream(
                auth_user.to_string(), pagin_config, timeout=timeout
            )
        except:
            logger.exception("Event stream failed")
            raise

        defer.returnValue((200, chunk))

    def on_OPTIONS(self, request):
        return (200, {})


# TODO: Unit test gets, with and without auth, with different kinds of events.
class EventRestServlet(RestServlet):
    PATTERN = client_path_pattern("/events/(?P<event_id>[^/]*)$")

    @defer.inlineCallbacks
    def on_GET(self, request, event_id):
        auth_user = yield self.auth.get_user_by_req(request)
        handler = self.handlers.event_handler
        event = yield handler.get_event(auth_user, event_id)

        if event:
            defer.returnValue((200, self.hs.serialize_event(event)))
        else:
            defer.returnValue((404, "Event not found."))


def register_servlets(hs, http_server):
    EventStreamRestServlet(hs).register(http_server)
    EventRestServlet(hs).register(http_server)
