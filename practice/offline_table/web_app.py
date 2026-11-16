"""Local web entry point for the offline four-player card table.

Run ``python web_app.py`` after installing requirements.txt. Each browser tab
keeps its own seat token; no LLM API, database, or cloud service is used.
"""

import os
import re
import secrets
import threading

from flask import Flask, jsonify, render_template, request

from game_engine import GameEngine, GameError, SEATS


ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Room:
    def __init__(self, code):
        self.code = code
        self.tokens = {"P1": secrets.token_urlsafe(32)}
        self.game = None
        self.lock = threading.RLock()

    def seat_for_token(self, token):
        for seat, saved_token in self.tokens.items():
            if secrets.compare_digest(saved_token, token):
                return seat
        return None

    def state_for(self, seat):
        if self.game is not None:
            state = self.game.view(seat)
            state["started"] = True
        else:
            joined = len(self.tokens)
            state = {
                "started": False,
                "phase": "lobby",
                "round": 0,
                "target_card": None,
                "current_player": None,
                "challenger": None,
                "last_play": None,
                "forced_challenge": False,
                "hand": [],
                "seats": [
                    {
                        "id": name,
                        "alive": True,
                        "card_count": 0,
                        "joined": name in self.tokens,
                    }
                    for name in SEATS
                ],
                "notice": f"已有 {joined}/4 位玩家入座",
                "result": None,
                "winner": None,
                "can": {
                    "start": seat == "P1" and joined == 4,
                    "play": False,
                    "challenge": False,
                    "advance": False,
                    "restart": False,
                },
            }
        state["room_code"] = self.code
        state["seat"] = seat
        return state


class RoomManager:
    def __init__(self):
        self.rooms = {}
        self.lock = threading.RLock()

    def create_room(self):
        with self.lock:
            while True:
                code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
                if code not in self.rooms:
                    break
            room = Room(code)
            self.rooms[code] = room
            return room

    def get_room(self, code):
        code = code.strip().upper()
        if not re.fullmatch(r"[A-Z2-9]{6}", code):
            raise ApiError("房间码无效", 404)
        with self.lock:
            room = self.rooms.get(code)
        if room is None:
            raise ApiError("找不到这个房间", 404)
        return room

    def join_room(self, code):
        room = self.get_room(code)
        with room.lock:
            if room.game is not None:
                raise ApiError("牌局已经开始，不能再入座", 409)
            if len(room.tokens) == len(SEATS):
                raise ApiError("房间已经坐满", 409)
            seat = SEATS[len(room.tokens)]
            token = secrets.token_urlsafe(32)
            room.tokens[seat] = token
            return room, seat, token


def create_app(manager=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    room_manager = manager if manager is not None else RoomManager()
    app.room_manager = room_manager

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        return jsonify({"error": str(error)}), error.status

    @app.errorhandler(GameError)
    def handle_game_error(error):
        return jsonify({"error": str(error)}), 409

    @app.after_request
    def no_cache_api(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def authorized_room(code):
        room = room_manager.get_room(code)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError("请先用自己的席位加入房间", 401)
        token = authorization[7:].strip()
        with room.lock:
            seat = room.seat_for_token(token)
        if seat is None:
            raise ApiError("席位凭证无效", 403)
        return room, seat

    def game_in_room(room):
        if room.game is None:
            raise ApiError("请等待房主开始牌局", 409)
        return room.game

    def json_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("请求内容必须是 JSON 对象")
        return data

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/rooms")
    def create_room():
        room = room_manager.create_room()
        return jsonify({
            "room_code": room.code,
            "seat": "P1",
            "token": room.tokens["P1"],
        }), 201

    @app.post("/api/rooms/<code>/join")
    def join_room(code):
        room, seat, token = room_manager.join_room(code)
        return jsonify({"room_code": room.code, "seat": seat, "token": token}), 201

    @app.get("/api/rooms/<code>/state")
    def get_state(code):
        room, seat = authorized_room(code)
        with room.lock:
            return jsonify(room.state_for(seat))

    @app.post("/api/rooms/<code>/start")
    def start_game(code):
        room, seat = authorized_room(code)
        with room.lock:
            if seat != "P1":
                raise ApiError("只有房主可以开始牌局", 403)
            if room.game is not None:
                raise ApiError("牌局已经开始", 409)
            if len(room.tokens) != len(SEATS):
                raise ApiError("需要四位玩家全部入座", 409)
            room.game = GameEngine()
            return jsonify(room.state_for(seat))

    @app.post("/api/rooms/<code>/play")
    def play_cards(code):
        room, seat = authorized_room(code)
        data = json_body()
        with room.lock:
            game_in_room(room).play(seat, data.get("cards"))
            return jsonify(room.state_for(seat))

    @app.post("/api/rooms/<code>/challenge")
    def challenge(code):
        room, seat = authorized_room(code)
        data = json_body()
        with room.lock:
            game_in_room(room).challenge(seat, data.get("challenge"))
            return jsonify(room.state_for(seat))

    @app.post("/api/rooms/<code>/advance")
    def advance(code):
        room, seat = authorized_room(code)
        with room.lock:
            game_in_room(room).advance()
            return jsonify(room.state_for(seat))

    @app.post("/api/rooms/<code>/restart")
    def restart(code):
        room, seat = authorized_room(code)
        with room.lock:
            game = game_in_room(room)
            if seat != "P1":
                raise ApiError("只有房主可以重开牌局", 403)
            if game.phase != "over":
                raise ApiError("当前牌局尚未结束", 409)
            room.game = GameEngine()
            return jsonify(room.state_for(seat))

    return app


if __name__ == "__main__":
    host = os.environ.get("LIARS_BAR_HOST", "127.0.0.1")
    port = int(os.environ.get("LIARS_BAR_PORT", "5000"))
    create_app().run(host=host, port=port, debug=False, use_reloader=False)
