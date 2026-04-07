# app/driver/socket/socket_manager.py

from typing import Dict, List, Any
import asyncio

# ----------------------------
# In-memory mapping: user_id -> list of socket_ids
# ----------------------------
# In production, you may persist this in Redis or DB
user_sockets: Dict[str, List[str]] = {}


# ----------------------------
# Register a new socket for a user
# ----------------------------
def register_socket(user_id: str, socket_id: str):
    sockets = user_sockets.get(user_id, [])
    if socket_id not in sockets:
        sockets.append(socket_id)
    user_sockets[user_id] = sockets


# ----------------------------
# Unregister a socket for a user
# ----------------------------
def unregister_socket(user_id: str, socket_id: str):
    sockets = user_sockets.get(user_id, [])
    if socket_id in sockets:
        sockets.remove(socket_id)
    if sockets:
        user_sockets[user_id] = sockets
    else:
        user_sockets.pop(user_id, None)


# ----------------------------
# Emit event to all sockets of a user
# ----------------------------
async def emit_to_user(user_id: str, event: str, data: Any):
    """
    Sends data to all active socket connections of the user.
    Replace the 'print' with actual socket.io emit in your implementation.
    """
    sockets = user_sockets.get(user_id, [])
    if not sockets:
        # No active sockets for this user
        return

    for socket_id in sockets:
        # In real implementation, you would do:
        # await sio.emit(event, data, room=socket_id)
        # For now, we just print for demonstration
        print(f"[Socket Emit] To user {user_id} (socket {socket_id}) => Event: {event}, Data: {data}")
        await asyncio.sleep(0)  # yield control