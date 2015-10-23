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


from synapse.api.errors import (
    cs_exception, SynapseError, CodeMessageException, UnrecognizedRequestError
)
from synapse.util.logcontext import LoggingContext, PreserveLoggingContext
import synapse.metrics
import synapse.events

from canonicaljson import (
    encode_canonical_json, encode_pretty_printed_json
)

from twisted.internet import defer
from twisted.web import server, resource
from twisted.web.server import NOT_DONE_YET
from twisted.web.util import redirectTo

import collections
import logging
import urllib
import ujson

logger = logging.getLogger(__name__)

metrics = synapse.metrics.get_metrics_for(__name__)

incoming_requests_counter = metrics.register_counter(
    "requests",
    labels=["method", "servlet"],
)
outgoing_responses_counter = metrics.register_counter(
    "responses",
    labels=["method", "code"],
)

response_timer = metrics.register_distribution(
    "response_time",
    labels=["method", "servlet"]
)

_next_request_id = 0


def request_handler(request_handler):
    """Wraps a method that acts as a request handler with the necessary logging
    and exception handling.

    The method must have a signature of "handle_foo(self, request)". The
    argument "self" must have "version_string" and "clock" attributes. The
    argument "request" must be a twisted HTTP request.

    The method must return a deferred. If the deferred succeeds we assume that
    a response has been sent. If the deferred fails with a SynapseError we use
    it to send a JSON response with the appropriate HTTP reponse code. If the
    deferred fails with any other type of error we send a 500 reponse.

    We insert a unique request-id into the logging context for this request and
    log the response and duration for this request.
    """

    @defer.inlineCallbacks
    def wrapped_request_handler(self, request):
        global _next_request_id
        request_id = "%s-%s" % (request.method, _next_request_id)
        _next_request_id += 1
        with LoggingContext(request_id) as request_context:
            request_context.request = request_id
            with request.processing():
                try:
                    d = request_handler(self, request)
                    with PreserveLoggingContext():
                        yield d
                except CodeMessageException as e:
                    code = e.code
                    if isinstance(e, SynapseError):
                        logger.info(
                            "%s SynapseError: %s - %s", request, code, e.msg
                        )
                    else:
                        logger.exception(e)
                    outgoing_responses_counter.inc(request.method, str(code))
                    respond_with_json(
                        request, code, cs_exception(e), send_cors=True,
                        pretty_print=_request_user_agent_is_curl(request),
                        version_string=self.version_string,
                    )
                except:
                    logger.exception(
                        "Failed handle request %s.%s on %r: %r",
                        request_handler.__module__,
                        request_handler.__name__,
                        self,
                        request
                    )
                    respond_with_json(
                        request,
                        500,
                        {"error": "Internal server error"},
                        send_cors=True
                    )
    return wrapped_request_handler


class HttpServer(object):
    """ Interface for registering callbacks on a HTTP server
    """

    def register_path(self, method, path_pattern, callback):
        """ Register a callback that gets fired if we receive a http request
        with the given method for a path that matches the given regex.

        If the regex contains groups these gets passed to the calback via
        an unpacked tuple.

        Args:
            method (str): The method to listen to.
            path_pattern (str): The regex used to match requests.
            callback (function): The function to fire if we receive a matched
                request. The first argument will be the request object and
                subsequent arguments will be any matched groups from the regex.
                This should return a tuple of (code, response).
        """
        pass


