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


import json
from typing import Dict
from unittest.mock import ANY, Mock, call

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor
from twisted.web.resource import Resource

from synapse.api.constants import EduTypes
from synapse.api.errors import AuthError
from synapse.federation.transport.server import TransportLayerServer
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util import Clock

from tests import unittest
from tests.test_utils import make_awaitable
from tests.unittest import override_config

# Some local users to test with
U_APPLE = UserID.from_string("@apple:test")
U_BANANA = UserID.from_string("@banana:test")

# Remote user
U_ONION = UserID.from_string("@onion:farm")

# Test room id
ROOM_ID = "a-room"

# Room we're not in
OTHER_ROOM_ID = "another-room"


def _expect_edu_transaction(
    edu_type: str, content: JsonDict, origin: str = "test"
) -> JsonDict:
    return {
        "origin": origin,
        "origin_server_ts": 1000000,
        "pdus": [],
        "edus": [{"edu_type": edu_type, "content": content}],
    }


def _make_edu_transaction_json(edu_type: str, content: JsonDict) -> bytes:
    return json.dumps(_expect_edu_transaction(edu_type, content)).encode("utf8")


class TypingNotificationsTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # we mock out the keyring so as to skip the authentication check on the
        # federation API call.
        mock_keyring = Mock(spec=["verify_json_for_server"])
        mock_keyring.verify_json_for_server.return_value = make_awaitable(True)

        # we mock out the federation client too
        mock_federation_client = Mock(spec=["put_json"])
        mock_federation_client.put_json.return_value = make_awaitable((200, "OK"))

        # the tests assume that we are starting at unix time 1000
        reactor.pump((1000,))

        hs = self.setup_test_homeserver(
            notifier=Mock(),
            federation_http_client=mock_federation_client,
            keyring=mock_keyring,
            replication_streams={},
        )

        return hs

    def create_resource_dict(self) -> Dict[str, Resource]:
        d = super().create_resource_dict()
        d["/_matrix/federation"] = TransportLayerServer(self.hs)
        return d

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        mock_notifier = hs.get_notifier()
        self.on_new_event = mock_notifier.on_new_event

        self.handler = hs.get_typing_handler()

        self.event_source = hs.get_event_sources().sources.typing

        self.datastore = hs.get_datastores().main
        self.datastore.get_destination_retry_timings = Mock(
            return_value=make_awaitable(None)
        )

        self.datastore.get_device_updates_by_remote = Mock(
            return_value=make_awaitable((0, []))
        )

        self.datastore.get_destination_last_successful_stream_ordering = Mock(
            return_value=make_awaitable(None)
        )

        def get_received_txn_response(*args):
            return defer.succeed(None)

        self.datastore.get_received_txn_response = get_received_txn_response

        self.room_members = []

        async def check_user_in_room(room_id: str, user_id: str) -> None:
            if user_id not in [u.to_string() for u in self.room_members]:
                raise AuthError(401, "User is not in the room")
            return None

        hs.get_auth().check_user_in_room = check_user_in_room

        async def check_host_in_room(room_id: str, server_name: str) -> bool:
            return room_id == ROOM_ID

        hs.get_event_auth_handler().check_host_in_room = check_host_in_room

        def get_joined_hosts_for_room(room_id: str):
            return {member.domain for member in self.room_members}

        self.datastore.get_joined_hosts_for_room = get_joined_hosts_for_room

        async def get_users_in_room(room_id: str):
            return {str(u) for u in self.room_members}

        self.datastore.get_users_in_room = get_users_in_room

        self.datastore.get_user_directory_stream_pos = Mock(
            side_effect=(
                # we deliberately return a non-None stream pos to avoid doing an initial_spam
                lambda: make_awaitable(1)
            )
        )

        self.datastore.get_partial_current_state_deltas = Mock(return_value=(0, None))

        self.datastore.get_to_device_stream_token = lambda: 0
        self.datastore.get_new_device_msgs_for_remote = (
            lambda *args, **kargs: make_awaitable(([], 0))
        )
        self.datastore.delete_device_msgs_for_remote = (
            lambda *args, **kargs: make_awaitable(None)
        )
        self.datastore.set_received_txn_response = (
            lambda *args, **kwargs: make_awaitable(None)
        )

    def test_started_typing_local(self) -> None:
        self.room_members = [U_APPLE, U_BANANA]

        self.assertEqual(self.event_source.get_current_key(), 0)

        self.get_success(
            self.handler.started_typing(
                target_user=U_APPLE,
                requester=create_requester(U_APPLE),
                room_id=ROOM_ID,
                timeout=20000,
            )
        )

        self.on_new_event.assert_has_calls([call("typing_key", 1, rooms=[ROOM_ID])])

        self.assertEqual(self.event_source.get_current_key(), 1)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE, from_key=0, limit=None, room_ids=[ROOM_ID], is_guest=False
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )

    @override_config({"send_federation": True})
    def test_started_typing_remote_send(self) -> None:
        self.room_members = [U_APPLE, U_ONION]

        self.get_success(
            self.handler.started_typing(
                target_user=U_APPLE,
                requester=create_requester(U_APPLE),
                room_id=ROOM_ID,
                timeout=20000,
            )
        )

        put_json = self.hs.get_federation_http_client().put_json
        put_json.assert_called_once_with(
            "farm",
            path="/_matrix/federation/v1/send/1000000",
            data=_expect_edu_transaction(
                EduTypes.TYPING,
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_APPLE.to_string(),
                    "typing": True,
                },
            ),
            json_data_callback=ANY,
            long_retries=True,
            backoff_on_404=True,
            try_trailing_slash_on_400=True,
        )

    def test_started_typing_remote_recv(self) -> None:
        self.room_members = [U_APPLE, U_ONION]

        self.assertEqual(self.event_source.get_current_key(), 0)

        channel = self.make_request(
            "PUT",
            "/_matrix/federation/v1/send/1000000",
            _make_edu_transaction_json(
                EduTypes.TYPING,
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_ONION.to_string(),
                    "typing": True,
                },
            ),
            federation_auth_origin=b"farm",
        )
        self.assertEqual(channel.code, 200)

        self.on_new_event.assert_has_calls([call("typing_key", 1, rooms=[ROOM_ID])])

        self.assertEqual(self.event_source.get_current_key(), 1)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE, from_key=0, limit=None, room_ids=[ROOM_ID], is_guest=False
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_ONION.to_string()]},
                }
            ],
        )

    def test_started_typing_remote_recv_not_in_room(self) -> None:
        self.room_members = [U_APPLE, U_ONION]

        self.assertEqual(self.event_source.get_current_key(), 0)

        channel = self.make_request(
            "PUT",
            "/_matrix/federation/v1/send/1000000",
            _make_edu_transaction_json(
                EduTypes.TYPING,
                content={
                    "room_id": OTHER_ROOM_ID,
                    "user_id": U_ONION.to_string(),
                    "typing": True,
                },
            ),
            federation_auth_origin=b"farm",
        )
        self.assertEqual(channel.code, 200)

        self.on_new_event.assert_not_called()

        self.assertEqual(self.event_source.get_current_key(), 0)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE,
                from_key=0,
                limit=None,
                room_ids=[OTHER_ROOM_ID],
                is_guest=False,
            )
        )
        self.assertEqual(events[0], [])
        self.assertEqual(events[1], 0)

    @override_config({"send_federation": True})
    def test_stopped_typing(self) -> None:
        self.room_members = [U_APPLE, U_BANANA, U_ONION]

        # Gut-wrenching
        from synapse.handlers.typing import RoomMember

        member = RoomMember(ROOM_ID, U_APPLE.to_string())
        self.handler._member_typing_until[member] = 1002000
        self.handler._room_typing[ROOM_ID] = {U_APPLE.to_string()}

        self.assertEqual(self.event_source.get_current_key(), 0)

        self.get_success(
            self.handler.stopped_typing(
                target_user=U_APPLE,
                requester=create_requester(U_APPLE),
                room_id=ROOM_ID,
            )
        )

        self.on_new_event.assert_has_calls([call("typing_key", 1, rooms=[ROOM_ID])])

        put_json = self.hs.get_federation_http_client().put_json
        put_json.assert_called_once_with(
            "farm",
            path="/_matrix/federation/v1/send/1000000",
            data=_expect_edu_transaction(
                EduTypes.TYPING,
                content={
                    "room_id": ROOM_ID,
                    "user_id": U_APPLE.to_string(),
                    "typing": False,
                },
            ),
            json_data_callback=ANY,
            long_retries=True,
            backoff_on_404=True,
            try_trailing_slash_on_400=True,
        )

        self.assertEqual(self.event_source.get_current_key(), 1)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE, from_key=0, limit=None, room_ids=[ROOM_ID], is_guest=False
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": []},
                }
            ],
        )

    def test_typing_timeout(self) -> None:
        self.room_members = [U_APPLE, U_BANANA]

        self.assertEqual(self.event_source.get_current_key(), 0)

        self.get_success(
            self.handler.started_typing(
                target_user=U_APPLE,
                requester=create_requester(U_APPLE),
                room_id=ROOM_ID,
                timeout=10000,
            )
        )

        self.on_new_event.assert_has_calls([call("typing_key", 1, rooms=[ROOM_ID])])
        self.on_new_event.reset_mock()

        self.assertEqual(self.event_source.get_current_key(), 1)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE,
                from_key=0,
                limit=None,
                room_ids=[ROOM_ID],
                is_guest=False,
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )

        self.reactor.pump([16])

        self.on_new_event.assert_has_calls([call("typing_key", 2, rooms=[ROOM_ID])])

        self.assertEqual(self.event_source.get_current_key(), 2)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE,
                from_key=1,
                limit=None,
                room_ids=[ROOM_ID],
                is_guest=False,
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": []},
                }
            ],
        )

        # SYN-230 - see if we can still set after timeout

        self.get_success(
            self.handler.started_typing(
                target_user=U_APPLE,
                requester=create_requester(U_APPLE),
                room_id=ROOM_ID,
                timeout=10000,
            )
        )

        self.on_new_event.assert_has_calls([call("typing_key", 3, rooms=[ROOM_ID])])
        self.on_new_event.reset_mock()

        self.assertEqual(self.event_source.get_current_key(), 3)
        events = self.get_success(
            self.event_source.get_new_events(
                user=U_APPLE,
                from_key=0,
                limit=None,
                room_ids=[ROOM_ID],
                is_guest=False,
            )
        )
        self.assertEqual(
            events[0],
            [
                {
                    "type": EduTypes.TYPING,
                    "room_id": ROOM_ID,
                    "content": {"user_ids": [U_APPLE.to_string()]},
                }
            ],
        )
