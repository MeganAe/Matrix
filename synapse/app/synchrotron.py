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

from synapse.api.constants import EventTypes, PresenceState
from synapse.config._base import ConfigError
from synapse.config.database import DatabaseConfig
from synapse.config.logger import LoggingConfig
from synapse.config.appservice import AppServiceConfig
from synapse.events import FrozenEvent
from synapse.handlers.presence import PresenceHandler
from synapse.http.site import SynapseSite
from synapse.http.server import JsonResource
from synapse.metrics.resource import MetricsResource, METRICS_PREFIX
from synapse.rest.client.v2_alpha import sync
from synapse.replication.slave.storage._base import BaseSlavedStore
from synapse.replication.slave.storage.events import SlavedEventStore
from synapse.replication.slave.storage.receipts import SlavedReceiptsStore
from synapse.replication.slave.storage.account_data import SlavedAccountDataStore
from synapse.replication.slave.storage.appservice import SlavedApplicationServiceStore
from synapse.replication.slave.storage.registration import SlavedRegistrationStore
from synapse.replication.slave.storage.filtering import SlavedFilteringStore
from synapse.replication.slave.storage.push_rule import SlavedPushRuleStore
from synapse.replication.slave.storage.presence import SlavedPresenceStore
from synapse.server import HomeServer
from synapse.storage.client_ips import ClientIpStore
from synapse.storage.engines import create_engine
from synapse.storage.presence import PresenceStore, UserPresenceState
from synapse.storage.roommember import RoomMemberStore
from synapse.util.async import sleep
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext, preserve_fn
from synapse.util.manhole import manhole
from synapse.util.rlimit import change_resource_limit
from synapse.util.stringutils import random_string
from synapse.util.versionstring import get_version_string

from twisted.internet import reactor, defer
from twisted.web.resource import Resource

from daemonize import Daemonize

import sys
import logging
import contextlib
import gc
import ujson as json

logger = logging.getLogger("synapse.app.synchrotron")


class SynchrotronConfig(DatabaseConfig, LoggingConfig, AppServiceConfig):
    def read_config(self, config):
        self.replication_url = config["replication_url"]
        self.server_name = config["server_name"]
        self.use_insecure_ssl_client_just_for_testing_do_not_use = config.get(
            "use_insecure_ssl_client_just_for_testing_do_not_use", False
        )
        self.user_agent_suffix = None
        self.listeners = config["listeners"]
        self.soft_file_limit = config.get("soft_file_limit")
        self.daemonize = config.get("daemonize")
        self.pid_file = self.abspath(config.get("pid_file"))
        self.macaroon_secret_key = config["macaroon_secret_key"]
        self.expire_access_token = config.get("expire_access_token", False)

        thresholds = config.get("gc_thresholds", None)
        if thresholds is not None:
            try:
                assert len(thresholds) == 3
                self.gc_thresholds = (
                    int(thresholds[0]), int(thresholds[1]), int(thresholds[2]),
                )
            except:
                raise ConfigError(
                    "Value of `gc_threshold` must be a list of three integers if set"
                )
        else:
            self.gc_thresholds = None

    def default_config(self, server_name, **kwargs):
        pid_file = self.abspath("synchroton.pid")
        return """\
        # Slave configuration

        # The replication listener on the synapse to talk to.
        #replication_url: https://localhost:{replication_port}/_synapse/replication

        server_name: "%(server_name)s"

        listeners:
        # Enable a /sync listener on the synchrontron
        #- type: http
        #    port: {http_port}
        #    bind_address: ""
        # Enable a ssh manhole listener on the synchrotron
        # - type: manhole
        #   port: {manhole_port}
        #   bind_address: 127.0.0.1
        # Enable a metric listener on the synchrotron
        # - type: http
        #   port: {metrics_port}
        #   bind_address: 127.0.0.1
        #   resources:
        #    - names: ["metrics"]
        #      compress: False

        report_stats: False

        daemonize: False

        pid_file: %(pid_file)s
        """ % locals()


class SynchrotronSlavedStore(
    SlavedPushRuleStore,
    SlavedEventStore,
    SlavedReceiptsStore,
    SlavedAccountDataStore,
    SlavedApplicationServiceStore,
    SlavedRegistrationStore,
    SlavedFilteringStore,
    SlavedPresenceStore,
    BaseSlavedStore,
    ClientIpStore,  # After BaseSlavedStore because the constructor is different
):
    # XXX: This is a bit broken because we don't persist forgotten rooms
    # in a way that they can be streamed. This means that we don't have a
    # way to invalidate the forgotten rooms cache correctly.
    # For now we expire the cache every 10 minutes.
    BROKEN_CACHE_EXPIRY_MS = 60 * 60 * 1000
    who_forgot_in_room = (
        RoomMemberStore.__dict__["who_forgot_in_room"]
    )

    # XXX: This is a bit broken because we don't persist the accepted list in a
    # way that can be replicated. This means that we don't have a way to
    # invalidate the cache correctly.
    get_presence_list_accepted = PresenceStore.__dict__[
        "get_presence_list_accepted"
    ]

