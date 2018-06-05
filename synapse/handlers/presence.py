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

"""This module is responsible for keeping track of presence status of local
and remote users.

The methods that define policy are:
    - PresenceHandler._update_states
    - PresenceHandler._handle_timeouts
    - should_notify
"""

from twisted.internet import defer, reactor
from contextlib import contextmanager

from six import itervalues, iteritems

from synapse.api.errors import SynapseError
from synapse.api.constants import PresenceState
from synapse.storage.presence import UserPresenceState

from synapse.util.caches.descriptors import cachedInlineCallbacks
from synapse.util.async import Linearizer
from synapse.util.logcontext import run_in_background
from synapse.util.logutils import log_function
from synapse.util.metrics import Measure
from synapse.util.wheel_timer import WheelTimer
from synapse.types import UserID, get_domain_from_id
from synapse.metrics import LaterGauge

import logging

from prometheus_client import Counter

logger = logging.getLogger(__name__)


notified_presence_counter = Counter("synapse_handler_presence_notified_presence", "")
federation_presence_out_counter = Counter(
    "synapse_handler_presence_federation_presence_out", "")
presence_updates_counter = Counter("synapse_handler_presence_presence_updates", "")
timers_fired_counter = Counter("synapse_handler_presence_timers_fired", "")
federation_presence_counter = Counter("synapse_handler_presence_federation_presence", "")
bump_active_time_counter = Counter("synapse_handler_presence_bump_active_time", "")

get_updates_counter = Counter("synapse_handler_presence_get_updates", "", ["type"])

notify_reason_counter = Counter(
    "synapse_handler_presence_notify_reason", "", ["reason"])
state_transition_counter = Counter(
    "synapse_handler_presence_state_transition", "", ["from", "to"]
)


# If a user was last active in the last LAST_ACTIVE_GRANULARITY, consider them
# "currently_active"
LAST_ACTIVE_GRANULARITY = 60 * 1000

# How long to wait until a new /events or /sync request before assuming
# the client has gone.
SYNC_ONLINE_TIMEOUT = 30 * 1000

# How long to wait before marking the user as idle. Compared against last active
IDLE_TIMER = 5 * 60 * 1000

# How often we expect remote servers to resend us presence.
FEDERATION_TIMEOUT = 30 * 60 * 1000

# How often to resend presence to remote servers
FEDERATION_PING_INTERVAL = 25 * 60 * 1000

# How long we will wait before assuming that the syncs from an external process
# are dead.
EXTERNAL_PROCESS_EXPIRY = 5 * 60 * 1000

assert LAST_ACTIVE_GRANULARITY < IDLE_TIMER


