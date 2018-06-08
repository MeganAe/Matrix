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


from twisted.internet import defer

from synapse import event_auth
from synapse.util.logutils import log_function
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.metrics import Measure
from synapse.api.constants import EventTypes
from synapse.api.errors import AuthError
from synapse.events.snapshot import EventContext
from synapse.util.async import Linearizer
from synapse.util.caches import CACHE_SIZE_FACTOR

from collections import namedtuple
from frozendict import frozendict

import logging
import hashlib

from six import iteritems, itervalues

logger = logging.getLogger(__name__)


KeyStateTuple = namedtuple("KeyStateTuple", ("context", "type", "state_key"))


SIZE_OF_CACHE = int(100000 * CACHE_SIZE_FACTOR)
EVICTION_TIMEOUT_SECONDS = 60 * 60


_NEXT_STATE_ID = 1

POWER_KEY = (EventTypes.PowerLevels, "")


def _gen_state_id():
    global _NEXT_STATE_ID
    s = "X%d" % (_NEXT_STATE_ID,)
    _NEXT_STATE_ID += 1
    return s


class _StateCacheEntry(object):
    __slots__ = ["state", "state_group", "state_id", "prev_group", "delta_ids"]

    def __init__(self, state, state_group, prev_group=None, delta_ids=None):
        # dict[(str, str), str] map  from (type, state_key) to event_id
        self.state = frozendict(state)

        # the ID of a state group if one and only one is involved.
        # otherwise, None otherwise?
        self.state_group = state_group

        self.prev_group = prev_group
        self.delta_ids = frozendict(delta_ids) if delta_ids is not None else None

        # The `state_id` is a unique ID we generate that can be used as ID for
        # this collection of state. Usually this would be the same as the
        # state group, but on worker instances we can't generate a new state
        # group each time we resolve state, so we generate a separate one that
        # isn't persisted and is used solely for caches.
        # `state_id` is either a state_group (and so an int) or a string. This
        # ensures we don't accidentally persist a state_id as a stateg_group
        if state_group:
            self.state_id = state_group
        else:
            self.state_id = _gen_state_id()

    def __len__(self):
        return len(self.state)


