### DISCORD CLIENT SETUP ###
# Creates the bot/tree instance exactly once. Every other module imports
# `bot`/`tree` from here rather than constructing its own.

import logging
import os
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

date = datetime.now()
handler = logging.FileHandler(
    filename=f"discordlogs/discordlog_{date.strftime('%d-%m-%Y_%H-%M-%S')}.log",
    encoding="utf-8",
    mode="w"
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
