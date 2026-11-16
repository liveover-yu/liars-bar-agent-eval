"""HTTP checks for a local four-seat game, using Flask's in-process client."""

import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_app import create_app  # noqa: E402


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def post(self, path, token=None, data=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if data is None:
            return self.client.post(path, headers=headers)
        return self.client.post(path, json=data, headers=headers)

    def get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get(path, headers=headers)

    def room_with_four_seats(self):
        created = self.post("/api/rooms")
        self.assertEqual(created.status_code, 201)
        first = created.get_json()
        code = first["room_code"]
        seats = {first["seat"]: first["token"]}
        for _ in range(3):
            response = self.post(f"/api/rooms/{code}/join")
            self.assertEqual(response.status_code, 201)
            joined = response.get_json()
            self.assertEqual(joined["room_code"], code)
            seats[joined["seat"]] = joined["token"]
        self.assertEqual(set(seats), {"P1", "P2", "P3", "P4"})
        self.assertEqual(len(set(seats.values())), 4)
        return code, seats

    def state(self, code, token):
        response = self.get(f"/api/rooms/{code}/state", token)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def started_room(self):
        code, seats = self.room_with_four_seats()
        response = self.post(f"/api/rooms/{code}/start", seats["P1"])
        self.assertEqual(response.status_code, 200)
        return code, seats

    def assert_client_error(self, response):
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)

    def test_lobby_state_survives_refresh_and_only_host_can_start(self):
        code, seats = self.room_with_four_seats()
        host_state = self.state(code, seats["P1"])
        guest_state = self.state(code, seats["P2"])
        self.assertEqual(host_state, self.state(code, seats["P1"]))
        self.assertFalse(host_state["started"])
        self.assertEqual(host_state["room_code"], code)
        self.assertEqual(host_state["seat"], "P1")
        self.assertTrue(host_state["can"]["start"])
        self.assertFalse(guest_state["can"]["start"])
        self.assert_client_error(self.post(f"/api/rooms/{code}/start", seats["P2"]))
        self.assert_client_error(self.post(f"/api/rooms/{code}/join"))
        self.assertEqual(self.post(f"/api/rooms/{code}/start", seats["P1"]).status_code, 200)
        self.assert_client_error(self.post(f"/api/rooms/{code}/start", seats["P1"]))
        self.assert_client_error(self.post(f"/api/rooms/{code}/join"))

    def test_each_protected_route_requires_a_room_seat_token(self):
        code, seats = self.room_with_four_seats()
        paths = (
            ("GET", "state"),
            ("POST", "start"),
            ("POST", "play"),
            ("POST", "challenge"),
            ("POST", "advance"),
            ("POST", "restart"),
        )
        for method, suffix in paths:
            path = f"/api/rooms/{code}/{suffix}"
            with self.subTest(path=path):
                if method == "GET":
                    missing = self.get(path)
                    forged = self.get(path, "not-a-valid-seat-token")
                else:
                    missing = self.post(path)
                    forged = self.post(path, "not-a-valid-seat-token")
                self.assertIn(missing.status_code, (401, 403))
                self.assertIn(forged.status_code, (401, 403))
        second_room = self.post("/api/rooms").get_json()
        self.assertIn(
            self.get(f"/api/rooms/{code}/state", second_room["token"]).status_code,
            (401, 403),
        )

    def test_state_filters_other_hands_bullets_and_tokens(self):
        code, seats = self.started_room()
        first = self.state(code, seats["P1"])
        second = self.state(code, seats["P2"])
        self.assertTrue(first["started"])
        self.assertEqual(len(first["hand"]), 5)
        self.assertEqual(len(second["hand"]), 5)
        self.assertEqual(first["seat"], "P1")
        self.assertEqual(second["seat"], "P2")
        self.assertEqual(first["room_code"], code)
        self.assertEqual(len(first["seats"]), 4)
        self.assertEqual(sum("hand" in seat for seat in first["seats"]), 0)
        encoded = json.dumps(first, ensure_ascii=False)
        self.assertNotIn('"bullet_position"', encoded)
        for token in seats.values():
            self.assertNotIn(token, encoded)

    def test_turn_authorization_duplicate_actions_and_state_refresh(self):
        code, seats = self.started_room()
        opening = self.state(code, seats["P1"])
        actor = opening["current_player"]
        challenger = next(seat for seat in seats if seat != actor)
        guest_card = self.state(code, seats[challenger])["hand"][0]
        self.assert_client_error(
            self.post(f"/api/rooms/{code}/play", seats[challenger], {"cards": [guest_card]})
        )
        actor_card = self.state(code, seats[actor])["hand"][0]
        played = self.post(f"/api/rooms/{code}/play", seats[actor], {"cards": [actor_card]})
        self.assertEqual(played.status_code, 200)
        waiting = self.state(code, seats[challenger])
        self.assertEqual(waiting, self.state(code, seats[challenger]))
        self.assertEqual(waiting["phase"], "challenge")
        self.assertEqual(waiting["last_play"], {"player": actor, "count": 1})
        self.assertEqual(waiting["challenger"], challenger)
        self.assert_client_error(
            self.post(f"/api/rooms/{code}/play", seats[actor], {"cards": [actor_card]})
        )
        wrong_challenger = next(seat for seat in seats if seat not in (actor, challenger))
        self.assert_client_error(
            self.post(f"/api/rooms/{code}/challenge", seats[wrong_challenger], {"challenge": True})
        )
        self.assert_client_error(
            self.post(f"/api/rooms/{code}/challenge", seats[challenger], {"challenge": "false"})
        )
        self.assertEqual(
            self.post(f"/api/rooms/{code}/challenge", seats[challenger], {"challenge": False}).status_code,
            200,
        )
        self.assert_client_error(
            self.post(f"/api/rooms/{code}/challenge", seats[challenger], {"challenge": False})
        )
        next_state = self.state(code, seats[challenger])
        self.assertEqual(next_state, self.state(code, seats[challenger]))
        self.assertEqual(next_state["current_player"], challenger)
        self.assertIsNone(next_state["result"])

    def test_challenge_resolution_and_advance_through_http(self):
        code, seats = self.started_room()
        actor = self.state(code, seats["P1"])["current_player"]
        card = self.state(code, seats[actor])["hand"][0]
        self.assertEqual(
            self.post(f"/api/rooms/{code}/play", seats[actor], {"cards": [card]}).status_code,
            200,
        )
        challenger = self.state(code, seats[actor])["challenger"]
        self.assertEqual(
            self.post(f"/api/rooms/{code}/challenge", seats[challenger], {"challenge": True}).status_code,
            200,
        )
        resolved = self.state(code, seats[actor])
        self.assertEqual(resolved["phase"], "result")
        self.assertEqual(resolved["result"]["cards"], [card])
        self.assertIn(resolved["result"]["shot"], ("blank", "dead"))
        self.assertNotIn('"bullet_position"', json.dumps(resolved))
        self.assertEqual(resolved, self.state(code, seats[actor]))
        self.assertEqual(
            self.post(f"/api/rooms/{code}/advance", seats["P3"]).status_code,
            200,
        )
        advanced = self.state(code, seats[actor])
        self.assertEqual(advanced["round"], 2)
        self.assertEqual(advanced["phase"], "play")
        self.assertIsNone(advanced["result"])
        self.assertEqual(advanced["room_code"], code)
        self.assert_client_error(self.post(f"/api/rooms/{code}/advance", seats["P3"]))
        self.assert_client_error(self.post(f"/api/rooms/{code}/restart", seats["P1"]))

    def test_completed_room_restarts_only_for_host_and_keeps_seats(self):
        code, seats = self.started_room()
        for _ in range(24):
            state = self.state(code, seats["P1"])
            if state["phase"] == "over":
                break
            self.assertEqual(state["phase"], "play")
            actor = state["current_player"]
            card = self.state(code, seats[actor])["hand"][0]
            self.assertEqual(
                self.post(f"/api/rooms/{code}/play", seats[actor], {"cards": [card]}).status_code,
                200,
            )
            challenger = self.state(code, seats[actor])["challenger"]
            self.assertEqual(
                self.post(
                    f"/api/rooms/{code}/challenge",
                    seats[challenger],
                    {"challenge": True},
                ).status_code,
                200,
            )
            resolved = self.state(code, seats["P1"])
            if resolved["phase"] == "result":
                self.assertEqual(
                    self.post(f"/api/rooms/{code}/advance", seats["P1"]).status_code,
                    200,
                )
        else:
            self.fail("A game with every play challenged did not finish within 24 penalties")

        finished = self.state(code, seats["P1"])
        self.assertEqual(finished["phase"], "over")
        self.assertIn(finished["winner"], seats)
        self.assert_client_error(self.post(f"/api/rooms/{code}/restart", seats["P2"]))
        self.assertEqual(self.state(code, seats["P1"]), finished)
        restarted = self.post(f"/api/rooms/{code}/restart", seats["P1"])
        self.assertEqual(restarted.status_code, 200)
        new_state = self.state(code, seats["P1"])
        self.assertEqual(new_state["phase"], "play")
        self.assertEqual(new_state["round"], 1)
        self.assertEqual(new_state["room_code"], code)
        self.assertIsNone(new_state["winner"])
        self.assertTrue(all(seat["alive"] and seat["card_count"] == 5 for seat in new_state["seats"]))
        self.assertEqual(len(self.state(code, seats["P2"])["hand"]), 5)
        self.assert_client_error(self.post(f"/api/rooms/{code}/restart", seats["P1"]))


if __name__ == "__main__":
    unittest.main()
