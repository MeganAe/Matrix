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


from twisted.internet import defer, reactor
from twisted.internet.error import DNSLookupError
from twisted.web.client import (
    _AgentBase, _URI, readBody, FileBodyProducer, PartialDownloadError
)
from twisted.web.http_headers import Headers

from synapse.http.endpoint import matrix_endpoint
from synapse.util.async import sleep
from synapse.util.logcontext import PreserveLoggingContext

from syutil.jsonutil import encode_canonical_json

from synapse.api.errors import CodeMessageException, SynapseError

from syutil.crypto.jsonsign import sign_json

from StringIO import StringIO

import json
import logging
import urllib
import urlparse


logger = logging.getLogger(__name__)


class MatrixHttpAgent(_AgentBase):

    def __init__(self, reactor, pool=None):
        _AgentBase.__init__(self, reactor, pool)

    def request(self, destination, endpoint, method, path, params, query,
                headers, body_producer):

        host = b""
        port = 0
        fragment = b""

        parsed_URI = _URI(b"http", destination, host, port, path, params,
                          query, fragment)

        # Set the connection pool key to be the destination.
        key = destination

        return self._requestWithEndpoint(key, endpoint, method, parsed_URI,
                                         headers, body_producer,
                                         parsed_URI.originForm)


class BaseHttpClient(object):
    """Base class for HTTP clients using twisted.
    """

    def __init__(self, hs):
        self.agent = MatrixHttpAgent(reactor)
        self.hs = hs

    @defer.inlineCallbacks
    def _create_request(self, destination, method, path_bytes,
                        body_callback, headers_dict={}, param_bytes=b"",
                        query_bytes=b"", retry_on_dns_fail=True):
        """ Creates and sends a request to the given url
        """
        headers_dict[b"User-Agent"] = [b"Synapse"]
        headers_dict[b"Host"] = [destination]

        url_bytes = urlparse.urlunparse(
            ("", "", path_bytes, param_bytes, query_bytes, "",)
        )

        logger.debug("Sending request to %s: %s %s",
                     destination, method, url_bytes)

        logger.debug(
            "Types: %s",
            [
                type(destination), type(method), type(path_bytes),
                type(param_bytes),
                type(query_bytes)
            ]
        )

        retries_left = 5

        endpoint = self._getEndpoint(reactor, destination)

        while True:

            producer = None
            if body_callback:
                producer = body_callback(method, url_bytes, headers_dict)

            try:
                with PreserveLoggingContext():
                    response = yield self.agent.request(
                        destination,
                        endpoint,
                        method,
                        path_bytes,
                        param_bytes,
                        query_bytes,
                        Headers(headers_dict),
                        producer
                    )

                logger.debug("Got response to %s", method)
                break
            except Exception as e:
                if not retry_on_dns_fail and isinstance(e, DNSLookupError):
                    logger.warn("DNS Lookup failed to %s with %s", destination,
                                e)
                    raise SynapseError(400, "Domain specified not found.")

                logger.exception("Got error in _create_request")
                _print_ex(e)

                if retries_left:
                    yield sleep(2 ** (5 - retries_left))
                    retries_left -= 1
                else:
                    raise

        if 200 <= response.code < 300:
            # We need to update the transactions table to say it was sent?
            pass
        else:
            # :'(
            # Update transactions table?
            logger.error(
                "Got response %d %s", response.code, response.phrase
            )
            raise CodeMessageException(
                response.code, response.phrase
            )

        defer.returnValue(response)


class SimpleHttpClient(BaseHttpClient):
    """
    A simple, no-frills HTTP client with methods that wrap up common ways of using HTTP in Matrix
    """
    def _getEndpoint(self, reactor, destination):
        return matrix_endpoint(reactor, destination, timeout=10)

    @defer.inlineCallbacks
    def post_urlencoded_get_json(self, destination, path, args={}):
        logger.debug("post_urlencoded_get_json args: %s", args)
        query_bytes = urllib.urlencode(args, True)

        def body_callback(method, url_bytes, headers_dict):
            return FileBodyProducer(StringIO(query_bytes))

        response = yield self._create_request(
            destination.encode("ascii"),
            "POST",
            path.encode("ascii"),
            body_callback=body_callback,
            headers_dict={
                "Content-Type": ["application/x-www-form-urlencoded"]
            }
        )

        body = yield readBody(response)

        defer.returnValue(json.loads(body))

    @defer.inlineCallbacks
    def get_json(self, destination, path, args={}, retry_on_dns_fail=True):
        """ Get's some json from the given host and path

        Args:
            destination (str): The remote server to send the HTTP request to.
            path (str): The HTTP path.
            args (dict): A dictionary used to create query strings, defaults to
                None.
                **Note**: The value of each key is assumed to be an iterable
                and *not* a string.

        Returns:
            Deferred: Succeeds when we get *any* HTTP response.

            The result of the deferred is a tuple of `(code, response)`,
            where `response` is a dict representing the decoded JSON body.
        """
        logger.debug("get_json args: %s", args)

        query_bytes = urllib.urlencode(args, True)
        logger.debug("Query bytes: %s Retry DNS: %s", args, retry_on_dns_fail)

        response = yield self._create_request(
            destination.encode("ascii"),
            "GET",
            path.encode("ascii"),
            query_bytes=query_bytes,
            retry_on_dns_fail=retry_on_dns_fail,
            body_callback=None
        )

        body = yield readBody(response)

        defer.returnValue(json.loads(body))


