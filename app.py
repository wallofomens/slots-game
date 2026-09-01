import os
import json
import hmac
import hashlib
import base64
import secrets
import uuid

import requests
from flask import Flask, request, jsonify, render_template


app = Flask(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

PSK = os.environ["LEOPARD_PSK"]
API_KEY = os.environ["LEOPARD_API_KEY"]

GS_ENDPOINT = (
    f"https://bl-stage-api.ctogs.com:9105/"
    f"online-gs/v2/{API_KEY}"
)

# Regular cash spin value.
# Freeround spin value is NOT taken from here.
# It is calculated from check_session.
REGULAR_BET_CENTS = 40


# ============================================================================
# LEOPARD / GS API
# ============================================================================

def make_checksum(payload_json):
    """
    Calculate:
        BASE64(HMAC_SHA1(payload_json, PSK))

    The exact payload_json string signed here is also sent as the HTTP body.
    """

    signature = hmac.new(
        PSK.encode("utf-8"),
        payload_json.encode("utf-8"),
        hashlib.sha1
    ).digest()

    return base64.b64encode(signature).decode("utf-8")


def leopard_request(command, params):
    """
    Sends one GS API request using exactly the same structure as our
    known-working local scripts:

        POST <GS_ENDPOINT>

        Content-Type: application/json
        x-api-key: ...
        x-checksum: ...

        {"command":"...","params":{...}}
    """

    payload = {
        "command": command,
        "params": params
    }

    # IMPORTANT:
    # This exact JSON string is BOTH signed and sent.
    payload_json = json.dumps(
        payload,
        separators=(",", ":")
    )

    checksum = make_checksum(payload_json)

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "x-api-key": API_KEY,
        "x-checksum": checksum,
    }

    print()
    print("=" * 80)
    print(f"LEOPARD REQUEST: {command}")
    print("Endpoint:")
    print(GS_ENDPOINT)
    print()
    print("Exact body:")
    print(payload_json)
    print()
    print("Checksum:")
    print(checksum)
    print("=" * 80)

    try:
        response = requests.post(
            GS_ENDPOINT,
            data=payload_json.encode("utf-8"),
            headers=headers,
            timeout=30
        )

    except requests.exceptions.Timeout as exc:
        print("LEOPARD TIMEOUT")
        print(repr(exc))

        raise RuntimeError(
            "Connection to Leopard timed out."
        ) from exc

    except requests.exceptions.RequestException as exc:
        print("LEOPARD CONNECTION ERROR")
        print(repr(exc))

        raise RuntimeError(
            f"Connection to Leopard failed: {exc}"
        ) from exc

    try:
        body = response.json()

    except ValueError:
        body = {
            "raw_response": response.text
        }

    print()
    print(f"LEOPARD RESPONSE: {command}")
    print("HTTP:", response.status_code)
    print(json.dumps(body, indent=2))
    print("=" * 80)
    print()

    if not response.ok:
        raise RuntimeError(
            f"Leopard returned HTTP {response.status_code}: {body}"
        )

    return body


# ============================================================================
# IDS
# ============================================================================

def generate_round_id():
    return str(uuid.uuid4())


def generate_transaction_id():
    return str(uuid.uuid4())


# ============================================================================
# SLOT RNG
# ============================================================================

SYMBOLS = [
    "🍒",
    "🍋",
    "🔔",
    "💎",
    "7️⃣",
    "⭐"
]


def spin_reels():
    """
    Very simple test-game RNG.

    3 x 7       -> 10x
    3 x Diamond -> 5x
    3 x Other   -> 3x
    Any pair    -> 1x
    Otherwise   -> 0x

    This is only intended as integration-test game logic.
    """

    reels = [
        secrets.choice(SYMBOLS),
        secrets.choice(SYMBOLS),
        secrets.choice(SYMBOLS)
    ]

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


# ============================================================================
# GAME PAGE
# ============================================================================

