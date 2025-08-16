from __future__ import annotations
import discord
async def handle_dues_notice(intent, ctx):
    await ctx["channel"].send("Dues notice recorded. (Gmail/ledger integration to be added)")