class MatrixFederationHttpClient(BaseHttpClient):
    """HTTP client used to talk to other homeservers over the federation protocol.
    Send client certificates and signs requests.

    Attributes:
        agent (twisted.web.client.Agent): The twisted Agent used to send the
            requests.
    """

    def __init__(self, hs):
        self.signing_key = hs.config.signing_key[0]
        self.server_name = hs.hostname
        BaseHttpClient.__init__(self, hs)

    def sign_request(self, destination, method, url_bytes, headers_dict,
                     content=None):
        request = {
            "method": method,
            "uri": url_bytes,
            "origin": self.server_name,
            "destination": destination,
        }

        if content is not None:
            request["content"] = content

        request = sign_json(request, self.server_name, self.signing_key)

        auth_headers = []

        for key, sig in request["signatures"][self.server_name].items():
            auth_headers.append(bytes(
                "X-Matrix origin=%s,key=\"%s\",sig=\"%s\"" % (
                    self.server_name, key, sig,
                )
            ))

        headers_dict[b"Authorization"] = auth_headers

    @defer.inlineCallbacks
    def put_json(self, destination, path, data={}, json_data_callback=None):
        """ Sends the specifed json data using PUT

        Args:
            destination (str): The remote server to send the HTTP request
                to.
            path (str): The HTTP path.
            data (dict): A dict containing the data that will be used as
                the request body. This will be encoded as JSON.
            json_data_callback (callable): A callable returning the dict to
                use as the request body.

        Returns:
            Deferred: Succeeds when we get a 2xx HTTP response. The result
            will be the decoded JSON body. On a 4xx or 5xx error response a
            CodeMessageException is raised.
        """

        if not json_data_callback:
            def json_data_callback():
                return data

        def body_callback(method, url_bytes, headers_dict):
            json_data = json_data_callback()
            self.sign_request(
                destination, method, url_bytes, headers_dict, json_data
            )
            producer = _JsonProducer(json_data)
            return producer

        response = yield self._create_request(
            destination.encode("ascii"),
            "PUT",
            path.encode("ascii"),
            body_callback=body_callback,
            headers_dict={"Content-Type": ["application/json"]},
        )

        logger.debug("Getting resp body")
        body = yield readBody(response)
        logger.debug("Got resp body")

        defer.returnValue((response.code, body))

    @defer.inlineCallbacks
    def get_json(self, destination, path, args={}, retry_on_dns_fail=True):
        """ Get's some json from the given host homeserver and path

        Args:
            destination (str): The remote server to send the HTTP request
                to.
            path (str): The HTTP path.
            args (dict): A dictionary used to create query strings, defaults to
                None.
                **Note**: The value of each key is assumed to be an iterable
                and *not* a string.

        Returns:
            Deferred: Succeeds when we get *any* HTTP response.

            The result of the deferred is a tuple of `(code, response)`,
            where `response` is a dict representing the decoded JSON body.
        """
        logger.debug("get_json args: %s", args)

        encoded_args = {}
        for k, vs in args.items():
            if isinstance(vs, basestring):
                vs = [vs]
            encoded_args[k] = [v.encode("UTF-8") for v in vs]

        query_bytes = urllib.urlencode(encoded_args, True)
        logger.debug("Query bytes: %s Retry DNS: %s", args, retry_on_dns_fail)

        def body_callback(method, url_bytes, headers_dict):
            self.sign_request(destination, method, url_bytes, headers_dict)
            return None

        response = yield self._create_request(
            destination.encode("ascii"),
            "GET",
            path.encode("ascii"),
            query_bytes=query_bytes,
            body_callback=body_callback,
            retry_on_dns_fail=retry_on_dns_fail
        )

        body = yield readBody(response)

        defer.returnValue(json.loads(body))

    def _getEndpoint(self, reactor, destination):
        return matrix_endpoint(
            reactor, destination, timeout=10,
            ssl_context_factory=self.hs.tls_context_factory
        )


class CaptchaServerHttpClient(BaseHttpClient):
    """
    Separate HTTP client for talking to google's captcha servers
    Only slightly special because accepts partial download responses
    """

    def _getEndpoint(self, reactor, destination):
        return matrix_endpoint(reactor, destination, timeout=10)

    @defer.inlineCallbacks
    def post_urlencoded_get_raw(self, destination, path, args={}):
        query_bytes = urllib.urlencode(args, True)

        def body_callback(method, url_bytes, headers_dict):
            return FileBodyProducer(StringIO(query_bytes))

        response = yield self._create_request(
            destination.encode("ascii"),
            "POST",
            path.encode("ascii"),
            body_callback=body_callback,
            headers_dict={
                "Content-Type": ["application/x-www-form-urlencoded"]
            }
        )

        try:
            body = yield readBody(response)
            defer.returnValue(body)
        except PartialDownloadError as e:
            defer.returnValue(e.response)


def _print_ex(e):
    if hasattr(e, "reasons") and e.reasons:
        for ex in e.reasons:
            _print_ex(ex)
    else:
        logger.exception(e)


class _JsonProducer(object):
    """ Used by the twisted http client to create the HTTP body from json
    """
    def __init__(self, jsn):
        self.reset(jsn)

    def reset(self, jsn):
        self.body = encode_canonical_json(jsn)
        self.length = len(self.body)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass
