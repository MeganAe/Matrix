#!/usr/bin/env python
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

# This script reads environment variables and generates a shared Synapse worker,
# nginx and supervisord configs depending on the workers requested.
#
# The environment variables it reads are:
#   * SYNAPSE_SERVER_NAME: The desired server_name of the homeserver.
#   * SYNAPSE_REPORT_STATS: Whether to report stats.
#   * SYNAPSE_WORKER_TYPES: A comma separated list of worker names as specified in WORKERS_CONFIG
#         below. Leave empty for no workers. Add a ':' and a number at the end to
#         multiply that worker. Append multiple worker types with '+' to merge the
#         worker types into a single worker. Add a name and a '=' to the front of a
#         worker type to give this instance a name in logs and nginx.
#         Examples:
#         SYNAPSE_WORKER_TYPES='event_persister, federation_sender, client_reader'
#         SYNAPSE_WORKER_TYPES='event_persister:2, federation_sender:2, client_reader'
#         SYNAPSE_WORKER_TYPES='stream_writers=account_data+presence+typing'
#   * SYNAPSE_AS_REGISTRATION_DIR: If specified, a directory in which .yaml and .yml files
#         will be treated as Application Service registration files.
#   * SYNAPSE_TLS_CERT: Path to a TLS certificate in PEM format.
#   * SYNAPSE_TLS_KEY: Path to a TLS key. If this and SYNAPSE_TLS_CERT are specified,
#         Nginx will be configured to serve TLS on port 8448.
#   * SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER: Whether to use the forking launcher,
#         only intended for usage in Complement at the moment.
#         No stability guarantees are provided.
#   * SYNAPSE_LOG_LEVEL: Set this to DEBUG, INFO, WARNING or ERROR to change the
#         log level. INFO is the default.
#   * SYNAPSE_LOG_SENSITIVE: If unset, SQL and SQL values won't be logged,
#         regardless of the SYNAPSE_LOG_LEVEL setting.
#   * SYNAPSE_LOG_TESTING: if set, Synapse will log additional information useful
#     for testing.
#
# NOTE: According to Complement's ENTRYPOINT expectations for a homeserver image (as defined
# in the project's README), this script may be run multiple times, and functionality should
# continue to work if so.

import dataclasses
import os
import platform
import re
import subprocess
import sys
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    MutableMapping,
    NoReturn,
    Optional,
    Set,
    SupportsIndex,
)

import yaml
from jinja2 import Environment, FileSystemLoader

MAIN_PROCESS_HTTP_LISTENER_PORT = 8080
MAIN_PROCESS_INSTANCE_NAME = "main"
MAIN_PROCESS_LOCALHOST_ADDRESS = "127.0.0.1"
MAIN_PROCESS_REPLICATION_PORT = 9093
# Obviously, these would only be used with the UNIX socket option
MAIN_PROCESS_UNIX_SOCKET_PUBLIC_PATH = "/run/main_public.sock"
MAIN_PROCESS_UNIX_SOCKET_PRIVATE_PATH = "/run/main_private.sock"

# We place a file at this path to indicate that the script has already been
# run and should not be run again.
MARKER_FILE_PATH = "/conf/workers_have_been_configured"


@dataclass
class WorkerTemplate:
    """
    A definition of individual settings for a specific worker type.
    A worker name can be fed into the template in order to generate a config.

    These worker templates can be merged with `merge_worker_template_configs`
    in order for a single worker to be made from multiple templates.
    """

    listener_resources: Set[str] = field(default_factory=set)
    endpoint_patterns: Set[str] = field(default_factory=set)
    # (worker_name) -> {config}
    shared_extra_conf: Callable[[str], Dict[str, Any]] = lambda _worker_name: {}
    worker_extra_conf: str = ""

    # True if and only if multiple of this worker type are allowed.
    sharding_allowed: bool = True


