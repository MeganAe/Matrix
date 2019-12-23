#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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

from __future__ import print_function

import gc
import logging
import math
import os
import resource
import sys

from six import iteritems

from prometheus_client import Gauge

from twisted.application import service
from twisted.internet import defer, reactor
from twisted.python.failure import Failure
from twisted.web.resource import EncodingResourceWrapper, NoResource
from twisted.web.server import GzipEncoderFactory
from twisted.web.static import File

import synapse
import synapse.config.logger
from synapse import events
from synapse.api.urls import (
    CONTENT_REPO_PREFIX,
    FEDERATION_PREFIX,
    LEGACY_MEDIA_PREFIX,
    MEDIA_PREFIX,
    SERVER_KEY_V2_PREFIX,
    STATIC_PREFIX,
    WEB_CLIENT_PREFIX,
)
from synapse.app import _base
from synapse.app._base import listen_ssl, listen_tcp, quit_with_error
from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.federation.transport.server import TransportLayerServer
from synapse.http.additional_resource import AdditionalResource
from synapse.http.server import RootRedirect
from synapse.http.site import SynapseSite
from synapse.logging.context import LoggingContext
from synapse.metrics import METRICS_PREFIX, MetricsResource, RegistryProxy
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import ModuleApi
from synapse.python_dependencies import check_requirements
from synapse.replication.http import REPLICATION_PREFIX, ReplicationRestResource
from synapse.replication.tcp.resource import ReplicationStreamProtocolFactory
from synapse.rest import ClientRestResource
from synapse.rest.admin import AdminRestResource
from synapse.rest.key.v2 import KeyApiV2Resource
from synapse.rest.media.v0.content_repository import ContentRepoResource
from synapse.rest.well_known import WellKnownResource
from synapse.server import HomeServer
from synapse.storage import DataStore
from synapse.storage.engines import IncorrectDatabaseSetup
from synapse.storage.prepare_database import UpgradeDatabaseException
from synapse.util.caches import CACHE_SIZE_FACTOR
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.manhole import manhole
from synapse.util.module_loader import load_module
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string

logger = logging.getLogger("synapse.app.homeserver")


def gz_wrap(r):
    return EncodingResourceWrapper(r, [GzipEncoderFactory()])


