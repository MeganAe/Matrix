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
from distutils.version import LooseVersion

logger = logging.getLogger(__name__)

REQUIREMENTS = {
    "frozendict>=0.4": ["frozendict"],
    "unpaddedbase64>=1.0.1": ["unpaddedbase64>=1.0.1"],
    "canonicaljson>=1.0.0": ["canonicaljson>=1.0.0"],
    "signedjson>=1.0.0": ["signedjson>=1.0.0"],
    "pynacl>=0.3.0": ["nacl>=0.3.0", "nacl.bindings"],
    "service_identity>=1.0.0": ["service_identity>=1.0.0"],
    "Twisted>=15.1.0": ["twisted>=15.1.0"],
    "pyopenssl>=0.14": ["OpenSSL>=0.14"],
    "pyyaml": ["yaml"],
    "pyasn1": ["pyasn1"],
    "daemonize": ["daemonize"],
    "py-bcrypt": ["bcrypt"],
    "pillow": ["PIL"],
    "pydenticon": ["pydenticon"],
    "ujson": ["ujson"],
    "blist": ["blist"],
    "pysaml2": ["saml2"],
    "pymacaroons-pynacl": ["pymacaroons"],
}
CONDITIONAL_REQUIREMENTS = {
    "web_client": {
        "matrix_angular_sdk>=0.6.6": ["syweb>=0.6.6"],
    }
}


def requirements(config=None, include_conditional=False):
    reqs = REQUIREMENTS.copy()
    if include_conditional:
        for _, req in CONDITIONAL_REQUIREMENTS.items():
            reqs.update(req)
    return reqs


def github_link(project, version, egg):
    return "https://github.com/%s/tarball/%s/#egg=%s" % (project, version, egg)

DEPENDENCY_LINKS = {
}


class MissingRequirementError(Exception):
    def __init__(self, message, module_name, dependency):
        super(MissingRequirementError, self).__init__(message)
        self.module_name = module_name
        self.dependency = dependency


def check_requirements(config=None):
    """Checks that all the modules needed by synapse have been correctly
    installed and are at the correct version"""
    for dependency, module_requirements in (
            requirements(config, include_conditional=False).items()):
        for module_requirement in module_requirements:
            if ">=" in module_requirement:
                module_name, required_version = module_requirement.split(">=")
                version_test = ">="
            elif "==" in module_requirement:
                module_name, required_version = module_requirement.split("==")
                version_test = "=="
            else:
                module_name = module_requirement
                version_test = None

            try:
                module = __import__(module_name)
            except ImportError:
                logging.exception(
                    "Can't import %r which is part of %r",
                    module_name, dependency
                )
                raise MissingRequirementError(
                    "Can't import %r which is part of %r"
                    % (module_name, dependency), module_name, dependency
                )
            version = getattr(module, "__version__", None)
            file_path = getattr(module, "__file__", None)
            logger.info(
                "Using %r version %r from %r to satisfy %r",
                module_name, version, file_path, dependency
            )

            if version_test == ">=":
                if version is None:
                    raise MissingRequirementError(
                        "Version of %r isn't set as __version__ of module %r"
                        % (dependency, module_name), module_name, dependency
                    )
                if LooseVersion(version) < LooseVersion(required_version):
                    raise MissingRequirementError(
                        "Version of %r in %r is too old. %r < %r"
                        % (dependency, file_path, version, required_version),
                        module_name, dependency
                    )
            elif version_test == "==":
                if version is None:
                    raise MissingRequirementError(
                        "Version of %r isn't set as __version__ of module %r"
                        % (dependency, module_name), module_name, dependency
                    )
                if LooseVersion(version) != LooseVersion(required_version):
                    raise MissingRequirementError(
                        "Unexpected version of %r in %r. %r != %r"
                        % (dependency, file_path, version, required_version),
                        module_name, dependency
                    )


def list_requirements():
    result = []
    linked = []
    for link in DEPENDENCY_LINKS.values():
        egg = link.split("#egg=")[1]
        linked.append(egg.split('-')[0])
        result.append(link)
    for requirement in requirements(include_conditional=True):
        is_linked = False
        for link in linked:
            if requirement.replace('-', '_').startswith(link):
                is_linked = True
        if not is_linked:
            result.append(requirement)
    return result

if __name__ == "__main__":
    import sys
    sys.stdout.writelines(req + "\n" for req in list_requirements())
