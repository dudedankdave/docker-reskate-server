"""/coach: the admin dialog for server commands. Pick a server, pick an action, type the input.

  /coach  ->  [server]  ->  [action: announce, say, msg, kick, poll, players, console, info]  ->  input form

Actions that type into the console wait a moment and show what the server printed back. Everything
goes through console.send (the same checks as /say etc.: admin role, blocked stop/restart words), and
each use is posted to the server's admin channel. Nothing here restarts or updates a server.
"""
import asyncio
import time
from datetime import datetime, timezone

import discord

import console
import rollover
from status import STAMP

# key: (label, console prefix, [(input label, placeholder, long text)])  -  no inputs = no form
ACTIONS = {
    "announce": ("Announce to everyone", "announce {0}", [("Text", "Shown to every player", True)]),
    "say": ("Say in chat", "say {0}", [("Text", "Appears as a chat line from the server", True)]),
    "msg": ("Message a player", "msg {0} {1}", [("Player", "Name or SteamID", False), ("Text", "Only they see it", True)]),
    "kick": ("Kick a player", "kick {0}", [("Player", "Name or SteamID (until the server restarts)", False)]),
    "poll": ("Start a poll", "poll {0}", [("Question | answer | answer", "Who wins? | Anna | Ben", True)]),
    "players": ("Who is online", "players", []),
    "console": ("Other console command", "{0}", [("Command", "e.g. votes poll-seconds 60 (not quit/stop/restart)", False)]),
    "info": ("Server info", None, []),
}
WAIT = 2.5          # seconds the server gets to answer a console line
SHOW = 1500         # characters of the answer shown


def answer_since(info, since):
    """What the server logged from `since` (Unix seconds) on, without time stamps."""
    out = []
    for line in rollover.logs(info, since=int(since), timestamps=True):
        m = STAMP.match(line)
        if not m:
            continue
        at = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        if at >= int(since):
            text = m.group(3)
            out.append(rollover.LINE.sub(r"\1", text))
    return out


async def execute(bot, i, number, action, values):
    """Run one action (the interaction is already deferred); returns the text for the admin."""
    label, template, _ = ACTIONS[action]
    if action == "info":
        return await server_info(bot, number)
    return await send_line(bot, i, number, template.format(*[v.strip() for v in values]), i.user.display_name)


class Target(discord.ui.View):
    """Which of a server's running containers (two versions run side by side during an update) gets the line."""

    def __init__(self, user, choices):
        super().__init__(timeout=60)
        self.user, self.chosen = user, asyncio.get_running_loop().create_future()
        for name, infos in choices:
            button = discord.ui.Button(label=name[:80], style=discord.ButtonStyle.primary if len(infos) == 1 else discord.ButtonStyle.secondary)
            button.callback = self.pick(infos)
            self.add_item(button)

    def pick(self, infos):
        async def callback(i: discord.Interaction):
            await i.response.edit_message(content="Sending…", view=None)
            if not self.chosen.done():
                self.chosen.set_result(infos)
        return callback

    async def interaction_check(self, i: discord.Interaction):
        return i.user.id == self.user.id

    async def on_timeout(self):
        if not self.chosen.done():
            self.chosen.set_result(None)


async def pick_targets(bot, i, number):
    """The running containers of a server to send to: asks A or B when two run; None if cancelled."""
    found = await asyncio.to_thread(bot.board.gather)
    running = [x for x in found.get(number, []) if x["State"].get("Running")]
    if len(running) < 2:
        return running
    choices = []
    for info in running:
        s = await asyncio.to_thread(bot.board.follow, info)
        choices.append((f"{status_version(info) or info['Name'].lstrip('/')} · {s.players} players", [info]))
    choices.append(("Both", running))
    view = Target(i.user, choices)
    await i.followup.send(f"Server {number} runs {len(running)} versions right now. Which one?", view=view, ephemeral=True)
    return await view.chosen