class JsonResource(HttpServer, resource.Resource):
    """ This implements the HttpServer interface and provides JSON support for
    Resources.

    Register callbacks via register_path()

    Callbacks can return a tuple of status code and a dict in which case the
    the dict will automatically be sent to the client as a JSON object.

    The JsonResource is primarily intended for returning JSON, but callbacks
    may send something other than JSON, they may do so by using the methods
    on the request object and instead returning None.
    """

    isLeaf = True

    _PathEntry = collections.namedtuple("_PathEntry", ["pattern", "callback"])

    def __init__(self, hs, canonical_json=True):
        resource.Resource.__init__(self)

        self.canonical_json = canonical_json
        self.clock = hs.get_clock()
        self.path_regexs = {}
        self.version_string = hs.version_string
        self.hs = hs

    def register_path(self, method, path_pattern, callback):
        self.path_regexs.setdefault(method, []).append(
            self._PathEntry(path_pattern, callback)
        )

    def render(self, request):
        """ This gets called by twisted every time someone sends us a request.
        """
        self._async_render(request)
        return server.NOT_DONE_YET

    @request_handler
    @defer.inlineCallbacks
    def _async_render(self, request):
        """ This gets called from render() every time someone sends us a request.
            This checks if anyone has registered a callback for that method and
            path.
        """
        start = self.clock.time_msec()
        if request.method == "OPTIONS":
            self._send_response(request, 200, {})
            return
        # Loop through all the registered callbacks to check if the method
        # and path regex match
        for path_entry in self.path_regexs.get(request.method, []):
            m = path_entry.pattern.match(request.path)
            if not m:
                continue

            # We found a match! Trigger callback and then return the
            # returned response. We pass both the request and any
            # matched groups from the regex to the callback.

            callback = path_entry.callback

            servlet_instance = getattr(callback, "__self__", None)
            if servlet_instance is not None:
                servlet_classname = servlet_instance.__class__.__name__
            else:
                servlet_classname = "%r" % callback
            incoming_requests_counter.inc(request.method, servlet_classname)

            args = [
                urllib.unquote(u).decode("UTF-8") if u else u for u in m.groups()
            ]

            callback_return = yield callback(request, *args)
            if callback_return is not None:
                code, response = callback_return
                self._send_response(request, code, response)

            response_timer.inc_by(
                self.clock.time_msec() - start, request.method, servlet_classname
            )

            return

        # Huh. No one wanted to handle that? Fiiiiiine. Send 400.
        raise UnrecognizedRequestError()

    def _send_response(self, request, code, response_json_object,
                       response_code_message=None):
        # could alternatively use request.notifyFinish() and flip a flag when
        # the Deferred fires, but since the flag is RIGHT THERE it seems like
        # a waste.
        if request._disconnected:
            logger.warn(
                "Not sending response to request %s, already disconnected.",
                request)
            return

        outgoing_responses_counter.inc(request.method, str(code))

        # TODO: Only enable CORS for the requests that need it.
        respond_with_json(
            request, code, response_json_object,
            send_cors=True,
            response_code_message=response_code_message,
            pretty_print=_request_user_agent_is_curl(request),
            version_string=self.version_string,
            canonical_json=self.canonical_json,
        )


class RootRedirect(resource.Resource):
    """Redirects the root '/' path to another path."""

    def __init__(self, path):
        resource.Resource.__init__(self)
        self.url = path

    def render_GET(self, request):
        return redirectTo(self.url, request)

    def getChild(self, name, request):
        if len(name) == 0:
            return self  # select ourselves as the child to render
        return resource.Resource.getChild(self, name, request)


def respond_with_json(request, code, json_object, send_cors=False,
                      response_code_message=None, pretty_print=False,
                      version_string="", canonical_json=True):
    if pretty_print:
        json_bytes = encode_pretty_printed_json(json_object) + "\n"
    else:
        if canonical_json or synapse.events.USE_FROZEN_DICTS:
            json_bytes = encode_canonical_json(json_object)
        else:
            # ujson doesn't like frozen_dicts.
            json_bytes = ujson.dumps(json_object, ensure_ascii=False)

    return respond_with_json_bytes(
        request, code, json_bytes,
        send_cors=send_cors,
        response_code_message=response_code_message,
        version_string=version_string
    )


def respond_with_json_bytes(request, code, json_bytes, send_cors=False,
                            version_string="", response_code_message=None):
    """Sends encoded JSON in response to the given request.

    Args:
        request (twisted.web.http.Request): The http request to respond to.
        code (int): The HTTP response code.
        json_bytes (bytes): The json bytes to use as the response body.
        send_cors (bool): Whether to send Cross-Origin Resource Sharing headers
            http://www.w3.org/TR/cors/
    Returns:
        twisted.web.server.NOT_DONE_YET"""

    request.setResponseCode(code, message=response_code_message)
    request.setHeader(b"Content-Type", b"application/json")
    request.setHeader(b"Server", version_string)
    request.setHeader(b"Content-Length", b"%d" % (len(json_bytes),))

    if send_cors:
        request.setHeader("Access-Control-Allow-Origin", "*")
        request.setHeader("Access-Control-Allow-Methods",
                          "GET, POST, PUT, DELETE, OPTIONS")
        request.setHeader("Access-Control-Allow-Headers",
                          "Origin, X-Requested-With, Content-Type, Accept")

    request.write(json_bytes)
    request.finish()
    return NOT_DONE_YET


def _request_user_agent_is_curl(request):
    user_agents = request.requestHeaders.getRawHeaders(
        "User-Agent", default=[]
    )
    for user_agent in user_agents:
        if "curl" in user_agent:
            return True
    return False