class StateHandler(object):
    """Fetches bits of state from the stores, and does state resolution
    where necessary
    """

    def __init__(self, hs):
        self.clock = hs.get_clock()
        self.store = hs.get_datastore()
        self.hs = hs
        self._state_resolution_handler = hs.get_state_resolution_handler()

    def start_caching(self):
        # TODO: remove this shim
        self._state_resolution_handler.start_caching()

    @defer.inlineCallbacks
    def get_current_state(self, room_id, event_type=None, state_key="",
                          latest_event_ids=None):
        """ Retrieves the current state for the room. This is done by
        calling `get_latest_events_in_room` to get the leading edges of the
        event graph and then resolving any of the state conflicts.

        This is equivalent to getting the state of an event that were to send
        next before receiving any new events.

        If `event_type` is specified, then the method returns only the one
        event (or None) with that `event_type` and `state_key`.

        Returns:
            map from (type, state_key) to event
        """
        if not latest_event_ids:
            latest_event_ids = yield self.store.get_latest_event_ids_in_room(room_id)

        logger.debug("calling resolve_state_groups from get_current_state")
        ret = yield self.resolve_state_groups_for_events(room_id, latest_event_ids)
        state = ret.state

        if event_type:
            event_id = state.get((event_type, state_key))
            event = None
            if event_id:
                event = yield self.store.get_event(event_id, allow_none=True)
            defer.returnValue(event)
            return

        state_map = yield self.store.get_events(list(state.values()),
                                                get_prev_content=False)
        state = {
            key: state_map[e_id] for key, e_id in iteritems(state) if e_id in state_map
        }

        defer.returnValue(state)

    @defer.inlineCallbacks
    def get_current_state_ids(self, room_id, latest_event_ids=None):
        """Get the current state, or the state at a set of events, for a room

        Args:
            room_id (str):

            latest_event_ids (iterable[str]|None): if given, the forward
                extremities to resolve. If None, we look them up from the
                database (via a cache)

        Returns:
            Deferred[dict[(str, str), str)]]: the state dict, mapping from
                (event_type, state_key) -> event_id
        """
        if not latest_event_ids:
            latest_event_ids = yield self.store.get_latest_event_ids_in_room(room_id)

        logger.debug("calling resolve_state_groups from get_current_state_ids")
        ret = yield self.resolve_state_groups_for_events(room_id, latest_event_ids)
        state = ret.state

        defer.returnValue(state)

    @defer.inlineCallbacks
    def get_current_user_in_room(self, room_id, latest_event_ids=None):
        if not latest_event_ids:
            latest_event_ids = yield self.store.get_latest_event_ids_in_room(room_id)
        logger.debug("calling resolve_state_groups from get_current_user_in_room")
        entry = yield self.resolve_state_groups_for_events(room_id, latest_event_ids)
        joined_users = yield self.store.get_joined_users_from_state(room_id, entry)
        defer.returnValue(joined_users)

    @defer.inlineCallbacks
    def get_current_hosts_in_room(self, room_id, latest_event_ids=None):
        if not latest_event_ids:
            latest_event_ids = yield self.store.get_latest_event_ids_in_room(room_id)
        logger.debug("calling resolve_state_groups from get_current_hosts_in_room")
        entry = yield self.resolve_state_groups_for_events(room_id, latest_event_ids)
        joined_hosts = yield self.store.get_joined_hosts(room_id, entry)
        defer.returnValue(joined_hosts)

    @defer.inlineCallbacks
    def compute_event_context(self, event, old_state=None):
        """Build an EventContext structure for the event.

        This works out what the current state should be for the event, and
        generates a new state group if necessary.

        Args:
            event (synapse.events.EventBase):
            old_state (dict|None): The state at the event if it can't be
                calculated from existing events. This is normally only specified
                when receiving an event from federation where we don't have the
                prev events for, e.g. when backfilling.
        Returns:
            synapse.events.snapshot.EventContext:
        """

        if event.internal_metadata.is_outlier():
            # If this is an outlier, then we know it shouldn't have any current
            # state. Certainly store.get_current_state won't return any, and
            # persisting the event won't store the state group.
            context = EventContext()
            if old_state:
                context.prev_state_ids = {
                    (s.type, s.state_key): s.event_id for s in old_state
                }
                if event.is_state():
                    context.current_state_ids = dict(context.prev_state_ids)
                    key = (event.type, event.state_key)
                    context.current_state_ids[key] = event.event_id
                else:
                    context.current_state_ids = context.prev_state_ids
            else:
                context.current_state_ids = {}
                context.prev_state_ids = {}
            context.prev_state_events = []

            # We don't store state for outliers, so we don't generate a state
            # froup for it.
            context.state_group = None

            defer.returnValue(context)

        if old_state:
            # We already have the state, so we don't need to calculate it.
            # Let's just correctly fill out the context and create a
            # new state group for it.

            context = EventContext()
            context.prev_state_ids = {
                (s.type, s.state_key): s.event_id for s in old_state
            }

            if event.is_state():
                key = (event.type, event.state_key)
                if key in context.prev_state_ids:
                    replaces = context.prev_state_ids[key]
                    if replaces != event.event_id:  # Paranoia check
                        event.unsigned["replaces_state"] = replaces
                context.current_state_ids = dict(context.prev_state_ids)
                context.current_state_ids[key] = event.event_id
            else:
                context.current_state_ids = context.prev_state_ids

            context.state_group = yield self.store.store_state_group(
                event.event_id,
                event.room_id,
                prev_group=None,
                delta_ids=None,
                current_state_ids=context.current_state_ids,
            )

            context.prev_state_events = []
            defer.returnValue(context)

        logger.debug("calling resolve_state_groups from compute_event_context")
        entry = yield self.resolve_state_groups_for_events(
            event.room_id, [e for e, _ in event.prev_events],
        )

        curr_state = entry.state

        context = EventContext()
        context.prev_state_ids = curr_state
        if event.is_state():
            # If this is a state event then we need to create a new state
            # group for the state after this event.

            key = (event.type, event.state_key)
            if key in context.prev_state_ids:
                replaces = context.prev_state_ids[key]
                event.unsigned["replaces_state"] = replaces

            context.current_state_ids = dict(context.prev_state_ids)
            context.current_state_ids[key] = event.event_id

            if entry.state_group:
                # If the state at the event has a state group assigned then
                # we can use that as the prev group
                context.prev_group = entry.state_group
                context.delta_ids = {
                    key: event.event_id
                }
            elif entry.prev_group:
                # If the state at the event only has a prev group, then we can
                # use that as a prev group too.
                context.prev_group = entry.prev_group
                context.delta_ids = dict(entry.delta_ids)
                context.delta_ids[key] = event.event_id

            context.state_group = yield self.store.store_state_group(
                event.event_id,
                event.room_id,
                prev_group=context.prev_group,
                delta_ids=context.delta_ids,
                current_state_ids=context.current_state_ids,
            )
        else:
            context.current_state_ids = context.prev_state_ids
            context.prev_group = entry.prev_group
            context.delta_ids = entry.delta_ids

            if entry.state_group is None:
                entry.state_group = yield self.store.store_state_group(
                    event.event_id,
                    event.room_id,
                    prev_group=entry.prev_group,
                    delta_ids=entry.delta_ids,
                    current_state_ids=context.current_state_ids,
                )
                entry.state_id = entry.state_group

            context.state_group = entry.state_group

        context.prev_state_events = []
        defer.returnValue(context)

    @defer.inlineCallbacks
    def resolve_state_groups_for_events(self, room_id, event_ids):
        """ Given a list of event_ids this method fetches the state at each
        event, resolves conflicts between them and returns them.

        Args:
            room_id (str):
            event_ids (list[str]):

        Returns:
            Deferred[_StateCacheEntry]: resolved state
        """
        logger.debug("resolve_state_groups event_ids %s", event_ids)

        # map from state group id to the state in that state group (where
        # 'state' is a map from state key to event id)
        # dict[int, dict[(str, str), str]]
        state_groups_ids = yield self.store.get_state_groups_ids(
            room_id, event_ids
        )

        if len(state_groups_ids) == 1:
            name, state_list = list(state_groups_ids.items()).pop()

            prev_group, delta_ids = yield self.store.get_state_group_delta(name)

            defer.returnValue(_StateCacheEntry(
                state=state_list,
                state_group=name,
                prev_group=prev_group,
                delta_ids=delta_ids,
            ))

        result = yield self._state_resolution_handler.resolve_state_groups(
            room_id, state_groups_ids, None, self._state_map_factory,
        )
        defer.returnValue(result)

    def _state_map_factory(self, ev_ids):
        return self.store.get_events(
            ev_ids, get_prev_content=False, check_redacted=False,
        )

    def resolve_events(self, state_sets, event):
        logger.info(
            "Resolving state for %s with %d groups", event.room_id, len(state_sets)
        )
        state_set_ids = [{
            (ev.type, ev.state_key): ev.event_id
            for ev in st
        } for st in state_sets]

        state_map = {
            ev.event_id: ev
            for st in state_sets
            for ev in st
        }

        with Measure(self.clock, "state._resolve_events"):
            new_state = resolve_events_with_state_map(state_set_ids, state_map)

        new_state = {
            key: state_map[ev_id] for key, ev_id in iteritems(new_state)
        }

        return new_state