class SynapseHomeServer(HomeServer):
    DATASTORE_CLASS = DataStore

    def _listener_http(self, config, listener_config):
        port = listener_config["port"]
        bind_addresses = listener_config["bind_addresses"]
        tls = listener_config.get("tls", False)
        site_tag = listener_config.get("tag", port)

        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "openid" and "federation" in res["names"]:
                    # Skip loading openid resource if federation is defined
                    # since federation resource will include openid
                    continue
                resources.update(
                    self._configure_named_resource(name, res.get("compress", False))
                )

        additional_resources = listener_config.get("additional_resources", {})
        logger.debug("Configuring additional resources: %r", additional_resources)
        module_api = ModuleApi(self, self.get_auth_handler())
        for path, resmodule in additional_resources.items():
            handler_cls, config = load_module(resmodule)
            handler = handler_cls(config, module_api)
            resources[path] = AdditionalResource(self, handler.handle_request)

        # try to find something useful to redirect '/' to
        if WEB_CLIENT_PREFIX in resources:
            root_resource = RootRedirect(WEB_CLIENT_PREFIX)
        elif STATIC_PREFIX in resources:
            root_resource = RootRedirect(STATIC_PREFIX)
        else:
            root_resource = NoResource()

        root_resource = create_resource_tree(resources, root_resource)

        if tls:
            ports = listen_ssl(
                bind_addresses,
                port,
                SynapseSite(
                    "synapse.access.https.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                    self.version_string,
                ),
                self.tls_server_context_factory,
                reactor=self.get_reactor(),
            )
            logger.info("Synapse now listening on TCP port %d (TLS)", port)

        else:
            ports = listen_tcp(
                bind_addresses,
                port,
                SynapseSite(
                    "synapse.access.http.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                    self.version_string,
                ),
                reactor=self.get_reactor(),
            )
            logger.info("Synapse now listening on TCP port %d", port)

        return ports

    def _configure_named_resource(self, name, compress=False):
        """Build a resource map for a named resource

        Args:
            name (str): named resource: one of "client", "federation", etc
            compress (bool): whether to enable gzip compression for this
                resource

        Returns:
            dict[str, Resource]: map from path to HTTP resource
        """
        resources = {}
        if name == "client":
            client_resource = ClientRestResource(self)
            if compress:
                client_resource = gz_wrap(client_resource)

            resources.update(
                {
                    "/_matrix/client/api/v1": client_resource,
                    "/_matrix/client/r0": client_resource,
                    "/_matrix/client/unstable": client_resource,
                    "/_matrix/client/v2_alpha": client_resource,
                    "/_matrix/client/versions": client_resource,
                    "/.well-known/matrix/client": WellKnownResource(self),
                    "/_synapse/admin": AdminRestResource(self),
                }
            )

            if self.get_config().saml2_enabled:
                from synapse.rest.saml2 import SAML2Resource

                resources["/_matrix/saml2"] = SAML2Resource(self)

        if name == "consent":
            from synapse.rest.consent.consent_resource import ConsentResource

            consent_resource = ConsentResource(self)
            if compress:
                consent_resource = gz_wrap(consent_resource)
            resources.update({"/_matrix/consent": consent_resource})

        if name == "federation":
            resources.update({FEDERATION_PREFIX: TransportLayerServer(self)})

        if name == "openid":
            resources.update(
                {
                    FEDERATION_PREFIX: TransportLayerServer(
                        self, servlet_groups=["openid"]
                    )
                }
            )

        if name in ["static", "client"]:
            resources.update(
                {
                    STATIC_PREFIX: File(
                        os.path.join(os.path.dirname(synapse.__file__), "static")
                    )
                }
            )

        if name in ["media", "federation", "client"]:
            if self.get_config().enable_media_repo:
                media_repo = self.get_media_repository_resource()
                resources.update(
                    {
                        MEDIA_PREFIX: media_repo,
                        LEGACY_MEDIA_PREFIX: media_repo,
                        CONTENT_REPO_PREFIX: ContentRepoResource(
                            self, self.config.uploads_path
                        ),
                    }
                )
            elif name == "media":
                raise ConfigError(
                    "'media' resource conflicts with enable_media_repo=False"
                )

        if name in ["keys", "federation"]:
            resources[SERVER_KEY_V2_PREFIX] = KeyApiV2Resource(self)

        if name == "webclient":
            webclient_path = self.get_config().web_client_location

            if webclient_path is None:
                logger.warning(
                    "Not enabling webclient resource, as web_client_location is unset."
                )
            else:
                # GZip is disabled here due to
                # https://twistedmatrix.com/trac/ticket/7678
                resources[WEB_CLIENT_PREFIX] = File(webclient_path)

        if name == "metrics" and self.get_config().enable_metrics:
            resources[METRICS_PREFIX] = MetricsResource(RegistryProxy)

        if name == "replication":
            resources[REPLICATION_PREFIX] = ReplicationRestResource(self)

        return resources

    def start_listening(self, listeners):
        config = self.get_config()

        for listener in listeners:
            if listener["type"] == "http":
                self._listening_services.extend(self._listener_http(config, listener))
            elif listener["type"] == "manhole":
                listen_tcp(
                    listener["bind_addresses"],
                    listener["port"],
                    manhole(
                        username="matrix", password="rabbithole", globals={"hs": self}
                    ),
                )
            elif listener["type"] == "replication":
                services = listen_tcp(
                    listener["bind_addresses"],
                    listener["port"],
                    ReplicationStreamProtocolFactory(self),
                )
                for s in services:
                    reactor.addSystemEventTrigger("before", "shutdown", s.stopListening)
            elif listener["type"] == "metrics":
                if not self.get_config().enable_metrics:
                    logger.warning(
                        (
                            "Metrics listener configured, but "
                            "enable_metrics is not True!"
                        )
                    )
                else:
                    _base.listen_metrics(listener["bind_addresses"], listener["port"])
            else:
                logger.warning("Unrecognized listener type: %s", listener["type"])


# Gauges to expose monthly active user control metrics
current_mau_gauge = Gauge("synapse_admin_mau:current", "Current MAU")
max_mau_gauge = Gauge("synapse_admin_mau:max", "MAU Limit")
registered_reserved_users_mau_gauge = Gauge(
    "synapse_admin_mau:registered_reserved_users",
    "Registered users with reserved threepids",
)