async def send_line(bot, i, number, line, who):
    """Type `line` into the chosen container(s) of a server and return the answer text."""
    why = console.refused(line)
    if why:
        return f"Not sent: {why}."
    targets = await pick_targets(bot, i, number)
    if targets is None:
        return "No choice made, nothing sent."
    if not targets:
        return "That server is not running."
    started, parts = time.time(), []
    for info in targets:
        name = status_version(info) or info["Name"].lstrip("/")
        try:
            await asyncio.to_thread(console.send, info, line)
        except (OSError, RuntimeError) as e:
            parts.append(f"{name}: could not type into the console: {e}")
            continue
        parts.append(name)
    await bot.audit(number, who, line + (f" (on {', '.join(parts)})" if len(targets) > 1 else ""))
    await asyncio.sleep(WAIT)
    out = [f"Sent: `{line[:200]}`" + (f" to {', '.join(parts)}" if len(targets) > 1 else "")]
    for info in targets:
        answer = "\n".join(await asyncio.to_thread(answer_since, info, started))[-SHOW:]
        if answer:
            out.append((f"**{status_version(info) or info['Name'].lstrip('/')}**\n" if len(targets) > 1 else "") + f"```\n{answer}\n```")
    return "\n".join(out)


async def server_info(bot, number):
    found = await asyncio.to_thread(bot.board.gather)
    role = bot.board.cfg["servers"][number].get("role", f"Server {number}")
    running = [i for i in found.get(number, []) if i["State"].get("Running")]
    if not running:
        return f"**{role}** is offline."
    s = await asyncio.to_thread(bot.board.follow, running[0])
    version = status_version(running[0])
    return f"**{role}** · {s.players}/{s.max or '?'} players · map {s.map or '?'} · version {version or '?'}"


def status_version(info):
    import status
    return status.version_of(info)


class InputForm(discord.ui.Modal):
    def __init__(self, bot, number, action):
        label, _, fields = ACTIONS[action]
        super().__init__(title=label[:45])
        self.bot, self.number, self.action = bot, number, action
        self.inputs = []
        for name, placeholder, long in fields:
            box = discord.ui.TextInput(label=name[:45], placeholder=placeholder[:100], required=True, max_length=300,
                                       style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
            self.inputs.append(box)
            self.add_item(box)

    async def on_submit(self, i: discord.Interaction):
        if not self.bot.is_admin(i.user):
            return await i.response.send_message("Admins only.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await i.followup.send(await execute(self.bot, i, self.number, self.action, [b.value for b in self.inputs]), ephemeral=True)


class Coach(discord.ui.View):
    def __init__(self, bot, user):
        super().__init__(timeout=300)
        self.bot, self.user, self.number = bot, user, None
        servers = [discord.SelectOption(label=f"{n} - {s.get('role', 'Server ' + n)}"[:100], value=n)
                   for n, s in bot.board.cfg["servers"].items()][:25]
        self.server_menu = discord.ui.Select(placeholder="Which server?", options=servers)
        self.server_menu.callback = self.picked_server
        self.add_item(self.server_menu)
        self.action_menu = discord.ui.Select(
            placeholder="What do you want to do? (pick a server first)", disabled=True,
            options=[discord.SelectOption(label=v[0], value=k) for k, v in ACTIONS.items()])
        self.action_menu.callback = self.picked_action
        self.add_item(self.action_menu)

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.user.id or not self.bot.is_admin(i.user):
            await i.response.send_message("This is not your menu.", ephemeral=True)
            return False
        return True

    async def picked_server(self, i: discord.Interaction):
        self.number = self.server_menu.values[0]
        self.action_menu.disabled = False
        self.action_menu.placeholder = f"What do you want to do on server {self.number}?"
        await i.response.edit_message(view=self)

    async def picked_action(self, i: discord.Interaction):
        action = self.action_menu.values[0]
        if ACTIONS[action][2]:
            return await i.response.send_modal(InputForm(self.bot, self.number, action))
        await i.response.defer(ephemeral=True)
        await i.followup.send(await execute(self.bot, i, self.number, action, []), ephemeral=True)


def add(bot):
    from discord import app_commands

    @bot.tree.command(name="coach", description="Admin: run a server command (pick server, action, input)")
    @app_commands.default_permissions(manage_guild=True)
    async def coach(i: discord.Interaction):
        if not bot.is_admin(i.user):
            return await i.response.send_message("Admins only.", ephemeral=True)
        await i.response.send_message("Coach: pick a server, then what to do.", view=Coach(bot, i.user), ephemeral=True)
