from discord.ext import commands
import discord

intents = discord.Intents.default()
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix='/', intents=intents)

async def load_cogs():
    await bot.load_extension('cogs.tts')

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} - {bot.user.id}')

if __name__ == "__main__":
    import asyncio
    import os

    TOKEN = os.getenv('DISCORD_TOKEN')
    if TOKEN is None:
        print("Please set the DISCORD_TOKEN environment variable.")
    else:
        asyncio.run(load_cogs())
        bot.run(TOKEN)