@app.route("/")
def game():
    """
    Browser launch.

    We only care about has_freerounds from the freeround-related launch
    parameters.

    It is useful as an INITIAL indication only.

    Actual freeround state is always obtained through check_session.
    """

    launch = {
        "account_id": request.args.get("account"),
        "session_id": request.args.get("session"),
        "game_id": request.args.get("game_id", "1"),
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
        "has_freerounds": request.args.get(
            "has_freerounds",
            "0"
        )
    }

    if not launch["account_id"] or not launch["session_id"]:
        return (
            "Missing account or session launch parameter.",
            400
        )

    return render_template(
        "game.html",
        launch=launch
    )


# ============================================================================
# CHECK SESSION ENDPOINT FOR BROWSER
# ============================================================================

@app.route("/api/session", methods=["POST"])
def api_session():

    data = request.get_json(silent=True) or {}

    account_id = data.get("account_id")
    session_id = data.get("session_id")

    if not account_id or not session_id:
        return jsonify({
            "success": False,
            "error": "account_id and session_id are required"
        }), 400

    try:
        result = leopard_request(
            "check_session",
            {
                "account_id": str(account_id),
                "session_id": session_id
            }
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "stage": "check_session",
            "error": str(exc)
        }), 502

    # Add our own convenience field for the frontend.
    freeround_limit = result.get(
        "freeround_limit",
        0
    )

    result["has_active_freerounds"] = (
        isinstance(freeround_limit, int)
        and freeround_limit > 0
    )

    return jsonify(result)


# ============================================================================
# SPIN
# ============================================================================

