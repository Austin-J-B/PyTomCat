from __future__ import annotations
import discord
async def handle_feeding_status(intent, ctx):
    await ctx["channel"] #.send("Feeding status: (hook up to Vision sheet)")