# Copyright 2021 Šimon Brandner <simon.bra.ag@gmail.com>
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


from typing import List

from synapse.api.constants import ReceiptTypes
from synapse.types import JsonDict

from tests import unittest


class ReceiptsTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor, clock, hs):
        self.event_source = hs.get_event_sources().sources.receipt

    def test_filters_out_private_receipt(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [],
        )

    def test_filters_out_private_receipt_and_ignores_rest(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def test_filters_out_event_with_only_private_receipts_and_ignores_the_rest(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            }
                        },
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def test_handles_missing_content_of_m_read(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419ggffg114394fHBLK:matrix.org": {ReceiptTypes.READ: {}},
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [
                {
                    "content": {
                        "$14356419ggffg114394fHBLK:matrix.org": {ReceiptTypes.READ: {}},
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def test_handles_empty_event(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$143564gdfg6114394fHBLK:matrix.org": {},
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def test_filters_out_receipt_event_with_only_private_receipt_and_ignores_rest(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                },
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                },
            ],
            [
                {
                    "content": {
                        "$1435641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@user:jki.re": {
                                    "ts": 1436451550453,
                                }
                            }
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def test_handles_string_data(self):
        """
        Tests that an invalid shape for read-receipts is handled.
        Context: https://github.com/matrix-org/synapse/issues/10603
        """

        self._test_filters_private(
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": "string",
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                },
            ],
            [
                {
                    "content": {
                        "$14356419edgd14394fHBLK:matrix.org": {
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": "string",
                            }
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                },
            ],
        )

    def test_leaves_our_private_and_their_public(self):
        self._test_filters_private(
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@me:server.org": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            "a.receipt.type": {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        },
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
            [
                {
                    "content": {
                        "$1dgdgrd5641916114394fHBLK:matrix.org": {
                            ReceiptTypes.READ_PRIVATE: {
                                "@me:server.org": {
                                    "ts": 1436451550453,
                                },
                            },
                            ReceiptTypes.READ: {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                            "a.receipt.type": {
                                "@rikj:jki.re": {
                                    "ts": 1436451550453,
                                },
                            },
                        }
                    },
                    "room_id": "!jEsUZKDJdhlrceRyVU:example.org",
                    "type": "m.receipt",
                }
            ],
        )

    def _test_filters_private(
        self, events: List[JsonDict], expected_output: List[JsonDict]
    ):
        """Tests that the _filter_out_private returns the expected output"""
        filtered_events = self.event_source.filter_out_private(events, "@me:server.org")
        self.assertEqual(filtered_events, expected_output)
