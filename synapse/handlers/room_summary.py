# Copyright 2021 The Matrix.org Foundation C.I.C.
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

import itertools
import logging
import re
from collections import deque
from typing import (
    TYPE_CHECKING,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import attr

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    HistoryVisibility,
    JoinRules,
    RoomTypes,
)
from synapse.api.errors import AuthError, Codes, NotFoundError, SynapseError
from synapse.events import EventBase
from synapse.events.utils import format_event_for_client_v2
from synapse.types import JsonDict
from synapse.util.caches.response_cache import ResponseCache
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# number of rooms to return. We'll stop once we hit this limit.
MAX_ROOMS = 50

# max number of events to return per room.
MAX_ROOMS_PER_SPACE = 50

# max number of federation servers to hit per room
MAX_SERVERS_PER_SPACE = 3


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _PaginationKey:
    """The key used to find unique pagination session."""

    # The first three entries match the request parameters (and cannot change
    # during a pagination session).
    room_id: str
    suggested_only: bool
    max_depth: Optional[int]
    # The randomly generated token.
    token: str


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _PaginationSession:
    """The information that is stored for pagination."""

    # The time the pagination session was created, in milliseconds.
    creation_time_ms: int
    # The queue of rooms which are still to process.
    room_queue: Deque["_RoomQueueEntry"]
    # A set of rooms which have been processed.
    processed_rooms: Set[str]


