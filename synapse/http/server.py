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
from synapse.util.logcontext import LoggingContext
import synapse.metrics

from syutil.jsonutil import (
    encode_canonical_json, encode_pretty_printed_json
)

from twisted.internet import defer, reactor
from twisted.web import server, resource
from twisted.web.server import NOT_DONE_YET
from twisted.web.util import redirectTo

import collections
import logging
import urllib

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

    def __init__(self, hs):
        resource.Resource.__init__(self)

        self.clock = hs.get_clock()
        self.path_regexs = {}
        self.version_string = hs.version_string
        self.hs = hs

    def register_path(self, method, path_pattern, callback):
        self.path_regexs.setdefault(method, []).append(
            self._PathEntry(path_pattern, callback)
        )

    def start_listening(self, port):
        """ Registers the http server with the twisted reactor.

        Args:
            port (int): The port to listen on.

        """
        reactor.listenTCP(
            port,
            server.Site(self),
            interface=self.hs.config.bind_host
        )

    def render(self, request):
        """ This gets called by twisted every time someone sends us a request.
        """
        self._async_render_with_logging_context(request)
        return server.NOT_DONE_YET

    _request_id = 0

    @defer.inlineCallbacks
    def _async_render_with_logging_context(self, request):
        request_id = "%s-%s" % (request.method, JsonResource._request_id)
        JsonResource._request_id += 1
        with LoggingContext(request_id) as request_context:
            request_context.request = request_id
            yield self._async_render(request)

    @defer.inlineCallbacks
    def _async_render(self, request):
        """ This gets called from render() every time someone sends us a request.
            This checks if anyone has registered a callback for that method and
            path.
        """
        code = None
        start = self.clock.time_msec()
        try:
            # Just say yes to OPTIONS.
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
                    urllib.unquote(u).decode("UTF-8") for u in m.groups()
                ]

                logger.info(
                    "Received request: %s %s",
                    request.method, request.path
                )

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
        except CodeMessageException as e:
            if isinstance(e, SynapseError):
                logger.info("%s SynapseError: %s - %s", request, e.code, e.msg)
            else:
                logger.exception(e)

            code = e.code
            self._send_response(
                request,
                code,
                cs_exception(e),
                response_code_message=e.response_code_message
            )
        except Exception as e:
            logger.exception(e)
            self._send_response(
                request,
                500,
                {"error": "Internal server error"}
            )
        finally:
            code = str(code) if code else "-"

            end = self.clock.time_msec()
            logger.info(
                "Processed request: %dms %s %s %s",
                end-start, code, request.method, request.path
            )

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
            pretty_print=self._request_user_agent_is_curl,
            version_string=self.version_string,
        )

    @staticmethod
    def _request_user_agent_is_curl(request):
        user_agents = request.requestHeaders.getRawHeaders(
            "User-Agent", default=[]
        )
        for user_agent in user_agents:
            if "curl" in user_agent:
                return True
        return False


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
                      version_string=""):
    if not pretty_print:
        json_bytes = encode_pretty_printed_json(json_object)
    else:
        json_bytes = encode_canonical_json(json_object)

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
