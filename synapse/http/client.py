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
from OpenSSL import SSL
from OpenSSL.SSL import VERIFY_NONE

from synapse.api.errors import CodeMessageException
from synapse.util.logcontext import preserve_context_over_fn
import synapse.metrics

from canonicaljson import encode_canonical_json

from twisted.internet import defer, reactor, ssl
from twisted.web.client import (
    Agent, readBody, FileBodyProducer, PartialDownloadError,
)
from twisted.web.http_headers import Headers

from StringIO import StringIO

import simplejson as json
import logging
import urllib


logger = logging.getLogger(__name__)

metrics = synapse.metrics.get_metrics_for(__name__)

outgoing_requests_counter = metrics.register_counter(
    "requests",
    labels=["method"],
)
incoming_responses_counter = metrics.register_counter(
    "responses",
    labels=["method", "code"],
)


class SimpleHttpClient(object):
    """
    A simple, no-frills HTTP client with methods that wrap up common ways of
    using HTTP in Matrix
    """
    def __init__(self, hs):
        self.hs = hs
        # The default context factory in Twisted 14.0.0 (which we require) is
        # BrowserLikePolicyForHTTPS which will do regular cert validation
        # 'like a browser'
        self.agent = Agent(
            reactor,
            connectTimeout=15,
            contextFactory=hs.get_http_client_context_factory()
        )
        self.user_agent = hs.version_string
        if hs.config.user_agent_suffix:
            self.user_agent = "%s %s" % (self.user_agent, hs.config.user_agent_suffix,)

    def request(self, method, uri, *args, **kwargs):
        # A small wrapper around self.agent.request() so we can easily attach
        # counters to it
        outgoing_requests_counter.inc(method)
        d = preserve_context_over_fn(
            self.agent.request,
            method, uri, *args, **kwargs
        )

        logger.info("Sending request %s %s", method, uri)

        def _cb(response):
            incoming_responses_counter.inc(method, response.code)
            logger.info(
                "Received response to  %s %s: %s",
                method, uri, response.code
            )
            return response

        def _eb(failure):
            incoming_responses_counter.inc(method, "ERR")
            logger.info(
                "Error sending request to  %s %s: %s %s",
                method, uri, failure.type, failure.getErrorMessage()
            )
            return failure

        d.addCallbacks(_cb, _eb)

        return d

    @defer.inlineCallbacks
    def post_urlencoded_get_json(self, uri, args={}):
        # TODO: Do we ever want to log message contents?
        logger.debug("post_urlencoded_get_json args: %s", args)

        query_bytes = urllib.urlencode(args, True)

        response = yield self.request(
            "POST",
            uri.encode("ascii"),
            headers=Headers({
                b"Content-Type": [b"application/x-www-form-urlencoded"],
                b"User-Agent": [self.user_agent],
            }),
            bodyProducer=FileBodyProducer(StringIO(query_bytes))
        )

        body = yield preserve_context_over_fn(readBody, response)

        defer.returnValue(json.loads(body))

    @defer.inlineCallbacks
    def post_json_get_json(self, uri, post_json):
        json_str = encode_canonical_json(post_json)

        logger.debug("HTTP POST %s -> %s", json_str, uri)

        response = yield self.request(
            "POST",
            uri.encode("ascii"),
            headers=Headers({
                b"Content-Type": [b"application/json"],
                b"User-Agent": [self.user_agent],
            }),
            bodyProducer=FileBodyProducer(StringIO(json_str))
        )

        body = yield preserve_context_over_fn(readBody, response)

        defer.returnValue(json.loads(body))

    @defer.inlineCallbacks
    def get_json(self, uri, args={}):
        """ Gets some json from the given URI.

        Args:
            uri (str): The URI to request, not including query parameters
            args (dict): A dictionary used to create query strings, defaults to
                None.
                **Note**: The value of each key is assumed to be an iterable
                and *not* a string.
        Returns:
            Deferred: Succeeds when we get *any* 2xx HTTP response, with the
            HTTP body as JSON.
        Raises:
            On a non-2xx HTTP response. The response body will be used as the
            error message.
        """
        body = yield self.get_raw(uri, args)
        defer.returnValue(json.loads(body))

    @defer.inlineCallbacks
    def put_json(self, uri, json_body, args={}):
        """ Puts some json to the given URI.

        Args:
            uri (str): The URI to request, not including query parameters
            json_body (dict): The JSON to put in the HTTP body,
            args (dict): A dictionary used to create query strings, defaults to
                None.
                **Note**: The value of each key is assumed to be an iterable
                and *not* a string.
        Returns:
            Deferred: Succeeds when we get *any* 2xx HTTP response, with the
            HTTP body as JSON.
        Raises:
            On a non-2xx HTTP response.
        """
        if len(args):
            query_bytes = urllib.urlencode(args, True)
            uri = "%s?%s" % (uri, query_bytes)

        json_str = encode_canonical_json(json_body)

        response = yield self.request(
            "PUT",
            uri.encode("ascii"),
            headers=Headers({
                b"User-Agent": [self.user_agent],
                "Content-Type": ["application/json"]
            }),
            bodyProducer=FileBodyProducer(StringIO(json_str))
        )

        body = yield preserve_context_over_fn(readBody, response)

        if 200 <= response.code < 300:
            defer.returnValue(json.loads(body))
        else:
            # NB: This is explicitly not json.loads(body)'d because the contract
            # of CodeMessageException is a *string* message. Callers can always
            # load it into JSON if they want.
            raise CodeMessageException(response.code, body)

    @defer.inlineCallbacks
    def get_raw(self, uri, args={}):
        """ Gets raw text from the given URI.

        Args:
            uri (str): The URI to request, not including query parameters
            args (dict): A dictionary used to create query strings, defaults to
                None.
                **Note**: The value of each key is assumed to be an iterable
                and *not* a string.
        Returns:
            Deferred: Succeeds when we get *any* 2xx HTTP response, with the
            HTTP body at text.
        Raises:
            On a non-2xx HTTP response. The response body will be used as the
            error message.
        """
        if len(args):
            query_bytes = urllib.urlencode(args, True)
            uri = "%s?%s" % (uri, query_bytes)

        response = yield self.request(
            "GET",
            uri.encode("ascii"),
            headers=Headers({
                b"User-Agent": [self.user_agent],
            })
        )

        body = yield preserve_context_over_fn(readBody, response)

        if 200 <= response.code < 300:
            defer.returnValue(body)
        else:
            raise CodeMessageException(response.code, body)


