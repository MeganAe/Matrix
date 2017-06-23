# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
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

from twisted.internet import defer

from .push_rule_evaluator import PushRuleEvaluatorForEvent

from synapse.visibility import filter_events_for_clients_context
from synapse.api.constants import EventTypes, Membership
from synapse.util.caches.descriptors import cached
from synapse.util.async import Linearizer

from collections import namedtuple


logger = logging.getLogger(__name__)


rules_by_room = {}


class BulkPushRuleEvaluator(object):
    """Calculates the outcome of push rules for an event for all users in the
    room at once.
    """

    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()

    @defer.inlineCallbacks
    def _get_rules_for_event(self, event, context):
        """This gets the rules for all users in the room at the time of the event,
        as well as the push rules for the invitee if the event is an invite.

        Returns:
            dict of user_id -> push_rules
        """
        room_id = event.room_id
        rules_for_room = self._get_rules_for_room(room_id)

        rules_by_user = yield rules_for_room.get_rules(event, context)

        # if this event is an invite event, we may need to run rules for the user
        # who's been invited, otherwise they won't get told they've been invited
        if event.type == 'm.room.member' and event.content['membership'] == 'invite':
            invited = event.state_key
            if invited and self.hs.is_mine_id(invited):
                has_pusher = yield self.store.user_has_pusher(invited)
                if has_pusher:
                    rules_by_user = dict(rules_by_user)
                    rules_by_user[invited] = yield self.store.get_push_rules_for_user(
                        invited
                    )

        defer.returnValue(rules_by_user)

    @cached()
    def _get_rules_for_room(self, room_id):
        """Get the current RulesForRoom object for the given room id

        Returns:
            RulesForRoom
        """
        # It's important that RulesForRoom gets added to self._get_rules_for_room.cache
        # before any lookup methods get called on it as otherwise there may be
        # a race if invalidate_all gets called (which assumes its in the cache)
        return RulesForRoom(self.hs, room_id, self._get_rules_for_room.cache)

    @defer.inlineCallbacks
    def action_for_event_by_user(self, event, context):
        """Given an event and context, evaluate the push rules and return
        the results

        Returns:
            dict of user_id -> action
        """
        rules_by_user = yield self._get_rules_for_event(event, context)
        actions_by_user = {}

        # None of these users can be peeking since this list of users comes
        # from the set of users in the room, so we know for sure they're all
        # actually in the room.
        user_tuples = [(u, False) for u in rules_by_user]

        filtered_by_user = yield filter_events_for_clients_context(
            self.store, user_tuples, [event], {event.event_id: context}
        )

        room_members = yield self.store.get_joined_users_from_context(
            event, context
        )

        evaluator = PushRuleEvaluatorForEvent(event, len(room_members))

        condition_cache = {}

        for uid, rules in rules_by_user.iteritems():
            display_name = None
            profile_info = room_members.get(uid)
            if profile_info:
                display_name = profile_info.display_name

            if not display_name:
                # Handle the case where we are pushing a membership event to
                # that user, as they might not be already joined.
                if event.type == EventTypes.Member and event.state_key == uid:
                    display_name = event.content.get("displayname", None)

            filtered = filtered_by_user[uid]
            if len(filtered) == 0:
                continue

            if filtered[0].sender == uid:
                continue

            for rule in rules:
                if 'enabled' in rule and not rule['enabled']:
                    continue

                matches = _condition_checker(
                    evaluator, rule['conditions'], uid, display_name, condition_cache
                )
                if matches:
                    actions = [x for x in rule['actions'] if x != 'dont_notify']
                    if actions and 'notify' in actions:
                        actions_by_user[uid] = actions
                    break
        defer.returnValue(actions_by_user)


def _condition_checker(evaluator, conditions, uid, display_name, cache):
    for cond in conditions:
        _id = cond.get("_id", None)
        if _id:
            res = cache.get(_id, None)
            if res is False:
                return False
            elif res is True:
                continue

        res = evaluator.matches(cond, uid, display_name)
        if _id:
            cache[_id] = bool(res)

        if not res:
            return False

    return True


