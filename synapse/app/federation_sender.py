#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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

import synapse

from synapse.server import HomeServer
from synapse.config._base import ConfigError
from synapse.config.logger import setup_logging
from synapse.config.homeserver import HomeServerConfig
from synapse.crypto import context_factory
from synapse.http.site import SynapseSite
from synapse.federation import send_queue
from synapse.federation.units import Edu
from synapse.metrics.resource import MetricsResource, METRICS_PREFIX
from synapse.replication.slave.storage.deviceinbox import SlavedDeviceInboxStore
from synapse.replication.slave.storage.events import SlavedEventStore
from synapse.replication.slave.storage.receipts import SlavedReceiptsStore
from synapse.replication.slave.storage.registration import SlavedRegistrationStore
from synapse.replication.slave.storage.transactions import TransactionStore
from synapse.replication.slave.storage.devices import SlavedDeviceStore
from synapse.replication.tcp.client import ReplicationClientHandler
from synapse.storage.engines import create_engine
from synapse.storage.presence import UserPresenceState
from synapse.util.async import Linearizer
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext, PreserveLoggingContext
from synapse.util.manhole import manhole
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string

from synapse import events

from twisted.internet import reactor, defer
from twisted.web.resource import Resource

from daemonize import Daemonize

import sys
import logging
import gc
import ujson as json

logger = logging.getLogger("synapse.app.appservice")


class FederationSenderSlaveStore(
    SlavedDeviceInboxStore, TransactionStore, SlavedReceiptsStore, SlavedEventStore,
    SlavedRegistrationStore, SlavedDeviceStore,
):
    def __init__(self, db_conn, hs):
        super(FederationSenderSlaveStore, self).__init__(db_conn, hs)
        self.federation_out_pos_startup = self._get_federation_out_pos(db_conn)

    def _get_federation_out_pos(self, db_conn):
        sql = (
            "SELECT stream_id FROM federation_stream_position"
            " WHERE type = ?"
        )
        sql = self.database_engine.convert_param_style(sql)

        txn = db_conn.cursor()
        txn.execute(sql, ("federation",))
        rows = txn.fetchall()
        txn.close()

        return rows[0][0] if rows else -1


