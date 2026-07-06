#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import signal
import sys

LISTEN_HOST = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
LISTEN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 17897
TARGET_HOST = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
TARGET_PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 7897

async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception:
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass
        return
    await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer), return_exceptions=True)

async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    print(f"forwarding {sockets} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