@app.route("/api/spin", methods=["POST"])
def api_spin():

    data = request.get_json(silent=True) or {}

    account_id = str(
        data.get("account_id", "")
    )

    session_id = data.get("session_id")

    game_id = str(
        data.get("game_id", "1")
    )

    if not account_id or not session_id:
        return jsonify({
            "success": False,
            "error": "Missing account/session"
        }), 400

    # ========================================================================
    # 1. CHECK SESSION
    # ========================================================================

    try:
        session = leopard_request(
            "check_session",
            {
                "account_id": account_id,
                "session_id": session_id
            }
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "stage": "check_session",
            "error": str(exc)
        }), 502

    if session.get("status") != "ok":
        return jsonify({
            "success": False,
            "stage": "check_session",
            "leopard": session
        }), 400

    # ========================================================================
    # 2. DETERMINE WHETHER THIS SPIN IS FREE
    # ========================================================================

    freeround_limit = session.get(
        "freeround_limit",
        0
    )

    is_freeround = (
        isinstance(freeround_limit, int)
        and freeround_limit > 0
    )

    freeround_id = None
    freeround_promo_id = None

    if is_freeround:

        freeround_lines = session.get(
            "freeround_lines"
        )

        freeround_bet_per_line_cents = session.get(
            "freeround_bet_per_line_cents"
        )

        if (
            freeround_lines is None
            or freeround_bet_per_line_cents is None
        ):
            return jsonify({
                "success": False,
                "stage": "freeround_validation",
                "error": (
                    "check_session says freerounds are available, "
                    "but freeround configuration is missing."
                ),
                "leopard": session
            }), 500

        # Documentation requirement:
        #
        # freerounds
        #   * freeround_lines
        #   * freeround_bet_per_line_cents
        #   = amount_cents
        #
        # We use exactly one freeround per BET.

        bet_amount = (
            int(freeround_lines)
            * int(freeround_bet_per_line_cents)
        )

        freeround_id = session.get(
            "freeround_id"
        )

        freeround_promo_id = session.get(
            "freeround_promo_id"
        )

        print(
            f"SPIN MODE: FREEROUND | "
            f"remaining={freeround_limit} | "
            f"lines={freeround_lines} | "
            f"bet_per_line={freeround_bet_per_line_cents} | "
            f"bet={bet_amount}"
        )

    else:

        bet_amount = REGULAR_BET_CENTS

        print(
            f"SPIN MODE: REGULAR | "
            f"bet={bet_amount}"
        )

    # ========================================================================
    # 3. CREATE ROUND
    # ========================================================================

    current_round_id = generate_round_id()

    bet_transaction_id = (
        generate_transaction_id()
    )

    # ========================================================================
    # 4. BET
    # ========================================================================

    bet_params = {
        "account_id": account_id,
        "session_id": session_id,
        "amount_cents": bet_amount,
        "game_id": game_id,
        "game_round_id": current_round_id,
        "transaction_id": bet_transaction_id,
        "description": (
            f"{'Freeround' if is_freeround else 'Regular'} "
            f"Bet: {bet_amount} cents "
            f"on game {game_id} "
            f"from account {account_id}"
        )
    }

    if is_freeround:
        bet_params["is_freeround"] = True
        bet_params["freerounds"] = 1

    try:
        bet_result = leopard_request(
            "bet",
            bet_params
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "stage": "bet",
            "error": str(exc)
        }), 502

    if bet_result.get("status") != "ok":
        return jsonify({
            "success": False,
            "stage": "bet",
            "leopard": bet_result
        }), 400

    # ========================================================================
    # 5. RNG
    # ========================================================================

    reels, multiplier = spin_reels()

    win_amount = (
        bet_amount
        * multiplier
    )

    # ========================================================================
    # 6. WIN
    #
    # Even a zero win is sent.
    # This closes the round.
    # ========================================================================

    win_transaction_id = (
        generate_transaction_id()
    )

    win_params = {
        "account_id": account_id,
        "session_id": session_id,
        "amount_cents": win_amount,
        "game_id": game_id,
        "game_round_id": current_round_id,
        "transaction_id": win_transaction_id,
        "bet_transaction_id": bet_transaction_id,
        "description": (
            f"{'Freeround' if is_freeround else 'Regular'} "
            f"Win: {win_amount} cents "
            f"on game {game_id} "
            f"from account {account_id}"
        )
    }

    # WIN must match the corresponding BET.
    if is_freeround:
        win_params["is_freeround"] = True

    try:
        win_result = leopard_request(
            "win",
            win_params
        )

    except Exception as exc:
        return jsonify({
            "success": False,
            "stage": "win",
            "error": str(exc),
            "reels": reels,
            "multiplier": multiplier,
            "win_amount_cents": win_amount,
            "game_round_id": current_round_id,
            "bet_transaction_id": bet_transaction_id
        }), 502

    if win_result.get("status") != "ok":
        return jsonify({
            "success": False,
            "stage": "win",
            "reels": reels,
            "multiplier": multiplier,
            "bet": bet_result,
            "leopard": win_result
        }), 400

    # ========================================================================
    # 7. CURRENT FREEROUND STATE AFTER THE SPIN
    # ========================================================================

    remaining_freerounds = win_result.get(
        "freeround_limit",
        0
    )

    # If this was the final freeround, Leopard may simply omit all freeround
    # fields. In that case remaining_freerounds becomes 0.
    if remaining_freerounds is None:
        remaining_freerounds = 0

    # ========================================================================
    # 8. RESPONSE TO BROWSER
    # ========================================================================

    return jsonify({
        "success": True,

        "mode": (
            "freeround"
            if is_freeround
            else "regular"
        ),

        "is_freeround": is_freeround,

        "reels": reels,
        "multiplier": multiplier,

        "bet_amount_cents": bet_amount,
        "win_amount_cents": win_amount,

        "balance": win_result.get(
            "balance"
        ),

        "currency": win_result.get(
            "currency",
            session.get("currency")
        ),

        "freeround_limit":
            remaining_freerounds,

        "freeround_id":
            win_result.get(
                "freeround_id",
                freeround_id
            ),

        "freeround_promo_id":
            win_result.get(
                "freeround_promo_id",
                freeround_promo_id
            ),

        "game_round_id":
            current_round_id,

        "bet_transaction_id":
            bet_transaction_id,

        "win_transaction_id":
            win_transaction_id
    })


# ============================================================================
# HEALTH
# ============================================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Leopard Test Slot"
    })


# ============================================================================
# LOCAL RUN
# ============================================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=True
    )