class FederationSenderServer(HomeServer):
    def get_db_conn(self, run_new_connection=True):
        # Any param beginning with cp_ is a parameter for adbapi, and should
        # not be passed to the database engine.
        db_params = {
            k: v for k, v in self.db_config.get("args", {}).items()
            if not k.startswith("cp_")
        }
        db_conn = self.database_engine.module.connect(**db_params)

        if run_new_connection:
            self.database_engine.on_new_connection(db_conn)
        return db_conn

    def setup(self):
        logger.info("Setting up.")
        self.datastore = FederationSenderSlaveStore(self.get_db_conn(), self)
        logger.info("Finished setting up.")

    def _listen_http(self, listener_config):
        port = listener_config["port"]
        bind_addresses = listener_config["bind_addresses"]
        site_tag = listener_config.get("tag", port)
        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(self)

        root_resource = create_resource_tree(resources, Resource())

        for address in bind_addresses:
            reactor.listenTCP(
                port,
                SynapseSite(
                    "synapse.access.http.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                ),
                interface=address
            )

        logger.info("Synapse federation_sender now listening on port %d", port)

    def start_listening(self, listeners):
        for listener in listeners:
            if listener["type"] == "http":
                self._listen_http(listener)
            elif listener["type"] == "manhole":
                bind_addresses = listener["bind_addresses"]

                for address in bind_addresses:
                    reactor.listenTCP(
                        listener["port"],
                        manhole(
                            username="matrix",
                            password="rabbithole",
                            globals={"hs": self},
                        ),
                        interface=address
                    )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

        self.get_tcp_replication().start_replication(self)

    def build_tcp_replication(self):
        return FederationSenderReplicationHandler(self)


class FederationSenderReplicationHandler(ReplicationClientHandler):
    def __init__(self, hs):
        super(FederationSenderReplicationHandler, self).__init__(hs.get_datastore())
        self.send_handler = FederationSenderHandler(hs)

    def on_rdata(self, stream_name, token, rows):
        super(FederationSenderReplicationHandler, self).on_rdata(
            stream_name, token, rows
        )
        self.send_handler.process_replication_rows(stream_name, token, rows)
        if stream_name == "federation":
            self.send_federation_ack(token)

    def get_streams_to_replicate(self):
        args = super(FederationSenderReplicationHandler, self).get_streams_to_replicate()
        args.update(self.send_handler.stream_positions())
        return args


def start(config_options):
    try:
        config = HomeServerConfig.load_config(
            "Synapse federation sender", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    assert config.worker_app == "synapse.app.federation_sender"

    setup_logging(config, use_worker_options=True)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    database_engine = create_engine(config.database_config)

    if config.send_federation:
        sys.stderr.write(
            "\nThe send_federation must be disabled in the main synapse process"
            "\nbefore they can be run in a separate worker."
            "\nPlease add ``send_federation: false`` to the main config"
            "\n"
        )
        sys.exit(1)

    # Force the pushers to start since they will be disabled in the main config
    config.send_federation = True

    tls_server_context_factory = context_factory.ServerContextFactory(config)

    ps = FederationSenderServer(
        config.server_name,
        db_config=config.database_config,
        tls_server_context_factory=tls_server_context_factory,
        config=config,
        version_string="Synapse/" + get_version_string(synapse),
        database_engine=database_engine,
    )

    ps.setup()
    ps.start_listening(config.worker_listeners)

    def run():
        # make sure that we run the reactor with the sentinel log context,
        # otherwise other PreserveLoggingContext instances will get confused
        # and complain when they see the logcontext arbitrarily swapping
        # between the sentinel and `run` logcontexts.
        with PreserveLoggingContext():
            logger.info("Running")
            change_resource_limit(config.soft_file_limit)
            if config.gc_thresholds:
                gc.set_threshold(*config.gc_thresholds)
            reactor.run()

    def start():
        ps.get_datastore().start_profiling()
        ps.get_state_handler().start_caching()

    reactor.callWhenRunning(start)

    if config.worker_daemonize:
        daemon = Daemonize(
            app="synapse-federation-sender",
            pid=config.worker_pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )
        daemon.start()
    else:
        run()


class FederationSenderHandler(object):
    """Processes the replication stream and forwards the appropriate entries
    to the federation sender.
    """
    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.federation_sender = hs.get_federation_sender()

        self.federation_position = self.store.federation_out_pos_startup
        self._fed_position_linearizer = Linearizer(name="_fed_position_linearizer")

        self._room_serials = {}
        self._room_typing = {}

    def on_start(self):
        # There may be some events that are persisted but haven't been sent,
        # so send them now.
        self.federation_sender.notify_new_events(
            self.store.get_room_max_stream_ordering()
        )

    def stream_positions(self):
        return {"federation": self.federation_position}

    def process_replication_rows(self, stream_name, token, rows):
        # The federation stream contains things that we want to send out, e.g.
        # presence, typing, etc.
        if stream_name == "federation":
            # The federation stream containis a bunch of different types of
            # rows that need to be handled differently. We parse the rows, put
            # them into the appropriate collection and then send them off.
            presence_to_send = {}
            keyed_edus = {}
            edus = {}
            failures = {}
            device_destinations = set()

            # Parse the rows in the stream
            for row in rows:
                typ = row.type
                content_js = row.data
                content = json.loads(content_js)

                if typ == send_queue.PRESENCE_TYPE:
                    destination = content["destination"]
                    state = UserPresenceState.from_dict(content["state"])

                    presence_to_send.setdefault(destination, []).append(state)
                elif typ == send_queue.KEYED_EDU_TYPE:
                    key = content["key"]
                    edu = Edu(**content["edu"])

                    keyed_edus.setdefault(
                        edu.destination, {}
                    )[(edu.destination, tuple(key))] = edu
                elif typ == send_queue.EDU_TYPE:
                    edu = Edu(**content)

                    edus.setdefault(edu.destination, []).append(edu)
                elif typ == send_queue.FAILURE_TYPE:
                    destination = content["destination"]
                    failure = content["failure"]

                    failures.setdefault(destination, []).append(failure)
                elif typ == send_queue.DEVICE_MESSAGE_TYPE:
                    device_destinations.add(content["destination"])
                else:
                    raise Exception("Unrecognised federation type: %r", typ)

            # We've finished collecting, send everything off
            for destination, states in presence_to_send.items():
                self.federation_sender.send_presence(destination, states)

            for destination, edu_map in keyed_edus.items():
                for key, edu in edu_map.items():
                    self.federation_sender.send_edu(
                        edu.destination, edu.edu_type, edu.content, key=key,
                    )

            for destination, edu_list in edus.items():
                for edu in edu_list:
                    self.federation_sender.send_edu(
                        edu.destination, edu.edu_type, edu.content, key=None,
                    )

            for destination, failure_list in failures.items():
                for failure in failure_list:
                    self.federation_sender.send_failure(destination, failure)

            for destination in device_destinations:
                self.federation_sender.send_device_messages(destination)

            self.update_token(token)

        # We also need to poke the federation sender when new events happen
        elif stream_name == "events":
            self.federation_sender.notify_new_events(token)

    @defer.inlineCallbacks
    def update_token(self, token):
        self.federation_position = token
        with (yield self._fed_position_linearizer.queue(None)):
            yield self.store.update_federation_out_pos(
                "federation", self.federation_position
            )


if __name__ == '__main__':
    with LoggingContext("main"):
        start(sys.argv[1:])
