import sys
import json
import time
import asyncio
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from monitor import run_monitor

with open("secrets.json", "r", encoding="utf-8") as f:
    secrets = json.load(f)

access_token   = secrets["access_token"]
user_id        = secrets["user_id"]
home_server    = secrets["home_server"]
MATRIX_ROOM_ID = secrets["room_id"]   # add "room_id": "!yourRoomId:yourserver.com" to secrets.json

start_time = 0        # will be set in main()
message_queue = None  # will be set in main()

async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if event.sender == client.user_id:
        return
    if event.server_timestamp < start_time:
        return  # ignore anything sent before the bot started this run
    if event.body.startswith("!echo "):
        response = event.body[6:]
        await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": response}
        )
    elif event.body.strip() == "!ping":
        await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": "Pong!"}
        )

async def invite_callback(room, event):
    await client.join(room.room_id)

# ── say(): routes monitor alerts into the Matrix room, in order ─────────────
async def _send_to_matrix(message: str):
    try:
        resp = await client.room_send(
            room_id=MATRIX_ROOM_ID,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message}
        )
        print(f"  [matrix] sent: {message!r} -> {resp}")
    except Exception as e:
        print(f"  [matrix] FAILED to send {message!r}: {e}")

async def _message_sender():
    """Sends queued alert messages to Matrix one at a time, in the order
    they were queued, so multi-part alerts always land in sequence."""
    while True:
        message = await message_queue.get()
        await _send_to_matrix(message)

def say(message: str):
    message_queue.put_nowait(message)

async def _run_monitor_safe():
    try:
        await run_monitor(say)
    except Exception as e:
        print(f"  [monitor] CRASHED: {e}")
        raise

async def _run_matrix_safe():
    try:
        await client.sync_forever(timeout=30000)
    except Exception as e:
        print(f"  [matrix] sync_forever CRASHED: {e}")
        raise

async def main():
    global client, start_time, message_queue

    client = AsyncClient(home_server, user_id)
    client.access_token = access_token
    client.user_id = user_id
    start_time = time.time() * 1000  # Matrix timestamps are in milliseconds
    message_queue = asyncio.Queue()

    await client.sync(timeout=30000, full_state=True)
    client.add_event_callback(message_callback, RoomMessageText)
    client.add_event_callback(invite_callback, InviteMemberEvent)
    print("Bot is ready")

    results = await asyncio.gather(
        _run_matrix_safe(),
        _run_monitor_safe(),
        _message_sender(),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            print(f"  [main] task ended with exception: {r}")

asyncio.run(main())
