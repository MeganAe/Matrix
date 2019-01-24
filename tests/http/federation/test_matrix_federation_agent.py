# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
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

from mock import Mock

import treq

from twisted.internet import defer
from twisted.internet.protocol import Factory
from twisted.protocols.tls import TLSMemoryBIOFactory
from twisted.test.ssl_helpers import ServerTLSContext
from twisted.web.http import HTTPChannel

from synapse.crypto.context_factory import ClientTLSOptionsFactory
from synapse.http.federation.matrix_federation_agent import MatrixFederationAgent
from synapse.util.logcontext import LoggingContext

from tests.server import FakeTransport, ThreadedMemoryReactorClock
from tests.unittest import TestCase

logger = logging.getLogger(__name__)


class MatrixFederationAgentTests(TestCase):
    def setUp(self):
        self.reactor = ThreadedMemoryReactorClock()

        self.mock_resolver = Mock()

        self.agent = MatrixFederationAgent(
            reactor=self.reactor,
            tls_client_options_factory=ClientTLSOptionsFactory(None),
            _srv_resolver=self.mock_resolver,
        )

    def _make_connection(self, client_factory, expected_sni):
        """Builds a test server, and completes the outgoing client connection

        Returns:
            HTTPChannel: the test server
        """

        # build the test server
        server_tls_protocol = _build_test_server()

        # now, tell the client protocol factory to build the client protocol (it will be a
        # _WrappingProtocol, around a TLSMemoryBIOProtocol, around an
        # HTTP11ClientProtocol) and wire the output of said protocol up to the server via
        # a FakeTransport.
        #
        # Normally this would be done by the TCP socket code in Twisted, but we are
        # stubbing that out here.
        client_protocol = client_factory.buildProtocol(None)
        client_protocol.makeConnection(FakeTransport(server_tls_protocol, self.reactor))

        # tell the server tls protocol to send its stuff back to the client, too
        server_tls_protocol.makeConnection(FakeTransport(client_protocol, self.reactor))

        # give the reactor a pump to get the TLS juices flowing.
        self.reactor.pump((0.1,))

        # check the SNI
        server_name = server_tls_protocol._tlsConnection.get_servername()
        self.assertEqual(
            server_name,
            expected_sni,
            "Expected SNI %s but got %s" % (expected_sni, server_name),
        )

        # fish the test server back out of the server-side TLS protocol.
        return server_tls_protocol.wrappedProtocol

    @defer.inlineCallbacks
    def _make_get_request(self, uri):
        """
        Sends a simple GET request via the agent, and checks its logcontext management
        """
        with LoggingContext("one") as context:
            fetch_d = self.agent.request(b'GET', uri)

            # Nothing happened yet
            self.assertNoResult(fetch_d)

            # should have reset logcontext to the sentinel
            _check_logcontext(LoggingContext.sentinel)

            try:
                fetch_res = yield fetch_d
                defer.returnValue(fetch_res)
            finally:
                _check_logcontext(context)

    def test_get(self):
        """
        happy-path test of a GET request
        """
        self.reactor.lookups["testserv"] = "1.2.3.4"
        test_d = self._make_get_request(b"matrix://testserv:8448/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory,
            expected_sni=b"testserv",
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'),
            [b'testserv:8448']
        )
        content = request.content.read()
        self.assertEqual(content, b'')

        # Deferred is still without a result
        self.assertNoResult(test_d)

        # send the headers
        request.responseHeaders.setRawHeaders(b'Content-Type', [b'application/json'])
        request.write('')

        self.reactor.pump((0.1,))

        response = self.successResultOf(test_d)

        # that should give us a Response object
        self.assertEqual(response.code, 200)

        # Send the body
        request.write('{ "a": 1 }'.encode('ascii'))
        request.finish()

        self.reactor.pump((0.1,))

        # check it can be read
        json = self.successResultOf(treq.json_content(response))
        self.assertEqual(json, {"a": 1})

    def test_get_ip_address(self):
        """
        Test the behaviour when the server name contains an explicit IP (with no port)
        """

        # the SRV lookup will return an empty list (XXX: why do we even do an SRV lookup?)
        self.mock_resolver.resolve_service.side_effect = lambda _: []

        # then there will be a getaddrinfo on the IP
        self.reactor.lookups["1.2.3.4"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://1.2.3.4/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_called_once()

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory,
            expected_sni=None,
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        # XXX currently broken
        # self.assertEqual(
        #     request.requestHeaders.getRawHeaders(b'host'),
        #     [b'1.2.3.4:8448']
        # )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)


def _check_logcontext(context):
    current = LoggingContext.current_context()
    if current is not context:
        raise AssertionError(
            "Expected logcontext %s but was %s" % (context, current),
        )


def _build_test_server():
    """Construct a test server

    This builds an HTTP channel, wrapped with a TLSMemoryBIOProtocol

    Returns:
        TLSMemoryBIOProtocol
    """
    server_factory = Factory.forProtocol(HTTPChannel)
    # Request.finish expects the factory to have a 'log' method.
    server_factory.log = _log_request

    server_tls_factory = TLSMemoryBIOFactory(
        ServerTLSContext(), isClient=False, wrappedFactory=server_factory,
    )

    return server_tls_factory.buildProtocol(None)


def _log_request(request):
    """Implements Factory.log, which is expected by Request.finish"""
    logger.info("Completed request %s", request)