UPDATE_SYNCING_USERS_MS = 10 * 1000


class SynchrotronPresence(object):
    def __init__(self, hs):
        self.http_client = hs.get_simple_http_client()
        self.store = hs.get_datastore()
        self.user_to_num_current_syncs = {}
        self.syncing_users_url = hs.config.replication_url + "/syncing_users"
        self.clock = hs.get_clock()

        active_presence = self.store.take_presence_startup_info()
        self.user_to_current_state = {
            state.user_id: state
            for state in active_presence
        }

        self.process_id = random_string(16)
        logger.info("Presence process_id is %r", self.process_id)

        self._sending_sync = False
        self._need_to_send_sync = False
        self.clock.looping_call(
            self._send_syncing_users_regularly,
            UPDATE_SYNCING_USERS_MS,
        )

        reactor.addSystemEventTrigger("before", "shutdown", self._on_shutdown)

    def set_state(self, user, state):
        # TODO Hows this supposed to work?
        pass

    get_states = PresenceHandler.get_states.__func__
    current_state_for_users = PresenceHandler.current_state_for_users.__func__

    @defer.inlineCallbacks
    def user_syncing(self, user_id, affect_presence):
        if affect_presence:
            curr_sync = self.user_to_num_current_syncs.get(user_id, 0)
            self.user_to_num_current_syncs[user_id] = curr_sync + 1
            prev_states = yield self.current_state_for_users([user_id])
            if prev_states[user_id].state == PresenceState.OFFLINE:
                # TODO: Don't block the sync request on this HTTP hit.
                yield self._send_syncing_users_now()

        def _end():
            # We check that the user_id is in user_to_num_current_syncs because
            # user_to_num_current_syncs may have been cleared if we are
            # shutting down.
            if affect_presence and user_id in self.user_to_num_current_syncs:
                self.user_to_num_current_syncs[user_id] -= 1

        @contextlib.contextmanager
        def _user_syncing():
            try:
                yield
            finally:
                _end()

        defer.returnValue(_user_syncing())

    @defer.inlineCallbacks
    def _on_shutdown(self):
        # When the synchrotron is shutdown tell the master to clear the in
        # progress syncs for this process
        self.user_to_num_current_syncs.clear()
        yield self._send_syncing_users_now()

    def _send_syncing_users_regularly(self):
        # Only send an update if we aren't in the middle of sending one.
        if not self._sending_sync:
            preserve_fn(self._send_syncing_users_now)()

    @defer.inlineCallbacks
    def _send_syncing_users_now(self):
        if self._sending_sync:
            # We don't want to race with sending another update.
            # Instead we wait for that update to finish and send another
            # update afterwards.
            self._need_to_send_sync = True
            return

        # Flag that we are sending an update.
        self._sending_sync = True

        yield self.http_client.post_json_get_json(self.syncing_users_url, {
            "process_id": self.process_id,
            "syncing_users": [
                user_id for user_id, count in self.user_to_num_current_syncs.items()
                if count > 0
            ],
        })

        # Unset the flag as we are no longer sending an update.
        self._sending_sync = False
        if self._need_to_send_sync:
            # If something happened while we were sending the update then
            # we might need to send another update.
            # TODO: Check if the update that was sent matches the current state
            # as we only need to send an update if they are different.
            self._need_to_send_sync = False
            yield self._send_syncing_users_now()

    def process_replication(self, result):
        stream = result.get("presence", {"rows": []})
        for row in stream["rows"]:
            (
                position, user_id, state, last_active_ts,
                last_federation_update_ts, last_user_sync_ts, status_msg,
                currently_active
            ) = row
            self.user_to_current_state[user_id] = UserPresenceState(
                user_id, state, last_active_ts,
                last_federation_update_ts, last_user_sync_ts, status_msg,
                currently_active
            )


class SynchrotronTyping(object):
    def __init__(self, hs):
        self._latest_room_serial = 0
        self._room_serials = {}
        self._room_typing = {}

    def stream_positions(self):
        return {"typing": self._latest_room_serial}

    def process_replication(self, result):
        stream = result.get("typing")
        if stream:
            self._latest_room_serial = int(stream["position"])

            for row in stream["rows"]:
                position, room_id, typing_json = row
                typing = json.loads(typing_json)
                self._room_serials[room_id] = position
                self._room_typing[room_id] = typing


class SynchrotronApplicationService(object):
    def notify_interested_services(self, event):
        pass