def setup(config_options):
    """
    Args:
        config_options_options: The options passed to Synapse. Usually
            `sys.argv[1:]`.

    Returns:
        HomeServer
    """
    try:
        config = HomeServerConfig.load_or_generate_config(
            "Synapse Homeserver", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + str(e) + "\n")
        sys.exit(1)

    if not config:
        # If a config isn't returned, and an exception isn't raised, we're just
        # generating config files and shouldn't try to continue.
        sys.exit(0)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    hs = SynapseHomeServer(
        config.server_name,
        config=config,
        version_string="Synapse/" + get_version_string(synapse),
    )

    synapse.config.logger.setup_logging(hs, config, use_worker_options=False)

    logger.info("Setting up server")

    try:
        hs.setup()
    except IncorrectDatabaseSetup as e:
        quit_with_error(str(e))
    except UpgradeDatabaseException as e:
        quit_with_error("Failed to upgrade database: %s" % (e,))

    hs.setup_master()

    @defer.inlineCallbacks
    def do_acme():
        """
        Reprovision an ACME certificate, if it's required.

        Returns:
            Deferred[bool]: Whether the cert has been updated.
        """
        acme = hs.get_acme_handler()

        # Check how long the certificate is active for.
        cert_days_remaining = hs.config.is_disk_cert_valid(allow_self_signed=False)

        # We want to reprovision if cert_days_remaining is None (meaning no
        # certificate exists), or the days remaining number it returns
        # is less than our re-registration threshold.
        provision = False

        if (
            cert_days_remaining is None
            or cert_days_remaining < hs.config.acme_reprovision_threshold
        ):
            provision = True

        if provision:
            yield acme.provision_certificate()

        return provision

    @defer.inlineCallbacks
    def reprovision_acme():
        """
        Provision a certificate from ACME, if required, and reload the TLS
        certificate if it's renewed.
        """
        reprovisioned = yield do_acme()
        if reprovisioned:
            _base.refresh_certificate(hs)

    @defer.inlineCallbacks
    def start():
        try:
            # Run the ACME provisioning code, if it's enabled.
            if hs.config.acme_enabled:
                acme = hs.get_acme_handler()
                # Start up the webservices which we will respond to ACME
                # challenges with, and then provision.
                yield acme.start_listening()
                yield do_acme()

                # Check if it needs to be reprovisioned every day.
                hs.get_clock().looping_call(reprovision_acme, 24 * 60 * 60 * 1000)

            _base.start(hs, config.listeners)

            hs.get_pusherpool().start()
            hs.get_datastore().db.updates.start_doing_background_updates()
        except Exception:
            # Print the exception and bail out.
            print("Error during startup:", file=sys.stderr)

            # this gives better tracebacks than traceback.print_exc()
            Failure().printTraceback(file=sys.stderr)

            if reactor.running:
                reactor.stop()
            sys.exit(1)

    reactor.callWhenRunning(start)

    return hs


class SynapseService(service.Service):
    """
    A twisted Service class that will start synapse. Used to run synapse
    via twistd and a .tac.
    """

    def __init__(self, config):
        self.config = config

    def startService(self):
        hs = setup(self.config)
        change_resource_limit(hs.config.soft_file_limit)
        if hs.config.gc_thresholds:
            gc.set_threshold(*hs.config.gc_thresholds)

    def stopService(self):
        return self._port.stopListening()


# Contains the list of processes we will be monitoring
# currently either 0 or 1
_stats_process = []


@defer.inlineCallbacks
def phone_stats_home(hs, stats, stats_process=_stats_process):
    logger.info("Gathering stats for reporting")
    now = int(hs.get_clock().time())
    uptime = int(now - hs.start_time)
    if uptime < 0:
        uptime = 0

    stats["homeserver"] = hs.config.server_name
    stats["server_context"] = hs.config.server_context
    stats["timestamp"] = now
    stats["uptime_seconds"] = uptime
    version = sys.version_info
    stats["python_version"] = "{}.{}.{}".format(
        version.major, version.minor, version.micro
    )
    stats["total_users"] = yield hs.get_datastore().count_all_users()

    total_nonbridged_users = yield hs.get_datastore().count_nonbridged_users()
    stats["total_nonbridged_users"] = total_nonbridged_users

    daily_user_type_results = yield hs.get_datastore().count_daily_user_type()
    for name, count in iteritems(daily_user_type_results):
        stats["daily_user_type_" + name] = count

    room_count = yield hs.get_datastore().get_room_count()
    stats["total_room_count"] = room_count

    stats["daily_active_users"] = yield hs.get_datastore().count_daily_users()
    stats["monthly_active_users"] = yield hs.get_datastore().count_monthly_users()
    stats["daily_active_rooms"] = yield hs.get_datastore().count_daily_active_rooms()
    stats["daily_messages"] = yield hs.get_datastore().count_daily_messages()

    r30_results = yield hs.get_datastore().count_r30_users()
    for name, count in iteritems(r30_results):
        stats["r30_users_" + name] = count

    daily_sent_messages = yield hs.get_datastore().count_daily_sent_messages()
    stats["daily_sent_messages"] = daily_sent_messages
    stats["cache_factor"] = CACHE_SIZE_FACTOR
    stats["event_cache_size"] = hs.config.event_cache_size

    #
    # Performance statistics
    #
    old = stats_process[0]
    new = (now, resource.getrusage(resource.RUSAGE_SELF))
    stats_process[0] = new

    # Get RSS in bytes
    stats["memory_rss"] = new[1].ru_maxrss

    # Get CPU time in % of a single core, not % of all cores
    used_cpu_time = (new[1].ru_utime + new[1].ru_stime) - (
        old[1].ru_utime + old[1].ru_stime
    )
    if used_cpu_time == 0 or new[0] == old[0]:
        stats["cpu_average"] = 0
    else:
        stats["cpu_average"] = math.floor(used_cpu_time / (new[0] - old[0]) * 100)

    #
    # Database version
    #

    # This only reports info about the *main* database.
    stats["database_engine"] = hs.get_datastore().db.engine.module.__name__
    stats["database_server_version"] = hs.get_datastore().db.engine.server_version

    logger.info("Reporting stats to %s: %s" % (hs.config.report_stats_endpoint, stats))
    try:
        yield hs.get_proxied_http_client().put_json(
            hs.config.report_stats_endpoint, stats
        )
    except Exception as e:
        logger.warning("Error reporting stats: %s", e)