class StateResolutionHandler(object):
    """Responsible for doing state conflict resolution.

    Note that the storage layer depends on this handler, so all functions must
    be storage-independent.
    """
    def __init__(self, hs):
        self.clock = hs.get_clock()

        # dict of set of event_ids -> _StateCacheEntry.
        self._state_cache = None
        self.resolve_linearizer = Linearizer(name="state_resolve_lock")

    def start_caching(self):
        logger.debug("start_caching")

        self._state_cache = ExpiringCache(
            cache_name="state_cache",
            clock=self.clock,
            max_len=SIZE_OF_CACHE,
            expiry_ms=EVICTION_TIMEOUT_SECONDS * 1000,
            iterable=True,
            reset_expiry_on_get=True,
        )

        self._state_cache.start()

    @defer.inlineCallbacks
    @log_function
    def resolve_state_groups(
        self, room_id, state_groups_ids, event_map, state_map_factory,
    ):
        """Resolves conflicts between a set of state groups

        Always generates a new state group (unless we hit the cache), so should
        not be called for a single state group

        Args:
            room_id (str): room we are resolving for (used for logging)
            state_groups_ids (dict[int, dict[(str, str), str]]):
                 map from state group id to the state in that state group
                (where 'state' is a map from state key to event id)

            event_map(dict[str,FrozenEvent]|None):
                a dict from event_id to event, for any events that we happen to
                have in flight (eg, those currently being persisted). This will be
                used as a starting point fof finding the state we need; any missing
                events will be requested via state_map_factory.

                If None, all events will be fetched via state_map_factory.

        Returns:
            Deferred[_StateCacheEntry]: resolved state
        """
        logger.debug(
            "resolve_state_groups state_groups %s",
            state_groups_ids.keys()
        )

        group_names = frozenset(state_groups_ids.keys())

        with (yield self.resolve_linearizer.queue(group_names)):
            if self._state_cache is not None:
                cache = self._state_cache.get(group_names, None)
                if cache:
                    defer.returnValue(cache)

            logger.info(
                "Resolving state for %s with %d groups", room_id, len(state_groups_ids)
            )

            # build a map from state key to the event_ids which set that state.
            # dict[(str, str), set[str])
            state = {}
            for st in itervalues(state_groups_ids):
                for key, e_id in iteritems(st):
                    state.setdefault(key, set()).add(e_id)

            # build a map from state key to the event_ids which set that state,
            # including only those where there are state keys in conflict.
            conflicted_state = {
                k: list(v)
                for k, v in iteritems(state)
                if len(v) > 1
            }

            if conflicted_state:
                logger.info("Resolving conflicted state for %r", room_id)
                with Measure(self.clock, "state._resolve_events"):
                    new_state = yield resolve_events_with_factory(
                        list(state_groups_ids.values()),
                        event_map=event_map,
                        state_map_factory=state_map_factory,
                    )
            else:
                new_state = {
                    key: e_ids.pop() for key, e_ids in iteritems(state)
                }

            with Measure(self.clock, "state.create_group_ids"):
                # if the new state matches any of the input state groups, we can
                # use that state group again. Otherwise we will generate a state_id
                # which will be used as a cache key for future resolutions, but
                # not get persisted.
                state_group = None
                new_state_event_ids = frozenset(itervalues(new_state))
                for sg, events in iteritems(state_groups_ids):
                    if new_state_event_ids == frozenset(e_id for e_id in events):
                        state_group = sg
                        break

                # TODO: We want to create a state group for this set of events, to
                # increase cache hits, but we need to make sure that it doesn't
                # end up as a prev_group without being added to the database

                prev_group = None
                delta_ids = None
                for old_group, old_ids in iteritems(state_groups_ids):
                    if not set(new_state) - set(old_ids):
                        n_delta_ids = {
                            k: v
                            for k, v in iteritems(new_state)
                            if old_ids.get(k) != v
                        }
                        if not delta_ids or len(n_delta_ids) < len(delta_ids):
                            prev_group = old_group
                            delta_ids = n_delta_ids

            cache = _StateCacheEntry(
                state=new_state,
                state_group=state_group,
                prev_group=prev_group,
                delta_ids=delta_ids,
            )

            if self._state_cache is not None:
                self._state_cache[group_names] = cache

            defer.returnValue(cache)