# Workers with exposed endpoints needs either "client", "federation", or "media" listener_resources
# Watching /_matrix/client needs a "client" listener
# Watching /_matrix/federation needs a "federation" listener
# Watching /_matrix/media and related needs a "media" listener
# Stream Writers require "client" and "replication" listeners because they
#   have to attach by instance_map to the master process and have client endpoints.
WORKERS_CONFIG: Dict[str, WorkerTemplate] = {
    "pusher": WorkerTemplate(
        shared_extra_conf=lambda worker_name: {
            "pusher_instances": [worker_name],
        }
    ),
    "user_dir": WorkerTemplate(
        listener_resources={"client"},
        endpoint_patterns={
            "^/_matrix/client/(api/v1|r0|v3|unstable)/user_directory/search$"
        },
        shared_extra_conf=lambda worker_name: {
            "update_user_directory_from_worker": worker_name
        },
    ),
    "media_repository": WorkerTemplate(
        listener_resources={"media"},
        endpoint_patterns={
            "^/_matrix/media/",
            "^/_synapse/admin/v1/purge_media_cache$",
            "^/_synapse/admin/v1/room/.*/media.*$",
            "^/_synapse/admin/v1/user/.*/media.*$",
            "^/_synapse/admin/v1/media/.*$",
            "^/_synapse/admin/v1/quarantine_media/.*$",
        },
        # The first configured media worker will run the media background jobs
        shared_extra_conf=lambda worker_name: {
            "enable_media_repo": False,
            "media_instance_running_background_jobs": worker_name,
        },
        worker_extra_conf="enable_media_repo: true",
    ),
    "appservice": WorkerTemplate(
        shared_extra_conf=lambda worker_name: {
            "notify_appservices_from_worker": worker_name
        },
    ),
    "federation_sender": WorkerTemplate(
        shared_extra_conf=lambda worker_name: {
            "federation_sender_instances": [worker_name],
        }
    ),
    "synchrotron": WorkerTemplate(
        listener_resources={"client"},
        endpoint_patterns={
            "^/_matrix/client/(v2_alpha|r0|v3)/sync$",
            "^/_matrix/client/(api/v1|v2_alpha|r0|v3)/events$",
            "^/_matrix/client/(api/v1|r0|v3)/initialSync$",
            "^/_matrix/client/(api/v1|r0|v3)/rooms/[^/]+/initialSync$",
        },
    ),
    "client_reader": WorkerTemplate(
        listener_resources={"client"},
        endpoint_patterns={
            "^/_matrix/client/(api/v1|r0|v3|unstable)/publicRooms$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/joined_members$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/context/.*$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/members$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/state$",
            "^/_matrix/client/v1/rooms/.*/hierarchy$",
            "^/_matrix/client/(v1|unstable)/rooms/.*/relations/",
            "^/_matrix/client/v1/rooms/.*/threads$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/login$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/account/3pid$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/account/whoami$",
            "^/_matrix/client/versions$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/voip/turnServer$",
            "^/_matrix/client/(r0|v3|unstable)/register$",
            "^/_matrix/client/(r0|v3|unstable)/register/available$",
            "^/_matrix/client/(r0|v3|unstable)/auth/.*/fallback/web$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/messages$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/event",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/joined_rooms",
            "^/_matrix/client/(api/v1|r0|v3|unstable/.*)/rooms/.*/aliases",
            "^/_matrix/client/v1/rooms/.*/timestamp_to_event$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/search",
            "^/_matrix/client/(r0|v3|unstable)/user/.*/filter(/|$)",
            "^/_matrix/client/(r0|v3|unstable)/password_policy$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/directory/room/.*$",
            "^/_matrix/client/(r0|v3|unstable)/capabilities$",
            "^/_matrix/client/(r0|v3|unstable)/notifications$",
        },
    ),
    "federation_reader": WorkerTemplate(
        listener_resources={"federation"},
        endpoint_patterns={
            "^/_matrix/federation/(v1|v2)/event/",
            "^/_matrix/federation/(v1|v2)/state/",
            "^/_matrix/federation/(v1|v2)/state_ids/",
            "^/_matrix/federation/(v1|v2)/backfill/",
            "^/_matrix/federation/(v1|v2)/get_missing_events/",
            "^/_matrix/federation/(v1|v2)/publicRooms",
            "^/_matrix/federation/(v1|v2)/query/",
            "^/_matrix/federation/(v1|v2)/make_join/",
            "^/_matrix/federation/(v1|v2)/make_leave/",
            "^/_matrix/federation/(v1|v2)/send_join/",
            "^/_matrix/federation/(v1|v2)/send_leave/",
            "^/_matrix/federation/(v1|v2)/invite/",
            "^/_matrix/federation/(v1|v2)/query_auth/",
            "^/_matrix/federation/(v1|v2)/event_auth/",
            "^/_matrix/federation/v1/timestamp_to_event/",
            "^/_matrix/federation/(v1|v2)/exchange_third_party_invite/",
            "^/_matrix/federation/(v1|v2)/user/devices/",
            "^/_matrix/federation/(v1|v2)/get_groups_publicised$",
            "^/_matrix/key/v2/query",
        },
    ),
    "federation_inbound": WorkerTemplate(
        listener_resources={"federation"},
        endpoint_patterns={"/_matrix/federation/(v1|v2)/send/"},
    ),
    "event_persister": WorkerTemplate(
        listener_resources={"replication"},
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"events": [worker_name]}
        },
    ),
    "background_worker": WorkerTemplate(
        # This worker cannot be sharded. Therefore, there should only ever be one
        # background worker. This is enforced for the safety of your database.
        shared_extra_conf=lambda worker_name: {"run_background_tasks_on": worker_name},
        sharding_allowed=False,
    ),
    "event_creator": WorkerTemplate(
        listener_resources={"client"},
        endpoint_patterns={
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/redact",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/send",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/(join|invite|leave|ban|unban|kick)$",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/join/",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/knock/",
            "^/_matrix/client/(api/v1|r0|v3|unstable)/profile/",
        },
    ),
    "frontend_proxy": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={"^/_matrix/client/(api/v1|r0|v3|unstable)/keys/upload"},
    ),
    "account_data": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={
            "^/_matrix/client/(r0|v3|unstable)/.*/tags",
            "^/_matrix/client/(r0|v3|unstable)/.*/account_data",
        },
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"account_data": [worker_name]}
        },
        sharding_allowed=False,
    ),
    "presence": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={"^/_matrix/client/(api/v1|r0|v3|unstable)/presence/"},
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"presence": [worker_name]}
        },
        sharding_allowed=False,
    ),
    "receipts": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={
            "^/_matrix/client/(r0|v3|unstable)/rooms/.*/receipt",
            "^/_matrix/client/(r0|v3|unstable)/rooms/.*/read_markers",
        },
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"receipts": [worker_name]}
        },
        sharding_allowed=False,
    ),
    "to_device": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={"^/_matrix/client/(r0|v3|unstable)/sendToDevice/"},
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"to_device": [worker_name]}
        },
        sharding_allowed=False,
    ),
    "typing": WorkerTemplate(
        listener_resources={"client", "replication"},
        endpoint_patterns={"^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/typing"},
        shared_extra_conf=lambda worker_name: {
            "stream_writers": {"typing": [worker_name]}
        },
        sharding_allowed=False,
    ),
}