def run(hs):
    PROFILE_SYNAPSE = False
    if PROFILE_SYNAPSE:

        def profile(func):
            from cProfile import Profile
            from threading import current_thread

            def profiled(*args, **kargs):
                profile = Profile()
                profile.enable()
                func(*args, **kargs)
                profile.disable()
                ident = current_thread().ident
                profile.dump_stats(
                    "/tmp/%s.%s.%i.pstat" % (hs.hostname, func.__name__, ident)
                )

            return profiled

        from twisted.python.threadpool import ThreadPool

        ThreadPool._worker = profile(ThreadPool._worker)
        reactor.run = profile(reactor.run)

    clock = hs.get_clock()

    stats = {}

    def performance_stats_init():
        _stats_process.clear()
        _stats_process.append(
            (int(hs.get_clock().time()), resource.getrusage(resource.RUSAGE_SELF))
        )

    def start_phone_stats_home():
        return run_as_background_process(
            "phone_stats_home", phone_stats_home, hs, stats
        )

    def generate_user_daily_visit_stats():
        return run_as_background_process(
            "generate_user_daily_visits", hs.get_datastore().generate_user_daily_visits
        )

    # Rather than update on per session basis, batch up the requests.
    # If you increase the loop period, the accuracy of user_daily_visits
    # table will decrease
    clock.looping_call(generate_user_daily_visit_stats, 5 * 60 * 1000)

    # monthly active user limiting functionality
    def reap_monthly_active_users():
        return run_as_background_process(
            "reap_monthly_active_users", hs.get_datastore().reap_monthly_active_users
        )

    clock.looping_call(reap_monthly_active_users, 1000 * 60 * 60)
    reap_monthly_active_users()

    @defer.inlineCallbacks
    def generate_monthly_active_users():
        current_mau_count = 0
        reserved_users = ()
        store = hs.get_datastore()
        if hs.config.limit_usage_by_mau or hs.config.mau_stats_only:
            current_mau_count = yield store.get_monthly_active_count()
            reserved_users = yield store.get_registered_reserved_users()
        current_mau_gauge.set(float(current_mau_count))
        registered_reserved_users_mau_gauge.set(float(len(reserved_users)))
        max_mau_gauge.set(float(hs.config.max_mau_value))

    def start_generate_monthly_active_users():
        return run_as_background_process(
            "generate_monthly_active_users", generate_monthly_active_users
        )

    start_generate_monthly_active_users()
    if hs.config.limit_usage_by_mau or hs.config.mau_stats_only:
        clock.looping_call(start_generate_monthly_active_users, 5 * 60 * 1000)
    # End of monthly active user settings

    if hs.config.report_stats:
        logger.info("Scheduling stats reporting for 3 hour intervals")
        clock.looping_call(start_phone_stats_home, 3 * 60 * 60 * 1000)

        # We need to defer this init for the cases that we daemonize
        # otherwise the process ID we get is that of the non-daemon process
        clock.call_later(0, performance_stats_init)

        # We wait 5 minutes to send the first set of stats as the server can
        # be quite busy the first few minutes
        clock.call_later(5 * 60, start_phone_stats_home)

    _base.start_reactor(
        "synapse-homeserver",
        soft_file_limit=hs.config.soft_file_limit,
        gc_thresholds=hs.config.gc_thresholds,
        pid_file=hs.config.pid_file,
        daemonize=hs.config.daemonize,
        print_pidfile=hs.config.print_pidfile,
        logger=logger,
    )


def main():
    with LoggingContext("main"):
        # check base requirements
        check_requirements()
        hs = setup(sys.argv[1:])
        run(hs)


if __name__ == "__main__":
    main()