def _ordered_events(events):
    def key_func(e):
        return -int(e.depth), hashlib.sha1(e.event_id.encode('ascii')).hexdigest()

    return sorted(events, key=key_func)


def resolve_events_with_state_map(state_sets, state_map):
    """
    Args:
        state_sets(list): List of dicts of (type, state_key) -> event_id,
            which are the different state groups to resolve.
        state_map(dict): a dict from event_id to event, for all events in
            state_sets.

    Returns
        dict[(str, str), str]:
            a map from (type, state_key) to event_id.
    """
    if len(state_sets) == 1:
        return state_sets[0]

    unconflicted_state, conflicted_state = _seperate(
        state_sets,
    )

    auth_events = _create_auth_events_from_maps(
        unconflicted_state, conflicted_state, state_map
    )

    return _resolve_with_state(
        unconflicted_state, conflicted_state, auth_events, state_map
    )


def _seperate(state_sets):
    """Takes the state_sets and figures out which keys are conflicted and
    which aren't. i.e., which have multiple different event_ids associated
    with them in different state sets.

    Args:
        state_sets(list[dict[(str, str), str]]):
            List of dicts of (type, state_key) -> event_id, which are the
            different state groups to resolve.

    Returns:
        (dict[(str, str), str], dict[(str, str), set[str]]):
            A tuple of (unconflicted_state, conflicted_state), where:

            unconflicted_state is a dict mapping (type, state_key)->event_id
            for unconflicted state keys.

            conflicted_state is a dict mapping (type, state_key) to a set of
            event ids for conflicted state keys.
    """
    unconflicted_state = dict(state_sets[0])
    conflicted_state = {}

    for state_set in state_sets[1:]:
        for key, value in iteritems(state_set):
            # Check if there is an unconflicted entry for the state key.
            unconflicted_value = unconflicted_state.get(key)
            if unconflicted_value is None:
                # There isn't an unconflicted entry so check if there is a
                # conflicted entry.
                ls = conflicted_state.get(key)
                if ls is None:
                    # There wasn't a conflicted entry so haven't seen this key before.
                    # Therefore it isn't conflicted yet.
                    unconflicted_state[key] = value
                else:
                    # This key is already conflicted, add our value to the conflict set.
                    ls.add(value)
            elif unconflicted_value != value:
                # If the unconflicted value is not the same as our value then we
                # have a new conflict. So move the key from the unconflicted_state
                # to the conflicted state.
                conflicted_state[key] = {value, unconflicted_value}
                unconflicted_state.pop(key, None)

    return unconflicted_state, conflicted_state


