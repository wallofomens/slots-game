import os
import json
import hmac
import hashlib
import base64
import random
import time
import uuid

import requests
from flask import Flask, request, jsonify, render_template


app = Flask(__name__)


# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

PSK = os.environ["LEOPARD_PSK"]
API_KEY = os.environ["LEOPARD_API_KEY"]

GS_ENDPOINT = (
    f"https://bl-stage-api.ctogs.com:9105/"
    f"online-gs/v2/{API_KEY}"
)


# ---------------------------------------------------------------------
# LEOPARD API
# ---------------------------------------------------------------------

def make_checksum(payload_json):
    signature = hmac.new(
        PSK.encode("utf-8"),
        payload_json.encode("utf-8"),
        hashlib.sha1
    ).digest()

    return base64.b64encode(signature).decode("utf-8")


def leopard_request(command, params):
    payload = {
        "command": command,
        "params": params
    }

    payload_json = json.dumps(
        payload,
        separators=(",", ":")
    )

    checksum = make_checksum(payload_json)

    response = requests.post(
        GS_ENDPOINT,
        params={
            "api_key": API_KEY,
            "checksum": checksum,
            "payload_json": payload_json
        },
        timeout=15
    )

    try:
        body = response.json()
    except Exception:
        body = {
            "raw_response": response.text
        }

    print()
    print("=" * 80)
    print("LEOPARD REQUEST")
    print(payload_json)
    print()
    print("LEOPARD RESPONSE")
    print(json.dumps(body, indent=2))
    print("=" * 80)
    print()

    response.raise_for_status()

    return body


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def transaction_id():
    return str(int(time.time() * 1000)) + str(
        random.randint(1000, 9999)
    )


def round_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------
# SIMPLE SLOT RNG
# ---------------------------------------------------------------------

SYMBOLS = [
    "🍒",
    "🍋",
    "🔔",
    "💎",
    "7️⃣",
    "⭐"
]


def spin_reels():
    reels = [
        random.choice(SYMBOLS),
        random.choice(SYMBOLS),
        random.choice(SYMBOLS)
    ]

    # Extremely simple payout model.
    #
    # Three identical:
    #     7       = 10x
    #     diamond = 5x
    #     other   = 3x
    #
    # Two identical:
    #     1x
    #
    # Everything else:
    #     0

    if reels[0] == reels[1] == reels[2]:

        if reels[0] == "7️⃣":
            multiplier = 10

        elif reels[0] == "💎":
            multiplier = 5

        else:
            multiplier = 3

    elif (
        reels[0] == reels[1]
        or reels[1] == reels[2]
        or reels[0] == reels[2]
    ):
        multiplier = 1

    else:
        multiplier = 0

    return reels, multiplier


# ---------------------------------------------------------------------
# GAME PAGE
# ---------------------------------------------------------------------

@app.route("/")
def game():
    launch = {
        "account_id": request.args.get("account"),
        "session_id": request.args.get("session"),
        "currency": request.args.get(
            "account_currency",
            "EUR"
        ),
        "language": request.args.get(
            "language",
            "en"
        ),
        "is_real": request.args.get(
            "is_real",
            "1"
        ),

        # Freeround launch information
        "has_freerounds": request.args.get(
            "has_freerounds",
            "0"
        ),
        "freeround_lines": request.args.get(
            "freeround_lines"
        ),
        "freeround_bet_per_line_cents":
            request.args.get(
                "freeround_bet_per_line_cents"
            ),
        "freeround_initial_limit":
            request.args.get(
                "freeround_initial_limit"
            )
    }

    return render_template(
        "game.html",
        launch=launch
    )


# ---------------------------------------------------------------------
# CHECK SESSION
# ---------------------------------------------------------------------

@app.route("/api/session", methods=["POST"])
def api_session():

    data = request.get_json()

    result = leopard_request(
        "check_session",
        {
            "account_id": str(
                data["account_id"]
            ),
            "session_id": data["session_id"]
        }
    )

    return jsonify(result)


# ---------------------------------------------------------------------
# SPIN
# ---------------------------------------------------------------------

