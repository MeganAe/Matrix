# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
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

# This file can't be called email.py because if it is, we cannot:
import email.utils

from ._base import Config


class PushConfig(Config):
    def read_config(self, config):
        self.push_redact_content = False

        push_config = config.get("email", {})
        self.push_redact_content = push_config.get("redact_content", False)

    def default_config(self, config_dir_path, server_name, **kwargs):
        return """
        # Control how push messages are sent to google/apple to notifications.
        # Normally every message is posted to a push server hosted by matrix.org
        # which is registered with google and apple in order to allow push
        # notifications to be sent to mobile devices.
        # Setting redact_content to true will make the push messages contain no
        # message content which will provide increased privacy.
        #
        #push:
        #   redact_content: false
        """