@defer.inlineCallbacks
def resolve_events_with_factory(state_sets, event_map, state_map_factory):
    """
    Args:
        state_sets(list): List of dicts of (type, state_key) -> event_id,
            which are the different state groups to resolve.

        event_map(dict[str,FrozenEvent]|None):
            a dict from event_id to event, for any events that we happen to
            have in flight (eg, those currently being persisted). This will be
            used as a starting point fof finding the state we need; any missing
            events will be requested via state_map_factory.

            If None, all events will be fetched via state_map_factory.

        state_map_factory(func): will be called
            with a list of event_ids that are needed, and should return with
            a Deferred of dict of event_id to event.

    Returns
        Deferred[dict[(str, str), str]]:
            a map from (type, state_key) to event_id.
    """
    if len(state_sets) == 1:
        defer.returnValue(state_sets[0])

    unconflicted_state, conflicted_state = _seperate(
        state_sets,
    )

    needed_events = set(
        event_id
        for event_ids in itervalues(conflicted_state)
        for event_id in event_ids
    )
    if event_map is not None:
        needed_events -= set(event_map.iterkeys())

    logger.info("Asking for %d conflicted events", len(needed_events))

    # dict[str, FrozenEvent]: a map from state event id to event. Only includes
    # the state events which are in conflict (and those in event_map)
    state_map = yield state_map_factory(needed_events)
    if event_map is not None:
        state_map.update(event_map)

    # get the ids of the auth events which allow us to authenticate the
    # conflicted state, picking only from the unconflicting state.
    #
    # dict[(str, str), str]: a map from state key to event id
    auth_events = _create_auth_events_from_maps(
        unconflicted_state, conflicted_state, state_map
    )

    new_needed_events = set(itervalues(auth_events))
    new_needed_events -= needed_events
    if event_map is not None:
        new_needed_events -= set(event_map.iterkeys())

    logger.info("Asking for %d auth events", len(new_needed_events))

    state_map_new = yield state_map_factory(new_needed_events)
    state_map.update(state_map_new)

    defer.returnValue(_resolve_with_state(
        unconflicted_state, conflicted_state, auth_events, state_map
    ))


