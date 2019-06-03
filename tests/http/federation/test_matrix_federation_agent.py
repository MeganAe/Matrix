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
from zope.interface import implementer

from twisted.internet import defer
from twisted.internet._sslverify import ClientTLSOptions, OpenSSLCertificateOptions
from twisted.internet.protocol import Factory
from twisted.protocols.tls import TLSMemoryBIOFactory
from twisted.web.http import HTTPChannel
from twisted.web.http_headers import Headers
from twisted.web.iweb import IPolicyForHTTPS

from synapse.crypto.context_factory import ClientTLSOptionsFactory
from synapse.http.federation.matrix_federation_agent import (
    MatrixFederationAgent,
    _cache_period_from_headers,
)
from synapse.http.federation.srv_resolver import Server
from synapse.util.caches.ttlcache import TTLCache
from synapse.util.logcontext import LoggingContext

from tests.http import ServerTLSContext
from tests.server import FakeTransport, ThreadedMemoryReactorClock
from tests.unittest import TestCase
from tests.utils import default_config

logger = logging.getLogger(__name__)


class MatrixFederationAgentTests(TestCase):
    def setUp(self):
        self.reactor = ThreadedMemoryReactorClock()

        self.mock_resolver = Mock()

        self.well_known_cache = TTLCache("test_cache", timer=self.reactor.seconds)

        self.agent = MatrixFederationAgent(
            reactor=self.reactor,
            tls_client_options_factory=ClientTLSOptionsFactory(
                default_config("test", parse=True)
            ),
            _well_known_tls_policy=TrustingTLSPolicyForHTTPS(),
            _srv_resolver=self.mock_resolver,
            _well_known_cache=self.well_known_cache,
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
        client_protocol.makeConnection(
            FakeTransport(server_tls_protocol, self.reactor, client_protocol)
        )

        # tell the server tls protocol to send its stuff back to the client, too
        server_tls_protocol.makeConnection(
            FakeTransport(client_protocol, self.reactor, server_tls_protocol)
        )

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
            except Exception as e:
                logger.info("Fetch of %s failed: %s", uri.decode("ascii"), e)
                raise
            finally:
                _check_logcontext(context)

    def _handle_well_known_connection(
        self, client_factory, expected_sni, content, response_headers={}
    ):
        """Handle an outgoing HTTPs connection: wire it up to a server, check that the
        request is for a .well-known, and send the response.

        Args:
            client_factory (IProtocolFactory): outgoing connection
            expected_sni (bytes): SNI that we expect the outgoing connection to send
            content (bytes): content to send back as the .well-known
        Returns:
            HTTPChannel: server impl
        """
        # make the connection for .well-known
        well_known_server = self._make_connection(
            client_factory, expected_sni=expected_sni
        )
        # check the .well-known request and send a response
        self.assertEqual(len(well_known_server.requests), 1)
        request = well_known_server.requests[0]
        self._send_well_known_response(request, content, headers=response_headers)
        return well_known_server

    def _send_well_known_response(self, request, content, headers={}):
        """Check that an incoming request looks like a valid .well-known request, and
        send back the response.
        """
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/.well-known/matrix/server')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'testserv'])
        # send back a response
        for k, v in headers.items():
            request.setHeader(k, v)
        request.write(content)
        request.finish()

        self.reactor.pump((0.1,))

    def test_get(self):
        """
        happy-path test of a GET request with an explicit port
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
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'testserv:8448']
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
        # there will be a getaddrinfo on the IP
        self.reactor.lookups["1.2.3.4"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://1.2.3.4/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'1.2.3.4'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_ipv6_address(self):
        """
        Test the behaviour when the server name contains an explicit IPv6 address
        (with no port)
        """

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["::1"] = "::1"

        test_d = self._make_get_request(b"matrix://[::1]/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '::1')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'[::1]'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_ipv6_address_with_port(self):
        """
        Test the behaviour when the server name contains an explicit IPv6 address
        (with explicit port)
        """

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["::1"] = "::1"

        test_d = self._make_get_request(b"matrix://[::1]:80/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '::1')
        self.assertEqual(port, 80)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'[::1]:80'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_no_srv_no_well_known(self):
        """
        Test the behaviour when the server name has no port, no SRV, and no well-known
        """

        self.mock_resolver.resolve_service.side_effect = lambda _: []
        self.reactor.lookups["testserv"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        # fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # attemptdelay on the hostnameendpoint is 0.3, so  takes that long before the
        # .well-known request fails.
        self.reactor.pump((0.4,))

        # now there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.testserv"
        )

        # we should fall back to a direct connection
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b'testserv')

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'testserv'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known(self):
        """Test the behaviour when the .well-known delegates elsewhere
        """

        self.mock_resolver.resolve_service.side_effect = lambda _: []
        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["target-server"] = "1::f"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "target-server" }',
        )

        # there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.target-server"
        )

        # now we should get a connection to the target server
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, '1::f')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b'target-server'
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'target-server']
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

        self.assertEqual(self.well_known_cache[b"testserv"], b"target-server")

        # check the cache expires
        self.reactor.pump((25 * 3600,))
        self.well_known_cache.expire()
        self.assertNotIn(b"testserv", self.well_known_cache)

    def test_get_well_known_redirect(self):
        """Test the behaviour when the server name has no port and no SRV record, but
        the .well-known has a 300 redirect
        """
        self.mock_resolver.resolve_service.side_effect = lambda _: []
        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["target-server"] = "1::f"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        redirect_server = self._make_connection(
            client_factory, expected_sni=b"testserv"
        )

        # send a 302 redirect
        self.assertEqual(len(redirect_server.requests), 1)
        request = redirect_server.requests[0]
        request.redirect(b'https://testserv/even_better_known')
        request.finish()

        self.reactor.pump((0.1,))

        # now there should be another connection
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        well_known_server = self._make_connection(
            client_factory, expected_sni=b"testserv"
        )

        self.assertEqual(len(well_known_server.requests), 1, "No request after 302")
        request = well_known_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/even_better_known')
        request.write(b'{ "m.server": "target-server" }')
        request.finish()

        self.reactor.pump((0.1,))

        # there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.target-server"
        )

        # now we should get a connection to the target server
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1::f')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b'target-server'
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'target-server']
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

        self.assertEqual(self.well_known_cache[b"testserv"], b"target-server")

        # check the cache expires
        self.reactor.pump((25 * 3600,))
        self.well_known_cache.expire()
        self.assertNotIn(b"testserv", self.well_known_cache)

    def test_get_invalid_well_known(self):
        """
        Test the behaviour when the server name has an *invalid* well-known (and no SRV)
        """

        self.mock_resolver.resolve_service.side_effect = lambda _: []
        self.reactor.lookups["testserv"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory, expected_sni=b"testserv", content=b'NOT JSON'
        )

        # now there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.testserv"
        )

        # we should fall back to a direct connection
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b'testserv')

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'testserv'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_hostname_srv(self):
        """
        Test the behaviour when there is a single SRV record
        """
        self.mock_resolver.resolve_service.side_effect = lambda _: [
            Server(host=b"srvtarget", port=8443)
        ]
        self.reactor.lookups["srvtarget"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # the request for a .well-known will have failed with a DNS lookup error.
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.testserv"
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b'testserv')

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(request.requestHeaders.getRawHeaders(b'host'), [b'testserv'])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known_srv(self):
        """Test the behaviour when the .well-known redirects to a place where there
        is a SRV.
        """
        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["srvtarget"] = "5.6.7.8"

        test_d = self._make_get_request(b"matrix://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        self.mock_resolver.resolve_service.side_effect = lambda _: [
            Server(host=b"srvtarget", port=8443)
        ]

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "target-server" }',
        )

        # there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.target-server"
        )

        # now we should get a connection to the target of the SRV record
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, '5.6.7.8')
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b'target-server'
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'target-server']
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_idna_servername(self):
        """test the behaviour when the server name has idna chars in"""

        self.mock_resolver.resolve_service.side_effect = lambda _: []

        # the resolver is always called with the IDNA hostname as a native string.
        self.reactor.lookups["xn--bcher-kva.com"] = "1.2.3.4"

        # this is idna for bücher.com
        test_d = self._make_get_request(b"matrix://xn--bcher-kva.com/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        # fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # attemptdelay on the hostnameendpoint is 0.3, so  takes that long before the
        # .well-known request fails.
        self.reactor.pump((0.4,))

        # now there should have been a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.xn--bcher-kva.com"
        )

        # We should fall back to port 8448
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b'xn--bcher-kva.com'
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'xn--bcher-kva.com']
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_idna_srv_target(self):
        """test the behaviour when the target of a SRV record has idna chars"""

        self.mock_resolver.resolve_service.side_effect = lambda _: [
            Server(host=b"xn--trget-3qa.com", port=8443)  # târget.com
        ]
        self.reactor.lookups["xn--trget-3qa.com"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix://xn--bcher-kva.com/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix._tcp.xn--bcher-kva.com"
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b'xn--bcher-kva.com'
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b'GET')
        self.assertEqual(request.path, b'/foo/bar')
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'host'), [b'xn--bcher-kva.com']
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    @defer.inlineCallbacks
    def do_get_well_known(self, serv):
        try:
            result = yield self.agent._get_well_known(serv)
            logger.info("Result from well-known fetch: %s", result)
        except Exception as e:
            logger.warning("Error fetching well-known: %s", e)
            raise
        defer.returnValue(result)

    def test_well_known_cache(self):
        self.reactor.lookups["testserv"] = "1.2.3.4"

        fetch_d = self.do_get_well_known(b'testserv')

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        well_known_server = self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            response_headers={b'Cache-Control': b'max-age=10'},
            content=b'{ "m.server": "target-server" }',
        )

        r = self.successResultOf(fetch_d)
        self.assertEqual(r, b'target-server')

        # close the tcp connection
        well_known_server.loseConnection()

        # repeat the request: it should hit the cache
        fetch_d = self.do_get_well_known(b'testserv')
        r = self.successResultOf(fetch_d)
        self.assertEqual(r, b'target-server')

        # expire the cache
        self.reactor.pump((10.0,))

        # now it should connect again
        fetch_d = self.do_get_well_known(b'testserv')

        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, '1.2.3.4')
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "other-server" }',
        )

        r = self.successResultOf(fetch_d)
        self.assertEqual(r, b'other-server')


class TestCachePeriodFromHeaders(TestCase):
    def test_cache_control(self):
        # uppercase
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b'Cache-Control': [b'foo, Max-Age = 100, bar']})
            ),
            100,
        )

        # missing value
        self.assertIsNone(
            _cache_period_from_headers(Headers({b'Cache-Control': [b'max-age=, bar']}))
        )

        # hackernews: bogus due to semicolon
        self.assertIsNone(
            _cache_period_from_headers(
                Headers({b'Cache-Control': [b'private; max-age=0']})
            )
        )

        # github
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b'Cache-Control': [b'max-age=0, private, must-revalidate']})
            ),
            0,
        )

        # google
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b'cache-control': [b'private, max-age=0']})
            ),
            0,
        )

    def test_expires(self):
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b'Expires': [b'Wed, 30 Jan 2019 07:35:33 GMT']}),
                time_now=lambda: 1548833700,
            ),
            33,
        )

        # cache-control overrides expires
        self.assertEqual(
            _cache_period_from_headers(
                Headers(
                    {
                        b'cache-control': [b'max-age=10'],
                        b'Expires': [b'Wed, 30 Jan 2019 07:35:33 GMT'],
                    }
                ),
                time_now=lambda: 1548833700,
            ),
            10,
        )

        # invalid expires means immediate expiry
        self.assertEqual(_cache_period_from_headers(Headers({b'Expires': [b'0']})), 0)


def _check_logcontext(context):
    current = LoggingContext.current_context()
    if current is not context:
        raise AssertionError("Expected logcontext %s but was %s" % (context, current))


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
        ServerTLSContext(), isClient=False, wrappedFactory=server_factory
    )

    return server_tls_factory.buildProtocol(None)


def _log_request(request):
    """Implements Factory.log, which is expected by Request.finish"""
    logger.info("Completed request %s", request)


@implementer(IPolicyForHTTPS)
class TrustingTLSPolicyForHTTPS(object):
    """An IPolicyForHTTPS which doesn't do any certificate verification"""

    def creatorForNetloc(self, hostname, port):
        certificateOptions = OpenSSLCertificateOptions()
        return ClientTLSOptions(hostname, certificateOptions.getContext())
