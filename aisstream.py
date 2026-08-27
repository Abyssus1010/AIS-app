import asyncio
import json
import os

import websockets
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["AISSTREAM_API_KEY"]

SUBSCRIPTION = {
    "APIKey": API_KEY,
    "BoundingBoxes": [
        [
            [1.20, 103.95],  # Southwest: lat, lon
            [1.35, 104.05],  # Northeast: lat, lon
        ]
    ]
}


async def main():
    async with websockets.connect("wss://stream.aisstream.io/v0/stream") as websocket:
        await websocket.send(json.dumps(SUBSCRIPTION))

        print("Connected. Waiting for AIS messages...\n")

        while True:
            message = await websocket.recv()
            data = json.loads(message)

            print(json.dumps(data, indent=2))
            print("-" * 80)


if __name__ == "__main__":
    asyncio.run(main())