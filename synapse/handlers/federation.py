# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
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

import itertools
import logging
import sys

from signedjson.key import decode_verify_key_bytes
from signedjson.sign import verify_signed_json
import six
from six.moves import http_client
from six import iteritems
from twisted.internet import defer
from unpaddedbase64 import decode_base64

from ._base import BaseHandler

from synapse.api.errors import (
    AuthError, FederationError, StoreError, CodeMessageException, SynapseError,
    FederationDeniedError,
)
from synapse.api.constants import EventTypes, Membership, RejectedReason
from synapse.events.validator import EventValidator
from synapse.util import unwrapFirstError, logcontext
from synapse.util.metrics import measure_func
from synapse.util.logutils import log_function
from synapse.util.async import run_on_reactor, Linearizer
from synapse.util.frozenutils import unfreeze
from synapse.crypto.event_signing import (
    compute_event_signature, add_hashes_and_signatures,
)
from synapse.types import UserID, get_domain_from_id

from synapse.events.utils import prune_event

from synapse.util.retryutils import NotRetryingDestination

from synapse.util.distributor import user_joined_room

from six import iteritems

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

        self.store = hs.get_datastore()
        self.replication_layer = hs.get_federation_client()
        self.state_handler = hs.get_state_handler()
        self.server_name = hs.hostname
        self.keyring = hs.get_keyring()
        self.action_generator = hs.get_action_generator()
        self.is_mine_id = hs.is_mine_id
        self.pusher_pool = hs.get_pusherpool()
        self.spam_checker = hs.get_spam_checker()
        self.event_creation_handler = hs.get_event_creation_handler()
        self._server_notices_mxid = hs.config.server_notices_mxid

        # When joining a room we need to queue any events for that room up
        self.room_queues = {}
        self._room_pdu_linearizer = Linearizer("fed_room_pdu")

    @defer.inlineCallbacks
    @log_function
    def on_receive_pdu(self, origin, pdu, get_missing=True):
        """ Process a PDU received via a federation /send/ transaction, or
        via backfill of missing prev_events

        Args:
            origin (str): server which initiated the /send/ transaction. Will
                be used to fetch missing events or state.
            pdu (FrozenEvent): received PDU
            get_missing (bool): True if we should fetch missing prev_events

        Returns (Deferred): completes with None
        """

        # We reprocess pdus when we have seen them only as outliers
        existing = yield self.get_persisted_pdu(
            origin, pdu.event_id, do_auth=False
        )

        # FIXME: Currently we fetch an event again when we already have it
        # if it has been marked as an outlier.

        already_seen = (
            existing and (
                not existing.internal_metadata.is_outlier()
                or pdu.internal_metadata.is_outlier()
            )
        )
        if already_seen:
            logger.debug("Already seen pdu %s", pdu.event_id)
            return

        # do some initial sanity-checking of the event. In particular, make
        # sure it doesn't have hundreds of prev_events or auth_events, which
        # could cause a huge state resolution or cascade of event fetches.
        try:
            self._sanity_check_event(pdu)
        except SynapseError as err:
            raise FederationError(
                "ERROR",
                err.code,
                err.msg,
                affected=pdu.event_id,
            )

        # If we are currently in the process of joining this room, then we
        # queue up events for later processing.
        if pdu.room_id in self.room_queues:
            logger.info("Ignoring PDU %s for room %s from %s for now; join "
                        "in progress", pdu.event_id, pdu.room_id, origin)
            self.room_queues[pdu.room_id].append((pdu, origin))
            return

        # If we're no longer in the room just ditch the event entirely. This
        # is probably an old server that has come back and thinks we're still
        # in the room (or we've been rejoined to the room by a state reset).
        #
        # If we were never in the room then maybe our database got vaped and
        # we should check if we *are* in fact in the room. If we are then we
        # can magically rejoin the room.
        is_in_room = yield self.auth.check_host_in_room(
            pdu.room_id,
            self.server_name
        )
        if not is_in_room:
            was_in_room = yield self.store.was_host_joined(
                pdu.room_id, self.server_name,
            )
            if was_in_room:
                logger.info(
                    "Ignoring PDU %s for room %s from %s as we've left the room!",
                    pdu.event_id, pdu.room_id, origin,
                )
                return

        state = None

        auth_chain = []

        fetch_state = False

        # Get missing pdus if necessary.
        if not pdu.internal_metadata.is_outlier():
            # We only backfill backwards to the min depth.
            min_depth = yield self.get_min_depth_for_context(
                pdu.room_id
            )

            logger.debug(
                "_handle_new_pdu min_depth for %s: %d",
                pdu.room_id, min_depth
            )

            prevs = {e_id for e_id, _ in pdu.prev_events}
            seen = yield self.store.have_seen_events(prevs)

            if min_depth and pdu.depth < min_depth:
                # This is so that we don't notify the user about this
                # message, to work around the fact that some events will
                # reference really really old events we really don't want to
                # send to the clients.
                pdu.internal_metadata.outlier = True
            elif min_depth and pdu.depth > min_depth:
                if get_missing and prevs - seen:
                    # If we're missing stuff, ensure we only fetch stuff one
                    # at a time.
                    logger.info(
                        "Acquiring lock for room %r to fetch %d missing events: %r...",
                        pdu.room_id, len(prevs - seen), list(prevs - seen)[:5],
                    )
                    with (yield self._room_pdu_linearizer.queue(pdu.room_id)):
                        logger.info(
                            "Acquired lock for room %r to fetch %d missing events",
                            pdu.room_id, len(prevs - seen),
                        )

                        yield self._get_missing_events_for_pdu(
                            origin, pdu, prevs, min_depth
                        )

                        # Update the set of things we've seen after trying to
                        # fetch the missing stuff
                        seen = yield self.store.have_seen_events(prevs)

                        if not prevs - seen:
                            logger.info(
                                "Found all missing prev events for %s", pdu.event_id
                            )
                elif prevs - seen:
                    logger.info(
                        "Not fetching %d missing events for room %r,event %s: %r...",
                        len(prevs - seen), pdu.room_id, pdu.event_id,
                        list(prevs - seen)[:5],
                    )

            if prevs - seen:
                logger.info(
                    "Still missing %d events for room %r: %r...",
                    len(prevs - seen), pdu.room_id, list(prevs - seen)[:5]
                )
                fetch_state = True

        if fetch_state:
            # We need to get the state at this event, since we haven't
            # processed all the prev events.
            logger.debug(
                "_handle_new_pdu getting state for %s",
                pdu.room_id
            )
            try:
                state, auth_chain = yield self.replication_layer.get_state_for_room(
                    origin, pdu.room_id, pdu.event_id,
                )
            except Exception:
                logger.exception("Failed to get state for event: %s", pdu.event_id)

        yield self._process_received_pdu(
            origin,
            pdu,
            state=state,
            auth_chain=auth_chain,
        )

    @defer.inlineCallbacks
    def _get_missing_events_for_pdu(self, origin, pdu, prevs, min_depth):
        """
        Args:
            origin (str): Origin of the pdu. Will be called to get the missing events
            pdu: received pdu
            prevs (set(str)): List of event ids which we are missing
            min_depth (int): Minimum depth of events to return.
        """
        # We recalculate seen, since it may have changed.
        seen = yield self.store.have_seen_events(prevs)

        if not prevs - seen:
            return

        latest = yield self.store.get_latest_event_ids_in_room(
            pdu.room_id
        )

        # We add the prev events that we have seen to the latest
        # list to ensure the remote server doesn't give them to us
        latest = set(latest)
        latest |= seen

        logger.info(
            "Missing %d events for room %r pdu %s: %r...",
            len(prevs - seen), pdu.room_id, pdu.event_id, list(prevs - seen)[:5]
        )

        # XXX: we set timeout to 10s to help workaround
        # https://github.com/matrix-org/synapse/issues/1733.
        # The reason is to avoid holding the linearizer lock
        # whilst processing inbound /send transactions, causing
        # FDs to stack up and block other inbound transactions
        # which empirically can currently take up to 30 minutes.
        #
        # N.B. this explicitly disables retry attempts.
        #
        # N.B. this also increases our chances of falling back to
        # fetching fresh state for the room if the missing event
        # can't be found, which slightly reduces our security.
        # it may also increase our DAG extremity count for the room,
        # causing additional state resolution?  See #1760.
        # However, fetching state doesn't hold the linearizer lock
        # apparently.
        #
        # see https://github.com/matrix-org/synapse/pull/1744

        missing_events = yield self.replication_layer.get_missing_events(
            origin,
            pdu.room_id,
            earliest_events_ids=list(latest),
            latest_events=[pdu],
            limit=10,
            min_depth=min_depth,
            timeout=10000,
        )

        logger.info(
            "Got %d events: %r...",
            len(missing_events), [e.event_id for e in missing_events[:5]]
        )

        # We want to sort these by depth so we process them and
        # tell clients about them in order.
        missing_events.sort(key=lambda x: x.depth)

        for e in missing_events:
            logger.info("Handling found event %s", e.event_id)
            yield self.on_receive_pdu(
                origin,
                e,
                get_missing=False
            )

    @log_function
    @defer.inlineCallbacks
    def _process_received_pdu(self, origin, pdu, state, auth_chain):
        """ Called when we have a new pdu. We need to do auth checks and put it
        through the StateHandler.
        """
        event = pdu

        logger.debug("Processing event: %s", event)

        # FIXME (erikj): Awful hack to make the case where we are not currently
        # in the room work
        # If state and auth_chain are None, then we don't need to do this check
        # as we already know we have enough state in the DB to handle this
        # event.
        if state and auth_chain and not event.internal_metadata.is_outlier():
            is_in_room = yield self.auth.check_host_in_room(
                event.room_id,
                self.server_name
            )
        else:
            is_in_room = True
        if not is_in_room:
            logger.info(
                "Got event for room we're not in: %r %r",
                event.room_id, event.event_id
            )

            try:
                event_stream_id, max_stream_id = yield self._persist_auth_tree(
                    origin, auth_chain, state, event
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

            seen_ids = yield self.store.have_seen_events(event_ids)

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

                yield self._handle_new_events(origin, event_infos)

            try:
                context, event_stream_id, max_stream_id = yield self._handle_new_event(
                    origin,
                    event,
                    state=state,
                )
            except AuthError as e:
                raise FederationError(
                    "ERROR",
                    e.code,
                    e.msg,
                    affected=event.event_id,
                )

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

        extra_users = []
        if event.type == EventTypes.Member:
            target_user_id = event.state_key
            target_user = UserID.from_string(target_user_id)
            extra_users.append(target_user)

        self.notifier.on_new_room_event(
            event, event_stream_id, max_stream_id,
            extra_users=extra_users
        )

        if event.type == EventTypes.Member:
            if event.membership == Membership.JOIN:
                # Only fire user_joined_room if the user has acutally
                # joined the room. Don't bother if the user is just
                # changing their profile info.
                newly_joined = True
                prev_state_id = context.prev_state_ids.get(
                    (event.type, event.state_key)
                )
                if prev_state_id:
                    prev_state = yield self.store.get_event(
                        prev_state_id, allow_none=True,
                    )
                    if prev_state and prev_state.membership == Membership.JOIN:
                        newly_joined = False

                if newly_joined:
                    user = UserID.from_string(event.state_key)
                    yield user_joined_room(self.distributor, user, event.room_id)

    @measure_func("_filter_events_for_server")
    @defer.inlineCallbacks
    def _filter_events_for_server(self, server_name, room_id, events):
        event_to_state_ids = yield self.store.get_state_ids_for_events(
            frozenset(e.event_id for e in events),
            types=(
                (EventTypes.RoomHistoryVisibility, ""),
                (EventTypes.Member, None),
            )
        )

        # We only want to pull out member events that correspond to the
        # server's domain.

        def check_match(id):
            try:
                return server_name == get_domain_from_id(id)
            except Exception:
                return False

        # Parses mapping `event_id -> (type, state_key) -> state event_id`
        # to get all state ids that we're interested in.
        event_map = yield self.store.get_events([
            e_id
            for key_to_eid in list(event_to_state_ids.values())
            for key, e_id in key_to_eid.items()
            if key[0] != EventTypes.Member or check_match(key[1])
        ])

        event_to_state = {
            e_id: {
                key: event_map[inner_e_id]
                for key, inner_e_id in key_to_eid.iteritems()
                if inner_e_id in event_map
            }
            for e_id, key_to_eid in event_to_state_ids.iteritems()
        }

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
                    for ev in state.itervalues():
                        if ev.type != EventTypes.Member:
                            continue
                        try:
                            domain = get_domain_from_id(ev.state_key)
                        except Exception:
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
    def backfill(self, dest, room_id, limit, extremities):
        """ Trigger a backfill request to `dest` for the given `room_id`

        This will attempt to get more events from the remote. If the other side
        has no new events to offer, this will return an empty list.

        As the events are received, we check their signatures, and also do some
        sanity-checking on them. If any of the backfilled events are invalid,
        this method throws a SynapseError.

        TODO: make this more useful to distinguish failures of the remote
        server from invalid events (there is probably no point in trying to
        re-fetch invalid events from every other HS in the room.)
        """
        if dest == self.server_name:
            raise SynapseError(400, "Can't backfill from self.")

        events = yield self.replication_layer.backfill(
            dest,
            room_id,
            limit=limit,
            extremities=extremities,
        )

        # ideally we'd sanity check the events here for excess prev_events etc,
        # but it's hard to reject events at this point without completely
        # breaking backfill in the same way that it is currently broken by
        # events whose signature we cannot verify (#3121).
        #
        # So for now we accept the events anyway. #3124 tracks this.
        #
        # for ev in events:
        #     self._sanity_check_event(ev)

        # Don't bother processing events we already have.
        seen_events = yield self.store.have_events_in_timeline(
            set(e.event_id for e in events)
        )

        events = [e for e in events if e.event_id not in seen_events]

        if not events:
            defer.returnValue([])

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

        required_auth = set(
            a_id
            for event in events + state_events.values() + auth_events.values()
            for a_id, _ in event.auth_events
        )
        auth_events.update({
            e_id: event_map[e_id] for e_id in required_auth if e_id in event_map
        })
        missing_auth = required_auth - set(auth_events)
        failed_to_fetch = set()

        # Try and fetch any missing auth events from both DB and remote servers.
        # We repeatedly do this until we stop finding new auth events.
        while missing_auth - failed_to_fetch:
            logger.info("Missing auth for backfill: %r", missing_auth)
            ret_events = yield self.store.get_events(missing_auth - failed_to_fetch)
            auth_events.update(ret_events)

            required_auth.update(
                a_id for event in ret_events.values() for a_id, _ in event.auth_events
            )
            missing_auth = required_auth - set(auth_events)

            if missing_auth - failed_to_fetch:
                logger.info(
                    "Fetching missing auth for backfill: %r",
                    missing_auth - failed_to_fetch
                )

                results = yield logcontext.make_deferred_yieldable(defer.gatherResults(
                    [
                        logcontext.run_in_background(
                            self.replication_layer.get_pdu,
                            [dest],
                            event_id,
                            outlier=True,
                            timeout=10000,
                        )
                        for event_id in missing_auth - failed_to_fetch
                    ],
                    consumeErrors=True
                )).addErrback(unwrapFirstError)
                auth_events.update({a.event_id: a for a in results if a})
                required_auth.update(
                    a_id
                    for event in results if event
                    for a_id, _ in event.auth_events
                )
                missing_auth = required_auth - set(auth_events)

                failed_to_fetch = missing_auth - set(auth_events)

        seen_events = yield self.store.have_seen_events(
            set(auth_events.keys()) | set(state_events.keys())
        )

        ev_infos = []
        for a in auth_events.values():
            if a.event_id in seen_events:
                continue
            a.internal_metadata.outlier = True
            ev_infos.append({
                "event": a,
                "auth_events": {
                    (auth_events[a_id].type, auth_events[a_id].state_key):
                    auth_events[a_id]
                    for a_id, _ in a.auth_events
                    if a_id in auth_events
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
                    if a_id in auth_events
                }
            })

        yield self._handle_new_events(
            dest, ev_infos,
            backfilled=True,
        )

        events.sort(key=lambda e: e.depth)

        for event in events:
            if event in events_to_state:
                continue

            # We store these one at a time since each event depends on the
            # previous to work out the state.
            # TODO: We can probably do something more clever here.
            yield self._handle_new_event(
                dest, event, backfilled=True,
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

        # We don't want to specify too many extremities as it causes the backfill
        # request URI to be too long.
        extremities = dict(sorted_extremeties_tuple[:5])

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
            """Get joined domains from state

            Args:
                state (dict[tuple, FrozenEvent]): State map from type/state
                    key to event.

            Returns:
                list[tuple[str, int]]: Returns a list of servers with the
                lowest depth of their joins. Sorted by lowest depth first.
            """
            joined_users = [
                (state_key, int(event.depth))
                for (e_type, state_key), event in state.iteritems()
                if e_type == EventTypes.Member
                and event.membership == Membership.JOIN
            ]

            joined_domains = {}
            for u, d in joined_users:
                try:
                    dom = get_domain_from_id(u)
                    old_d = joined_domains.get(dom)
                    if old_d:
                        joined_domains[dom] = min(d, old_d)
                    else:
                        joined_domains[dom] = d
                except Exception:
                    pass

            return sorted(joined_domains.iteritems(), key=lambda d: d[1])

        curr_domains = get_domains_from_state(curr_state)

        likely_domains = [
            domain for domain, depth in curr_domains
            if domain != self.server_name
        ]

        @defer.inlineCallbacks
        def try_backfill(domains):
            # TODO: Should we try multiple of these at a time?
            for dom in domains:
                try:
                    yield self.backfill(
                        dom, room_id,
                        limit=100,
                        extremities=extremities,
                    )
                    # If this succeeded then we probably already have the
                    # appropriate stuff.
                    # TODO: We can probably do something more intelligent here.
                    defer.returnValue(True)
                except SynapseError as e:
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
                except FederationDeniedError as e:
                    logger.info(e)
                    continue
                except Exception as e:
                    logger.exception(
                        "Failed to backfill from %s because %s",
                        dom, e,
                    )
                    continue

            defer.returnValue(False)

        success = yield try_backfill(likely_domains)
        if success:
            defer.returnValue(True)

        # Huh, well *those* domains didn't work out. Lets try some domains
        # from the time.

        tried_domains = set(likely_domains)
        tried_domains.add(self.server_name)

        event_ids = list(extremities.iterkeys())

        logger.debug("calling resolve_state_groups in _maybe_backfill")
        resolve = logcontext.preserve_fn(
            self.state_handler.resolve_state_groups_for_events
        )
        states = yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [resolve(room_id, [e]) for e in event_ids],
            consumeErrors=True,
        ))

        # dict[str, dict[tuple, str]], a map from event_id to state map of
        # event_ids.
        states = dict(zip(event_ids, [s.state for s in states]))

        state_map = yield self.store.get_events(
            [e_id for ids in states.itervalues() for e_id in ids.itervalues()],
            get_prev_content=False
        )
        states = {
            key: {
                k: state_map[e_id]
                for k, e_id in state_dict.iteritems()
                if e_id in state_map
            } for key, state_dict in states.iteritems()
        }

        for e_id, _ in sorted_extremeties_tuple:
            likely_domains = get_domains_from_state(states[e_id])

            success = yield try_backfill([
                dom for dom, _ in likely_domains
                if dom not in tried_domains
            ])
            if success:
                defer.returnValue(True)

            tried_domains.update(dom for dom, _ in likely_domains)

        defer.returnValue(False)

    def _sanity_check_event(self, ev):
        """
        Do some early sanity checks of a received event

        In particular, checks it doesn't have an excessive number of
        prev_events or auth_events, which could cause a huge state resolution
        or cascade of event fetches.

        Args:
            ev (synapse.events.EventBase): event to be checked

        Returns: None

        Raises:
            SynapseError if the event does not pass muster
        """
        if len(ev.prev_events) > 20:
            logger.warn("Rejecting event %s which has %i prev_events",
                        ev.event_id, len(ev.prev_events))
            raise SynapseError(
                http_client.BAD_REQUEST,
                "Too many prev_events",
            )

        if len(ev.auth_events) > 10:
            logger.warn("Rejecting event %s which has %i auth_events",
                        ev.event_id, len(ev.auth_events))
            raise SynapseError(
                http_client.BAD_REQUEST,
                "Too many auth_events",
            )

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
        event = yield self.store.get_event(event_id)
        auth = yield self.store.get_auth_chain(
            [auth_id for auth_id, _ in event.auth_events],
            include_given=True
        )

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
    def do_invite_join(self, target_hosts, room_id, joinee, content):
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

        origin, event = yield self._make_and_verify_event(
            target_hosts,
            room_id,
            joinee,
            "join",
            content,
        )

        # This shouldn't happen, because the RoomMemberHandler has a
        # linearizer lock which only allows one operation per user per room
        # at a time - so this is just paranoia.
        assert (room_id not in self.room_queues)

        self.room_queues[room_id] = []

        yield self.store.clean_room_for_join(room_id)

        handled_events = set()

        try:
            event = self._sign_event(event)
            # Try the host we successfully got a response to /make_join/
            # request first.
            try:
                target_hosts.remove(origin)
                target_hosts.insert(0, origin)
            except ValueError:
                pass
            ret = yield self.replication_layer.send_join(target_hosts, event)

            origin = ret["origin"]
            state = ret["state"]
            auth_chain = ret["auth_chain"]
            auth_chain.sort(key=lambda e: e.depth)

            handled_events.update([s.event_id for s in state])
            handled_events.update([a.event_id for a in auth_chain])
            handled_events.add(event.event_id)

            logger.debug("do_invite_join auth_chain: %s", auth_chain)
            logger.debug("do_invite_join state: %s", state)

            logger.debug("do_invite_join event: %s", event)

            try:
                yield self.store.store_room(
                    room_id=room_id,
                    room_creator_user_id="",
                    is_public=False
                )
            except Exception:
                # FIXME
                pass

            event_stream_id, max_stream_id = yield self._persist_auth_tree(
                origin, auth_chain, state, event
            )

            self.notifier.on_new_room_event(
                event, event_stream_id, max_stream_id,
                extra_users=[joinee]
            )

            logger.debug("Finished joining %s to %s", joinee, room_id)
        finally:
            room_queue = self.room_queues[room_id]
            del self.room_queues[room_id]

            # we don't need to wait for the queued events to be processed -
            # it's just a best-effort thing at this point. We do want to do
            # them roughly in order, though, otherwise we'll end up making
            # lots of requests for missing prev_events which we do actually
            # have. Hence we fire off the deferred, but don't wait for it.

            logcontext.run_in_background(self._handle_queued_pdus, room_queue)

        defer.returnValue(True)

    @defer.inlineCallbacks
    def _handle_queued_pdus(self, room_queue):
        """Process PDUs which got queued up while we were busy send_joining.

        Args:
            room_queue (list[FrozenEvent, str]): list of PDUs to be processed
                and the servers that sent them
        """
        for p, origin in room_queue:
            try:
                logger.info("Processing queued PDU %s which was received "
                            "while we were joining %s", p.event_id, p.room_id)
                yield self.on_receive_pdu(origin, p)
            except Exception as e:
                logger.warn(
                    "Error handling queued PDU %s from %s: %s",
                    p.event_id, origin, e)

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

        try:
            event, context = yield self.event_creation_handler.create_new_client_event(
                builder=builder,
            )
        except AuthError as e:
            logger.warn("Failed to create join %r because %s", event, e)
            raise e

        # The remote hasn't signed it yet, obviously. We'll do the full checks
        # when we get the event back in `on_send_join_request`
        yield self.auth.check_from_context(event, context, do_sig_check=False)

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
        # Send this event on behalf of the origin server.
        #
        # The reasons we have the destination server rather than the origin
        # server send it are slightly mysterious: the origin server should have
        # all the neccessary state once it gets the response to the send_join,
        # so it could send the event itself if it wanted to. It may be that
        # doing it this way reduces failure modes, or avoids certain attacks
        # where a new server selectively tells a subset of the federation that
        # it has joined.
        #
        # The fact is that, as of the current writing, Synapse doesn't send out
        # the join event over federation after joining, and changing it now
        # would introduce the danger of backwards-compatibility problems.
        event.internal_metadata.send_on_behalf_of = origin

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

        self.notifier.on_new_room_event(
            event, event_stream_id, max_stream_id, extra_users=extra_users
        )

        if event.type == EventTypes.Member:
            if event.content["membership"] == Membership.JOIN:
                user = UserID.from_string(event.state_key)
                yield user_joined_room(self.distributor, user, event.room_id)

        state_ids = list(context.prev_state_ids.values())
        auth_chain = yield self.store.get_auth_chain(state_ids)

        state = yield self.store.get_events(list(context.prev_state_ids.values()))

        defer.returnValue({
            "state": list(state.values()),
            "auth_chain": auth_chain,
        })

    @defer.inlineCallbacks
    def on_invite_request(self, origin, pdu):
        """ We've got an invite event. Process and persist it. Sign it.

        Respond with the now signed event.
        """
        event = pdu

        if event.state_key is None:
            raise SynapseError(400, "The invite event did not have a state key")

        is_blocked = yield self.store.is_room_blocked(event.room_id)
        if is_blocked:
            raise SynapseError(403, "This room has been blocked on this server")

        if self.hs.config.block_non_admin_invites:
            raise SynapseError(403, "This server does not accept room invites")

        if not self.spam_checker.user_may_invite(
            event.sender, event.state_key, event.room_id,
        ):
            raise SynapseError(
                403, "This user is not permitted to send invites to this server/user"
            )

        membership = event.content.get("membership")
        if event.type != EventTypes.Member or membership != Membership.INVITE:
            raise SynapseError(400, "The event was not an m.room.member invite event")

        sender_domain = get_domain_from_id(event.sender)
        if sender_domain != origin:
            raise SynapseError(400, "The invite event was not from the server sending it")

        if not self.is_mine_id(event.state_key):
            raise SynapseError(400, "The invite event must be for this server")

        # block any attempts to invite the server notices mxid
        if event.state_key == self._server_notices_mxid:
            raise SynapseError(
                http_client.FORBIDDEN,
                "Cannot invite this user",
            )

        event.internal_metadata.outlier = True
        event.internal_metadata.invite_from_remote = True

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
        )

        target_user = UserID.from_string(event.state_key)
        self.notifier.on_new_room_event(
            event, event_stream_id, max_stream_id,
            extra_users=[target_user],
        )

        defer.returnValue(event)

    @defer.inlineCallbacks
    def do_remotely_reject_invite(self, target_hosts, room_id, user_id):
        origin, event = yield self._make_and_verify_event(
            target_hosts,
            room_id,
            user_id,
            "leave"
        )
        # Mark as outlier as we don't have any state for this event; we're not
        # even in the room.
        event.internal_metadata.outlier = True
        event = self._sign_event(event)

        # Try the host that we succesfully called /make_leave/ on first for
        # the /send_leave/ request.
        try:
            target_hosts.remove(origin)
            target_hosts.insert(0, origin)
        except ValueError:
            pass

        yield self.replication_layer.send_leave(
            target_hosts,
            event
        )

        context = yield self.state_handler.compute_event_context(event)

        event_stream_id, max_stream_id = yield self.store.persist_event(
            event,
            context=context,
        )

        target_user = UserID.from_string(event.state_key)
        self.notifier.on_new_room_event(
            event, event_stream_id, max_stream_id,
            extra_users=[target_user],
        )

        defer.returnValue(event)

    @defer.inlineCallbacks
    def _make_and_verify_event(self, target_hosts, room_id, user_id, membership,
                               content={},):
        origin, pdu = yield self.replication_layer.make_membership_event(
            target_hosts,
            room_id,
            user_id,
            membership,
            content,
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

        event, context = yield self.event_creation_handler.create_new_client_event(
            builder=builder,
        )

        try:
            # The remote hasn't signed it yet, obviously. We'll do the full checks
            # when we get the event back in `on_send_leave_request`
            yield self.auth.check_from_context(event, context, do_sig_check=False)
        except AuthError as e:
            logger.warn("Failed to create new leave %r because %s", event, e)
            raise e

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

        self.notifier.on_new_room_event(
            event, event_stream_id, max_stream_id, extra_users=extra_users
        )

        defer.returnValue(None)

    @defer.inlineCallbacks
    def get_state_for_pdu(self, room_id, event_id):
        """Returns the state at the event. i.e. not including said event.
        """
        yield run_on_reactor()

        state_groups = yield self.store.get_state_groups(
            room_id, [event_id]
        )

        if state_groups:
            _, state = list(iteritems(state_groups)).pop()
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

            res = list(results.values())
            for event in res:
                # We sign these again because there was a bug where we
                # incorrectly signed things the first time round
                if self.is_mine_id(event.event_id):
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
    def get_state_ids_for_pdu(self, room_id, event_id):
        """Returns the state at the event. i.e. not including said event.
        """
        yield run_on_reactor()

        state_groups = yield self.store.get_state_groups_ids(
            room_id, [event_id]
        )

        if state_groups:
            _, state = state_groups.items().pop()
            results = state

            event = yield self.store.get_event(event_id)
            if event and event.is_state():
                # Get previous state
                if "replaces_state" in event.unsigned:
                    prev_id = event.unsigned["replaces_state"]
                    if prev_id != event.event_id:
                        results[(event.type, event.state_key)] = prev_id
                else:
                    results.pop((event.type, event.state_key), None)

            defer.returnValue(list(results.values()))
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
            if self.is_mine_id(event.event_id):
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

                events = yield self._filter_events_for_server(
                    origin, event.room_id, [event]
                )

                event = events[0]

            defer.returnValue(event)
        else:
            defer.returnValue(None)

    @log_function
    def get_min_depth_for_context(self, context):
        return self.store.get_min_depth(context)

    @defer.inlineCallbacks
    @log_function
    def _handle_new_event(self, origin, event, state=None, auth_events=None,
                          backfilled=False):
        context = yield self._prep_event(
            origin, event,
            state=state,
            auth_events=auth_events,
        )

        try:
            if not event.internal_metadata.is_outlier() and not backfilled:
                yield self.action_generator.handle_push_actions_for_event(
                    event, context
                )

            event_stream_id, max_stream_id = yield self.store.persist_event(
                event,
                context=context,
                backfilled=backfilled,
            )
        except:  # noqa: E722, as we reraise the exception this is fine.
            tp, value, tb = sys.exc_info()

            logcontext.run_in_background(
                self.store.remove_push_actions_from_staging,
                event.event_id,
            )

            six.reraise(tp, value, tb)

        if not backfilled:
            # this intentionally does not yield: we don't care about the result
            # and don't need to wait for it.
            logcontext.run_in_background(
                self.pusher_pool.on_new_notifications,
                event_stream_id, max_stream_id,
            )

        defer.returnValue((context, event_stream_id, max_stream_id))

    @defer.inlineCallbacks
    def _handle_new_events(self, origin, event_infos, backfilled=False):
        """Creates the appropriate contexts and persists events. The events
        should not depend on one another, e.g. this should be used to persist
        a bunch of outliers, but not a chunk of individual events that depend
        on each other for state calculations.
        """
        contexts = yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                logcontext.run_in_background(
                    self._prep_event,
                    origin,
                    ev_info["event"],
                    state=ev_info.get("state"),
                    auth_events=ev_info.get("auth_events"),
                )
                for ev_info in event_infos
            ], consumeErrors=True,
        ))

        yield self.store.persist_events(
            [
                (ev_info["event"], context)
                for ev_info, context in itertools.izip(event_infos, contexts)
            ],
            backfilled=backfilled,
        )

    @defer.inlineCallbacks
    def _persist_auth_tree(self, origin, auth_events, state, event):
        """Checks the auth chain is valid (and passes auth checks) for the
        state and event. Then persists the auth chain and state atomically.
        Persists the event seperately.

        Will attempt to fetch missing auth events.

        Args:
            origin (str): Where the events came from
            auth_events (list)
            state (list)
            event (Event)

        Returns:
            2-tuple of (event_stream_id, max_stream_id) from the persist_event
            call for `event`
        """
        events_to_context = {}
        for e in itertools.chain(auth_events, state):
            e.internal_metadata.outlier = True
            ctx = yield self.state_handler.compute_event_context(e)
            events_to_context[e.event_id] = ctx

        event_map = {
            e.event_id: e
            for e in itertools.chain(auth_events, state, [event])
        }

        create_event = None
        for e in auth_events:
            if (e.type, e.state_key) == (EventTypes.Create, ""):
                create_event = e
                break

        missing_auth_events = set()
        for e in itertools.chain(auth_events, state, [event]):
            for e_id, _ in e.auth_events:
                if e_id not in event_map:
                    missing_auth_events.add(e_id)

        for e_id in missing_auth_events:
            m_ev = yield self.replication_layer.get_pdu(
                [origin],
                e_id,
                outlier=True,
                timeout=10000,
            )
            if m_ev and m_ev.event_id == e_id:
                event_map[e_id] = m_ev
            else:
                logger.info("Failed to find auth event %r", e_id)

        for e in itertools.chain(auth_events, state, [event]):
            auth_for_e = {
                (event_map[e_id].type, event_map[e_id].state_key): event_map[e_id]
                for e_id, _ in e.auth_events
                if e_id in event_map
            }
            if create_event:
                auth_for_e[(EventTypes.Create, "")] = create_event

            try:
                self.auth.check(e, auth_events=auth_for_e)
            except SynapseError as err:
                # we may get SynapseErrors here as well as AuthErrors. For
                # instance, there are a couple of (ancient) events in some
                # rooms whose senders do not have the correct sigil; these
                # cause SynapseErrors in auth.check. We don't want to give up
                # the attempt to federate altogether in such cases.

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
        )

        new_event_context = yield self.state_handler.compute_event_context(
            event, old_state=state
        )

        event_stream_id, max_stream_id = yield self.store.persist_event(
            event, new_event_context,
        )

        defer.returnValue((event_stream_id, max_stream_id))

    @defer.inlineCallbacks
    def _prep_event(self, origin, event, state=None, auth_events=None):
        """

        Args:
            origin:
            event:
            state:
            auth_events:

        Returns:
            Deferred, which resolves to synapse.events.snapshot.EventContext
        """
        context = yield self.state_handler.compute_event_context(
            event, old_state=state,
        )

        if not auth_events:
            auth_events_ids = yield self.auth.compute_auth_events(
                event, context.prev_state_ids, for_verification=True,
            )
            auth_events = yield self.store.get_events(auth_events_ids)
            auth_events = {
                (e.type, e.state_key): e for e in auth_events.values()
            }

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

        if event.type == EventTypes.GuestAccess and not context.rejected:
            yield self.maybe_kick_guest_users(event)

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
        event = yield self.store.get_event(event_id)
        local_auth_chain = yield self.store.get_auth_chain(
            [auth_id for auth_id, _ in event.auth_events],
            include_given=True
        )

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
        """

        Args:
            origin (str):
            event (synapse.events.FrozenEvent):
            context (synapse.events.snapshot.EventContext):
            auth_events (dict[(str, str)->str]):

        Returns:
            defer.Deferred[None]
        """
        # Check if we have all the auth events.
        current_state = set(e.event_id for e in auth_events.values())
        event_auth_events = set(e_id for e_id, _ in event.auth_events)

        if event.is_state():
            event_key = (event.type, event.state_key)
        else:
            event_key = None

        if event_auth_events - current_state:
            # TODO: can we use store.have_seen_events here instead?
            have_events = yield self.store.get_seen_events_with_rejections(
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

                seen_remotes = yield self.store.have_seen_events(
                    [e.event_id for e in remote_auth_chain]
                )

                for e in remote_auth_chain:
                    if e.event_id in seen_remotes:
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

                have_events = yield self.store.get_seen_events_with_rejections(
                    [e_id for e_id, _ in event.auth_events]
                )
                seen_events = set(have_events.keys())
            except Exception:
                # FIXME:
                logger.exception("Failed to get auth chain")

        # FIXME: Assumes we have and stored all the state for all the
        # prev_events
        current_state = set(e.event_id for e in auth_events.values())
        different_auth = event_auth_events - current_state

        if different_auth and not event.internal_metadata.is_outlier():
            # Do auth conflict res.
            logger.info("Different auth: %s", different_auth)

            different_events = yield logcontext.make_deferred_yieldable(
                defer.gatherResults([
                    logcontext.run_in_background(
                        self.store.get_event,
                        d,
                        allow_none=True,
                        allow_rejected=False,
                    )
                    for d in different_auth
                    if d in have_events and not have_events[d]
                ], consumeErrors=True)
            ).addErrback(unwrapFirstError)

            if different_events:
                local_view = dict(auth_events)
                remote_view = dict(auth_events)
                remote_view.update({
                    (d.type, d.state_key): d for d in different_events if d
                })

                new_state = self.state_handler.resolve_events(
                    [list(local_view.values()), list(remote_view.values())],
                    event
                )

                auth_events.update(new_state)

                current_state = set(e.event_id for e in auth_events.values())
                different_auth = event_auth_events - current_state

                yield self._update_context_for_auth_events(
                    event, context, auth_events, event_key,
                )

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
                auth_ids = yield self.auth.compute_auth_events(
                    event, context.prev_state_ids
                )
                local_auth_chain = yield self.store.get_auth_chain(
                    auth_ids, include_given=True
                )

                try:
                    # 2. Get remote difference.
                    result = yield self.replication_layer.query_auth(
                        origin,
                        event.room_id,
                        event.event_id,
                        local_auth_chain,
                    )

                    seen_remotes = yield self.store.have_seen_events(
                        [e.event_id for e in result["auth_chain"]]
                    )

                    # 3. Process any remote auth chain events we haven't seen.
                    for ev in result["auth_chain"]:
                        if ev.event_id in seen_remotes:
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

                except Exception:
                    # FIXME:
                    logger.exception("Failed to query auth chain")

                # 4. Look at rejects and their proofs.
                # TODO.

                yield self._update_context_for_auth_events(
                    event, context, auth_events, event_key,
                )

        try:
            self.auth.check(event, auth_events=auth_events)
        except AuthError as e:
            logger.warn("Failed auth resolution for %r because %s", event, e)
            raise e

    @defer.inlineCallbacks
    def _update_context_for_auth_events(self, event, context, auth_events,
                                        event_key):
        """Update the state_ids in an event context after auth event resolution,
        storing the changes as a new state group.

        Args:
            event (Event): The event we're handling the context for

            context (synapse.events.snapshot.EventContext): event context
                to be updated

            auth_events (dict[(str, str)->str]): Events to update in the event
                context.

            event_key ((str, str)): (type, state_key) for the current event.
                this will not be included in the current_state in the context.
        """
        state_updates = {
            k: a.event_id for k, a in iteritems(auth_events)
            if k != event_key
        }
        context.current_state_ids = dict(context.current_state_ids)
        context.current_state_ids.update(state_updates)
        if context.delta_ids is not None:
            context.delta_ids = dict(context.delta_ids)
            context.delta_ids.update(state_updates)
        context.prev_state_ids = dict(context.prev_state_ids)
        context.prev_state_ids.update({
            k: a.event_id for k, a in iteritems(auth_events)
        })
        context.state_group = yield self.store.store_state_group(
            event.event_id,
            event.room_id,
            prev_group=context.prev_group,
            delta_ids=context.delta_ids,
            current_state_ids=context.current_state_ids,
        )

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
                return next(it)
            except Exception:
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
    def exchange_third_party_invite(
            self,
            sender_user_id,
            target_user_id,
            room_id,
            signed,
    ):
        third_party_invite = {
            "signed": signed,
        }

        event_dict = {
            "type": EventTypes.Member,
            "content": {
                "membership": Membership.INVITE,
                "third_party_invite": third_party_invite,
            },
            "room_id": room_id,
            "sender": sender_user_id,
            "state_key": target_user_id,
        }

        if (yield self.auth.check_host_in_room(room_id, self.hs.hostname)):
            builder = self.event_builder_factory.new(event_dict)
            EventValidator().validate_new(builder)
            event, context = yield self.event_creation_handler.create_new_client_event(
                builder=builder
            )

            event, context = yield self.add_display_name_to_third_party_invite(
                event_dict, event, context
            )

            try:
                yield self.auth.check_from_context(event, context)
            except AuthError as e:
                logger.warn("Denying new third party invite %r because %s", event, e)
                raise e

            yield self._check_signature(event, context)
            member_handler = self.hs.get_room_member_handler()
            yield member_handler.send_membership_event(None, event, context)
        else:
            destinations = set(x.split(":", 1)[-1] for x in (sender_user_id, room_id))
            yield self.replication_layer.forward_third_party_invite(
                destinations,
                room_id,
                event_dict,
            )

    @defer.inlineCallbacks
    @log_function
    def on_exchange_third_party_invite_request(self, origin, room_id, event_dict):
        """Handle an exchange_third_party_invite request from a remote server

        The remote server will call this when it wants to turn a 3pid invite
        into a normal m.room.member invite.

        Returns:
            Deferred: resolves (to None)
        """
        builder = self.event_builder_factory.new(event_dict)

        event, context = yield self.event_creation_handler.create_new_client_event(
            builder=builder,
        )

        event, context = yield self.add_display_name_to_third_party_invite(
            event_dict, event, context
        )

        try:
            self.auth.check_from_context(event, context)
        except AuthError as e:
            logger.warn("Denying third party invite %r because %s", event, e)
            raise e
        yield self._check_signature(event, context)

        # XXX we send the invite here, but send_membership_event also sends it,
        # so we end up making two requests. I think this is redundant.
        returned_invite = yield self.send_invite(origin, event)
        # TODO: Make sure the signatures actually are correct.
        event.signatures.update(returned_invite.signatures)

        member_handler = self.hs.get_room_member_handler()
        yield member_handler.send_membership_event(None, event, context)

    @defer.inlineCallbacks
    def add_display_name_to_third_party_invite(self, event_dict, event, context):
        key = (
            EventTypes.ThirdPartyInvite,
            event.content["third_party_invite"]["signed"]["token"]
        )
        original_invite = None
        original_invite_id = context.prev_state_ids.get(key)
        if original_invite_id:
            original_invite = yield self.store.get_event(
                original_invite_id, allow_none=True
            )
        if original_invite:
            display_name = original_invite.content["display_name"]
            event_dict["content"]["third_party_invite"]["display_name"] = display_name
        else:
            logger.info(
                "Could not find invite event for third_party_invite: %r",
                event_dict
            )
            # We don't discard here as this is not the appropriate place to do
            # auth checks. If we need the invite and don't have it then the
            # auth check code will explode appropriately.

        builder = self.event_builder_factory.new(event_dict)
        EventValidator().validate_new(builder)
        event, context = yield self.event_creation_handler.create_new_client_event(
            builder=builder,
        )
        defer.returnValue((event, context))

    @defer.inlineCallbacks
    def _check_signature(self, event, context):
        """
        Checks that the signature in the event is consistent with its invite.

        Args:
            event (Event): The m.room.member event to check
            context (EventContext):

        Raises:
            AuthError: if signature didn't match any keys, or key has been
                revoked,
            SynapseError: if a transient error meant a key couldn't be checked
                for revocation.
        """
        signed = event.content["third_party_invite"]["signed"]
        token = signed["token"]

        invite_event_id = context.prev_state_ids.get(
            (EventTypes.ThirdPartyInvite, token,)
        )

        invite_event = None
        if invite_event_id:
            invite_event = yield self.store.get_event(invite_event_id, allow_none=True)

        if not invite_event:
            raise AuthError(403, "Could not find invite")

        last_exception = None
        for public_key_object in self.hs.get_auth().get_public_keys(invite_event):
            try:
                for server, signature_block in signed["signatures"].items():
                    for key_name, encoded_signature in signature_block.items():
                        if not key_name.startswith("ed25519:"):
                            continue

                        public_key = public_key_object["public_key"]
                        verify_key = decode_verify_key_bytes(
                            key_name,
                            decode_base64(public_key)
                        )
                        verify_signed_json(signed, server, verify_key)
                        if "key_validity_url" in public_key_object:
                            yield self._check_key_revocation(
                                public_key,
                                public_key_object["key_validity_url"]
                            )
                        return
            except Exception as e:
                last_exception = e
        raise last_exception

    @defer.inlineCallbacks
    def _check_key_revocation(self, public_key, url):
        """
        Checks whether public_key has been revoked.

        Args:
            public_key (str): base-64 encoded public key.
            url (str): Key revocation URL.

        Raises:
            AuthError: if they key has been revoked.
            SynapseError: if a transient error meant a key couldn't be checked
                for revocation.
        """
        try:
            response = yield self.hs.get_simple_http_client().get_json(
                url,
                {"public_key": public_key}
            )
        except Exception:
            raise SynapseError(
                502,
                "Third party certificate could not be checked"
            )
        if "valid" not in response or not response["valid"]:
            raise AuthError(403, "Third party certificate was invalid")