class RoomSummaryHandler:
    # The time a pagination session remains valid for.
    _PAGINATION_SESSION_VALIDITY_PERIOD_MS = 5 * 60 * 1000

    def __init__(self, hs: "HomeServer"):
        self._clock = hs.get_clock()
        self._event_auth_handler = hs.get_event_auth_handler()
        self._store = hs.get_datastore()
        self._auth = hs.get_auth()
        self._event_auth_handler = hs.get_event_auth_handler()
        self._event_serializer = hs.get_event_client_serializer()
        self._server_name = hs.hostname
        self._federation_client = hs.get_federation_client()

        # A map of query information to the current pagination state.
        #
        # TODO Allow for multiple workers to share this data.
        # TODO Expire pagination tokens.
        self._pagination_sessions: Dict[_PaginationKey, _PaginationSession] = {}

        # If a user tries to fetch the same page multiple times in quick succession,
        # only process the first attempt and return its result to subsequent requests.
        self._pagination_response_cache: ResponseCache[
            Tuple[str, bool, Optional[int], Optional[int], Optional[str]]
        ] = ResponseCache(
            hs.get_clock(),
            "get_room_hierarchy",
        )

    async def _is_remote_room_accessible(
        self, requester: Optional[str], room_id: str, room: JsonDict
    ) -> bool:
        """
        Calculate whether the room received over federation should be shown in the summary.

        It should be included if:

        * The requester is joined or can join the room (per MSC3173).
        * The history visibility is set to world readable.

        Note that the local server is not in the requested room (which is why the
        remote call was made in the first place), but the user could have access
        due to an invite, etc.

        Args:
            requester: The user requesting the summary, if authenticated.
            room_id: The room ID returned over federation.
            room: The summary of the child room returned over federation.

        Returns:
            True if the room should be included in the spaces summary.
        """
        # The API doesn't return the room version so assume that a
        # join rule of knock is valid.
        if (
            room.get("join_rules") in (JoinRules.PUBLIC, JoinRules.KNOCK)
            or room.get("world_readable") is True
        ):
            return True

        # Check if the user is a member of any of the allowed spaces
        # from the response.
        allowed_rooms = room.get("allowed_room_ids") or room.get("allowed_spaces")
        if requester and allowed_rooms and isinstance(allowed_rooms, list):
            if await self._event_auth_handler.is_user_in_rooms(
                allowed_rooms, requester
            ):
                return True

        # Finally, check locally if we can access the room. The user might
        # already be in the room (if it was a child room), or there might be a
        # pending invite, etc.
        return await self._auth.is_room_visible(room_id, requester)

    def _expire_pagination_sessions(self):
        """Expire pagination session which are old."""
        expire_before = (
            self._clock.time_msec() - self._PAGINATION_SESSION_VALIDITY_PERIOD_MS
        )
        to_expire = []

        for key, value in self._pagination_sessions.items():
            if value.creation_time_ms < expire_before:
                to_expire.append(key)

        for key in to_expire:
            logger.debug("Expiring pagination session id %s", key)
            del self._pagination_sessions[key]

    async def get_space_summary(
        self,
        requester: str,
        room_id: str,
        suggested_only: bool = False,
        max_rooms_per_space: Optional[int] = None,
    ) -> JsonDict:
        """
        Implementation of the space summary C-S API

        Args:
            requester:  user id of the user making this request

            room_id: room id to start the hierarchy from

            suggested_only: whether we should only return children with the "suggested"
                flag set.

            max_rooms_per_space: an optional limit on the number of child rooms we will
                return. This does not apply to the root room (ie, room_id), and
                is overridden by MAX_ROOMS_PER_SPACE.

        Returns:
            hierarchy dict to return
        """
        # First of all, check that the room is accessible.
        if not await self._auth.is_room_visible(room_id, requester):
            raise AuthError(
                403,
                "User %s not in room %s, and room previews are disabled"
                % (requester, room_id),
            )

        # the queue of rooms to process
        room_queue = deque((_RoomQueueEntry(room_id, ()),))

        # rooms we have already processed
        processed_rooms: Set[str] = set()

        # events we have already processed. We don't necessarily have their event ids,
        # so instead we key on (room id, state key)
        processed_events: Set[Tuple[str, str]] = set()

        rooms_result: List[JsonDict] = []
        events_result: List[JsonDict] = []

        while room_queue and len(rooms_result) < MAX_ROOMS:
            queue_entry = room_queue.popleft()
            room_id = queue_entry.room_id
            if room_id in processed_rooms:
                # already done this room
                continue

            logger.debug("Processing room %s", room_id)

            is_in_room = await self._store.is_host_joined(room_id, self._server_name)

            # The client-specified max_rooms_per_space limit doesn't apply to the
            # room_id specified in the request, so we ignore it if this is the
            # first room we are processing.
            max_children = max_rooms_per_space if processed_rooms else None

            if is_in_room:
                room_entry = await self._summarize_local_room_hierarchy(
                    requester, None, room_id, suggested_only, max_children
                )

                events: Sequence[JsonDict] = []
                if room_entry:
                    rooms_result.append(room_entry.room)
                    events = room_entry.children

                logger.debug(
                    "Query of local room %s returned events %s",
                    room_id,
                    ["%s->%s" % (ev["room_id"], ev["state_key"]) for ev in events],
                )
            else:
                fed_rooms = await self._summarize_remote_room_hierarchy(
                    queue_entry,
                    suggested_only,
                    max_children,
                    exclude_rooms=processed_rooms,
                )

                # The results over federation might include rooms that the we,
                # as the requesting server, are allowed to see, but the requesting
                # user is not permitted see.
                #
                # Filter the returned results to only what is accessible to the user.
                events = []
                for room_entry in fed_rooms:
                    room = room_entry.room
                    fed_room_id = room_entry.room_id

                    # The user can see the room, include it!
                    if await self._is_remote_room_accessible(
                        requester, fed_room_id, room
                    ):
                        # Before returning to the client, remove the allowed_room_ids
                        # and allowed_spaces keys.
                        room.pop("allowed_room_ids", None)
                        room.pop("allowed_spaces", None)

                        rooms_result.append(room)
                        events.extend(room_entry.children)

                    # All rooms returned don't need visiting again (even if the user
                    # didn't have access to them).
                    processed_rooms.add(fed_room_id)

                logger.debug(
                    "Query of %s returned rooms %s, events %s",
                    room_id,
                    [room_entry.room.get("room_id") for room_entry in fed_rooms],
                    ["%s->%s" % (ev["room_id"], ev["state_key"]) for ev in events],
                )

            # the room we queried may or may not have been returned, but don't process
            # it again, anyway.
            processed_rooms.add(room_id)

            # XXX: is it ok that we blindly iterate through any events returned by
            #   a remote server, whether or not they actually link to any rooms in our
            #   tree?
            for ev in events:
                # remote servers might return events we have already processed
                # (eg, Dendrite returns inward pointers as well as outward ones), so
                # we need to filter them out, to avoid returning duplicate links to the
                # client.
                ev_key = (ev["room_id"], ev["state_key"])
                if ev_key in processed_events:
                    continue
                events_result.append(ev)

                # add the child to the queue. we have already validated
                # that the vias are a list of server names.
                room_queue.append(
                    _RoomQueueEntry(ev["state_key"], ev["content"]["via"])
                )
                processed_events.add(ev_key)

        return {"rooms": rooms_result, "events": events_result}

    async def get_room_hierarchy(
        self,
        requester: str,
        requested_room_id: str,
        suggested_only: bool = False,
        max_depth: Optional[int] = None,
        limit: Optional[int] = None,
        from_token: Optional[str] = None,
    ) -> JsonDict:
        """
        Implementation of the room hierarchy C-S API.

        Args:
            requester: The user ID of the user making this request.
            requested_room_id: The room ID to start the hierarchy at (the "root" room).
            suggested_only: Whether we should only return children with the "suggested"
                flag set.
            max_depth: The maximum depth in the tree to explore, must be a
                non-negative integer.

                0 would correspond to just the root room, 1 would include just
                the root room's children, etc.
            limit: An optional limit on the number of rooms to return per
                page. Must be a positive integer.
            from_token: An optional pagination token.

        Returns:
            The JSON hierarchy dictionary.
        """
        # If a user tries to fetch the same page multiple times in quick succession,
        # only process the first attempt and return its result to subsequent requests.
        #
        # This is due to the pagination process mutating internal state, attempting
        # to process multiple requests for the same page will result in errors.
        return await self._pagination_response_cache.wrap(
            (requested_room_id, suggested_only, max_depth, limit, from_token),
            self._get_room_hierarchy,
            requester,
            requested_room_id,
            suggested_only,
            max_depth,
            limit,
            from_token,
        )

    async def _get_room_hierarchy(
        self,
        requester: str,
        requested_room_id: str,
        suggested_only: bool = False,
        max_depth: Optional[int] = None,
        limit: Optional[int] = None,
        from_token: Optional[str] = None,
    ) -> JsonDict:
        """See docstring for SpaceSummaryHandler.get_room_hierarchy."""

        # First of all, check that the room is accessible.
        if not await self._auth.is_room_visible(requested_room_id, requester):
            raise AuthError(
                403,
                "User %s not in room %s, and room previews are disabled"
                % (requester, requested_room_id),
            )

        # If this is continuing a previous session, pull the persisted data.
        if from_token:
            self._expire_pagination_sessions()

            pagination_key = _PaginationKey(
                requested_room_id, suggested_only, max_depth, from_token
            )
            if pagination_key not in self._pagination_sessions:
                raise SynapseError(400, "Unknown pagination token", Codes.INVALID_PARAM)

            # Load the previous state.
            pagination_session = self._pagination_sessions[pagination_key]
            room_queue = pagination_session.room_queue
            processed_rooms = pagination_session.processed_rooms
        else:
            # the queue of rooms to process
            room_queue = deque((_RoomQueueEntry(requested_room_id, ()),))

            # Rooms we have already processed.
            processed_rooms = set()

        rooms_result: List[JsonDict] = []

        # Cap the limit to a server-side maximum.
        if limit is None:
            limit = MAX_ROOMS
        else:
            limit = min(limit, MAX_ROOMS)

        # Iterate through the queue until we reach the limit or run out of
        # rooms to include.
        while room_queue and len(rooms_result) < limit:
            queue_entry = room_queue.popleft()
            room_id = queue_entry.room_id
            current_depth = queue_entry.depth
            if room_id in processed_rooms:
                # already done this room
                continue

            logger.debug("Processing room %s", room_id)

            is_in_room = await self._store.is_host_joined(room_id, self._server_name)
            if is_in_room:
                room_entry = await self._summarize_local_room_hierarchy(
                    requester,
                    None,
                    room_id,
                    suggested_only,
                    # TODO Handle max children.
                    max_children=None,
                )

                if room_entry:
                    rooms_result.append(room_entry.as_json())

                    # Add the child to the queue. We have already validated
                    # that the vias are a list of server names.
                    #
                    # If the current depth is the maximum depth, do not queue
                    # more entries.
                    if max_depth is None or current_depth < max_depth:
                        room_queue.extendleft(
                            _RoomQueueEntry(
                                ev["state_key"], ev["content"]["via"], current_depth + 1
                            )
                            for ev in reversed(room_entry.children)
                        )

                processed_rooms.add(room_id)
            else:
                # TODO Federation.
                pass

        result: JsonDict = {"rooms": rooms_result}

        # If there's additional data, generate a pagination token (and persist state).
        if room_queue:
            next_batch = random_string(24)
            result["next_batch"] = next_batch
            pagination_key = _PaginationKey(
                requested_room_id, suggested_only, max_depth, next_batch
            )
            self._pagination_sessions[pagination_key] = _PaginationSession(
                self._clock.time_msec(), room_queue, processed_rooms
            )

        return result

    async def federation_space_summary(
        self,
        origin: str,
        room_id: str,
        suggested_only: bool,
        max_rooms_per_space: Optional[int],
        exclude_rooms: Iterable[str],
    ) -> JsonDict:
        """
        Implementation of the space summary Federation API

        Args:
            origin: The server requesting the spaces summary.

            room_id: room id to start the summary at

            suggested_only: whether we should only return children with the "suggested"
                flag set.

            max_rooms_per_space: an optional limit on the number of child rooms we will
                return. Unlike the C-S API, this applies to the root room (room_id).
                It is clipped to MAX_ROOMS_PER_SPACE.

            exclude_rooms: a list of rooms to skip over (presumably because the
                calling server has already seen them).

        Returns:
            summary dict to return
        """
        # the queue of rooms to process
        room_queue = deque((room_id,))

        # the set of rooms that we should not walk further. Initialise it with the
        # excluded-rooms list; we will add other rooms as we process them so that
        # we do not loop.
        processed_rooms: Set[str] = set(exclude_rooms)

        rooms_result: List[JsonDict] = []
        events_result: List[JsonDict] = []

        while room_queue and len(rooms_result) < MAX_ROOMS:
            room_id = room_queue.popleft()
            if room_id in processed_rooms:
                # already done this room
                continue

            room_entry = await self._summarize_local_room_hierarchy(
                None, origin, room_id, suggested_only, max_rooms_per_space
            )

            processed_rooms.add(room_id)

            if room_entry:
                rooms_result.append(room_entry.room)
                events_result.extend(room_entry.children)

                # add any children to the queue
                room_queue.extend(
                    edge_event["state_key"] for edge_event in room_entry.children
                )

        return {"rooms": rooms_result, "events": events_result}

    async def _summarize_local_room_hierarchy(
        self,
        requester: Optional[str],
        origin: Optional[str],
        room_id: str,
        suggested_only: bool,
        max_children: Optional[int],
    ) -> Optional["_RoomEntry"]:
        """
        Generate a room entry and a list of event entries for a given room.

        Args:
            requester:
                The user requesting the summary, if it is a local request. None
                if this is a federation request.
            origin:
                The server requesting the summary, if it is a federation request.
                None if this is a local request.
            room_id: The room ID to summarize.
            suggested_only: True if only suggested children should be returned.
                Otherwise, all children are returned.
            max_children:
                The maximum number of children rooms to include. This is capped
                to a server-set limit.

        Returns:
            A room entry if the room should be returned. None, otherwise.
        """
        try:
            room_entry = await self._summarize_local_room(requester, origin, room_id)
        except NotFoundError:
            return None

        # If the room is not a space, return just the room information.
        if room_entry.get("room_type") != RoomTypes.SPACE:
            return _RoomEntry(room_id, room_entry)

        # Otherwise, look for child rooms/spaces.
        child_events = await self._get_child_events(room_id)

        if suggested_only:
            # we only care about suggested children
            child_events = filter(_is_suggested_child_event, child_events)

        if max_children is None or max_children > MAX_ROOMS_PER_SPACE:
            max_children = MAX_ROOMS_PER_SPACE

        now = self._clock.time_msec()
        events_result: List[JsonDict] = []
        for edge_event in itertools.islice(child_events, max_children):
            events_result.append(
                await self._event_serializer.serialize_event(
                    edge_event,
                    time_now=now,
                    event_format=format_event_for_client_v2,
                )
            )

        return _RoomEntry(room_id, room_entry, events_result)

    async def _summarize_remote_room_hierarchy(
        self,
        room: "_RoomQueueEntry",
        suggested_only: bool,
        max_children: Optional[int],
        exclude_rooms: Iterable[str],
    ) -> Iterable["_RoomEntry"]:
        """
        Request room entries and a list of event entries for a given room by querying a remote server.

        Args:
            room: The room to summarize.
            suggested_only: True if only suggested children should be returned.
                Otherwise, all children are returned.
            max_children:
                The maximum number of children rooms to include. This is capped
                to a server-set limit.
            exclude_rooms:
                Rooms IDs which do not need to be summarized.

        Returns:
            An iterable of room entries.
        """
        room_id = room.room_id
        logger.info("Requesting summary for %s via %s", room_id, room.via)

        # we need to make the exclusion list json-serialisable
        exclude_rooms = list(exclude_rooms)

        via = itertools.islice(room.via, MAX_SERVERS_PER_SPACE)
        try:
            res = await self._federation_client.get_space_summary(
                via,
                room_id,
                suggested_only=suggested_only,
                max_rooms_per_space=max_children,
                exclude_rooms=exclude_rooms,
            )
        except Exception as e:
            logger.warning(
                "Unable to get summary of %s via federation: %s",
                room_id,
                e,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return ()

        # Group the events by their room.
        children_by_room: Dict[str, List[JsonDict]] = {}
        for ev in res.events:
            if ev.event_type == EventTypes.SpaceChild:
                children_by_room.setdefault(ev.room_id, []).append(ev.data)

        # Generate the final results.
        results = []
        for fed_room in res.rooms:
            fed_room_id = fed_room.get("room_id")
            if not fed_room_id or not isinstance(fed_room_id, str):
                continue

            results.append(
                _RoomEntry(
                    fed_room_id,
                    fed_room,
                    children_by_room.get(fed_room_id, []),
                )
            )

        return results

    async def _get_child_events(self, room_id: str) -> Iterable[EventBase]:
        """
        Get the child events for a given room.

        The returned results are sorted for stability.

        Args:
            room_id: The room id to get the children of.

        Returns:
            An iterable of sorted child events.
        """

        # look for child rooms/spaces.
        current_state_ids = await self._store.get_current_state_ids(room_id)

        events = await self._store.get_events_as_list(
            [
                event_id
                for key, event_id in current_state_ids.items()
                if key[0] == EventTypes.SpaceChild
            ]
        )

        # filter out any events without a "via" (which implies it has been redacted),
        # and order to ensure we return stable results.
        return sorted(filter(_has_valid_via, events), key=_child_events_comparison_key)

    async def get_room_summary(
        self,
        requester: Optional[str],
        room_id: str,
        remote_room_hosts: Optional[List[str]] = None,
    ) -> JsonDict:
        """
        Implementation of the room summary C-S API MSC3266

        Args:
            requester:  user id of the user making this request,
                can be None for unauthenticated requests

            room_id: room id to start the summary at

            remote_room_hosts: a list of homeservers to try fetching data through
                if we don't know it ourselves

        Returns:
            summary dict to return
        """
        is_in_room = await self._store.is_host_joined(room_id, self._server_name)

        if is_in_room:
            room_summary = await self._summarize_local_room(requester, None, room_id)

            if requester:
                (
                    membership,
                    _,
                ) = await self._store.get_local_current_membership_for_user_in_room(
                    requester, room_id
                )

                room_summary["membership"] = membership or "leave"
        else:
            room_summary = await self._summarize_remote_room(room_id, remote_room_hosts)

            # validate that the requester has permission to see this room
            include_room = self._is_remote_room_accessible(
                requester, room_id, room_summary
            )

            if not include_room:
                raise NotFoundError("Room not found or is not accessible")

        # Before returning to the client, remove the allowed_room_ids
        # and allowed_spaces keys.
        room_summary.pop("allowed_room_ids", None)
        room_summary.pop("allowed_spaces", None)

        return room_summary

    async def _build_room_entry(self, room_id: str, for_federation: bool) -> JsonDict:
        """
        Generate en entry suitable for the 'rooms' list in the summary response.

        Args:
            room_id: The room ID to summarize.
            for_federation: True if this is a summary requested over federation
                (which includes additional fields).

        Returns:
            The JSON dictionary for the room.
        """
        stats = await self._store.get_room_with_stats(room_id)

        # currently this should be impossible because we call
        # _is_local_room_accessible on the room before we get here, so
        # there should always be an entry
        assert stats is not None, "unable to retrieve stats for %s" % (room_id,)

        current_state_ids = await self._store.get_current_state_ids(room_id)
        create_event = await self._store.get_event(
            current_state_ids[(EventTypes.Create, "")]
        )

        entry = {
            "room_id": stats["room_id"],
            "name": stats["name"],
            "topic": stats["topic"],
            "canonical_alias": stats["canonical_alias"],
            "num_joined_members": stats["joined_members"],
            "avatar_url": stats["avatar"],
            "join_rules": stats["join_rules"],
            "world_readable": (
                stats["history_visibility"] == HistoryVisibility.WORLD_READABLE
            ),
            "guest_can_join": stats["guest_access"] == "can_join",
            "creation_ts": create_event.origin_server_ts,
            "room_type": create_event.content.get(EventContentFields.ROOM_TYPE),
        }

        # Federation requests need to provide additional information so the
        # requested server is able to filter the response appropriately.
        if for_federation:
            room_version = await self._store.get_room_version(room_id)
            if await self._event_auth_handler.has_restricted_join_rules(
                current_state_ids, room_version
            ):
                allowed_rooms = (
                    await self._event_auth_handler.get_rooms_that_allow_join(
                        current_state_ids
                    )
                )
                if allowed_rooms:
                    entry["allowed_room_ids"] = allowed_rooms
                    # TODO Remove this key once the API is stable.
                    entry["allowed_spaces"] = allowed_rooms

        # Filter out Nones – rather omit the field altogether
        room_entry = {k: v for k, v in entry.items() if v is not None}

        return room_entry

    async def _summarize_local_room(
        self,
        requester: Optional[str],
        origin: Optional[str],
        room_id: str,
    ) -> JsonDict:
        """
        Generate a room entry for a given room.

        Args:
            requester:
                The user requesting the summary, if it is a local request. None
                if this is a federation request.
            origin:
                The server requesting the summary, if it is a federation request.
                None if this is a local request.
            room_id: The room ID to summarize.

        Returns:
            room summary dict to return
        """
        if not await self._auth.is_room_visible(room_id, requester, origin):
            raise NotFoundError("Room not found or is not accessible")

        return await self._build_room_entry(room_id, for_federation=bool(origin))

    async def _summarize_remote_room(
        self,
        room_id: str,
        remote_room_hosts: Optional[List[str]],
    ) -> JsonDict:
        """
        Request room summary entry for a given room by querying a remote server.

        Args:
            room_id: The room to summarize.
            remote_room_hosts: List of homeservers to attempt to fetch the data from.

        Returns:
            summary dict to return
        """
        logger.info("Requesting summary for %s via %s", room_id, remote_room_hosts)

        # TODO federation API, descoped from initial unstable implementation as MSC needs more maturing on that side.
        raise NotFoundError("Room not found or is not accessible")


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _RoomQueueEntry:
    room_id: str
    via: Sequence[str]
    depth: int = 0


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _RoomEntry:
    room_id: str
    # The room summary for this room.
    room: JsonDict
    # An iterable of the sorted, stripped children events for children of this room.
    #
    # This may not include all children.
    children: Sequence[JsonDict] = ()

    def as_json(self) -> JsonDict:
        result = dict(self.room)
        result["children_state"] = self.children
        return result


def _has_valid_via(e: EventBase) -> bool:
    via = e.content.get("via")
    if not via or not isinstance(via, Sequence):
        return False
    for v in via:
        if not isinstance(v, str):
            logger.debug("Ignoring edge event %s with invalid via entry", e.event_id)
            return False
    return True


def _is_suggested_child_event(edge_event: EventBase) -> bool:
    suggested = edge_event.content.get("suggested")
    if isinstance(suggested, bool) and suggested:
        return True
    logger.debug("Ignorning not-suggested child %s", edge_event.state_key)
    return False


# Order may only contain characters in the range of \x20 (space) to \x7E (~) inclusive.
_INVALID_ORDER_CHARS_RE = re.compile(r"[^\x20-\x7E]")


def _child_events_comparison_key(child: EventBase) -> Tuple[bool, Optional[str], str]:
    """
    Generate a value for comparing two child events for ordering.

    The rules for ordering are supposed to be:

    1. The 'order' key, if it is valid.
    2. The 'origin_server_ts' of the 'm.room.create' event.
    3. The 'room_id'.

    But we skip step 2 since we may not have any state from the room.

    Args:
        child: The event for generating a comparison key.

    Returns:
        The comparison key as a tuple of:
            False if the ordering is valid.
            The ordering field.
            The room ID.
    """
    order = child.content.get("order")
    # If order is not a string or doesn't meet the requirements, ignore it.
    if not isinstance(order, str):
        order = None
    elif len(order) > 50 or _INVALID_ORDER_CHARS_RE.search(order):
        order = None

    # Items without an order come last.
    return (order is None, order, child.room_id)