class RulesForRoom(object):
    """Caches push rules for users in a room.

    This efficiently handles users joining/leaving the room by not invalidating
    the entire cache for the room.
    """

    def __init__(self, hs, room_id, rules_for_room_cache):
        """
        Args:
            hs (HomeServer)
            room_id (str)
            rules_for_room_cache(Cache): The cache object that caches these
                RoomsForUser objects.
        """
        self.room_id = room_id
        self.is_mine_id = hs.is_mine_id
        self.store = hs.get_datastore()

        self.linearizer = Linearizer(name="rules_for_room")

        self.member_map = {}  # event_id -> (user_id, state)
        self.rules_by_user = {}  # user_id -> rules

        # The last state group we updated the caches for. If the state_group of
        # a new event comes along, we know that we can just return the cached
        # result.
        # On invalidation of the rules themselves (if the user changes them),
        # we invalidate everything and set state_group to `object()`
        self.state_group = object()

        # A sequence number to keep track of when we're allowed to update the
        # cache. We bump the sequence number when we invalidate the cache. If
        # the sequence number changes while we're calculating stuff we should
        # not update the cache with it.
        self.sequence = 0

        # A cache of user_ids that we *know* aren't interesting, e.g. user_ids
        # owned by AS's, or remote users, etc. (I.e. users we will never need to
        # calculate push for)
        # These never need to be invalidated as we will never set up push for
        # them.
        self.uninteresting_user_set = set()

        # We need to be clever on the invalidating caches callbacks, as
        # otherwise the invalidation callback holds a reference to the object,
        # potentially causing it to leak.
        # To get around this we pass a function that on invalidations looks ups
        # the RoomsForUser entry in the cache, rather than keeping a reference
        # to self around in the callback.
        self.invalidate_all_cb = _Invalidation(rules_for_room_cache, room_id)

    @defer.inlineCallbacks
    def get_rules(self, event, context):
        """Given an event context return the rules for all users who are
        currently in the room.
        """
        state_group = context.state_group

        with (yield self.linearizer.queue(())):
            if state_group and self.state_group == state_group:
                logger.debug("Using cached rules for %r", self.room_id)
                defer.returnValue(self.rules_by_user)

            ret_rules_by_user = {}
            missing_member_event_ids = {}
            if state_group and self.state_group == context.prev_group:
                # If we have a simple delta then we can reuse most of the previous
                # results.
                ret_rules_by_user = self.rules_by_user
                current_state_ids = context.delta_ids
            else:
                current_state_ids = context.current_state_ids

            logger.debug(
                "Looking for member changes in %r %r", state_group, current_state_ids
            )

            # Loop through to see which member events we've seen and have rules
            # for and which we need to fetch
            for key in current_state_ids:
                typ, user_id = key
                if typ != EventTypes.Member:
                    continue

                if user_id in self.uninteresting_user_set:
                    continue

                if not self.is_mine_id(user_id):
                    self.uninteresting_user_set.add(user_id)
                    continue

                if self.store.get_if_app_services_interested_in_user(user_id):
                    self.uninteresting_user_set.add(user_id)
                    continue

                event_id = current_state_ids[key]

                res = self.member_map.get(event_id, None)
                if res:
                    user_id, state = res
                    if state == Membership.JOIN:
                        rules = self.rules_by_user.get(user_id, None)
                        if rules:
                            ret_rules_by_user[user_id] = rules
                    continue

                # If a user has left a room we remove their push rule. If they
                # joined then we readd it later in _update_rules_with_member_event_ids
                ret_rules_by_user.pop(user_id, None)
                missing_member_event_ids[user_id] = event_id

            if missing_member_event_ids:
                # If we have some memebr events we haven't seen, look them up
                # and fetch push rules for them if appropriate.
                logger.debug("Found new member events %r", missing_member_event_ids)
                yield self._update_rules_with_member_event_ids(
                    ret_rules_by_user, missing_member_event_ids, state_group, event
                )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Returning push rules for %r %r",
                self.room_id, ret_rules_by_user.keys(),
            )
        defer.returnValue(ret_rules_by_user)

    @defer.inlineCallbacks
    def _update_rules_with_member_event_ids(self, ret_rules_by_user, member_event_ids,
                                            state_group, event):
        """Update the partially filled rules_by_user dict by fetching rules for
        any newly joined users in the `member_event_ids` list.

        Args:
            ret_rules_by_user (dict): Partiallly filled dict of push rules. Gets
                updated with any new rules.
            member_event_ids (list): List of event ids for membership events that
                have happened since the last time we filled rules_by_user
            state_group: The state group we are currently computing push rules
                for. Used when updating the cache.
        """
        sequence = self.sequence

        rows = yield self.store._simple_select_many_batch(
            table="room_memberships",
            column="event_id",
            iterable=member_event_ids.values(),
            retcols=('user_id', 'membership', 'event_id'),
            keyvalues={},
            batch_size=500,
            desc="_get_rules_for_member_event_ids",
        )

        members = {
            row["event_id"]: (row["user_id"], row["membership"])
            for row in rows
        }

        # If the event is a join event then it will be in current state evnts
        # map but not in the DB, so we have to explicitly insert it.
        if event.type == EventTypes.Member:
            for event_id in member_event_ids.itervalues():
                if event_id == event.event_id:
                    members[event_id] = (event.state_key, event.membership)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Found members %r: %r", self.room_id, members.values())

        interested_in_user_ids = set(
            user_id for user_id, membership in members.itervalues()
            if membership == Membership.JOIN
        )

        logger.debug("Joined: %r", interested_in_user_ids)

        if_users_with_pushers = yield self.store.get_if_users_have_pushers(
            interested_in_user_ids,
            on_invalidate=self.invalidate_all_cb,
        )

        user_ids = set(
            uid for uid, have_pusher in if_users_with_pushers.iteritems() if have_pusher
        )

        logger.debug("With pushers: %r", user_ids)

        users_with_receipts = yield self.store.get_users_with_read_receipts_in_room(
            self.room_id, on_invalidate=self.invalidate_all_cb,
        )

        logger.debug("With receipts: %r", users_with_receipts)

        # any users with pushers must be ours: they have pushers
        for uid in users_with_receipts:
            if uid in interested_in_user_ids:
                user_ids.add(uid)

        rules_by_user = yield self.store.bulk_get_push_rules(
            user_ids, on_invalidate=self.invalidate_all_cb,
        )

        ret_rules_by_user.update(
            item for item in rules_by_user.iteritems() if item[0] is not None
        )

        self.update_cache(sequence, members, ret_rules_by_user, state_group)

    def invalidate_all(self):
        # Note: Don't hand this function directly to an invalidation callback
        # as it keeps a reference to self and will stop this instance from being
        # GC'd if it gets dropped from the rules_to_user cache. Instead use
        # `self.invalidate_all_cb`
        logger.debug("Invalidating RulesForRoom for %r", self.room_id)
        self.sequence += 1
        self.state_group = object()
        self.member_map = {}
        self.rules_by_user = {}

    def update_cache(self, sequence, members, rules_by_user, state_group):
        if sequence == self.sequence:
            self.member_map.update(members)
            self.rules_by_user = rules_by_user
            self.state_group = state_group


class _Invalidation(namedtuple("_Invalidation", ("cache", "room_id"))):
    # We rely on _CacheContext implementing __eq__ and __hash__ sensibly,
    # which namedtuple does for us (i.e. two _CacheContext are the same if
    # their caches and keys match). This is important in particular to
    # dedupe when we add callbacks to lru cache nodes, otherwise the number
    # of callbacks would grow.
    def __call__(self):
        rules = self.cache.get(self.room_id, None, update_metrics=False)
        if rules:
            rules.invalidate_all()
