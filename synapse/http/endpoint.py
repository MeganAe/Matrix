# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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
import random
import re

from twisted.internet import defer
from twisted.internet.endpoints import HostnameEndpoint, wrapClientTLS
from twisted.internet.error import ConnectError

from synapse.http.federation.srv_resolver import _Server, resolve_service

logger = logging.getLogger(__name__)


def parse_server_name(server_name):
    """Split a server name into host/port parts.

    Args:
        server_name (str): server name to parse

    Returns:
        Tuple[str, int|None]: host/port parts.

    Raises:
        ValueError if the server name could not be parsed.
    """
    try:
        if server_name[-1] == ']':
            # ipv6 literal, hopefully
            return server_name, None

        domain_port = server_name.rsplit(":", 1)
        domain = domain_port[0]
        port = int(domain_port[1]) if domain_port[1:] else None
        return domain, port
    except Exception:
        raise ValueError("Invalid server name '%s'" % server_name)


VALID_HOST_REGEX = re.compile(
    "\\A[0-9a-zA-Z.-]+\\Z",
)


def parse_and_validate_server_name(server_name):
    """Split a server name into host/port parts and do some basic validation.

    Args:
        server_name (str): server name to parse

    Returns:
        Tuple[str, int|None]: host/port parts.

    Raises:
        ValueError if the server name could not be parsed.
    """
    host, port = parse_server_name(server_name)

    # these tests don't need to be bulletproof as we'll find out soon enough
    # if somebody is giving us invalid data. What we *do* need is to be sure
    # that nobody is sneaking IP literals in that look like hostnames, etc.

    # look for ipv6 literals
    if host[0] == '[':
        if host[-1] != ']':
            raise ValueError("Mismatched [...] in server name '%s'" % (
                server_name,
            ))
        return host, port

    # otherwise it should only be alphanumerics.
    if not VALID_HOST_REGEX.match(host):
        raise ValueError("Server name '%s' contains invalid characters" % (
            server_name,
        ))

    return host, port


def matrix_federation_endpoint(reactor, destination, tls_client_options_factory=None,
                               timeout=None):
    """Construct an endpoint for the given matrix destination.

    Args:
        reactor: Twisted reactor.
        destination (unicode): The name of the server to connect to.
        tls_client_options_factory
            (synapse.crypto.context_factory.ClientTLSOptionsFactory):
            Factory which generates TLS options for client connections.
        timeout (int): connection timeout in seconds
    """

    domain, port = parse_server_name(destination)

    endpoint_kw_args = {}

    if timeout is not None:
        endpoint_kw_args.update(timeout=timeout)

    if tls_client_options_factory is None:
        transport_endpoint = HostnameEndpoint
        default_port = 8008
    else:
        # the SNI string should be the same as the Host header, minus the port.
        # as per https://github.com/matrix-org/synapse/issues/2525#issuecomment-336896777,
        # the Host header and SNI should therefore be the server_name of the remote
        # server.
        tls_options = tls_client_options_factory.get_options(domain)

        def transport_endpoint(reactor, host, port, timeout):
            return wrapClientTLS(
                tls_options,
                HostnameEndpoint(reactor, host, port, timeout=timeout),
            )
        default_port = 8448

    if port is None:
        return SRVClientEndpoint(
            reactor, "matrix", domain, protocol="tcp",
            default_port=default_port, endpoint=transport_endpoint,
            endpoint_kw_args=endpoint_kw_args
        )
    else:
        return transport_endpoint(
            reactor, domain, port, **endpoint_kw_args
        )


class SRVClientEndpoint(object):
    """An endpoint which looks up SRV records for a service.
    Cycles through the list of servers starting with each call to connect
    picking the next server.
    Implements twisted.internet.interfaces.IStreamClientEndpoint.
    """

    def __init__(self, reactor, service, domain, protocol="tcp",
                 default_port=None, endpoint=HostnameEndpoint,
                 endpoint_kw_args={}):
        self.reactor = reactor
        self.service_name = "_%s._%s.%s" % (service, protocol, domain)

        if default_port is not None:
            self.default_server = _Server(
                host=domain,
                port=default_port,
                priority=0,
                weight=0,
                expires=0,
            )
        else:
            self.default_server = None

        self.endpoint = endpoint
        self.endpoint_kw_args = endpoint_kw_args

        self.servers = None
        self.used_servers = None

    @defer.inlineCallbacks
    def fetch_servers(self):
        self.used_servers = []
        self.servers = yield resolve_service(self.service_name)

    def pick_server(self):
        if not self.servers:
            if self.used_servers:
                self.servers = self.used_servers
                self.used_servers = []
                self.servers.sort()
            elif self.default_server:
                return self.default_server
            else:
                raise ConnectError(
                    "No server available for %s" % self.service_name
                )

        # look for all servers with the same priority
        min_priority = self.servers[0].priority
        weight_indexes = list(
            (index, server.weight + 1)
            for index, server in enumerate(self.servers)
            if server.priority == min_priority
        )

        total_weight = sum(weight for index, weight in weight_indexes)
        target_weight = random.randint(0, total_weight)
        for index, weight in weight_indexes:
            target_weight -= weight
            if target_weight <= 0:
                server = self.servers[index]
                # XXX: this looks totally dubious:
                #
                # (a) we never reuse a server until we have been through
                #     all of the servers at the same priority, so if the
                #     weights are A: 100, B:1, we always do ABABAB instead of
                #     AAAA...AAAB (approximately).
                #
                # (b) After using all the servers at the lowest priority,
                #     we move onto the next priority. We should only use the
                #     second priority if servers at the top priority are
                #     unreachable.
                #
                del self.servers[index]
                self.used_servers.append(server)
                return server

    @defer.inlineCallbacks
    def connect(self, protocolFactory):
        if self.servers is None:
            yield self.fetch_servers()
        server = self.pick_server()
        logger.info("Connecting to %s:%s", server.host, server.port)
        endpoint = self.endpoint(
            self.reactor, server.host, server.port, **self.endpoint_kw_args
        )
        connection = yield endpoint.connect(protocolFactory)
        defer.returnValue(connection)