class SynchrotronServer(HomeServer):
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
        self.datastore = SynchrotronSlavedStore(self.get_db_conn(), self)
        logger.info("Finished setting up.")

    def _listen_http(self, listener_config):
        port = listener_config["port"]
        bind_address = listener_config.get("bind_address", "")
        site_tag = listener_config.get("tag", port)
        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(self)
                elif name == "client":
                    resource = JsonResource(self, canonical_json=False)
                    sync.register_servlets(self, resource)
                    resources.update({
                        "/_matrix/client/r0": resource,
                        "/_matrix/client/unstable": resource,
                        "/_matrix/client/v2_alpha": resource,
                    })

        root_resource = create_resource_tree(resources, Resource())
        reactor.listenTCP(
            port,
            SynapseSite(
                "synapse.access.http.%s" % (site_tag,),
                site_tag,
                listener_config,
                root_resource,
            ),
            interface=bind_address
        )
        logger.info("Synapse synchrotron now listening on port %d", port)

    def start_listening(self):
        for listener in self.config.listeners:
            if listener["type"] == "http":
                self._listen_http(listener)
            elif listener["type"] == "manhole":
                reactor.listenTCP(
                    listener["port"],
                    manhole(
                        username="matrix",
                        password="rabbithole",
                        globals={"hs": self},
                    ),
                    interface=listener.get("bind_address", '127.0.0.1')
                )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

    @defer.inlineCallbacks
    def replicate(self):
        http_client = self.get_simple_http_client()
        store = self.get_datastore()
        replication_url = self.config.replication_url
        clock = self.get_clock()
        notifier = self.get_notifier()
        presence_handler = self.get_presence_handler()
        typing_handler = self.get_typing_handler()

        def expire_broken_caches():
            store.who_forgot_in_room.invalidate_all()
            store.get_presence_list_accepted.invalidate_all()

        def notify_from_stream(
            result, stream_name, stream_key, room=None, user=None
        ):
            stream = result.get(stream_name)
            if stream:
                position_index = stream["field_names"].index("position")
                if room:
                    room_index = stream["field_names"].index(room)
                if user:
                    user_index = stream["field_names"].index(user)

                users = ()
                rooms = ()
                for row in stream["rows"]:
                    position = row[position_index]

                    if user:
                        users = (row[user_index],)

                    if room:
                        rooms = (row[room_index],)

                    notifier.on_new_event(
                        stream_key, position, users=users, rooms=rooms
                    )

        def notify(result):
            stream = result.get("events")
            if stream:
                max_position = stream["position"]
                for row in stream["rows"]:
                    position = row[0]
                    internal = json.loads(row[1])
                    event_json = json.loads(row[2])
                    event = FrozenEvent(event_json, internal_metadata_dict=internal)
                    extra_users = ()
                    if event.type == EventTypes.Member:
                        extra_users = (event.state_key,)
                    notifier.on_new_room_event(
                        event, position, max_position, extra_users
                    )

            notify_from_stream(
                result, "push_rules", "push_rules_key", user="user_id"
            )
            notify_from_stream(
                result, "user_account_data", "account_data_key", user="user_id"
            )
            notify_from_stream(
                result, "room_account_data", "account_data_key", user="user_id"
            )
            notify_from_stream(
                result, "tag_account_data", "account_data_key", user="user_id"
            )
            notify_from_stream(
                result, "receipts", "receipt_key", room="room_id"
            )
            notify_from_stream(
                result, "typing", "typing_key", room="room_id"
            )

        next_expire_broken_caches_ms = 0
        while True:
            try:
                args = store.stream_positions()
                args.update(typing_handler.stream_positions())
                args["timeout"] = 30000
                result = yield http_client.get_json(replication_url, args=args)
                now_ms = clock.time_msec()
                if now_ms > next_expire_broken_caches_ms:
                    expire_broken_caches()
                    next_expire_broken_caches_ms = (
                        now_ms + store.BROKEN_CACHE_EXPIRY_MS
                    )
                yield store.process_replication(result)
                typing_handler.process_replication(result)
                presence_handler.process_replication(result)
                notify(result)
            except:
                logger.exception("Error replicating from %r", replication_url)
                yield sleep(5)

    def build_presence_handler(self):
        return SynchrotronPresence(self)

    def build_typing_handler(self):
        return SynchrotronTyping(self)


def setup(config_options):
    try:
        config = SynchrotronConfig.load_config(
            "Synapse synchrotron", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    if not config:
        sys.exit(0)

    config.setup_logging()

    database_engine = create_engine(config.database_config)

    ss = SynchrotronServer(
        config.server_name,
        db_config=config.database_config,
        config=config,
        version_string=get_version_string("Synapse", synapse),
        database_engine=database_engine,
        application_service_handler=SynchrotronApplicationService(),
    )

    ss.setup()
    ss.start_listening()

    change_resource_limit(ss.config.soft_file_limit)
    if ss.config.gc_thresholds:
        ss.set_threshold(*ss.config.gc_thresholds)

    def start():
        ss.get_datastore().start_profiling()
        ss.replicate()

    reactor.callWhenRunning(start)

    return ss


if __name__ == '__main__':
    with LoggingContext("main"):
        ss = setup(sys.argv[1:])

        if ss.config.daemonize:
            def run():
                with LoggingContext("run"):
                    change_resource_limit(ss.config.soft_file_limit)
                    if ss.config.gc_thresholds:
                        gc.set_threshold(*ss.config.gc_thresholds)
                    reactor.run()

            daemon = Daemonize(
                app="synapse-synchrotron",
                pid=ss.config.pid_file,
                action=run,
                auto_close_fds=False,
                verbose=True,
                logger=logger,
            )

            daemon.start()
        else:
            reactor.run()
