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
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Union,
)

from synapse.api.presence import UserPresenceState
from synapse.util.async_helpers import maybe_awaitable

if TYPE_CHECKING:
    from synapse.server import HomeServer

GET_USERS_FOR_STATES = Callable[
    [Iterable[UserPresenceState]], Awaitable[Dict[str, Set[UserPresenceState]]]
]
GET_INTERESTED_USERS = Callable[[str], Awaitable[Union[Set[str], str]]]


def load_legacy_presence_router(hs: "HomeServer"):
    """Wrapper that loads a presence router module configured using the old
    configuration, and registers the hooks it implements.
    """

    if hs.config.presence_router_module_class is None:
        return

    module = hs.config.presence_router_module_class
    config = hs.config.presence_router_config
    api = hs.get_module_api()

    presence_router = module(config=config, module_api=api)

    # The known hooks. If a module implements a method which name appears in this set,
    # we'll want to register it.
    presence_router_methods = {
        "get_users_for_states",
        "get_interested_users",
    }

    # All methods that the module provides should be async, but this wasn't enforced
    # in the old module system, so we wrap them if needed
    def async_wrapper(f: Optional[Callable]) -> Optional[Callable[..., Awaitable]]:
        # f might be None if the callback isn't implemented by the module. In this
        # case we don't want to register a callback at all so we return None.
        if f is None:
            return None

        def run(*args, **kwargs):
            # mypy doesn't do well across function boundaries so we need to tell it
            # f is definitely not None.
            assert f is not None

            return maybe_awaitable(f(*args, **kwargs))

        return run

    # Register the hooks through the module API.
    hooks = {
        hook: async_wrapper(getattr(presence_router, hook, None))
        for hook in presence_router_methods
    }

    api.register_presence_router_callbacks(**hooks)


class PresenceRouter:
    """
    A module that the homeserver will call upon to help route user presence updates to
    additional destinations.
    """

    ALL_USERS = "ALL"

    def __init__(self, hs: "HomeServer"):
        # Initially there are no callbacks
        self._get_users_for_states_callbacks: List[GET_USERS_FOR_STATES] = []
        self._get_interested_users_callbacks: List[GET_INTERESTED_USERS] = []

    def register_presence_router_callbacks(
        self,
        get_users_for_states: Optional[GET_USERS_FOR_STATES] = None,
        get_interested_users: Optional[GET_INTERESTED_USERS] = None,
    ):
        # PresenceRouter modules are required to implement both of these methods
        # or neither of them as they are assumed to act in a complementary manner
        paired_methods = [get_users_for_states, get_interested_users]
        if paired_methods.count(None) == 1:
            raise Exception(
                "PresenceRouter modules must register neither or both of the paired callbacks: "
                "[get_users_for_states, get_interested_users]"
            )

        # Append the methods provided to the lists of callbacks
        if get_users_for_states is not None:
            self._get_users_for_states_callbacks.append(get_users_for_states)

        if get_interested_users is not None:
            self._get_interested_users_callbacks.append(get_interested_users)

    async def get_users_for_states(
        self,
        state_updates: Iterable[UserPresenceState],
    ) -> Dict[str, Set[UserPresenceState]]:
        """
        Given an iterable of user presence updates, determine where each one
        needs to go.

        Args:
            state_updates: An iterable of user presence state updates.

        Returns:
          A dictionary of user_id -> set of UserPresenceState, indicating which
          presence updates each user should receive.
        """

        # Bail out early without if we don't have any callbacks to run.
        if len(self._get_users_for_states_callbacks) == 0:
            # Don't include any extra destinations for presence updates
            return {}

        # If there are multiple callbacks for get_users_for_state then we want to
        # return all of the extra destinations, this method joins two sets of extra
        # destinations into one
        def combine(
            dict1: Dict[str, Set[UserPresenceState]],
            dict2: Dict[str, Set[UserPresenceState]],
        ) -> Dict[str, Set[UserPresenceState]]:
            for key, new_entries in dict2.items():
                old_entries = dict1.get(key, set())
                dict1[key] = old_entries.union(new_entries)

            return dict1

        users_for_states = {}
        # run all the callbacks for get_users_for_states and combine the results
        for callback in self._get_users_for_states_callbacks:
            users_for_states = combine(
                users_for_states, await callback(state_updates=state_updates)
            )

        return users_for_states

    async def get_interested_users(self, user_id: str) -> Union[Set[str], ALL_USERS]:
        """
        Retrieve a list of users that `user_id` is interested in receiving the
        presence of. This will be in addition to those they share a room with.
        Optionally, the object PresenceRouter.ALL_USERS can be returned to indicate
        that this user should receive all incoming local and remote presence updates.

        Note that this method will only be called for local users, but can return users
        that are local or remote.

        Args:
            user_id: A user requesting presence updates.

        Returns:
            A set of user IDs to return presence updates for, or ALL_USERS to return all
            known updates.
        """

        # Bail out early if we don't have any callbacks to run.
        if len(self._get_interested_users_callbacks) == 0:
            # Don't report any additional interested users
            return set()

        # If there are multiple callbacks for get_interested_users then we want to
        # return all of the users, this method joins two sets of users into one
        def combine(
            set1: Union[Set[str], str],
            set2: Union[Set[str], str],
        ) -> Union[Set[str], str]:
            # if one of the two sets is ALL_USERS then the union is also ALL_USERS
            if set1 == PresenceRouter.ALL_USERS or set2 == PresenceRouter.ALL_USERS:
                return PresenceRouter.ALL_USERS
            else:
                return set1.union(set2)

        interested_users = set()
        # run all the callbacks for get_interested_users and combine the results
        for callback in self._get_interested_users_callbacks:
            # Ask the custom module for interested users
            interested_users = combine(
                interested_users, await callback(user_id=user_id)
            )

        return interested_users
