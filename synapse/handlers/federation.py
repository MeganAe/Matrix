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

"""Contains handlers for federation events."""

from ._base import BaseHandler

from synapse.api.errors import (
    AuthError, FederationError, StoreError, CodeMessageException, SynapseError,
)
from synapse.api.constants import EventTypes, Membership, RejectedReason
from synapse.events.validator import EventValidator
from synapse.util import unwrapFirstError
from synapse.util.logcontext import PreserveLoggingContext
from synapse.util.logutils import log_function
from synapse.util.async import run_on_reactor
from synapse.util.frozenutils import unfreeze
from synapse.crypto.event_signing import (
    compute_event_signature, add_hashes_and_signatures,
)
from synapse.types import UserID

from synapse.events.utils import prune_event

from synapse.util.retryutils import NotRetryingDestination

from twisted.internet import defer

import itertools
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

        self.hs = hs

        self.distributor.observe(
            "user_joined_room",
            self._on_user_joined
        )

        self.waiting_for_join_list = {}

        self.store = hs.get_datastore()
        self.replication_layer = hs.get_replication_layer()
        self.state_handler = hs.get_state_handler()
        self.server_name = hs.hostname
        self.keyring = hs.get_keyring()

        self.replication_layer.set_handler(self)

        # When joining a room we need to queue any events for that room up
        self.room_queues = {}

    def handle_new_event(self, event, destinations):
        """ Takes in an event from the client to server side, that has already
        been authed and handled by the state module, and sends it to any
        remote home servers that may be interested.

        Args:
            event: The event to send
            destinations: A list of destinations to send it to

        Returns:
            Deferred: Resolved when it has successfully been queued for
            processing.
        """

        return self.replication_layer.send_pdu(event, destinations)

    @log_function
    @defer.inlineCallbacks
    def on_receive_pdu(self, origin, pdu, backfilled, state=None,
                       auth_chain=None):
        """ Called by the ReplicationLayer when we have a new pdu. We need to
        do auth checks and put it through the StateHandler.
        """
        event = pdu

        logger.debug("Got event: %s", event.event_id)

        # If we are currently in the process of joining this room, then we
        # queue up events for later processing.
        if event.room_id in self.room_queues:
            self.room_queues[event.room_id].append((pdu, origin))
            return

        logger.debug("Processing event: %s", event.event_id)

        logger.debug("Event: %s", event)

        # FIXME (erikj): Awful hack to make the case where we are not currently
        # in the room work
        current_state = None
        is_in_room = yield self.auth.check_host_in_room(
            event.room_id,
            self.server_name
        )
        if not is_in_room and not event.internal_metadata.is_outlier():
            logger.debug("Got event for room we're not in.")

            try:
                event_stream_id, max_stream_id = yield self._persist_auth_tree(
                    auth_chain, state, event
                )
            except AuthError as e:
                raise FederationError(
                    "ERROR",
                    e.code,
                    e.msg,
                    affected=event.event_id,
                )

        else:
            event_ids = set()
            if state:
                event_ids |= {e.event_id for e in state}
            if auth_chain:
                event_ids |= {e.event_id for e in auth_chain}

            seen_ids = set(
                (yield self.store.have_events(event_ids)).keys()
            )

            if state and auth_chain is not None:
                # If we have any state or auth_chain given to us by the replication
                # layer, then we should handle them (if we haven't before.)

                event_infos = []

                for e in itertools.chain(auth_chain, state):
                    if e.event_id in seen_ids:
                        continue
                    e.internal_metadata.outlier = True
                    auth_ids = [e_id for e_id, _ in e.auth_events]
                    auth = {
                        (e.type, e.state_key): e for e in auth_chain
                        if e.event_id in auth_ids or e.type == EventTypes.Create
                    }
                    event_infos.append({
                        "event": e,
                        "auth_events": auth,
                    })
                    seen_ids.add(e.event_id)

                yield self._handle_new_events(
                    origin,
                    event_infos,
                    outliers=True
                )

            try:
                _, event_stream_id, max_stream_id = yield self._handle_new_event(
                    origin,
                    event,
                    state=state,
                    backfilled=backfilled,
                    current_state=current_state,
                )
            except AuthError as e:
                raise FederationError(
                    "ERROR",
                    e.code,
                    e.msg,
                    affected=event.event_id,
                )

        # if we're receiving valid events from an origin,
        # it's probably a good idea to mark it as not in retry-state
        # for sending (although this is a bit of a leap)
        retry_timings = yield self.store.get_destination_retry_timings(origin)
        if retry_timings and retry_timings["retry_last_ts"]:
            self.store.set_destination_retry_timings(origin, 0, 0)

        room = yield self.store.get_room(event.room_id)

        if not room:
            try:
                yield self.store.store_room(
                    room_id=event.room_id,
                    room_creator_user_id="",
                    is_public=False,
                )
            except StoreError:
                logger.exception("Failed to store room.")

        if not backfilled:
            extra_users = []
            if event.type == EventTypes.Member:
                target_user_id = event.state_key
                target_user = UserID.from_string(target_user_id)
                extra_users.append(target_user)

            with PreserveLoggingContext():
                d = self.notifier.on_new_room_event(
                    event, event_stream_id, max_stream_id,
                    extra_users=extra_users
                )

            def log_failure(f):
                logger.warn(
                    "Failed to notify about %s: %s",
                    event.event_id, f.value
                )

            d.addErrback(log_failure)

        if event.type == EventTypes.Member:
            if event.membership == Membership.JOIN:
                user = UserID.from_string(event.state_key)
                yield self.distributor.fire(
                    "user_joined_room", user=user, room_id=event.room_id
                )

    @defer.inlineCallbacks
    def _filter_events_for_server(self, server_name, room_id, events):
        event_to_state = yield self.store.get_state_for_events(
            frozenset(e.event_id for e in events),
            types=(
                (EventTypes.RoomHistoryVisibility, ""),
                (EventTypes.Member, None),
            )
        )

        def redact_disallowed(event, state):
            if not state:
                return event

            history = state.get((EventTypes.RoomHistoryVisibility, ''), None)
            if history:
                visibility = history.content.get("history_visibility", "shared")
                if visibility in ["invited", "joined"]:
                    # We now loop through all state events looking for
                    # membership states for the requesting server to determine
                    # if the server is either in the room or has been invited
                    # into the room.
                    for ev in state.values():
                        if ev.type != EventTypes.Member:
                            continue
                        try:
                            domain = UserID.from_string(ev.state_key).domain
                        except:
                            continue

                        if domain != server_name:
                            continue

                        memtype = ev.membership
                        if memtype == Membership.JOIN:
                            return event
                        elif memtype == Membership.INVITE:
                            if visibility == "invited":
                                return event
                    else:
                        return prune_event(event)

            return event

        defer.returnValue([
            redact_disallowed(e, event_to_state[e.event_id])
            for e in events
        ])

    @log_function
    @defer.inlineCallbacks
    def backfill(self, dest, room_id, limit, extremities=[]):
        """ Trigger a backfill request to `dest` for the given `room_id`
        """
        if not extremities:
            extremities = yield self.store.get_oldest_events_in_room(room_id)

        events = yield self.replication_layer.backfill(
            dest,
            room_id,
            limit=limit,
            extremities=extremities,
        )

        event_map = {e.event_id: e for e in events}

        event_ids = set(e.event_id for e in events)

        edges = [
            ev.event_id
            for ev in events
            if set(e_id for e_id, _ in ev.prev_events) - event_ids
        ]

        logger.info(
            "backfill: Got %d events with %d edges",
            len(events), len(edges),
        )

        # For each edge get the current state.

        auth_events = {}
        state_events = {}
        events_to_state = {}
        for e_id in edges:
            state, auth = yield self.replication_layer.get_state_for_room(
                destination=dest,
                room_id=room_id,
                event_id=e_id
            )
            auth_events.update({a.event_id: a for a in auth})
            auth_events.update({s.event_id: s for s in state})
            state_events.update({s.event_id: s for s in state})
            events_to_state[e_id] = state

        seen_events = yield self.store.have_events(
            set(auth_events.keys()) | set(state_events.keys())
        )

        all_events = events + state_events.values() + auth_events.values()
        required_auth = set(
            a_id for event in all_events for a_id, _ in event.auth_events
        )

        missing_auth = required_auth - set(auth_events)
        results = yield defer.gatherResults(
            [
                self.replication_layer.get_pdu(
                    [dest],
                    event_id,
                    outlier=True,
                    timeout=10000,
                )
                for event_id in missing_auth
            ],
            consumeErrors=True
        ).addErrback(unwrapFirstError)
        auth_events.update({a.event_id: a for a in results})

        ev_infos = []
        for a in auth_events.values():
            if a.event_id in seen_events:
                continue
            ev_infos.append({
                "event": a,
                "auth_events": {
                    (auth_events[a_id].type, auth_events[a_id].state_key):
                    auth_events[a_id]
                    for a_id, _ in a.auth_events
                }
            })

        for e_id in events_to_state:
            ev_infos.append({
                "event": event_map[e_id],
                "state": events_to_state[e_id],
                "auth_events": {
                    (auth_events[a_id].type, auth_events[a_id].state_key):
                    auth_events[a_id]
                    for a_id, _ in event_map[e_id].auth_events
                }
            })

        events.sort(key=lambda e: e.depth)

        for event in events:
            if event in events_to_state:
                continue

            ev_infos.append({
                "event": event,
            })

        yield self._handle_new_events(
            dest, ev_infos,
            backfilled=True,
        )

        defer.returnValue(events)

    @defer.inlineCallbacks
    def maybe_backfill(self, room_id, current_depth):
        """Checks the database to see if we should backfill before paginating,
        and if so do.
        """
        extremities = yield self.store.get_oldest_events_with_depth_in_room(
            room_id
        )

        if not extremities:
            logger.debug("Not backfilling as no extremeties found.")
            return

        # Check if we reached a point where we should start backfilling.
        sorted_extremeties_tuple = sorted(
            extremities.items(),
            key=lambda e: -int(e[1])
        )
        max_depth = sorted_extremeties_tuple[0][1]

        if current_depth > max_depth:
            logger.debug(
                "Not backfilling as we don't need to. %d < %d",
                max_depth, current_depth,
            )
            return

        # Now we need to decide which hosts to hit first.

        # First we try hosts that are already in the room
        # TODO: HEURISTIC ALERT.

        curr_state = yield self.state_handler.get_current_state(room_id)

        def get_domains_from_state(state):
            joined_users = [
                (state_key, int(event.depth))
                for (e_type, state_key), event in state.items()
                if e_type == EventTypes.Member
                and event.membership == Membership.JOIN
            ]

            joined_domains = {}
            for u, d in joined_users:
                try:
                    dom = UserID.from_string(u).domain
                    old_d = joined_domains.get(dom)
                    if old_d:
                        joined_domains[dom] = min(d, old_d)
                    else:
                        joined_domains[dom] = d
                except:
                    pass

            return sorted(joined_domains.items(), key=lambda d: d[1])

        curr_domains = get_domains_from_state(curr_state)

        likely_domains = [
            domain for domain, depth in curr_domains
            if domain is not self.server_name
        ]

        @defer.inlineCallbacks
        def try_backfill(domains):
            # TODO: Should we try multiple of these at a time?
            for dom in domains:
                try:
                    events = yield self.backfill(
                        dom, room_id,
                        limit=100,
                        extremities=[e for e in extremities.keys()]
                    )
                except SynapseError:
                    logger.info(
                        "Failed to backfill from %s because %s",
                        dom, e,
                    )
                    continue
                except CodeMessageException as e:
                    if 400 <= e.code < 500:
                        raise

                    logger.info(
                        "Failed to backfill from %s because %s",
                        dom, e,
                    )
                    continue
                except NotRetryingDestination as e:
                    logger.info(e.message)
                    continue
                except Exception as e:
                    logger.exception(
                        "Failed to backfill from %s because %s",
                        dom, e,
                    )
                    continue

                if events:
                    defer.returnValue(True)
            defer.returnValue(False)

        success = yield try_backfill(likely_domains)
        if success:
            defer.returnValue(True)

        # Huh, well *those* domains didn't work out. Lets try some domains
        # from the time.

        tried_domains = set(likely_domains)
        tried_domains.add(self.server_name)

        event_ids = list(extremities.keys())

        states = yield defer.gatherResults([
            self.state_handler.resolve_state_groups(room_id, [e])
            for e in event_ids
        ])
        states = dict(zip(event_ids, [s[1] for s in states]))

        for e_id, _ in sorted_extremeties_tuple:
            likely_domains = get_domains_from_state(states[e_id])

            success = yield try_backfill([
                dom for dom in likely_domains
                if dom not in tried_domains
            ])
            if success:
                defer.returnValue(True)

            tried_domains.update(likely_domains)

        defer.returnValue(False)

    @defer.inlineCallbacks
    def send_invite(self, target_host, event):
        """ Sends the invite to the remote server for signing.

        Invites must be signed by the invitee's server before distribution.
        """
        pdu = yield self.replication_layer.send_invite(
            destination=target_host,
            room_id=event.room_id,
            event_id=event.event_id,
            pdu=event
        )

        defer.returnValue(pdu)

    @defer.inlineCallbacks
    def on_event_auth(self, event_id):
        auth = yield self.store.get_auth_chain([event_id])

        for event in auth:
            event.signatures.update(
                compute_event_signature(
                    event,
                    self.hs.hostname,
                    self.hs.config.signing_key[0]
                )
            )

        defer.returnValue([e for e in auth])

    @log_function
    @defer.inlineCallbacks
    def do_invite_join(self, target_hosts, room_id, joinee):
        """ Attempts to join the `joinee` to the room `room_id` via the
        server `target_host`.

        This first triggers a /make_join/ request that returns a partial
        event that we can fill out and sign. This is then sent to the
        remote server via /send_join/ which responds with the state at that
        event and the auth_chains.

        We suspend processing of any received events from this room until we
        have finished processing the join.
        """
        logger.debug("Joining %s to %s", joinee, room_id)

        yield self.store.clean_room_for_join(room_id)

        origin, event = yield self._make_and_verify_event(
            target_hosts,
            room_id,
            joinee,
            "join"
        )

        self.room_queues[room_id] = []
        handled_events = set()

        try:
            new_event = self._sign_event(event)
            # Try the host we successfully got a response to /make_join/
            # request first.
            try:
                target_hosts.remove(origin)
                target_hosts.insert(0, origin)
            except ValueError:
                pass
            ret = yield self.replication_layer.send_join(target_hosts, new_event)

            origin = ret["origin"]
            state = ret["state"]
            auth_chain = ret["auth_chain"]
            auth_chain.sort(key=lambda e: e.depth)

            handled_events.update([s.event_id for s in state])
            handled_events.update([a.event_id for a in auth_chain])
            handled_events.add(new_event.event_id)

            logger.debug("do_invite_join auth_chain: %s", auth_chain)
            logger.debug("do_invite_join state: %s", state)

            logger.debug("do_invite_join event: %s", new_event)

            try:
                yield self.store.store_room(
                    room_id=room_id,
                    room_creator_user_id="",
                    is_public=False
                )
            except:
                # FIXME
                pass

            event_stream_id, max_stream_id = yield self._persist_auth_tree(
                auth_chain, state, event
            )

            with PreserveLoggingContext():
                d = self.notifier.on_new_room_event(
                    new_event, event_stream_id, max_stream_id,
                    extra_users=[joinee]
                )

            def log_failure(f):
                logger.warn(
                    "Failed to notify about %s: %s",
                    new_event.event_id, f.value
                )

            d.addErrback(log_failure)

            logger.debug("Finished joining %s to %s", joinee, room_id)
        finally:
            room_queue = self.room_queues[room_id]
            del self.room_queues[room_id]

            for p, origin in room_queue:
                if p.event_id in handled_events:
                    continue

                try:
                    self.on_receive_pdu(origin, p, backfilled=False)
                except:
                    logger.exception("Couldn't handle pdu")

        defer.returnValue(True)

    @defer.inlineCallbacks
    @log_function
    def on_make_join_request(self, room_id, user_id):
        """ We've received a /make_join/ request, so we create a partial
        join event for the room and return that. We do *not* persist or
        process it until the other server has signed it and sent it back.
        """
        event_content = {"membership": Membership.JOIN}

        builder = self.event_builder_factory.new({
            "type": EventTypes.Member,
            "content": event_content,
            "room_id": room_id,
            "sender": user_id,
            "state_key": user_id,
        })

        event, context = yield self._create_new_client_event(
            builder=builder,
        )

        self.auth.check(event, auth_events=context.current_state)

        defer.returnValue(event)

    @defer.inlineCallbacks
    @log_function
    def on_send_join_request(self, origin, pdu):
        """ We have received a join event for a room. Fully process it and
        respond with the current state and auth chains.
        """
        event = pdu

        logger.debug(
            "on_send_join_request: Got event: %s, signatures: %s",
            event.event_id,
            event.signatures,
        )

        event.internal_metadata.outlier = False

        context, event_stream_id, max_stream_id = yield self._handle_new_event(
            origin, event
        )

        logger.debug(
            "on_send_join_request: After _handle_new_event: %s, sigs: %s",
            event.event_id,
            event.signatures,
        )

        extra_users = []
        if event.type == EventTypes.Member:
            target_user_id = event.state_key
            target_user = UserID.from_string(target_user_id)
            extra_users.append(target_user)

        with PreserveLoggingContext():
            d = self.notifier.on_new_room_event(
                event, event_stream_id, max_stream_id, extra_users=extra_users
            )

        def log_failure(f):
            logger.warn(
                "Failed to notify about %s: %s",
                event.event_id, f.value
            )

        d.addErrback(log_failure)

        if event.type == EventTypes.Member:
            if event.content["membership"] == Membership.JOIN:
                user = UserID.from_string(event.state_key)
                yield self.distributor.fire(
                    "user_joined_room", user=user, room_id=event.room_id
                )

        new_pdu = event

        destinations = set()

        for k, s in context.current_state.items():
            try:
                if k[0] == EventTypes.Member:
                    if s.content["membership"] == Membership.JOIN:
                        destinations.add(
                            UserID.from_string(s.state_key).domain
                        )
            except:
                logger.warn(
                    "Failed to get destination from event %s", s.event_id
                )

        destinations.discard(origin)

        logger.debug(
            "on_send_join_request: Sending event: %s, signatures: %s",
            event.event_id,
            event.signatures,
        )

        self.replication_layer.send_pdu(new_pdu, destinations)

        state_ids = [e.event_id for e in context.current_state.values()]
        auth_chain = yield self.store.get_auth_chain(set(
            [event.event_id] + state_ids
        ))

        defer.returnValue({
            "state": context.current_state.values(),
            "auth_chain": auth_chain,
        })

    @defer.inlineCallbacks
    def on_invite_request(self, origin, pdu):
        """ We've got an invite event. Process and persist it. Sign it.

        Respond with the now signed event.
        """
        event = pdu

        event.internal_metadata.outlier = True

        event.signatures.update(
            compute_event_signature(
                event,
                self.hs.hostname,
                self.hs.config.signing_key[0]
            )
        )

        context = yield self.state_handler.compute_event_context(event)

        event_stream_id, max_stream_id = yield self.store.persist_event(
            event,
            context=context,
            backfilled=False,
        )

        target_user = UserID.from_string(event.state_key)
        with PreserveLoggingContext():
            d = self.notifier.on_new_room_event(
                event, event_stream_id, max_stream_id,
                extra_users=[target_user],
            )

        def log_failure(f):
            logger.warn(
                "Failed to notify about %s: %s",
                event.event_id, f.value
            )

        d.addErrback(log_failure)

        defer.returnValue(event)

    @defer.inlineCallbacks
    def do_remotely_reject_invite(self, target_hosts, room_id, user_id):
        origin, event = yield self._make_and_verify_event(
            target_hosts,
            room_id,
            user_id,
            "leave"
        )
        signed_event = self._sign_event(event)

        # Try the host we successfully got a response to /make_join/
        # request first.
        try:
            target_hosts.remove(origin)
            target_hosts.insert(0, origin)
        except ValueError:
            pass

        yield self.replication_layer.send_leave(
            target_hosts,
            signed_event
        )
        defer.returnValue(None)

    @defer.inlineCallbacks
    def _make_and_verify_event(self, target_hosts, room_id, user_id, membership):
        origin, pdu = yield self.replication_layer.make_membership_event(
            target_hosts,
            room_id,
            user_id,
            membership
        )

        logger.debug("Got response to make_%s: %s", membership, pdu)

        event = pdu

        # We should assert some things.
        # FIXME: Do this in a nicer way
        assert(event.type == EventTypes.Member)
        assert(event.user_id == user_id)
        assert(event.state_key == user_id)
        assert(event.room_id == room_id)
        defer.returnValue((origin, event))

    def _sign_event(self, event):
        event.internal_metadata.outlier = False

        builder = self.event_builder_factory.new(
            unfreeze(event.get_pdu_json())
        )

        builder.event_id = self.event_builder_factory.create_event_id()
        builder.origin = self.hs.hostname

        if not hasattr(event, "signatures"):
            builder.signatures = {}

        add_hashes_and_signatures(
            builder,
            self.hs.hostname,
            self.hs.config.signing_key[0],
        )

        return builder.build()

    @defer.inlineCallbacks
    @log_function
    def on_make_leave_request(self, room_id, user_id):
        """ We've received a /make_leave/ request, so we create a partial
        join event for the room and return that. We do *not* persist or
        process it until the other server has signed it and sent it back.
        """
        builder = self.event_builder_factory.new({
            "type": EventTypes.Member,
            "content": {"membership": Membership.LEAVE},
            "room_id": room_id,
            "sender": user_id,
            "state_key": user_id,
        })

        event, context = yield self._create_new_client_event(
            builder=builder,
        )

        self.auth.check(event, auth_events=context.current_state)

        defer.returnValue(event)

    @defer.inlineCallbacks
    @log_function
    def on_send_leave_request(self, origin, pdu):
        """ We have received a leave event for a room. Fully process it."""
        event = pdu

        logger.debug(
            "on_send_leave_request: Got event: %s, signatures: %s",
            event.event_id,
            event.signatures,
        )

        event.internal_metadata.outlier = False

        context, event_stream_id, max_stream_id = yield self._handle_new_event(
            origin, event
        )

        logger.debug(
            "on_send_leave_request: After _handle_new_event: %s, sigs: %s",
            event.event_id,
            event.signatures,
        )

        extra_users = []
        if event.type == EventTypes.Member:
            target_user_id = event.state_key
            target_user = UserID.from_string(target_user_id)
            extra_users.append(target_user)

        with PreserveLoggingContext():
            d = self.notifier.on_new_room_event(
                event, event_stream_id, max_stream_id, extra_users=extra_users
            )

        def log_failure(f):
            logger.warn(
                "Failed to notify about %s: %s",
                event.event_id, f.value
            )

        d.addErrback(log_failure)

        new_pdu = event

        destinations = set()

        for k, s in context.current_state.items():
            try:
                if k[0] == EventTypes.Member:
                    if s.content["membership"] == Membership.LEAVE:
                        destinations.add(
                            UserID.from_string(s.state_key).domain
                        )
            except:
                logger.warn(
                    "Failed to get destination from event %s", s.event_id
                )

        destinations.discard(origin)

        logger.debug(
            "on_send_leave_request: Sending event: %s, signatures: %s",
            event.event_id,
            event.signatures,
        )

        self.replication_layer.send_pdu(new_pdu, destinations)

        defer.returnValue(None)

    @defer.inlineCallbacks
    def get_state_for_pdu(self, origin, room_id, event_id, do_auth=True):
        yield run_on_reactor()

        if do_auth:
            in_room = yield self.auth.check_host_in_room(room_id, origin)
            if not in_room:
                raise AuthError(403, "Host not in room.")

        state_groups = yield self.store.get_state_groups(
            room_id, [event_id]
        )

        if state_groups:
            _, state = state_groups.items().pop()
            results = {
                (e.type, e.state_key): e for e in state
            }

            event = yield self.store.get_event(event_id)
            if event and event.is_state():
                # Get previous state
                if "replaces_state" in event.unsigned:
                    prev_id = event.unsigned["replaces_state"]
                    if prev_id != event.event_id:
                        prev_event = yield self.store.get_event(prev_id)
                        results[(event.type, event.state_key)] = prev_event
                else:
                    del results[(event.type, event.state_key)]

            res = results.values()
            for event in res:
                event.signatures.update(
                    compute_event_signature(
                        event,
                        self.hs.hostname,
                        self.hs.config.signing_key[0]
                    )
                )

            defer.returnValue(res)
        else:
            defer.returnValue([])

    @defer.inlineCallbacks
    @log_function
    def on_backfill_request(self, origin, room_id, pdu_list, limit):
        in_room = yield self.auth.check_host_in_room(room_id, origin)
        if not in_room:
            raise AuthError(403, "Host not in room.")

        events = yield self.store.get_backfill_events(
            room_id,
            pdu_list,
            limit
        )

        events = yield self._filter_events_for_server(origin, room_id, events)

        defer.returnValue(events)

    @defer.inlineCallbacks
    @log_function
    def get_persisted_pdu(self, origin, event_id, do_auth=True):
        """ Get a PDU from the database with given origin and id.

        Returns:
            Deferred: Results in a `Pdu`.
        """
        event = yield self.store.get_event(
            event_id,
            allow_none=True,
            allow_rejected=True,
        )

        if event:
            # FIXME: This is a temporary work around where we occasionally
            # return events slightly differently than when they were
            # originally signed
            event.signatures.update(
                compute_event_signature(
                    event,
                    self.hs.hostname,
                    self.hs.config.signing_key[0]
                )
            )

            if do_auth:
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

    @defer.inlineCallbacks
    @log_function
    def _handle_new_event(self, origin, event, state=None, backfilled=False,
                          current_state=None, auth_events=None):

        outlier = event.internal_metadata.is_outlier()

        context = yield self._prep_event(
            origin, event,
            state=state,
            backfilled=backfilled,
            current_state=current_state,
            auth_events=auth_events,
        )

        event_stream_id, max_stream_id = yield self.store.persist_event(
            event,
            context=context,
            backfilled=backfilled,
            is_new_state=(not outlier and not backfilled),
            current_state=current_state,
        )

        defer.returnValue((context, event_stream_id, max_stream_id))

    @defer.inlineCallbacks
    def _handle_new_events(self, origin, event_infos, backfilled=False,
                           outliers=False):
        contexts = yield defer.gatherResults(
            [
                self._prep_event(
                    origin,
                    ev_info["event"],
                    state=ev_info.get("state"),
                    backfilled=backfilled,
                    auth_events=ev_info.get("auth_events"),
                )
                for ev_info in event_infos
            ]
        )

        yield self.store.persist_events(
            [
                (ev_info["event"], context)
                for ev_info, context in itertools.izip(event_infos, contexts)
            ],
            backfilled=backfilled,
            is_new_state=(not outliers and not backfilled),
        )

    @defer.inlineCallbacks
    def _persist_auth_tree(self, auth_events, state, event):
        """Checks the auth chain is valid (and passes auth checks) for the
        state and event. Then persists the auth chain and state atomically.
        Persists the event seperately.

        Returns:
            2-tuple of (event_stream_id, max_stream_id) from the persist_event
            call for `event`
        """
        events_to_context = {}
        for e in itertools.chain(auth_events, state):
            ctx = yield self.state_handler.compute_event_context(
                e, outlier=True,
            )
            events_to_context[e.event_id] = ctx
            e.internal_metadata.outlier = True

        event_map = {
            e.event_id: e
            for e in auth_events
        }

        create_event = None
        for e in auth_events:
            if (e.type, e.state_key) == (EventTypes.Create, ""):
                create_event = e
                break

        for e in itertools.chain(auth_events, state, [event]):
            auth_for_e = {
                (event_map[e_id].type, event_map[e_id].state_key): event_map[e_id]
                for e_id, _ in e.auth_events
            }
            if create_event:
                auth_for_e[(EventTypes.Create, "")] = create_event

            try:
                self.auth.check(e, auth_events=auth_for_e)
            except AuthError as err:
                logger.warn(
                    "Rejecting %s because %s",
                    e.event_id, err.msg
                )

                if e == event:
                    raise
                events_to_context[e.event_id].rejected = RejectedReason.AUTH_ERROR

        yield self.store.persist_events(
            [
                (e, events_to_context[e.event_id])
                for e in itertools.chain(auth_events, state)
            ],
            is_new_state=False,
        )

        new_event_context = yield self.state_handler.compute_event_context(
            event, old_state=state, outlier=False,
        )

        event_stream_id, max_stream_id = yield self.store.persist_event(
            event, new_event_context,
            backfilled=False,
            is_new_state=True,
            current_state=state,
        )

        defer.returnValue((event_stream_id, max_stream_id))

    @defer.inlineCallbacks
    def _prep_event(self, origin, event, state=None, backfilled=False,
                    current_state=None, auth_events=None):
        outlier = event.internal_metadata.is_outlier()

        context = yield self.state_handler.compute_event_context(
            event, old_state=state, outlier=outlier,
        )

        if not auth_events:
            auth_events = context.current_state

        # This is a hack to fix some old rooms where the initial join event
        # didn't reference the create event in its auth events.
        if event.type == EventTypes.Member and not event.auth_events:
            if len(event.prev_events) == 1 and event.depth < 5:
                c = yield self.store.get_event(
                    event.prev_events[0][0],
                    allow_none=True,
                )
                if c and c.type == EventTypes.Create:
                    auth_events[(c.type, c.state_key)] = c

        try:
            yield self.do_auth(
                origin, event, context, auth_events=auth_events
            )
        except AuthError as e:
            logger.warn(
                "Rejecting %s because %s",
                event.event_id, e.msg
            )

            context.rejected = RejectedReason.AUTH_ERROR

        defer.returnValue(context)

    @defer.inlineCallbacks
    def on_query_auth(self, origin, event_id, remote_auth_chain, rejects,
                      missing):
        # Just go through and process each event in `remote_auth_chain`. We
        # don't want to fall into the trap of `missing` being wrong.
        for e in remote_auth_chain:
            try:
                yield self._handle_new_event(origin, e)
            except AuthError:
                pass

        # Now get the current auth_chain for the event.
        local_auth_chain = yield self.store.get_auth_chain([event_id])

        # TODO: Check if we would now reject event_id. If so we need to tell
        # everyone.

        ret = yield self.construct_auth_difference(
            local_auth_chain, remote_auth_chain
        )

        for event in ret["auth_chain"]:
            event.signatures.update(
                compute_event_signature(
                    event,
                    self.hs.hostname,
                    self.hs.config.signing_key[0]
                )
            )

        logger.debug("on_query_auth returning: %s", ret)

        defer.returnValue(ret)

    @defer.inlineCallbacks
    def on_get_missing_events(self, origin, room_id, earliest_events,
                              latest_events, limit, min_depth):
        in_room = yield self.auth.check_host_in_room(
            room_id,
            origin
        )
        if not in_room:
            raise AuthError(403, "Host not in room.")

        limit = min(limit, 20)
        min_depth = max(min_depth, 0)

        missing_events = yield self.store.get_missing_events(
            room_id=room_id,
            earliest_events=earliest_events,
            latest_events=latest_events,
            limit=limit,
            min_depth=min_depth,
        )

        defer.returnValue(missing_events)

    @defer.inlineCallbacks
    @log_function
    def do_auth(self, origin, event, context, auth_events):
        # Check if we have all the auth events.
        current_state = set(e.event_id for e in auth_events.values())
        event_auth_events = set(e_id for e_id, _ in event.auth_events)

        if event_auth_events - current_state:
            have_events = yield self.store.have_events(
                event_auth_events - current_state
            )
        else:
            have_events = {}

        have_events.update({
            e.event_id: ""
            for e in auth_events.values()
        })

        seen_events = set(have_events.keys())

        missing_auth = event_auth_events - seen_events - current_state

        if missing_auth:
            logger.info("Missing auth: %s", missing_auth)
            # If we don't have all the auth events, we need to get them.
            try:
                remote_auth_chain = yield self.replication_layer.get_event_auth(
                    origin, event.room_id, event.event_id
                )

                seen_remotes = yield self.store.have_events(
                    [e.event_id for e in remote_auth_chain]
                )

                for e in remote_auth_chain:
                    if e.event_id in seen_remotes.keys():
                        continue

                    if e.event_id == event.event_id:
                        continue

                    try:
                        auth_ids = [e_id for e_id, _ in e.auth_events]
                        auth = {
                            (e.type, e.state_key): e for e in remote_auth_chain
                            if e.event_id in auth_ids or e.type == EventTypes.Create
                        }
                        e.internal_metadata.outlier = True

                        logger.debug(
                            "do_auth %s missing_auth: %s",
                            event.event_id, e.event_id
                        )
                        yield self._handle_new_event(
                            origin, e, auth_events=auth
                        )

                        if e.event_id in event_auth_events:
                            auth_events[(e.type, e.state_key)] = e
                    except AuthError:
                        pass

                have_events = yield self.store.have_events(
                    [e_id for e_id, _ in event.auth_events]
                )
                seen_events = set(have_events.keys())
            except:
                # FIXME:
                logger.exception("Failed to get auth chain")

        # FIXME: Assumes we have and stored all the state for all the
        # prev_events
        current_state = set(e.event_id for e in auth_events.values())
        different_auth = event_auth_events - current_state

        if different_auth and not event.internal_metadata.is_outlier():
            # Do auth conflict res.
            logger.info("Different auth: %s", different_auth)

            different_events = yield defer.gatherResults(
                [
                    self.store.get_event(
                        d,
                        allow_none=True,
                        allow_rejected=False,
                    )
                    for d in different_auth
                    if d in have_events and not have_events[d]
                ],
                consumeErrors=True
            ).addErrback(unwrapFirstError)

            if different_events:
                local_view = dict(auth_events)
                remote_view = dict(auth_events)
                remote_view.update({
                    (d.type, d.state_key): d for d in different_events
                })

                new_state, prev_state = self.state_handler.resolve_events(
                    [local_view.values(), remote_view.values()],
                    event
                )

                auth_events.update(new_state)

                current_state = set(e.event_id for e in auth_events.values())
                different_auth = event_auth_events - current_state

                context.current_state.update(auth_events)
                context.state_group = None

        if different_auth and not event.internal_metadata.is_outlier():
            logger.info("Different auth after resolution: %s", different_auth)

            # Only do auth resolution if we have something new to say.
            # We can't rove an auth failure.
            do_resolution = False

            provable = [
                RejectedReason.NOT_ANCESTOR, RejectedReason.NOT_ANCESTOR,
            ]

            for e_id in different_auth:
                if e_id in have_events:
                    if have_events[e_id] in provable:
                        do_resolution = True
                        break

            if do_resolution:
                # 1. Get what we think is the auth chain.
                auth_ids = self.auth.compute_auth_events(
                    event, context.current_state
                )
                local_auth_chain = yield self.store.get_auth_chain(auth_ids)

                try:
                    # 2. Get remote difference.
                    result = yield self.replication_layer.query_auth(
                        origin,
                        event.room_id,
                        event.event_id,
                        local_auth_chain,
                    )

                    seen_remotes = yield self.store.have_events(
                        [e.event_id for e in result["auth_chain"]]
                    )

                    # 3. Process any remote auth chain events we haven't seen.
                    for ev in result["auth_chain"]:
                        if ev.event_id in seen_remotes.keys():
                            continue

                        if ev.event_id == event.event_id:
                            continue

                        try:
                            auth_ids = [e_id for e_id, _ in ev.auth_events]
                            auth = {
                                (e.type, e.state_key): e
                                for e in result["auth_chain"]
                                if e.event_id in auth_ids
                                or event.type == EventTypes.Create
                            }
                            ev.internal_metadata.outlier = True

                            logger.debug(
                                "do_auth %s different_auth: %s",
                                event.event_id, e.event_id
                            )

                            yield self._handle_new_event(
                                origin, ev, auth_events=auth
                            )

                            if ev.event_id in event_auth_events:
                                auth_events[(ev.type, ev.state_key)] = ev
                        except AuthError:
                            pass

                except:
                    # FIXME:
                    logger.exception("Failed to query auth chain")

                # 4. Look at rejects and their proofs.
                # TODO.

                context.current_state.update(auth_events)
                context.state_group = None

        try:
            self.auth.check(event, auth_events=auth_events)
        except AuthError:
            raise

    @defer.inlineCallbacks
    def construct_auth_difference(self, local_auth, remote_auth):
        """ Given a local and remote auth chain, find the differences. This
        assumes that we have already processed all events in remote_auth

        Params:
            local_auth (list)
            remote_auth (list)

        Returns:
            dict
        """

        logger.debug("construct_auth_difference Start!")

        # TODO: Make sure we are OK with local_auth or remote_auth having more
        # auth events in them than strictly necessary.

        def sort_fun(ev):
            return ev.depth, ev.event_id

        logger.debug("construct_auth_difference after sort_fun!")

        # We find the differences by starting at the "bottom" of each list
        # and iterating up on both lists. The lists are ordered by depth and
        # then event_id, we iterate up both lists until we find the event ids
        # don't match. Then we look at depth/event_id to see which side is
        # missing that event, and iterate only up that list. Repeat.

        remote_list = list(remote_auth)
        remote_list.sort(key=sort_fun)

        local_list = list(local_auth)
        local_list.sort(key=sort_fun)

        local_iter = iter(local_list)
        remote_iter = iter(remote_list)

        logger.debug("construct_auth_difference before get_next!")

        def get_next(it, opt=None):
            try:
                return it.next()
            except:
                return opt

        current_local = get_next(local_iter)
        current_remote = get_next(remote_iter)

        logger.debug("construct_auth_difference before while")

        missing_remotes = []
        missing_locals = []
        while current_local or current_remote:
            if current_remote is None:
                missing_locals.append(current_local)
                current_local = get_next(local_iter)
                continue

            if current_local is None:
                missing_remotes.append(current_remote)
                current_remote = get_next(remote_iter)
                continue

            if current_local.event_id == current_remote.event_id:
                current_local = get_next(local_iter)
                current_remote = get_next(remote_iter)
                continue

            if current_local.depth < current_remote.depth:
                missing_locals.append(current_local)
                current_local = get_next(local_iter)
                continue

            if current_local.depth > current_remote.depth:
                missing_remotes.append(current_remote)
                current_remote = get_next(remote_iter)
                continue

            # They have the same depth, so we fall back to the event_id order
            if current_local.event_id < current_remote.event_id:
                missing_locals.append(current_local)
                current_local = get_next(local_iter)

            if current_local.event_id > current_remote.event_id:
                missing_remotes.append(current_remote)
                current_remote = get_next(remote_iter)
                continue

        logger.debug("construct_auth_difference after while")

        # missing locals should be sent to the server
        # We should find why we are missing remotes, as they will have been
        # rejected.

        # Remove events from missing_remotes if they are referencing a missing
        # remote. We only care about the "root" rejected ones.
        missing_remote_ids = [e.event_id for e in missing_remotes]
        base_remote_rejected = list(missing_remotes)
        for e in missing_remotes:
            for e_id, _ in e.auth_events:
                if e_id in missing_remote_ids:
                    try:
                        base_remote_rejected.remove(e)
                    except ValueError:
                        pass

        reason_map = {}

        for e in base_remote_rejected:
            reason = yield self.store.get_rejection_reason(e.event_id)
            if reason is None:
                # TODO: e is not in the current state, so we should
                # construct some proof of that.
                continue

            reason_map[e.event_id] = reason

            if reason == RejectedReason.AUTH_ERROR:
                pass
            elif reason == RejectedReason.REPLACED:
                # TODO: Get proof
                pass
            elif reason == RejectedReason.NOT_ANCESTOR:
                # TODO: Get proof.
                pass

        logger.debug("construct_auth_difference returning")

        defer.returnValue({
            "auth_chain": local_auth,
            "rejects": {
                e.event_id: {
                    "reason": reason_map[e.event_id],
                    "proof": None,
                }
                for e in base_remote_rejected
            },
            "missing": [e.event_id for e in missing_locals],
        })

    @defer.inlineCallbacks
    @log_function
    def exchange_third_party_invite(self, invite):
        sender = invite["sender"]
        room_id = invite["room_id"]

        event_dict = {
            "type": EventTypes.Member,
            "content": {
                "membership": Membership.INVITE,
                "third_party_invite": invite,
            },
            "room_id": room_id,
            "sender": sender,
            "state_key": invite["mxid"],
        }

        if (yield self.auth.check_host_in_room(room_id, self.hs.hostname)):
            builder = self.event_builder_factory.new(event_dict)
            EventValidator().validate_new(builder)
            event, context = yield self._create_new_client_event(builder=builder)
            self.auth.check(event, context.current_state)
            yield self._validate_keyserver(event, auth_events=context.current_state)
            member_handler = self.hs.get_handlers().room_member_handler
            yield member_handler.change_membership(event, context)
        else:
            destinations = set([x.split(":", 1)[-1] for x in (sender, room_id)])
            yield self.replication_layer.forward_third_party_invite(
                destinations,
                room_id,
                event_dict,
            )

    @defer.inlineCallbacks
    @log_function
    def on_exchange_third_party_invite_request(self, origin, room_id, event_dict):
        builder = self.event_builder_factory.new(event_dict)

        event, context = yield self._create_new_client_event(
            builder=builder,
        )

        self.auth.check(event, auth_events=context.current_state)
        yield self._validate_keyserver(event, auth_events=context.current_state)

        returned_invite = yield self.send_invite(origin, event)
        # TODO: Make sure the signatures actually are correct.
        event.signatures.update(returned_invite.signatures)
        member_handler = self.hs.get_handlers().room_member_handler
        yield member_handler.change_membership(event, context)

    @defer.inlineCallbacks
    def _validate_keyserver(self, event, auth_events):
        token = event.content["third_party_invite"]["signed"]["token"]

        invite_event = auth_events.get(
            (EventTypes.ThirdPartyInvite, token,)
        )

        try:
            response = yield self.hs.get_simple_http_client().get_json(
                invite_event.content["key_validity_url"],
                {"public_key": invite_event.content["public_key"]}
            )
        except Exception:
            raise SynapseError(
                502,
                "Third party certificate could not be checked"
            )
        if "valid" not in response or not response["valid"]:
            raise AuthError(403, "Third party certificate was invalid")