# Templates for sections that may be inserted multiple times in config files
NGINX_LOCATION_CONFIG_BLOCK = """
    location ~* {endpoint} {{
        proxy_pass {upstream};
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $host;
    }}
"""

NGINX_UPSTREAM_CONFIG_BLOCK = """
upstream {upstream_worker_base_name} {{
{body}
}}
"""


# Utility functions
def log(txt: str) -> None:
    print(txt)


def error(txt: str) -> NoReturn:
    print(txt, file=sys.stderr)
    sys.exit(2)


def flush_buffers() -> None:
    sys.stdout.flush()
    sys.stderr.flush()


def merge_into(dest: Any, new: Any) -> None:
    """
    Merges `new` into `dest` with the following rules:

    - dicts: values with the same key will be merged recursively
    - lists: `new` will be appended to `dest`
    - primitives: they will be checked for equality and inequality will result
        in a ValueError

    It is an error for `dest` and `new` to be of different types.
    """
    if isinstance(dest, dict) and isinstance(new, dict):
        for k, v in new.items():
            if k in dest:
                merge_into(dest[k], v)
            else:
                dest[k] = v
    elif isinstance(dest, list) and isinstance(new, list):
        dest.extend(new)
    elif type(dest) != type(new):
        raise TypeError(f"Cannot merge {type(dest).__name__} and {type(new).__name__}")
    elif dest != new:
        raise ValueError(f"Cannot merge primitive values: {dest!r} != {new!r}")