def _create_auth_events_from_maps(unconflicted_state, conflicted_state, state_map):
    auth_events = {}
    for event_ids in itervalues(conflicted_state):
        for event_id in event_ids:
            if event_id in state_map:
                keys = event_auth.auth_types_for_event(state_map[event_id])
                for key in keys:
                    if key not in auth_events:
                        event_id = unconflicted_state.get(key, None)
                        if event_id:
                            auth_events[key] = event_id
    return auth_events


def _resolve_with_state(unconflicted_state_ids, conflicted_state_ds, auth_event_ids,
                        state_map):
    conflicted_state = {}
    for key, event_ids in iteritems(conflicted_state_ds):
        events = [state_map[ev_id] for ev_id in event_ids if ev_id in state_map]
        if len(events) > 1:
            conflicted_state[key] = events
        elif len(events) == 1:
            unconflicted_state_ids[key] = events[0].event_id

    auth_events = {
        key: state_map[ev_id]
        for key, ev_id in iteritems(auth_event_ids)
        if ev_id in state_map
    }

    try:
        resolved_state = _resolve_state_events(
            conflicted_state, auth_events
        )
    except Exception:
        logger.exception("Failed to resolve state")
        raise

    new_state = unconflicted_state_ids
    for key, event in iteritems(resolved_state):
        new_state[key] = event.event_id

    return new_state


def _resolve_state_events(conflicted_state, auth_events):
    """ This is where we actually decide which of the conflicted state to
    use.

    We resolve conflicts in the following order:
        1. power levels
        2. join rules
        3. memberships
        4. other events.
    """
    resolved_state = {}
    if POWER_KEY in conflicted_state:
        events = conflicted_state[POWER_KEY]
        logger.debug("Resolving conflicted power levels %r", events)
        resolved_state[POWER_KEY] = _resolve_auth_events(
            events, auth_events)

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key[0] == EventTypes.JoinRules:
            logger.debug("Resolving conflicted join rules %r", events)
            resolved_state[key] = _resolve_auth_events(
                events,
                auth_events
            )

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key[0] == EventTypes.Member:
            logger.debug("Resolving conflicted member lists %r", events)
            resolved_state[key] = _resolve_auth_events(
                events,
                auth_events
            )

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key not in resolved_state:
            logger.debug("Resolving conflicted state %r:%r", key, events)
            resolved_state[key] = _resolve_normal_events(
                events, auth_events
            )

    return resolved_state


def _resolve_auth_events(events, auth_events):
    reverse = [i for i in reversed(_ordered_events(events))]

    auth_keys = set(
        key
        for event in events
        for key in event_auth.auth_types_for_event(event)
    )

    new_auth_events = {}
    for key in auth_keys:
        auth_event = auth_events.get(key, None)
        if auth_event:
            new_auth_events[key] = auth_event

    auth_events = new_auth_events

    prev_event = reverse[0]
    for event in reverse[1:]:
        auth_events[(prev_event.type, prev_event.state_key)] = prev_event
        try:
            # The signatures have already been checked at this point
            event_auth.check(event, auth_events, do_sig_check=False, do_size_check=False)
            prev_event = event
        except AuthError:
            return prev_event

    return event


def _resolve_normal_events(events, auth_events):
    for event in _ordered_events(events):
        try:
            # The signatures have already been checked at this point
            event_auth.check(event, auth_events, do_sig_check=False, do_size_check=False)
            return event
        except AuthError:
            pass

    # Use the last event (the one with the least depth) if they all fail
    # the auth check.
    return event
