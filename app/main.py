import os
import asyncio
from dotenv import load_dotenv

from app.bot import bot
from app.client import client

load_dotenv()

client.start()
bot.start(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])

loop = asyncio.get_event_loop()
loop.run_forever()