class PresenceHandler(object):

    def __init__(self, hs):
        """

        Args:
            hs (synapse.server.HomeServer):
        """
        self.is_mine = hs.is_mine
        self.is_mine_id = hs.is_mine_id
        self.clock = hs.get_clock()
        self.store = hs.get_datastore()
        self.wheel_timer = WheelTimer()
        self.notifier = hs.get_notifier()
        self.federation = hs.get_federation_sender()
        self.state = hs.get_state_handler()

        federation_registry = hs.get_federation_registry()

        federation_registry.register_edu_handler(
            "m.presence", self.incoming_presence
        )
        federation_registry.register_edu_handler(
            "m.presence_invite",
            lambda origin, content: self.invite_presence(
                observed_user=UserID.from_string(content["observed_user"]),
                observer_user=UserID.from_string(content["observer_user"]),
            )
        )
        federation_registry.register_edu_handler(
            "m.presence_accept",
            lambda origin, content: self.accept_presence(
                observed_user=UserID.from_string(content["observed_user"]),
                observer_user=UserID.from_string(content["observer_user"]),
            )
        )
        federation_registry.register_edu_handler(
            "m.presence_deny",
            lambda origin, content: self.deny_presence(
                observed_user=UserID.from_string(content["observed_user"]),
                observer_user=UserID.from_string(content["observer_user"]),
            )
        )

        distributor = hs.get_distributor()
        distributor.observe("user_joined_room", self.user_joined_room)

        active_presence = self.store.take_presence_startup_info()

        # A dictionary of the current state of users. This is prefilled with
        # non-offline presence from the DB. We should fetch from the DB if
        # we can't find a users presence in here.
        self.user_to_current_state = {
            state.user_id: state
            for state in active_presence
        }

        LaterGauge(
            "synapse_handlers_presence_user_to_current_state_size", "", [],
            lambda: len(self.user_to_current_state)
        )

        now = self.clock.time_msec()
        for state in active_presence:
            self.wheel_timer.insert(
                now=now,
                obj=state.user_id,
                then=state.last_active_ts + IDLE_TIMER,
            )
            self.wheel_timer.insert(
                now=now,
                obj=state.user_id,
                then=state.last_user_sync_ts + SYNC_ONLINE_TIMEOUT,
            )
            if self.is_mine_id(state.user_id):
                self.wheel_timer.insert(
                    now=now,
                    obj=state.user_id,
                    then=state.last_federation_update_ts + FEDERATION_PING_INTERVAL,
                )
            else:
                self.wheel_timer.insert(
                    now=now,
                    obj=state.user_id,
                    then=state.last_federation_update_ts + FEDERATION_TIMEOUT,
                )

        # Set of users who have presence in the `user_to_current_state` that
        # have not yet been persisted
        self.unpersisted_users_changes = set()

        reactor.addSystemEventTrigger("before", "shutdown", self._on_shutdown)

        self.serial_to_user = {}
        self._next_serial = 1

        # Keeps track of the number of *ongoing* syncs on this process. While
        # this is non zero a user will never go offline.
        self.user_to_num_current_syncs = {}

        # Keeps track of the number of *ongoing* syncs on other processes.
        # While any sync is ongoing on another process the user will never
        # go offline.
        # Each process has a unique identifier and an update frequency. If
        # no update is received from that process within the update period then
        # we assume that all the sync requests on that process have stopped.
        # Stored as a dict from process_id to set of user_id, and a dict of
        # process_id to millisecond timestamp last updated.
        self.external_process_to_current_syncs = {}
        self.external_process_last_updated_ms = {}
        self.external_sync_linearizer = Linearizer(name="external_sync_linearizer")

        # Start a LoopingCall in 30s that fires every 5s.
        # The initial delay is to allow disconnected clients a chance to
        # reconnect before we treat them as offline.
        self.clock.call_later(
            30,
            self.clock.looping_call,
            self._handle_timeouts,
            5000,
        )

        self.clock.call_later(
            60,
            self.clock.looping_call,
            self._persist_unpersisted_changes,
            60 * 1000,
        )

        LaterGauge("synapse_handlers_presence_wheel_timer_size", "", [],
                   lambda: len(self.wheel_timer))

    @defer.inlineCallbacks
    def _on_shutdown(self):
        """Gets called when shutting down. This lets us persist any updates that
        we haven't yet persisted, e.g. updates that only changes some internal
        timers. This allows changes to persist across startup without having to
        persist every single change.

        If this does not run it simply means that some of the timers will fire
        earlier than they should when synapse is restarted. This affect of this
        is some spurious presence changes that will self-correct.
        """
        logger.info(
            "Performing _on_shutdown. Persisting %d unpersisted changes",
            len(self.user_to_current_state)
        )

        if self.unpersisted_users_changes:
            yield self.store.update_presence([
                self.user_to_current_state[user_id]
                for user_id in self.unpersisted_users_changes
            ])
        logger.info("Finished _on_shutdown")

    @defer.inlineCallbacks
    def _persist_unpersisted_changes(self):
        """We periodically persist the unpersisted changes, as otherwise they
        may stack up and slow down shutdown times.
        """
        logger.info(
            "Performing _persist_unpersisted_changes. Persisting %d unpersisted changes",
            len(self.unpersisted_users_changes)
        )

        unpersisted = self.unpersisted_users_changes
        self.unpersisted_users_changes = set()

        if unpersisted:
            yield self.store.update_presence([
                self.user_to_current_state[user_id]
                for user_id in unpersisted
            ])

        logger.info("Finished _persist_unpersisted_changes")

    @defer.inlineCallbacks
    def _update_states_and_catch_exception(self, new_states):
        try:
            res = yield self._update_states(new_states)
            defer.returnValue(res)
        except Exception:
            logger.exception("Error updating presence")

    @defer.inlineCallbacks
    def _update_states(self, new_states):
        """Updates presence of users. Sets the appropriate timeouts. Pokes
        the notifier and federation if and only if the changed presence state
        should be sent to clients/servers.
        """
        now = self.clock.time_msec()

        with Measure(self.clock, "presence_update_states"):

            # NOTE: We purposefully don't yield between now and when we've
            # calculated what we want to do with the new states, to avoid races.

            to_notify = {}  # Changes we want to notify everyone about
            to_federation_ping = {}  # These need sending keep-alives

            # Only bother handling the last presence change for each user
            new_states_dict = {}
            for new_state in new_states:
                new_states_dict[new_state.user_id] = new_state
            new_state = new_states_dict.values()

            for new_state in new_states:
                user_id = new_state.user_id

                # Its fine to not hit the database here, as the only thing not in
                # the current state cache are OFFLINE states, where the only field
                # of interest is last_active which is safe enough to assume is 0
                # here.
                prev_state = self.user_to_current_state.get(
                    user_id, UserPresenceState.default(user_id)
                )

                new_state, should_notify, should_ping = handle_update(
                    prev_state, new_state,
                    is_mine=self.is_mine_id(user_id),
                    wheel_timer=self.wheel_timer,
                    now=now
                )

                self.user_to_current_state[user_id] = new_state

                if should_notify:
                    to_notify[user_id] = new_state
                elif should_ping:
                    to_federation_ping[user_id] = new_state

            # TODO: We should probably ensure there are no races hereafter

            presence_updates_counter.inc(len(new_states))

            if to_notify:
                notified_presence_counter.inc(len(to_notify))
                yield self._persist_and_notify(list(to_notify.values()))

            self.unpersisted_users_changes |= set(s.user_id for s in new_states)
            self.unpersisted_users_changes -= set(to_notify.keys())

            to_federation_ping = {
                user_id: state for user_id, state in to_federation_ping.items()
                if user_id not in to_notify
            }
            if to_federation_ping:
                federation_presence_out_counter.inc(len(to_federation_ping))

                self._push_to_remotes(to_federation_ping.values())

    def _handle_timeouts(self):
        """Checks the presence of users that have timed out and updates as
        appropriate.
        """
        logger.info("Handling presence timeouts")
        now = self.clock.time_msec()

        try:
            with Measure(self.clock, "presence_handle_timeouts"):
                # Fetch the list of users that *may* have timed out. Things may have
                # changed since the timeout was set, so we won't necessarily have to
                # take any action.
                users_to_check = set(self.wheel_timer.fetch(now))

                # Check whether the lists of syncing processes from an external
                # process have expired.
                expired_process_ids = [
                    process_id for process_id, last_update
                    in self.external_process_last_updated_ms.items()
                    if now - last_update > EXTERNAL_PROCESS_EXPIRY
                ]
                for process_id in expired_process_ids:
                    users_to_check.update(
                        self.external_process_last_updated_ms.pop(process_id, ())
                    )
                    self.external_process_last_update.pop(process_id)

                states = [
                    self.user_to_current_state.get(
                        user_id, UserPresenceState.default(user_id)
                    )
                    for user_id in users_to_check
                ]

                timers_fired_counter.inc(len(states))

                changes = handle_timeouts(
                    states,
                    is_mine_fn=self.is_mine_id,
                    syncing_user_ids=self.get_currently_syncing_users(),
                    now=now,
                )

            run_in_background(self._update_states_and_catch_exception, changes)
        except Exception:
            logger.exception("Exception in _handle_timeouts loop")

    @defer.inlineCallbacks
    def bump_presence_active_time(self, user):
        """We've seen the user do something that indicates they're interacting
        with the app.
        """
        user_id = user.to_string()

        bump_active_time_counter.inc()

        prev_state = yield self.current_state_for_user(user_id)

        new_fields = {
            "last_active_ts": self.clock.time_msec(),
        }
        if prev_state.state == PresenceState.UNAVAILABLE:
            new_fields["state"] = PresenceState.ONLINE

        yield self._update_states([prev_state.copy_and_replace(**new_fields)])

    @defer.inlineCallbacks
    def user_syncing(self, user_id, affect_presence=True):
        """Returns a context manager that should surround any stream requests
        from the user.

        This allows us to keep track of who is currently streaming and who isn't
        without having to have timers outside of this module to avoid flickering
        when users disconnect/reconnect.

        Args:
            user_id (str)
            affect_presence (bool): If false this function will be a no-op.
                Useful for streams that are not associated with an actual
                client that is being used by a user.
        """
        if affect_presence:
            curr_sync = self.user_to_num_current_syncs.get(user_id, 0)
            self.user_to_num_current_syncs[user_id] = curr_sync + 1

            prev_state = yield self.current_state_for_user(user_id)
            if prev_state.state == PresenceState.OFFLINE:
                # If they're currently offline then bring them online, otherwise
                # just update the last sync times.
                yield self._update_states([prev_state.copy_and_replace(
                    state=PresenceState.ONLINE,
                    last_active_ts=self.clock.time_msec(),
                    last_user_sync_ts=self.clock.time_msec(),
                )])
            else:
                yield self._update_states([prev_state.copy_and_replace(
                    last_user_sync_ts=self.clock.time_msec(),
                )])

        @defer.inlineCallbacks
        def _end():
            try:
                self.user_to_num_current_syncs[user_id] -= 1

                prev_state = yield self.current_state_for_user(user_id)
                yield self._update_states([prev_state.copy_and_replace(
                    last_user_sync_ts=self.clock.time_msec(),
                )])
            except Exception:
                logger.exception("Error updating presence after sync")

        @contextmanager
        def _user_syncing():
            try:
                yield
            finally:
                if affect_presence:
                    run_in_background(_end)

        defer.returnValue(_user_syncing())

    def get_currently_syncing_users(self):
        """Get the set of user ids that are currently syncing on this HS.
        Returns:
            set(str): A set of user_id strings.
        """
        syncing_user_ids = {
            user_id for user_id, count in self.user_to_num_current_syncs.items()
            if count
        }
        for user_ids in self.external_process_to_current_syncs.values():
            syncing_user_ids.update(user_ids)
        return syncing_user_ids

    @defer.inlineCallbacks
    def update_external_syncs_row(self, process_id, user_id, is_syncing, sync_time_msec):
        """Update the syncing users for an external process as a delta.

        Args:
            process_id (str): An identifier for the process the users are
                syncing against. This allows synapse to process updates
                as user start and stop syncing against a given process.
            user_id (str): The user who has started or stopped syncing
            is_syncing (bool): Whether or not the user is now syncing
            sync_time_msec(int): Time in ms when the user was last syncing
        """
        with (yield self.external_sync_linearizer.queue(process_id)):
            prev_state = yield self.current_state_for_user(user_id)

            process_presence = self.external_process_to_current_syncs.setdefault(
                process_id, set()
            )

            updates = []
            if is_syncing and user_id not in process_presence:
                if prev_state.state == PresenceState.OFFLINE:
                    updates.append(prev_state.copy_and_replace(
                        state=PresenceState.ONLINE,
                        last_active_ts=sync_time_msec,
                        last_user_sync_ts=sync_time_msec,
                    ))
                else:
                    updates.append(prev_state.copy_and_replace(
                        last_user_sync_ts=sync_time_msec,
                    ))
                process_presence.add(user_id)
            elif user_id in process_presence:
                updates.append(prev_state.copy_and_replace(
                    last_user_sync_ts=sync_time_msec,
                ))

            if not is_syncing:
                process_presence.discard(user_id)

            if updates:
                yield self._update_states(updates)

            self.external_process_last_updated_ms[process_id] = self.clock.time_msec()

    @defer.inlineCallbacks
    def update_external_syncs_clear(self, process_id):
        """Marks all users that had been marked as syncing by a given process
        as offline.

        Used when the process has stopped/disappeared.
        """
        with (yield self.external_sync_linearizer.queue(process_id)):
            process_presence = self.external_process_to_current_syncs.pop(
                process_id, set()
            )
            prev_states = yield self.current_state_for_users(process_presence)
            time_now_ms = self.clock.time_msec()

            yield self._update_states([
                prev_state.copy_and_replace(
                    last_user_sync_ts=time_now_ms,
                )
                for prev_state in itervalues(prev_states)
            ])
            self.external_process_last_updated_ms.pop(process_id, None)

    @defer.inlineCallbacks
    def current_state_for_user(self, user_id):
        """Get the current presence state for a user.
        """
        res = yield self.current_state_for_users([user_id])
        defer.returnValue(res[user_id])

    @defer.inlineCallbacks
    def current_state_for_users(self, user_ids):
        """Get the current presence state for multiple users.

        Returns:
            dict: `user_id` -> `UserPresenceState`
        """
        states = {
            user_id: self.user_to_current_state.get(user_id, None)
            for user_id in user_ids
        }

        missing = [user_id for user_id, state in iteritems(states) if not state]
        if missing:
            # There are things not in our in memory cache. Lets pull them out of
            # the database.
            res = yield self.store.get_presence_for_users(missing)
            states.update(res)

            missing = [user_id for user_id, state in iteritems(states) if not state]
            if missing:
                new = {
                    user_id: UserPresenceState.default(user_id)
                    for user_id in missing
                }
                states.update(new)
                self.user_to_current_state.update(new)

        defer.returnValue(states)

    @defer.inlineCallbacks
    def _persist_and_notify(self, states):
        """Persist states in the database, poke the notifier and send to
        interested remote servers
        """
        stream_id, max_token = yield self.store.update_presence(states)

        parties = yield get_interested_parties(self.store, states)
        room_ids_to_states, users_to_states = parties

        self.notifier.on_new_event(
            "presence_key", stream_id, rooms=room_ids_to_states.keys(),
            users=[UserID.from_string(u) for u in users_to_states]
        )

        self._push_to_remotes(states)

    @defer.inlineCallbacks
    def notify_for_states(self, state, stream_id):
        parties = yield get_interested_parties(self.store, [state])
        room_ids_to_states, users_to_states = parties

        self.notifier.on_new_event(
            "presence_key", stream_id, rooms=room_ids_to_states.keys(),
            users=[UserID.from_string(u) for u in users_to_states]
        )

    def _push_to_remotes(self, states):
        """Sends state updates to remote servers.

        Args:
            states (list(UserPresenceState))
        """
        self.federation.send_presence(states)

    @defer.inlineCallbacks
    def incoming_presence(self, origin, content):
        """Called when we receive a `m.presence` EDU from a remote server.
        """
        now = self.clock.time_msec()
        updates = []
        for push in content.get("push", []):
            # A "push" contains a list of presence that we are probably interested
            # in.
            # TODO: Actually check if we're interested, rather than blindly
            # accepting presence updates.
            user_id = push.get("user_id", None)
            if not user_id:
                logger.info(
                    "Got presence update from %r with no 'user_id': %r",
                    origin, push,
                )
                continue

            if get_domain_from_id(user_id) != origin:
                logger.info(
                    "Got presence update from %r with bad 'user_id': %r",
                    origin, user_id,
                )
                continue

            presence_state = push.get("presence", None)
            if not presence_state:
                logger.info(
                    "Got presence update from %r with no 'presence_state': %r",
                    origin, push,
                )
                continue

            new_fields = {
                "state": presence_state,
                "last_federation_update_ts": now,
            }

            last_active_ago = push.get("last_active_ago", None)
            if last_active_ago is not None:
                new_fields["last_active_ts"] = now - last_active_ago

            new_fields["status_msg"] = push.get("status_msg", None)
            new_fields["currently_active"] = push.get("currently_active", False)

            prev_state = yield self.current_state_for_user(user_id)
            updates.append(prev_state.copy_and_replace(**new_fields))

        if updates:
            federation_presence_counter.inc(len(updates))
            yield self._update_states(updates)

    @defer.inlineCallbacks
    def get_state(self, target_user, as_event=False):
        results = yield self.get_states(
            [target_user.to_string()],
            as_event=as_event,
        )

        defer.returnValue(results[0])

    @defer.inlineCallbacks
    def get_states(self, target_user_ids, as_event=False):
        """Get the presence state for users.

        Args:
            target_user_ids (list)
            as_event (bool): Whether to format it as a client event or not.

        Returns:
            list
        """

        updates = yield self.current_state_for_users(target_user_ids)
        updates = list(updates.values())

        for user_id in set(target_user_ids) - set(u.user_id for u in updates):
            updates.append(UserPresenceState.default(user_id))

        now = self.clock.time_msec()
        if as_event:
            defer.returnValue([
                {
                    "type": "m.presence",
                    "content": format_user_presence_state(state, now),
                }
                for state in updates
            ])
        else:
            defer.returnValue(updates)

    @defer.inlineCallbacks
    def set_state(self, target_user, state, ignore_status_msg=False):
        """Set the presence state of the user.
        """
        status_msg = state.get("status_msg", None)
        presence = state["presence"]

        valid_presence = (
            PresenceState.ONLINE, PresenceState.UNAVAILABLE, PresenceState.OFFLINE
        )
        if presence not in valid_presence:
            raise SynapseError(400, "Invalid presence state")

        user_id = target_user.to_string()

        prev_state = yield self.current_state_for_user(user_id)

        new_fields = {
            "state": presence
        }

        if not ignore_status_msg:
            msg = status_msg if presence != PresenceState.OFFLINE else None
            new_fields["status_msg"] = msg

        if presence == PresenceState.ONLINE:
            new_fields["last_active_ts"] = self.clock.time_msec()

        yield self._update_states([prev_state.copy_and_replace(**new_fields)])

    @defer.inlineCallbacks
    def user_joined_room(self, user, room_id):
        """Called (via the distributor) when a user joins a room. This funciton
        sends presence updates to servers, either:
            1. the joining user is a local user and we send their presence to
               all servers in the room.
            2. the joining user is a remote user and so we send presence for all
               local users in the room.
        """
        # We only need to send presence to servers that don't have it yet. We
        # don't need to send to local clients here, as that is done as part
        # of the event stream/sync.
        # TODO: Only send to servers not already in the room.
        if self.is_mine(user):
            state = yield self.current_state_for_user(user.to_string())

            self._push_to_remotes([state])
        else:
            user_ids = yield self.store.get_users_in_room(room_id)
            user_ids = list(filter(self.is_mine_id, user_ids))

            states = yield self.current_state_for_users(user_ids)

            self._push_to_remotes(list(states.values()))

    @defer.inlineCallbacks
    def get_presence_list(self, observer_user, accepted=None):
        """Returns the presence for all users in their presence list.
        """
        if not self.is_mine(observer_user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        presence_list = yield self.store.get_presence_list(
            observer_user.localpart, accepted=accepted
        )

        results = yield self.get_states(
            target_user_ids=[row["observed_user_id"] for row in presence_list],
            as_event=False,
        )

        now = self.clock.time_msec()
        results[:] = [format_user_presence_state(r, now) for r in results]

        is_accepted = {
            row["observed_user_id"]: row["accepted"] for row in presence_list
        }

        for result in results:
            result.update({
                "accepted": is_accepted,
            })

        defer.returnValue(results)

    @defer.inlineCallbacks
    def send_presence_invite(self, observer_user, observed_user):
        """Sends a presence invite.
        """
        yield self.store.add_presence_list_pending(
            observer_user.localpart, observed_user.to_string()
        )

        if self.is_mine(observed_user):
            yield self.invite_presence(observed_user, observer_user)
        else:
            yield self.federation.send_edu(
                destination=observed_user.domain,
                edu_type="m.presence_invite",
                content={
                    "observed_user": observed_user.to_string(),
                    "observer_user": observer_user.to_string(),
                }
            )

    @defer.inlineCallbacks
    def invite_presence(self, observed_user, observer_user):
        """Handles new presence invites.
        """
        if not self.is_mine(observed_user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        # TODO: Don't auto accept
        if self.is_mine(observer_user):
            yield self.accept_presence(observed_user, observer_user)
        else:
            self.federation.send_edu(
                destination=observer_user.domain,
                edu_type="m.presence_accept",
                content={
                    "observed_user": observed_user.to_string(),
                    "observer_user": observer_user.to_string(),
                }
            )

            state_dict = yield self.get_state(observed_user, as_event=False)
            state_dict = format_user_presence_state(state_dict, self.clock.time_msec())

            self.federation.send_edu(
                destination=observer_user.domain,
                edu_type="m.presence",
                content={
                    "push": [state_dict]
                }
            )

    @defer.inlineCallbacks
    def accept_presence(self, observed_user, observer_user):
        """Handles a m.presence_accept EDU. Mark a presence invite from a
        local or remote user as accepted in a local user's presence list.
        Starts polling for presence updates from the local or remote user.
        Args:
            observed_user(UserID): The user to update in the presence list.
            observer_user(UserID): The owner of the presence list to update.
        """
        yield self.store.set_presence_list_accepted(
            observer_user.localpart, observed_user.to_string()
        )

    @defer.inlineCallbacks
    def deny_presence(self, observed_user, observer_user):
        """Handle a m.presence_deny EDU. Removes a local or remote user from a
        local user's presence list.
        Args:
            observed_user(UserID): The local or remote user to remove from the
                list.
            observer_user(UserID): The local owner of the presence list.
        Returns:
            A Deferred.
        """
        yield self.store.del_presence_list(
            observer_user.localpart, observed_user.to_string()
        )

        # TODO(paul): Inform the user somehow?

    @defer.inlineCallbacks
    def drop(self, observed_user, observer_user):
        """Remove a local or remote user from a local user's presence list and
        unsubscribe the local user from updates that user.
        Args:
            observed_user(UserId): The local or remote user to remove from the
                list.
            observer_user(UserId): The local owner of the presence list.
        Returns:
            A Deferred.
        """
        if not self.is_mine(observer_user):
            raise SynapseError(400, "User is not hosted on this Home Server")

        yield self.store.del_presence_list(
            observer_user.localpart, observed_user.to_string()
        )

        # TODO: Inform the remote that we've dropped the presence list.

    @defer.inlineCallbacks
    def is_visible(self, observed_user, observer_user):
        """Returns whether a user can see another user's presence.
        """
        observer_room_ids = yield self.store.get_rooms_for_user(
            observer_user.to_string()
        )
        observed_room_ids = yield self.store.get_rooms_for_user(
            observed_user.to_string()
        )

        if observer_room_ids & observed_room_ids:
            defer.returnValue(True)

        accepted_observers = yield self.store.get_presence_list_observers_accepted(
            observed_user.to_string()
        )

        defer.returnValue(observer_user.to_string() in accepted_observers)

    @defer.inlineCallbacks
    def get_all_presence_updates(self, last_id, current_id):
        """
        Gets a list of presence update rows from between the given stream ids.
        Each row has:
        - stream_id(str)
        - user_id(str)
        - state(str)
        - last_active_ts(int)
        - last_federation_update_ts(int)
        - last_user_sync_ts(int)
        - status_msg(int)
        - currently_active(int)
        """
        # TODO(markjh): replicate the unpersisted changes.
        # This could use the in-memory stores for recent changes.
        rows = yield self.store.get_all_presence_updates(last_id, current_id)
        defer.returnValue(rows)


def should_notify(old_state, new_state):
    """Decides if a presence state change should be sent to interested parties.
    """
    if old_state == new_state:
        return False

    if old_state.status_msg != new_state.status_msg:
        notify_reason_counter.labels("status_msg_change").inc()
        return True

    if old_state.state != new_state.state:
        notify_reason_counter.labels("state_change").inc()
        state_transition_counter.labels(old_state.state, new_state.state).inc()
        return True

    if old_state.state == PresenceState.ONLINE:
        if new_state.currently_active != old_state.currently_active:
            notify_reason_counter.labels("current_active_change").inc()
            return True

        if new_state.last_active_ts - old_state.last_active_ts > LAST_ACTIVE_GRANULARITY:
            # Only notify about last active bumps if we're not currently acive
            if not new_state.currently_active:
                notify_reason_counter.labels("last_active_change_online").inc()
                return True

    elif new_state.last_active_ts - old_state.last_active_ts > LAST_ACTIVE_GRANULARITY:
        # Always notify for a transition where last active gets bumped.
        notify_reason_counter.labels("last_active_change_not_online").inc()
        return True

    return False


def format_user_presence_state(state, now, include_user_id=True):
    """Convert UserPresenceState to a format that can be sent down to clients
    and to other servers.

    The "user_id" is optional so that this function can be used to format presence
    updates for client /sync responses and for federation /send requests.
    """
    content = {
        "presence": state.state,
    }
    if include_user_id:
        content["user_id"] = state.user_id
    if state.last_active_ts:
        content["last_active_ago"] = now - state.last_active_ts
    if state.status_msg and state.state != PresenceState.OFFLINE:
        content["status_msg"] = state.status_msg
    if state.state == PresenceState.ONLINE:
        content["currently_active"] = state.currently_active

    return content


class PresenceEventSource(object):
    def __init__(self, hs):
        # We can't call get_presence_handler here because there's a cycle:
        #
        #   Presence -> Notifier -> PresenceEventSource -> Presence
        #
        self.get_presence_handler = hs.get_presence_handler
        self.clock = hs.get_clock()
        self.store = hs.get_datastore()
        self.state = hs.get_state_handler()

    @defer.inlineCallbacks
    @log_function
    def get_new_events(self, user, from_key, room_ids=None, include_offline=True,
                       explicit_room_id=None, **kwargs):
        # The process for getting presence events are:
        #  1. Get the rooms the user is in.
        #  2. Get the list of user in the rooms.
        #  3. Get the list of users that are in the user's presence list.
        #  4. If there is a from_key set, cross reference the list of users
        #     with the `presence_stream_cache` to see which ones we actually
        #     need to check.
        #  5. Load current state for the users.
        #
        # We don't try and limit the presence updates by the current token, as
        # sending down the rare duplicate is not a concern.

        with Measure(self.clock, "presence.get_new_events"):
            if from_key is not None:
                from_key = int(from_key)

            presence = self.get_presence_handler()
            stream_change_cache = self.store.presence_stream_cache

            max_token = self.store.get_current_presence_token()

            users_interested_in = yield self._get_interested_in(user, explicit_room_id)

            user_ids_changed = set()
            changed = None
            if from_key:
                changed = stream_change_cache.get_all_entities_changed(from_key)

            if changed is not None and len(changed) < 500:
                # For small deltas, its quicker to get all changes and then
                # work out if we share a room or they're in our presence list
                get_updates_counter.labels("stream").inc()
                for other_user_id in changed:
                    if other_user_id in users_interested_in:
                        user_ids_changed.add(other_user_id)
            else:
                # Too many possible updates. Find all users we can see and check
                # if any of them have changed.
                get_updates_counter.labels("full").inc()

                if from_key:
                    user_ids_changed = stream_change_cache.get_entities_changed(
                        users_interested_in, from_key,
                    )
                else:
                    user_ids_changed = users_interested_in

            updates = yield presence.current_state_for_users(user_ids_changed)

        if include_offline:
            defer.returnValue((list(updates.values()), max_token))
        else:
            defer.returnValue(([
                s for s in itervalues(updates)
                if s.state != PresenceState.OFFLINE
            ], max_token))

    def get_current_key(self):
        return self.store.get_current_presence_token()

    def get_pagination_rows(self, user, pagination_config, key):
        return self.get_new_events(user, from_key=None, include_offline=False)

    @cachedInlineCallbacks(num_args=2, cache_context=True)
    def _get_interested_in(self, user, explicit_room_id, cache_context):
        """Returns the set of users that the given user should see presence
        updates for
        """
        user_id = user.to_string()
        plist = yield self.store.get_presence_list_accepted(
            user.localpart, on_invalidate=cache_context.invalidate,
        )
        users_interested_in = set(row["observed_user_id"] for row in plist)
        users_interested_in.add(user_id)  # So that we receive our own presence

        users_who_share_room = yield self.store.get_users_who_share_room_with_user(
            user_id, on_invalidate=cache_context.invalidate,
        )
        users_interested_in.update(users_who_share_room)

        if explicit_room_id:
            user_ids = yield self.store.get_users_in_room(
                explicit_room_id, on_invalidate=cache_context.invalidate,
            )
            users_interested_in.update(user_ids)

        defer.returnValue(users_interested_in)


def handle_timeouts(user_states, is_mine_fn, syncing_user_ids, now):
    """Checks the presence of users that have timed out and updates as
    appropriate.

    Args:
        user_states(list): List of UserPresenceState's to check.
        is_mine_fn (fn): Function that returns if a user_id is ours
        syncing_user_ids (set): Set of user_ids with active syncs.
        now (int): Current time in ms.

    Returns:
        List of UserPresenceState updates
    """
    changes = {}  # Actual changes we need to notify people about

    for state in user_states:
        is_mine = is_mine_fn(state.user_id)

        new_state = handle_timeout(state, is_mine, syncing_user_ids, now)
        if new_state:
            changes[state.user_id] = new_state

    return list(changes.values())


def handle_timeout(state, is_mine, syncing_user_ids, now):
    """Checks the presence of the user to see if any of the timers have elapsed

    Args:
        state (UserPresenceState)
        is_mine (bool): Whether the user is ours
        syncing_user_ids (set): Set of user_ids with active syncs.
        now (int): Current time in ms.

    Returns:
        A UserPresenceState update or None if no update.
    """
    if state.state == PresenceState.OFFLINE:
        # No timeouts are associated with offline states.
        return None

    changed = False
    user_id = state.user_id

    if is_mine:
        if state.state == PresenceState.ONLINE:
            if now - state.last_active_ts > IDLE_TIMER:
                # Currently online, but last activity ages ago so auto
                # idle
                state = state.copy_and_replace(
                    state=PresenceState.UNAVAILABLE,
                )
                changed = True
            elif now - state.last_active_ts > LAST_ACTIVE_GRANULARITY:
                # So that we send down a notification that we've
                # stopped updating.
                changed = True

        if now - state.last_federation_update_ts > FEDERATION_PING_INTERVAL:
            # Need to send ping to other servers to ensure they don't
            # timeout and set us to offline
            changed = True

        # If there are have been no sync for a while (and none ongoing),
        # set presence to offline
        if user_id not in syncing_user_ids:
            # If the user has done something recently but hasn't synced,
            # don't set them as offline.
            sync_or_active = max(state.last_user_sync_ts, state.last_active_ts)
            if now - sync_or_active > SYNC_ONLINE_TIMEOUT:
                state = state.copy_and_replace(
                    state=PresenceState.OFFLINE,
                    status_msg=None,
                )
                changed = True
    else:
        # We expect to be poked occasionally by the other side.
        # This is to protect against forgetful/buggy servers, so that
        # no one gets stuck online forever.
        if now - state.last_federation_update_ts > FEDERATION_TIMEOUT:
            # The other side seems to have disappeared.
            state = state.copy_and_replace(
                state=PresenceState.OFFLINE,
                status_msg=None,
            )
            changed = True

    return state if changed else None


def handle_update(prev_state, new_state, is_mine, wheel_timer, now):
    """Given a presence update:
        1. Add any appropriate timers.
        2. Check if we should notify anyone.

    Args:
        prev_state (UserPresenceState)
        new_state (UserPresenceState)
        is_mine (bool): Whether the user is ours
        wheel_timer (WheelTimer)
        now (int): Time now in ms

    Returns:
        3-tuple: `(new_state, persist_and_notify, federation_ping)` where:
            - new_state: is the state to actually persist
            - persist_and_notify (bool): whether to persist and notify people
            - federation_ping (bool): whether we should send a ping over federation
    """
    user_id = new_state.user_id

    persist_and_notify = False
    federation_ping = False

    # If the users are ours then we want to set up a bunch of timers
    # to time things out.
    if is_mine:
        if new_state.state == PresenceState.ONLINE:
            # Idle timer
            wheel_timer.insert(
                now=now,
                obj=user_id,
                then=new_state.last_active_ts + IDLE_TIMER
            )

            active = now - new_state.last_active_ts < LAST_ACTIVE_GRANULARITY
            new_state = new_state.copy_and_replace(
                currently_active=active,
            )

            if active:
                wheel_timer.insert(
                    now=now,
                    obj=user_id,
                    then=new_state.last_active_ts + LAST_ACTIVE_GRANULARITY
                )

        if new_state.state != PresenceState.OFFLINE:
            # User has stopped syncing
            wheel_timer.insert(
                now=now,
                obj=user_id,
                then=new_state.last_user_sync_ts + SYNC_ONLINE_TIMEOUT
            )

            last_federate = new_state.last_federation_update_ts
            if now - last_federate > FEDERATION_PING_INTERVAL:
                # Been a while since we've poked remote servers
                new_state = new_state.copy_and_replace(
                    last_federation_update_ts=now,
                )
                federation_ping = True

    else:
        wheel_timer.insert(
            now=now,
            obj=user_id,
            then=new_state.last_federation_update_ts + FEDERATION_TIMEOUT
        )

    # Check whether the change was something worth notifying about
    if should_notify(prev_state, new_state):
        new_state = new_state.copy_and_replace(
            last_federation_update_ts=now,
        )
        persist_and_notify = True

    return new_state, persist_and_notify, federation_ping


@defer.inlineCallbacks
def get_interested_parties(store, states):
    """Given a list of states return which entities (rooms, users)
    are interested in the given states.

    Args:
        states (list(UserPresenceState))

    Returns:
        2-tuple: `(room_ids_to_states, users_to_states)`,
        with each item being a dict of `entity_name` -> `[UserPresenceState]`
    """
    room_ids_to_states = {}
    users_to_states = {}
    for state in states:
        room_ids = yield store.get_rooms_for_user(state.user_id)
        for room_id in room_ids:
            room_ids_to_states.setdefault(room_id, []).append(state)

        plist = yield store.get_presence_list_observers_accepted(state.user_id)
        for u in plist:
            users_to_states.setdefault(u, []).append(state)

        # Always notify self
        users_to_states.setdefault(state.user_id, []).append(state)

    defer.returnValue((room_ids_to_states, users_to_states))


@defer.inlineCallbacks
def get_interested_remotes(store, states, state_handler):
    """Given a list of presence states figure out which remote servers
    should be sent which.

    All the presence states should be for local users only.

    Args:
        store (DataStore)
        states (list(UserPresenceState))

    Returns:
        Deferred list of ([destinations], [UserPresenceState]), where for
        each row the list of UserPresenceState should be sent to each
        destination
    """
    hosts_and_states = []

    # First we look up the rooms each user is in (as well as any explicit
    # subscriptions), then for each distinct room we look up the remote
    # hosts in those rooms.
    room_ids_to_states, users_to_states = yield get_interested_parties(store, states)

    for room_id, states in iteritems(room_ids_to_states):
        hosts = yield state_handler.get_current_hosts_in_room(room_id)
        hosts_and_states.append((hosts, states))

    for user_id, states in iteritems(users_to_states):
        host = get_domain_from_id(user_id)
        hosts_and_states.append(([host], states))

    defer.returnValue(hosts_and_states)
