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

"""Contains handlers for federation events."""

from ._base import BaseHandler

from synapse.api.events.utils import prune_event
from synapse.api.errors import AuthError, FederationError, SynapseError
from synapse.api.events.room import RoomMemberEvent
from synapse.api.constants import Membership
from synapse.util.logutils import log_function
from synapse.util.async import run_on_reactor
from synapse.crypto.event_signing import (
    compute_event_signature, check_event_content_hash
)
from syutil.jsonutil import encode_canonical_json

from twisted.internet import defer

import logging


logger = logging.getLogger(__name__)


class FederationHandler(BaseHandler):
    """Handles events that originated from federation.
        Responsible for:
        a) handling received Pdus before handing them on as Events to the rest
        of the home server (including auth and state conflict resoultion)
        b) converting events that were produced by local clients that may need
        to be sent to remote home servers.
        c) doing the necessary dances to invite remote users and join remote
        rooms.
    """

    def __init__(self, hs):
        super(FederationHandler, self).__init__(hs)

        self.distributor.observe(
            "user_joined_room",
            self._on_user_joined
        )

        self.waiting_for_join_list = {}

        self.store = hs.get_datastore()
        self.replication_layer = hs.get_replication_layer()
        self.state_handler = hs.get_state_handler()
        # self.auth_handler = gs.get_auth_handler()
        self.server_name = hs.hostname
        self.keyring = hs.get_keyring()

        self.lock_manager = hs.get_room_lock_manager()

        self.replication_layer.set_handler(self)

        # When joining a room we need to queue any events for that room up
        self.room_queues = {}

    @log_function
    @defer.inlineCallbacks
    def handle_new_event(self, event, snapshot):
        """ Takes in an event from the client to server side, that has already
        been authed and handled by the state module, and sends it to any
        remote home servers that may be interested.

        Args:
            event
            snapshot (.storage.Snapshot): THe snapshot the event happened after

        Returns:
            Deferred: Resolved when it has successfully been queued for
            processing.
        """

        yield run_on_reactor()

        pdu = event

        if not hasattr(pdu, "destinations") or not pdu.destinations:
            pdu.destinations = []

        yield self.replication_layer.send_pdu(pdu)

    @log_function
    @defer.inlineCallbacks
    def on_receive_pdu(self, pdu, backfilled, state=None):
        """ Called by the ReplicationLayer when we have a new pdu. We need to
        do auth checks and put it through the StateHandler.
        """
        event = pdu

        logger.debug("Got event: %s", event.event_id)

        # If we are currently in the process of joining this room, then we
        # queue up events for later processing.
        if event.room_id in self.room_queues:
            self.room_queues[event.room_id].append(pdu)
            return

        logger.debug("Processing event: %s", event.event_id)

        redacted_event = prune_event(event)

        redacted_pdu_json = redacted_event.get_pdu_json()
        try:
            yield self.keyring.verify_json_for_server(
                event.origin, redacted_pdu_json
            )
        except SynapseError as e:
            logger.warn("Signature check failed for %s redacted to %s",
                encode_canonical_json(pdu.get_pdu_json()),
                encode_canonical_json(redacted_pdu_json),
            )
            raise FederationError(
                "ERROR",
                e.code,
                e.msg,
                affected=event.event_id,
            )

        if not check_event_content_hash(event):
            logger.warn(
                "Event content has been tampered, redacting %s, %s",
                event.event_id, encode_canonical_json(event.get_full_dict())
            )
            event = redacted_event

        is_new_state = yield self.state_handler.annotate_event_with_state(
            event,
            old_state=state
        )

        logger.debug("Event: %s", event)

        try:
            self.auth.check(event, raises=True)
        except AuthError as e:
            raise FederationError(
                "ERROR",
                e.code,
                e.msg,
                affected=event.event_id,
            )

        is_new_state = is_new_state and not backfilled

        # TODO: Implement something in federation that allows us to
        # respond to PDU.

        yield self.store.persist_event(
            event,
            backfilled,
            is_new_state=is_new_state
        )

        room = yield self.store.get_room(event.room_id)

        if not room:
            # Huh, let's try and get the current state
            try:
                yield self.replication_layer.get_state_for_context(
                    event.origin, event.room_id, event.event_id,
                )

                hosts = yield self.store.get_joined_hosts_for_room(
                    event.room_id
                )
                if self.hs.hostname in hosts:
                    try:
                        yield self.store.store_room(
                            room_id=event.room_id,
                            room_creator_user_id="",
                            is_public=False,
                        )
                    except:
                        pass
            except:
                logger.exception(
                    "Failed to get current state for room %s",
                    event.room_id
                )

        if not backfilled:
            extra_users = []
            if event.type == RoomMemberEvent.TYPE:
                target_user_id = event.state_key
                target_user = self.hs.parse_userid(target_user_id)
                extra_users.append(target_user)

            yield self.notifier.on_new_room_event(
                event, extra_users=extra_users
            )

        if event.type == RoomMemberEvent.TYPE:
            if event.membership == Membership.JOIN:
                user = self.hs.parse_userid(event.state_key)
                self.distributor.fire(
                    "user_joined_room", user=user, room_id=event.room_id
                )

    @log_function
    @defer.inlineCallbacks
    def backfill(self, dest, room_id, limit):
        """ Trigger a backfill request to `dest` for the given `room_id`
        """
        extremities = yield self.store.get_oldest_events_in_room(room_id)

        pdus = yield self.replication_layer.backfill(
            dest,
            room_id,
            limit,
            extremities=extremities,
        )

        events = []

        for pdu in pdus:
            event = pdu

            # FIXME (erikj): Not sure this actually works :/
            yield self.state_handler.annotate_event_with_state(event)

            events.append(event)

            yield self.store.persist_event(event, backfilled=True)

        defer.returnValue(events)

    @defer.inlineCallbacks
    def send_invite(self, target_host, event):
        """ Sends the invite to the remote server for signing.

        Invites must be signed by the invitee's server before distribution.
        """
        pdu = yield self.replication_layer.send_invite(
            destination=target_host,
            context=event.room_id,
            event_id=event.event_id,
            pdu=event
        )

        defer.returnValue(pdu)

    @defer.inlineCallbacks
    def on_event_auth(self, event_id):
        auth = yield self.store.get_auth_chain(event_id)
        defer.returnValue([e for e in auth])

    @log_function
    @defer.inlineCallbacks
    def do_invite_join(self, target_host, room_id, joinee, content, snapshot):
        """ Attempts to join the `joinee` to the room `room_id` via the
        server `target_host`.

        This first triggers a /make_join/ request that returns a partial
        event that we can fill out and sign. This is then sent to the
        remote server via /send_join/ which responds with the state at that
        event and the auth_chains.

        We suspend processing of any received events from this room until we
        have finished processing the join.
        """
        pdu = yield self.replication_layer.make_join(
            target_host,
            room_id,
            joinee
        )

        logger.debug("Got response to make_join: %s", pdu)

        event = pdu

        # We should assert some things.
        assert(event.type == RoomMemberEvent.TYPE)
        assert(event.user_id == joinee)
        assert(event.state_key == joinee)
        assert(event.room_id == room_id)

        event.outlier = False

        self.room_queues[room_id] = []

        try:
            event.event_id = self.event_factory.create_event_id()
            event.content = content

            state = yield self.replication_layer.send_join(
                target_host,
                event
            )

            logger.debug("do_invite_join state: %s", state)

            yield self.state_handler.annotate_event_with_state(
                event,
                old_state=state
            )

            logger.debug("do_invite_join event: %s", event)

            try:
                yield self.store.store_room(
                    room_id=room_id,
                    room_creator_user_id="",
                    is_public=False
                )
            except:
                # FIXME
                pass

            for e in state:
                # FIXME: Auth these.
                e.outlier = True

                yield self.state_handler.annotate_event_with_state(
                    e,
                )

                yield self.store.persist_event(
                    e,
                    backfilled=False,
                    is_new_state=True
                )

            yield self.store.persist_event(
                event,
                backfilled=False,
                is_new_state=True
            )
        finally:
            room_queue = self.room_queues[room_id]
            del self.room_queues[room_id]

            for p in room_queue:
                try:
                    yield self.on_receive_pdu(p, backfilled=False)
                except:
                    pass

        defer.returnValue(True)

    @defer.inlineCallbacks
    @log_function
    def on_make_join_request(self, context, user_id):
        """ We've received a /make_join/ request, so we create a partial
        join event for the room and return that. We don *not* persist or
        process it until the other server has signed it and sent it back.
        """
        event = self.event_factory.create_event(
            etype=RoomMemberEvent.TYPE,
            content={"membership": Membership.JOIN},
            room_id=context,
            user_id=user_id,
            state_key=user_id,
        )

        snapshot = yield self.store.snapshot_room(event)
        snapshot.fill_out_prev_events(event)

        yield self.state_handler.annotate_event_with_state(event)
        yield self.auth.add_auth_events(event)
        self.auth.check(event, raises=True)

        pdu = event

        defer.returnValue(pdu)

    @defer.inlineCallbacks
    @log_function
    def on_send_join_request(self, origin, pdu):
        """ We have received a join event for a room. Fully process it and
        respond with the current state and auth chains.
        """
        event = pdu

        event.outlier = False

        is_new_state = yield self.state_handler.annotate_event_with_state(event)
        self.auth.check(event, raises=True)

        # FIXME (erikj):  All this is duplicated above :(

        yield self.store.persist_event(
            event,
            backfilled=False,
            is_new_state=is_new_state
        )

        extra_users = []
        if event.type == RoomMemberEvent.TYPE:
            target_user_id = event.state_key
            target_user = self.hs.parse_userid(target_user_id)
            extra_users.append(target_user)

        yield self.notifier.on_new_room_event(
            event, extra_users=extra_users
        )

        if event.type == RoomMemberEvent.TYPE:
            if event.membership == Membership.JOIN:
                user = self.hs.parse_userid(event.state_key)
                self.distributor.fire(
                    "user_joined_room", user=user, room_id=event.room_id
                )

        new_pdu = event

        destinations = set()

        for k, s in event.state_events.items():
            try:
                if k[0] == RoomMemberEvent.TYPE:
                    if s.content["membership"] == Membership.JOIN:
                        destinations.add(
                            self.hs.parse_userid(s.state_key).domain
                        )
            except:
                logger.warn(
                    "Failed to get destination from event %s", s.event_id
                )

        new_pdu.destinations = list(destinations)

        yield self.replication_layer.send_pdu(new_pdu)

        auth_chain = yield self.store.get_auth_chain(event.event_id)

        defer.returnValue({
            "state": event.state_events.values(),
            "auth_chain": auth_chain,
        })

    @defer.inlineCallbacks
    def on_invite_request(self, origin, pdu):
        """ We've got an invite event. Process and persist it. Sign it.

        Respond with the now signed event.
        """
        event = pdu

        event.outlier = True

        event.signatures.update(
            compute_event_signature(
                event,
                self.hs.hostname,
                self.hs.config.signing_key[0]
            )
        )

        yield self.state_handler.annotate_event_with_state(event)

        yield self.store.persist_event(
            event,
            backfilled=False,
        )

        target_user = self.hs.parse_userid(event.state_key)
        yield self.notifier.on_new_room_event(
            event, extra_users=[target_user],
        )

        defer.returnValue(event)

    @defer.inlineCallbacks
    def get_state_for_pdu(self, origin, room_id, event_id):
        yield run_on_reactor()

        in_room = yield self.auth.check_host_in_room(room_id, origin)
        if not in_room:
            raise AuthError(403, "Host not in room.")

        state_groups = yield self.store.get_state_groups(
            [event_id]
        )

        if state_groups:
            _, state = state_groups.items().pop()
            results = {
                (e.type, e.state_key): e for e in state
            }

            event = yield self.store.get_event(event_id)
            if hasattr(event, "state_key"):
                # Get previous state
                if hasattr(event, "replaces_state") and event.replaces_state:
                    prev_event = yield self.store.get_event(
                        event.replaces_state
                    )
                    results[(event.type, event.state_key)] = prev_event
                else:
                    del results[(event.type, event.state_key)]

            defer.returnValue(results.values())
        else:
            defer.returnValue([])

    @defer.inlineCallbacks
    @log_function
    def on_backfill_request(self, origin, context, pdu_list, limit):
        in_room = yield self.auth.check_host_in_room(context, origin)
        if not in_room:
            raise AuthError(403, "Host not in room.")

        events = yield self.store.get_backfill_events(
            context,
            pdu_list,
            limit
        )

        defer.returnValue(events)

    @defer.inlineCallbacks
    @log_function
    def get_persisted_pdu(self, origin, event_id):
        """ Get a PDU from the database with given origin and id.

        Returns:
            Deferred: Results in a `Pdu`.
        """
        event = yield self.store.get_event(
            event_id,
            allow_none=True,
        )

        if event:
            in_room = yield self.auth.check_host_in_room(
                event.room_id,
                origin
            )
            if not in_room:
                raise AuthError(403, "Host not in room.")

            defer.returnValue(event)
        else:
            defer.returnValue(None)

    @log_function
    def get_min_depth_for_context(self, context):
        return self.store.get_min_depth(context)

    @log_function
    def _on_user_joined(self, user, room_id):
        waiters = self.waiting_for_join_list.get(
            (user.to_string(), room_id),
            []
        )
        while waiters:
            waiters.pop().callback(None)
