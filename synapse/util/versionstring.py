# Copyright 2016 OpenMarket Ltd
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
import os
import subprocess

logger = logging.getLogger(__name__)


def get_version_string(module) -> str:
    """Given a module calculate a git-aware version string for it.

    If called on a module not in a git checkout will return `__verison__`.

    Args:
        module (module)

    Returns:
        str
    """

    cached_version = getattr(module, "_synapse_version_string_cache", None)
    if cached_version:
        return cached_version

    version_string = module.__version__

    try:
        null = open(os.devnull, "w")
        cwd = os.path.dirname(os.path.abspath(module.__file__))

        try:
            git_branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=null, cwd=cwd
                )
                .strip()
                .decode("ascii")
            )
            git_branch = "b=" + git_branch
        except (subprocess.CalledProcessError, FileNotFoundError):
            # FileNotFoundError can arise when git is not installed
            git_branch = ""

        try:
            git_tag = (
                subprocess.check_output(
                    ["git", "describe", "--exact-match"], stderr=null, cwd=cwd
                )
                .strip()
                .decode("ascii")
            )
            git_tag = "t=" + git_tag
        except (subprocess.CalledProcessError, FileNotFoundError):
            git_tag = ""

        try:
            git_commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], stderr=null, cwd=cwd
                )
                .strip()
                .decode("ascii")
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            git_commit = ""

        try:
            dirty_string = "-this_is_a_dirty_checkout"
            is_dirty = (
                subprocess.check_output(
                    ["git", "describe", "--dirty=" + dirty_string], stderr=null, cwd=cwd
                )
                .strip()
                .decode("ascii")
                .endswith(dirty_string)
            )

            git_dirty = "dirty" if is_dirty else ""
        except (subprocess.CalledProcessError, FileNotFoundError):
            git_dirty = ""

        if git_branch or git_tag or git_commit or git_dirty:
            git_version = ",".join(
                s for s in (git_branch, git_tag, git_commit, git_dirty) if s
            )

            version_string = "%s (%s)" % (module.__version__, git_version)
    except Exception as e:
        logger.info("Failed to check for git repository: %s", e)

    module._synapse_version_string_cache = version_string

    return version_string
