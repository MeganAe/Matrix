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

"""This module contains base REST classes for constructing client v1 servlets.
"""
import logging
import re

from twisted.internet import defer

from synapse.api.errors import InteractiveAuthIncompleteError
from synapse.api.urls import CLIENT_API_PREFIX

logger = logging.getLogger(__name__)


def client_v2_patterns(path_regex, releases=(0,),
                       unstable=True):
    """Creates a regex compiled client path with the correct client path
    prefix.

    Args:
        path_regex (str): The regex string to match. This should NOT have a ^
        as this will be prefixed.
    Returns:
        SRE_Pattern
    """
    patterns = []
    if unstable:
        unstable_prefix = CLIENT_API_PREFIX + "/unstable"
        patterns.append(re.compile("^" + unstable_prefix + path_regex))
    for release in releases:
        new_prefix = CLIENT_API_PREFIX + "/r%d" % (release,)
        patterns.append(re.compile("^" + new_prefix + path_regex))
    return patterns


def set_timeline_upper_limit(filter_json, filter_timeline_limit):
    if filter_timeline_limit < 0:
        return  # no upper limits
    timeline = filter_json.get('room', {}).get('timeline', {})
    if 'limit' in timeline:
        filter_json['room']['timeline']["limit"] = min(
            filter_json['room']['timeline']['limit'],
            filter_timeline_limit)


def interactive_auth_handler(orig):
    """Wraps an on_POST method to handle InteractiveAuthIncompleteErrors

    Takes a on_POST method which returns a deferred (errcode, body) response
    and adds exception handling to turn a InteractiveAuthIncompleteError into
    a 401 response.

    Normal usage is:

    @interactive_auth_handler
    @defer.inlineCallbacks
    def on_POST(self, request):
        # ...
        yield self.auth_handler.check_auth
            """
    def wrapped(*args, **kwargs):
        res = defer.maybeDeferred(orig, *args, **kwargs)
        res.addErrback(_catch_incomplete_interactive_auth)
        return res
    return wrapped


def _catch_incomplete_interactive_auth(f):
    """helper for interactive_auth_handler

    Catches InteractiveAuthIncompleteErrors and turns them into 401 responses

    Args:
        f (failure.Failure):
    """
    f.trap(InteractiveAuthIncompleteError)
    return 401, f.value.result
