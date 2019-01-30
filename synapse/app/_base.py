# -*- coding: utf-8 -*-
# Copyright 2017 New Vector Ltd
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

import gc
import logging
import sys

import psutil
from daemonize import Daemonize

from twisted.internet import error, reactor

from synapse.app import check_bind_error
from synapse.util import PreserveLoggingContext
from synapse.util.rlimit import change_resource_limit

logger = logging.getLogger(__name__)


def start_worker_reactor(appname, config):
    """ Run the reactor in the main process

    Daemonizes if necessary, and then configures some resources, before starting
    the reactor. Pulls configuration from the 'worker' settings in 'config'.

    Args:
        appname (str): application name which will be sent to syslog
        config (synapse.config.Config): config object
    """

    logger = logging.getLogger(config.worker_app)

    start_reactor(
        appname,
        config.soft_file_limit,
        config.gc_thresholds,
        config.worker_pid_file,
        config.worker_daemonize,
        config.worker_cpu_affinity,
        logger,
    )


def start_reactor(
        appname,
        soft_file_limit,
        gc_thresholds,
        pid_file,
        daemonize,
        cpu_affinity,
        logger,
):
    """ Run the reactor in the main process

    Daemonizes if necessary, and then configures some resources, before starting
    the reactor

    Args:
        appname (str): application name which will be sent to syslog
        soft_file_limit (int):
        gc_thresholds:
        pid_file (str): name of pid file to write to if daemonize is True
        daemonize (bool): true to run the reactor in a background process
        cpu_affinity (int|None): cpu affinity mask
        logger (logging.Logger): logger instance to pass to Daemonize
    """

    def run():
        # make sure that we run the reactor with the sentinel log context,
        # otherwise other PreserveLoggingContext instances will get confused
        # and complain when they see the logcontext arbitrarily swapping
        # between the sentinel and `run` logcontexts.
        with PreserveLoggingContext():
            logger.info("Running")
            if cpu_affinity is not None:
                # Turn the bitmask into bits, reverse it so we go from 0 up
                mask_to_bits = bin(cpu_affinity)[2:][::-1]

                cpus = []
                cpu_num = 0

                for i in mask_to_bits:
                    if i == "1":
                        cpus.append(cpu_num)
                    cpu_num += 1

                p = psutil.Process()
                p.cpu_affinity(cpus)

            change_resource_limit(soft_file_limit)
            if gc_thresholds:
                gc.set_threshold(*gc_thresholds)
            reactor.run()

    if daemonize:
        daemon = Daemonize(
            app=appname,
            pid=pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )
        daemon.start()
    else:
        run()


def quit_with_error(error_string):
    message_lines = error_string.split("\n")
    line_length = max([len(l) for l in message_lines if len(l) < 80]) + 2
    sys.stderr.write("*" * line_length + '\n')
    for line in message_lines:
        sys.stderr.write(" %s\n" % (line.rstrip(),))
    sys.stderr.write("*" * line_length + '\n')
    sys.exit(1)


def listen_metrics(bind_addresses, port):
    """
    Start Prometheus metrics server.
    """
    from synapse.metrics import RegistryProxy
    from prometheus_client import start_http_server

    for host in bind_addresses:
        reactor.callInThread(start_http_server, int(port),
                             addr=host, registry=RegistryProxy)
        logger.info("Metrics now reporting on %s:%d", host, port)


def listen_tcp(bind_addresses, port, factory, reactor=reactor, backlog=50):
    """
    Create a TCP socket for a port and several addresses

    Returns:
        list (empty)
    """
    for address in bind_addresses:
        try:
            reactor.listenTCP(
                port,
                factory,
                backlog,
                address
            )
        except error.CannotListenError as e:
            check_bind_error(e, address, bind_addresses)

    logger.info("Synapse now listening on TCP port %d", port)
    return []


def listen_ssl(
    bind_addresses, port, factory, context_factory, reactor=reactor, backlog=50
):
    """
    Create an TLS-over-TCP socket for a port and several addresses

    Returns:
        list of twisted.internet.tcp.Port listening for TLS connections
    """
    r = []
    for address in bind_addresses:
        try:
            r.append(
                reactor.listenSSL(
                    port,
                    factory,
                    context_factory,
                    backlog,
                    address
                )
            )
        except error.CannotListenError as e:
            check_bind_error(e, address, bind_addresses)

    logger.info("Synapse now listening on port %d (TLS)", port)
    return r
