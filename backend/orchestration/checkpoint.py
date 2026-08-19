import asyncio
import os

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from runtime_paths import CHECKPOINT_FILE


_connection = None
_saver = None
_event_loop = None


async def get_checkpointer():
    global _connection, _saver, _event_loop
    current_loop = asyncio.get_running_loop()
    if _saver is not None and _event_loop is not current_loop:
        await close_checkpointer()
    if _saver is None:
        path = os.getenv(
            "LANGGRAPH_CHECKPOINT_PATH",
            str(CHECKPOINT_FILE),
        )
        _connection = await aiosqlite.connect(path)
        _saver = AsyncSqliteSaver(_connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        _event_loop = current_loop
        await _saver.setup()
    return _saver


async def close_checkpointer():
    global _connection, _saver, _event_loop
    if _connection is not None:
        await _connection.close()
    _connection = None
    _saver = None
    _event_loop = None