def merged(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merges `b` into `a` and returns `a`. Here because we can't use `merge_into`
    in a lamba conveniently.
    """
    merge_into(a, b)
    return a


def convert(src: str, dst: str, **template_vars: object) -> None:
    """Generate a file from a template

    Args:
        src: Path to the input file.
        dst: Path to write to.
        template_vars: The arguments to replace placeholder variables in the template with.
    """
    # Read the template file
    # We disable autoescape to prevent template variables from being escaped,
    # as we're not using HTML.
    env = Environment(loader=FileSystemLoader(os.path.dirname(src)), autoescape=False)
    template = env.get_template(os.path.basename(src))

    # Generate a string from the template.
    rendered = template.render(**template_vars)

    # Write the generated contents to a file
    #
    # We use append mode in case the files have already been written to by something else
    # (for instance, as part of the instructions in a dockerfile).
    with open(dst, "a") as outfile:
        # In case the existing file doesn't end with a newline
        outfile.write("\n")

        outfile.write(rendered)


def add_worker_to_instance_map(
    shared_config: dict,
    worker_name: str,
    worker_port: int,
) -> None:
    """
    Update the shared config map to add the worker in the instance_map.

    Args:
        shared_config: The config dict that all worker instances share (after being
            converted to YAML)
        worker_name: The name of the worker instance.
        worker_port: The HTTP replication port that the worker instance is listening on.
    """
    instance_map = shared_config.setdefault("instance_map", {})

    if os.environ.get("SYNAPSE_USE_UNIX_SOCKET", False):
        instance_map[worker_name] = {
            "path": f"/run/worker.{worker_port}",
        }
    else:
        instance_map[worker_name] = {
            "host": "localhost",
            "port": worker_port,
        }


def merge_worker_template_configs(
    left: WorkerTemplate,
    right: WorkerTemplate,
) -> WorkerTemplate:
    """Merges two templates together, returning a new template that includes
    the listeners, endpoint patterns and configuration from both.

    Does not mutate the input templates.
    """

    return WorkerTemplate(
        # include listener resources from both
        listener_resources=left.listener_resources | right.listener_resources,
        # include endpoint patterns from both
        endpoint_patterns=left.endpoint_patterns | right.endpoint_patterns,
        # merge shared config dictionaries; the worker name will be replaced later
        shared_extra_conf=lambda worker_name: merged(
            left.shared_extra_conf(worker_name),
            right.shared_extra_conf(worker_name),
        ),
        # There is only one worker type that has a 'worker_extra_conf' and it is
        # the media_repo. Since duplicate worker types on the same worker don't
        # work, this is fine.
        worker_extra_conf=(left.worker_extra_conf + right.worker_extra_conf),
        # (This is unused, but in principle sharding this hybrid worker type
        # would be allowed if both constituent types are shardable)
        sharding_allowed=left.sharding_allowed and right.sharding_allowed,
    )


def instantiate_worker_template(
    template: WorkerTemplate, worker_name: str
) -> Dict[str, Any]:
    """Given a worker template, instantiate it into a worker configuration
    (which is currently represented as a dictionary).

    Args:
        template: The WorkerTemplate to template
        worker_name: The name of the worker to use.
    Returns: worker configuration dictionary
    """
    worker_config_dict = dataclasses.asdict(template)
    worker_config_dict["shared_extra_conf"] = template.shared_extra_conf(worker_name)
    worker_config_dict["endpoint_patterns"] = sorted(template.endpoint_patterns)
    worker_config_dict["listener_resources"] = sorted(template.listener_resources)
    return worker_config_dict


def apply_requested_multiplier_for_worker(worker_types: List[str]) -> List[str]:
    """
    Apply multiplier(if found) by returning a new expanded list with some basic error
    checking.

    Args:
        worker_types: The unprocessed List of requested workers
    Returns:
        A new list with all requested workers expanded.
    """
    # Checking performed:
    # 1. if worker:2 or more is declared, it will create additional workers up to number
    # 2. if worker:1, it will create a single copy of this worker as if no number was
    #   given
    # 3. if worker:0 is declared, this worker will be ignored. This is to allow for
    #   scripting and automated expansion and is intended behaviour.
    # 4. if worker:NaN or is a negative number, it will error and log it.
    new_worker_types = []
    for worker_type in worker_types:
        if ":" in worker_type:
            worker_type_components = split_and_strip_string(worker_type, ":", 1)
            worker_count = 0
            # Should only be 2 components, a type of worker(s) and an integer as a
            # string. Cast the number as an int then it can be used as a counter.
            try:
                worker_count = int(worker_type_components[1])
            except ValueError:
                error(
                    f"Bad number in worker count for '{worker_type}': "
                    f"'{worker_type_components[1]}' is not an integer"
                )

            # As long as there are more than 0, we add one to the list to make below.
            for _ in range(worker_count):
                new_worker_types.append(worker_type_components[0])

        else:
            # If it's not a real worker_type, it will error out later.
            new_worker_types.append(worker_type)
    return new_worker_types


def split_and_strip_string(
    given_string: str, split_char: str, max_split: SupportsIndex = -1
) -> List[str]:
    """
    Helper to split a string on split_char and strip whitespace from each end of each
        element.
    Args:
        given_string: The string to split
        split_char: The character to split the string on
        max_split: kwarg for split() to limit how many times the split() happens
    Returns:
        A List of strings
    """
    # Removes whitespace from ends of result strings before adding to list. Allow for
    # overriding 'maxsplit' kwarg, default being -1 to signify no maximum.
    return [x.strip() for x in given_string.split(split_char, maxsplit=max_split)]


def generate_base_homeserver_config() -> None:
    """Starts Synapse and generates a basic homeserver config, which will later be
    modified for worker support.

    Raises: CalledProcessError if calling start.py returned a non-zero exit code.
    """
    # start.py already does this for us, so just call that.
    # note that this script is copied in in the official, monolith dockerfile
    os.environ["SYNAPSE_HTTP_PORT"] = str(MAIN_PROCESS_HTTP_LISTENER_PORT)
    subprocess.run(["/usr/local/bin/python", "/start.py", "migrate_config"], check=True)


def parse_worker_types(
    requested_worker_types: List[str],
) -> Dict[str, Set[str]]:
    """Read the desired list of requested workers and prepare the data for use in
        generating worker config files while also checking for potential gotchas.

    Args:
        requested_worker_types: The list formed from the split environment variable
            containing the unprocessed requests for workers.

    Returns: A dict of worker names to set of worker types. Format:
        {'worker_name':
            {'worker_type', 'worker_type2'}
        }
    """
    # A counter of worker_base_name -> int. Used for determining the name for a given
    # worker when generating its config file, as each worker's name is just
    # worker_base_name followed by instance number
    worker_base_name_counter: Dict[str, int] = defaultdict(int)

    # Similar to above, but more finely grained. This is used to determine we don't have
    # more than a single worker for cases where multiples would be bad(e.g. presence).
    worker_type_shard_counter: Dict[str, int] = defaultdict(int)

    # The final result of all this processing
    dict_to_return: Dict[str, Set[str]] = {}

    # Handle any multipliers requested for given workers.
    multiple_processed_worker_types = apply_requested_multiplier_for_worker(
        requested_worker_types
    )

    # Process each worker_type_string
    # Examples of expected formats:
    #  - requested_name=type1+type2+type3
    #  - synchrotron
    #  - event_creator+event_persister
    for worker_type_string in multiple_processed_worker_types:
        # First, if a name is requested, use that — otherwise generate one.
        worker_base_name: str = ""
        if "=" in worker_type_string:
            # Split on "=", remove extra whitespace from ends then make list
            worker_type_split = split_and_strip_string(worker_type_string, "=")
            if len(worker_type_split) > 2:
                error(
                    "There should only be one '=' in the worker type string. "
                    f"Please fix: {worker_type_string}"
                )

            # Assign the name
            worker_base_name = worker_type_split[0]

            if not re.match(r"^[a-zA-Z0-9_+-]*[a-zA-Z_+-]$", worker_base_name):
                # Apply a fairly narrow regex to the worker names. Some characters
                # aren't safe for use in file paths or nginx configurations.
                # Don't allow to end with a number because we'll add a number
                # ourselves in a moment.
                error(
                    "Invalid worker name; please choose a name consisting of "
                    "alphanumeric letters, _ + -, but not ending with a digit: "
                    f"{worker_base_name!r}"
                )

            # Continue processing the remainder of the worker_type string
            # with the name override removed.
            worker_type_string = worker_type_split[1]

        # Split the worker_type_string on "+", remove whitespace from ends then make
        # the list a set so it's deduplicated.
        worker_types_set: Set[str] = set(
            split_and_strip_string(worker_type_string, "+")
        )

        if not worker_base_name:
            # No base name specified: generate one deterministically from set of
            # types
            worker_base_name = "+".join(sorted(worker_types_set))

        # At this point, we have:
        #   worker_base_name which is the name for the worker, without counter.
        #   worker_types_set which is the set of worker types for this worker.

        # Validate worker_type and make sure we don't allow sharding for a worker type
        # that doesn't support it. Will error and stop if it is a problem,
        # e.g. 'background_worker'.
        for worker_type in worker_types_set:
            # Verify this is a real defined worker type. If it's not, stop everything so
            # it can be fixed.
            if worker_type not in WORKERS_CONFIG:
                error(
                    f"{worker_type} is an unknown worker type! Was found in "
                    f"'{worker_type_string}'. Please fix!"
                )

            if worker_type in worker_type_shard_counter:
                if not WORKERS_CONFIG[worker_type].sharding_allowed:
                    error(
                        f"There can be only a single worker with {worker_type} "
                        "type. Please recount and remove."
                    )
            # Not in shard counter, must not have seen it yet, add it.
            worker_type_shard_counter[worker_type] += 1

        # Generate the number for the worker using incrementing counter
        worker_base_name_counter[worker_base_name] += 1
        worker_number = worker_base_name_counter[worker_base_name]
        worker_name = f"{worker_base_name}{worker_number}"

        if worker_number > 1:
            # If this isn't the first worker, check that we don't have a confusing
            # mixture of worker types with the same base name.
            first_worker_with_base_name = dict_to_return[f"{worker_base_name}1"]
            if first_worker_with_base_name != worker_types_set:
                error(
                    f"Can not use worker_name: '{worker_name}' for worker_type(s): "
                    f"{worker_types_set!r}. It is already in use by "
                    f"worker_type(s): {first_worker_with_base_name!r}"
                )

        dict_to_return[worker_name] = worker_types_set

    return dict_to_return


def generate_worker_files(
    environ: Mapping[str, str],
    config_path: str,
    data_dir: str,
    requested_worker_types: Dict[str, Set[str]],
) -> None:
    """Read the desired workers(if any) that is passed in and generate shared
        homeserver, nginx and supervisord configs.

    Args:
        environ: os.environ instance.
        config_path: The location of the generated Synapse main worker config file.
        data_dir: The location of the synapse data directory. Where log and
            user-facing config files live.
        requested_worker_types: A Dict containing requested workers in the format of
            {'worker_name1': {'worker_type', ...}}
    """
    # Note that yaml cares about indentation, so care should be taken to insert lines
    # into files at the correct indentation below.

    # Convenience helper for if using unix sockets instead of host:port
    using_unix_sockets = environ.get("SYNAPSE_USE_UNIX_SOCKET", False)
    # First read the original config file and extract the listeners block. Then we'll
    # add another listener for replication. Later we'll write out the result to the
    # shared config file.
    listeners: List[Any]
    if using_unix_sockets:
        listeners = [
            {
                "path": MAIN_PROCESS_UNIX_SOCKET_PRIVATE_PATH,
                "type": "http",
                "resources": [{"names": ["replication"]}],
            }
        ]
    else:
        listeners = [
            {
                "port": MAIN_PROCESS_REPLICATION_PORT,
                "bind_address": MAIN_PROCESS_LOCALHOST_ADDRESS,
                "type": "http",
                "resources": [{"names": ["replication"]}],
            }
        ]
    with open(config_path) as file_stream:
        original_config = yaml.safe_load(file_stream)
        original_listeners = original_config.get("listeners")
        if original_listeners:
            listeners += original_listeners

    # The shared homeserver config. The contents of which will be inserted into the
    # base shared worker jinja2 template. This config file will be passed to all
    # workers, included Synapse's main process. It is intended mainly for disabling
    # functionality when certain workers are spun up, and adding a replication listener.
    shared_config: Dict[str, Any] = {"listeners": listeners}

    # List of dicts that describe workers.
    # We pass this to the Supervisor template later to generate the appropriate
    # program blocks.
    worker_descriptors: List[Dict[str, Any]] = []

    # Upstreams for load-balancing purposes. This dict takes the form of the worker
    # type to the ports of each worker. For example:
    # {
    #   worker_type: {1234, 1235, ...}}
    # }
    # and will be used to construct 'upstream' nginx directives.
    nginx_upstreams: Dict[str, Set[int]] = {}

    # A map of: {"endpoint": "upstream"}, where "upstream" is a str representing what
    # will be placed after the proxy_pass directive. The main benefit to representing
    # this data as a dict over a str is that we can easily deduplicate endpoints
    # across multiple instances of the same worker. The final rendering will be combined
    # with nginx_upstreams and placed in /etc/nginx/conf.d.
    nginx_locations: Dict[str, str] = {}

    # Create the worker configuration directory if it doesn't already exist
    os.makedirs("/conf/workers", exist_ok=True)

    # Start worker ports from this arbitrary port
    worker_port = 18009

    # A list of internal endpoints to healthcheck, starting with the main process
    # which exists even if no workers do.
    # This list ends up being part of the command line to curl, (curl added support for
    # Unix sockets in version 7.40).
    if using_unix_sockets:
        healthcheck_urls = [
            f"--unix-socket {MAIN_PROCESS_UNIX_SOCKET_PUBLIC_PATH} "
            # The scheme and hostname from the following URL are ignored.
            # The only thing that matters is the path `/health`
            "http://localhost/health"
        ]
    else:
        healthcheck_urls = ["http://localhost:8080/health"]

    # Get the set of all worker types that we have configured
    all_worker_types_in_use = set(chain(*requested_worker_types.values()))
    # Map locations to upstreams (corresponding to worker types) in Nginx
    # but only if we use the appropriate worker type
    for worker_type in all_worker_types_in_use:
        for endpoint_pattern in sorted(WORKERS_CONFIG[worker_type].endpoint_patterns):
            nginx_locations[endpoint_pattern] = f"http://{worker_type}"

    # For each worker type specified by the user, create config values and write it's
    # yaml config file
    for worker_name, worker_types_set in requested_worker_types.items():
        # The collected and processed data will live here.
        worker_template: WorkerTemplate = WorkerTemplate()

        # Merge all worker config templates for this worker into a single config
        for worker_type in worker_types_set:
            # Merge worker type template configuration data. It's a combination of lists
            # and dicts, so use this helper.
            worker_template = merge_worker_template_configs(
                worker_template, WORKERS_CONFIG[worker_type]
            )

        # Replace placeholder names in the config template with the actual worker name.
        worker_config: Dict[str, Any] = instantiate_worker_template(
            worker_template, worker_name
        )

        worker_config.update(
            {"name": worker_name, "port": str(worker_port), "config_path": config_path}
        )

        # Update the shared config with any options needed to enable this worker.
        merge_into(shared_config, worker_config["shared_extra_conf"])

        if using_unix_sockets:
            healthcheck_urls.append(
                f"--unix-socket /run/worker.{worker_port} http://localhost/health"
            )
        else:
            healthcheck_urls.append("http://localhost:%d/health" % (worker_port,))

        # Add all workers to the `instance_map`
        # Technically only certain types of workers, such as stream writers, are needed
        # here but it is simpler just to be consistent.
        add_worker_to_instance_map(shared_config, worker_name, worker_port)

        # Enable the worker in supervisord
        worker_descriptors.append(worker_config)

        # Write out the worker's logging config file
        log_config_filepath = generate_worker_log_config(environ, worker_name, data_dir)

        # Then a worker config file
        convert(
            "/conf/worker.yaml.j2",
            f"/conf/workers/{worker_name}.yaml",
            **worker_config,
            worker_log_config_filepath=log_config_filepath,
            using_unix_sockets=using_unix_sockets,
        )

        # Save this worker's port number to the correct nginx upstreams
        for worker_type in worker_types_set:
            nginx_upstreams.setdefault(worker_type, set()).add(worker_port)

        worker_port += 1

    # Build the nginx location config blocks
    nginx_location_config = ""
    for endpoint, upstream in nginx_locations.items():
        nginx_location_config += NGINX_LOCATION_CONFIG_BLOCK.format(
            endpoint=endpoint,
            upstream=upstream,
        )

    # Determine the load-balancing upstreams to configure
    nginx_upstream_config = ""
    for upstream_worker_base_name, upstream_worker_ports in nginx_upstreams.items():
        body = ""
        if using_unix_sockets:
            for port in upstream_worker_ports:
                body += f"    server unix:/run/worker.{port};\n"

        else:
            for port in upstream_worker_ports:
                body += f"    server localhost:{port};\n"

        # Add to the list of configured upstreams
        nginx_upstream_config += NGINX_UPSTREAM_CONFIG_BLOCK.format(
            upstream_worker_base_name=upstream_worker_base_name,
            body=body,
        )

    # Finally, we'll write out the config files.

    # log config for the master process
    master_log_config = generate_worker_log_config(environ, "master", data_dir)
    shared_config["log_config"] = master_log_config

    # Find application service registrations
    appservice_registrations = None
    appservice_registration_dir = os.environ.get("SYNAPSE_AS_REGISTRATION_DIR")
    if appservice_registration_dir:
        # Scan for all YAML files that should be application service registrations.
        appservice_registrations = [
            str(reg_path.resolve())
            for reg_path in Path(appservice_registration_dir).iterdir()
            if reg_path.suffix.lower() in (".yaml", ".yml")
        ]

    workers_in_use = len(requested_worker_types) > 0

    # If there are workers, add the main process to the instance_map too.
    if workers_in_use:
        instance_map = shared_config.setdefault("instance_map", {})
        if using_unix_sockets:
            instance_map[MAIN_PROCESS_INSTANCE_NAME] = {
                "path": MAIN_PROCESS_UNIX_SOCKET_PRIVATE_PATH,
            }
        else:
            instance_map[MAIN_PROCESS_INSTANCE_NAME] = {
                "host": MAIN_PROCESS_LOCALHOST_ADDRESS,
                "port": MAIN_PROCESS_REPLICATION_PORT,
            }

    # Shared homeserver config
    convert(
        "/conf/shared.yaml.j2",
        "/conf/workers/shared.yaml",
        shared_worker_config=yaml.dump(shared_config),
        appservice_registrations=appservice_registrations,
        enable_redis=workers_in_use,
        workers_in_use=workers_in_use,
        using_unix_sockets=using_unix_sockets,
    )

    # Nginx config
    convert(
        "/conf/nginx.conf.j2",
        "/etc/nginx/conf.d/matrix-synapse.conf",
        worker_locations=nginx_location_config,
        upstream_directives=nginx_upstream_config,
        tls_cert_path=os.environ.get("SYNAPSE_TLS_CERT"),
        tls_key_path=os.environ.get("SYNAPSE_TLS_KEY"),
        using_unix_sockets=using_unix_sockets,
    )

    # Supervisord config
    os.makedirs("/etc/supervisor", exist_ok=True)
    convert(
        "/conf/supervisord.conf.j2",
        "/etc/supervisor/supervisord.conf",
        main_config_path=config_path,
        enable_redis=workers_in_use,
        using_unix_sockets=using_unix_sockets,
    )

    convert(
        "/conf/synapse.supervisord.conf.j2",
        "/etc/supervisor/conf.d/synapse.conf",
        workers=worker_descriptors,
        main_config_path=config_path,
        use_forking_launcher=environ.get("SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER"),
    )

    # healthcheck config
    convert(
        "/conf/healthcheck.sh.j2",
        "/healthcheck.sh",
        healthcheck_urls=healthcheck_urls,
    )

    # Ensure the logging directory exists
    log_dir = data_dir + "/logs"
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)


def generate_worker_log_config(
    environ: Mapping[str, str], worker_name: str, data_dir: str
) -> str:
    """Generate a log.config file for the given worker.

    Returns: the path to the generated file
    """
    # Check whether we should write worker logs to disk, in addition to the console
    extra_log_template_args: Dict[str, Optional[str]] = {}
    if environ.get("SYNAPSE_WORKERS_WRITE_LOGS_TO_DISK"):
        extra_log_template_args["LOG_FILE_PATH"] = f"{data_dir}/logs/{worker_name}.log"

    extra_log_template_args["SYNAPSE_LOG_LEVEL"] = environ.get("SYNAPSE_LOG_LEVEL")
    extra_log_template_args["SYNAPSE_LOG_SENSITIVE"] = environ.get(
        "SYNAPSE_LOG_SENSITIVE"
    )
    extra_log_template_args["SYNAPSE_LOG_TESTING"] = environ.get("SYNAPSE_LOG_TESTING")

    # Render and write the file
    log_config_filepath = f"/conf/workers/{worker_name}.log.config"
    convert(
        "/conf/log.config",
        log_config_filepath,
        worker_name=worker_name,
        **extra_log_template_args,
        include_worker_name_in_log_line=environ.get(
            "SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER"
        ),
    )
    return log_config_filepath


def main(args: List[str], environ: MutableMapping[str, str]) -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Only generate configuration; don't run Synapse.",
    )
    opts = parser.parse_args(args)

    config_dir = environ.get("SYNAPSE_CONFIG_DIR", "/data")
    config_path = environ.get("SYNAPSE_CONFIG_PATH", config_dir + "/homeserver.yaml")
    data_dir = environ.get("SYNAPSE_DATA_DIR", "/data")

    # override SYNAPSE_NO_TLS, we don't support TLS in worker mode,
    # this needs to be handled by a frontend proxy
    environ["SYNAPSE_NO_TLS"] = "yes"

    # Generate the base homeserver config if one does not yet exist
    if not os.path.exists(config_path):
        log("Generating base homeserver config")
        generate_base_homeserver_config()
    else:
        log("Base homeserver config exists—not regenerating")
    # This script may be run multiple times (mostly by Complement, see note at top of
    # file). Don't re-configure workers in this instance.

    if not os.path.exists(MARKER_FILE_PATH):
        # Collect and validate worker_type requests
        # Read the desired worker configuration from the environment
        worker_types_env = environ.get("SYNAPSE_WORKER_TYPES", "").strip()
        # Only process worker_types if they exist
        if not worker_types_env:
            # No workers, just the main process
            worker_types = []
            requested_worker_types: Dict[str, Any] = {}
        else:
            # Split type names by comma, ignoring whitespace.
            worker_types = split_and_strip_string(worker_types_env, ",")
            requested_worker_types = parse_worker_types(worker_types)

        # Always regenerate all other config files
        log("Generating worker config files")
        generate_worker_files(environ, config_path, data_dir, requested_worker_types)

        # Mark workers as being configured
        with open(MARKER_FILE_PATH, "w") as f:
            f.write("")
    else:
        log("Worker config exists—not regenerating")

    if opts.generate_only:
        log("--generate-only: won't run Synapse")
        return

    # Lifted right out of start.py
    jemallocpath = "/usr/lib/%s-linux-gnu/libjemalloc.so.2" % (platform.machine(),)

    if os.path.isfile(jemallocpath):
        environ["LD_PRELOAD"] = jemallocpath
    else:
        log("Could not find %s, will not use" % (jemallocpath,))

    # Start supervisord, which will start Synapse, all of the configured worker
    # processes, redis, nginx etc. according to the config we created above.
    log("Starting supervisord")
    flush_buffers()
    os.execle(
        "/usr/local/bin/supervisord",
        "supervisord",
        "-c",
        "/etc/supervisor/supervisord.conf",
        environ,
    )


if __name__ == "__main__":
    main(sys.argv[1:], os.environ)