class CaptchaServerHttpClient(SimpleHttpClient):
    """
    Separate HTTP client for talking to google's captcha servers
    Only slightly special because accepts partial download responses

    used only by c/s api v1
    """

    @defer.inlineCallbacks
    def post_urlencoded_get_raw(self, url, args={}):
        query_bytes = urllib.urlencode(args, True)

        response = yield self.request(
            "POST",
            url.encode("ascii"),
            bodyProducer=FileBodyProducer(StringIO(query_bytes)),
            headers=Headers({
                b"Content-Type": [b"application/x-www-form-urlencoded"],
                b"User-Agent": [self.user_agent],
            })
        )

        try:
            body = yield preserve_context_over_fn(readBody, response)
            defer.returnValue(body)
        except PartialDownloadError as e:
            # twisted dislikes google's response, no content length.
            defer.returnValue(e.response)


def _print_ex(e):
    if hasattr(e, "reasons") and e.reasons:
        for ex in e.reasons:
            _print_ex(ex)
    else:
        logger.exception(e)


class InsecureInterceptableContextFactory(ssl.ContextFactory):
    """
    Factory for PyOpenSSL SSL contexts which accepts any certificate for any domain.

    Do not use this since it allows an attacker to intercept your communications.
    """

    def __init__(self):
        self._context = SSL.Context(SSL.SSLv23_METHOD)
        self._context.set_verify(VERIFY_NONE, lambda *_: None)

    def getContext(self, hostname, port):
        return self._context