@app.route("/api/spin", methods=["POST"])
def api_spin():

    data = request.get_json()

    account_id = str(data["account_id"])
    session_id = data["session_id"]
    game_id = str(data.get("game_id", "1"))

    # --------------------------------------------------------------
    # 1. Check current session / freerounds
    # --------------------------------------------------------------

    session = leopard_request(
        "check_session",
        {
            "account_id": account_id,
            "session_id": session_id
        }
    )

    if session.get("status") != "ok":
        return jsonify({
            "success": False,
            "stage": "check_session",
            "leopard": session
        }), 400


    # --------------------------------------------------------------
    # 2. Decide if this is a freeround
    # --------------------------------------------------------------

    freeround_limit = session.get(
        "freeround_limit",
        0
    )

    is_freeround = freeround_limit > 0


    if is_freeround:

        lines = session[
            "freeround_lines"
        ]

        bet_per_line = session[
            "freeround_bet_per_line_cents"
        ]

        bet_amount = (
            lines
            * bet_per_line
        )

    else:

        # Normal cash wager.
        bet_amount = 40


    # --------------------------------------------------------------
    # 3. Generate transaction IDs
    # --------------------------------------------------------------

    current_round_id = round_id()

    bet_transaction_id = transaction_id()


    # --------------------------------------------------------------
    # 4. BET
    # --------------------------------------------------------------

    bet_params = {
        "account_id": account_id,
        "session_id": session_id,
        "amount_cents": bet_amount,
        "game_id": game_id,
        "game_round_id": current_round_id,
        "transaction_id": bet_transaction_id,
        "description": (
            f"Test Slot Bet: "
            f"{bet_amount} cents"
        )
    }


    if is_freeround:

        bet_params["is_freeround"] = True
        bet_params["freerounds"] = 1


    bet_result = leopard_request(
        "bet",
        bet_params
    )


    if bet_result.get("status") != "ok":

        return jsonify({
            "success": False,
            "stage": "bet",
            "leopard": bet_result
        }), 400


    # --------------------------------------------------------------
    # 5. RNG
    # --------------------------------------------------------------

    reels, multiplier = spin_reels()

    win_amount = (
        bet_amount
        * multiplier
    )


    # --------------------------------------------------------------
    # 6. WIN
    #
    # IMPORTANT:
    # We send win even when win_amount == 0.
    # This closes the game round.
    # --------------------------------------------------------------

    win_transaction_id = transaction_id()


    win_params = {
        "account_id": account_id,
        "session_id": session_id,
        "amount_cents": win_amount,
        "game_id": game_id,
        "game_round_id": current_round_id,
        "transaction_id": win_transaction_id,

        "bet_transaction_id":
            bet_transaction_id,

        "description": (
            f"Test Slot Win: "
            f"{win_amount} cents"
        )
    }


    if is_freeround:
        win_params["is_freeround"] = True


    win_result = leopard_request(
        "win",
        win_params
    )


    if win_result.get("status") != "ok":

        return jsonify({
            "success": False,
            "stage": "win",
            "reels": reels,
            "bet": bet_result,
            "leopard": win_result
        }), 400


    # --------------------------------------------------------------
    # RESPONSE TO BROWSER
    # --------------------------------------------------------------

    return jsonify({
        "success": True,

        "reels": reels,

        "multiplier": multiplier,

        "bet_amount_cents":
            bet_amount,

        "win_amount_cents":
            win_amount,

        "is_freeround":
            is_freeround,

        "bet_transaction_id":
            bet_transaction_id,

        "win_transaction_id":
            win_transaction_id,

        "game_round_id":
            current_round_id,

        "balance":
            win_result.get("balance"),

        "currency":
            win_result.get("currency"),

        "freeround_limit":
            win_result.get(
                "freeround_limit"
            ),

        "freeround_id":
            win_result.get(
                "freeround_id"
            ),

        "freeround_promo_id":
            win_result.get(
                "freeround_promo_id"
            )
    })


# ---------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({
        "status": "ok"
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
