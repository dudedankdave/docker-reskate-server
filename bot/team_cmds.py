"""Teams in Discord (the bridge keeps them, see app/teams.py): /team apply | info | list | join | leave |
invite | kick | set | owner | disband | cancel | link, and the admins' review of applications.

Applications are posted by the bot into `teams_channel` (bot.json) with Accept and Deny buttons that only
members with an admin role may press; the decision goes to the bridge, which tells the applicant in game
(else by Discord DM when they linked their account). Without a bridge (BRIDGE_TOKEN) the commands answer
that teams are off."""
import asyncio

import discord
from discord import app_commands

import rollover

try:
    import bridgeclient
except ImportError:
    bridgeclient = None

say = rollover.say
CHECK = 30            # seconds between two looks for new applications
GREEN, RED, BLURPLE = 0x2ECC71, 0xE74C3C, 0x5865F2


def available():
    return bridgeclient is not None and bool(bridgeclient.TOKEN)


def team_tags():
    """{steam_id: tag}, {} when there are no teams (or no bridge)."""
    try:
        return bridgeclient.team_tags() if available() else {}
    except bridgeclient.BridgeError:
        return {}


def with_tag(row, tags):
    """A board row's name with the team tag in front: [TAG] Name."""
    tag = tags.get(row["steam_id"]) if "steam_id" in row.keys() else None
    return f"[{tag}] {row['name']}" if tag else row["name"]


def application_embed(app, state=None, by=""):
    embed = discord.Embed(title=f"Team application: [{app['tag']}] {app['name']}", color=BLURPLE,
                          description=f"From **{app['applicant']}** (`{app['steam_id']}`)\nSent <t:{int(app['created'])}:R>")
    if state:
        embed.color = GREEN if state == "accepted" else RED
        embed.add_field(name="Decision", value=f"{state.title()} by {by}", inline=False)
    return embed


class Decision(discord.ui.DynamicItem[discord.ui.Button], template=r"team:(?P<what>accept|deny):(?P<id>\d+)"):
    """The Accept and Deny buttons under an application; they survive a restart (the id is in the custom_id)."""

    def __init__(self, what, application):
        style = discord.ButtonStyle.success if what == "accept" else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(label=what.title(), style=style, custom_id=f"team:{what}:{application}"))
        self.what, self.application = what, int(application)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"], match["id"])

    async def callback(self, i: discord.Interaction):
        bot = i.client
        if not bot.is_admin(i.user):
            return await i.response.send_message("Admins only.", ephemeral=True)
        await i.response.defer()
        try:
            result = await asyncio.to_thread(bridgeclient.team_decide, self.application, self.what == "accept", i.user.display_name)
        except bridgeclient.BridgeError as e:
            return await i.followup.send(f"Could not decide: {e}", ephemeral=True)
        if not result.get("ok"):
            return await i.followup.send(result.get("text") or "Could not decide.", ephemeral=True)
        apps = await asyncio.to_thread(bridgeclient.team_applications, "accepted" if self.what == "accept" else "denied")
        app = next((a for a in apps if a["id"] == self.application), None)
        if app:
            await i.message.edit(embed=application_embed(app, "accepted" if self.what == "accept" else "denied", i.user.display_name), view=None)
        else:
            await i.message.edit(view=None)
        await tell(bot, result.get("notify"))


async def tell(bot, notes):
    """Players who were not on a server (the bridge could not message them) get a DM when they linked Discord."""
    for note in notes or []:
        if note.get("delivered") or not note.get("discord"):
            continue
        try:
            user = bot.get_user(int(note["discord"])) or await bot.fetch_user(int(note["discord"]))
            await user.send(note["text"])
        except (discord.HTTPException, ValueError) as e:
            say(f"teams: could not DM {note['discord']}: {e}")


async def run_applications(bot):
    """Post each new application into teams_channel, once (the bridge remembers the message)."""
    await bot.wait_until_ready()
    while True:
        try:
            if available() and bot.cfg.get("teams_channel"):
                channel = bot.get_channel(int(bot.cfg["teams_channel"])) or await bot.fetch_channel(int(bot.cfg["teams_channel"]))
                for app in await asyncio.to_thread(bridgeclient.team_applications):
                    if app["message"]:
                        continue
                    view = discord.ui.View(timeout=None)
                    view.add_item(Decision("accept", app["id"]))
                    view.add_item(Decision("deny", app["id"]))
                    message = await channel.send(embed=application_embed(app), view=view)
                    await asyncio.to_thread(bridgeclient.team_posted, app["id"], message.id)
        except Exception as e:
            say(f"teams: applications: {e!r}")
        await asyncio.sleep(CHECK)


def add(bot):
    team = app_commands.Group(name="team", description="Teams: apply, join, leave, configure")

    async def act(i: discord.Interaction, verb, **args):
        if not available():
            return await i.response.send_message("Teams are not available (the bot has no bridge).", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(bridgeclient.team_act, {"discord": str(i.user.id), "name": i.user.display_name}, verb, args)
        except bridgeclient.BridgeError as e:
            return await i.followup.send(f"Teams are not available right now ({e}).", ephemeral=True)
        await i.followup.send("\n".join(result.get("lines") or ["Done."])[:1900], ephemeral=True)
        await tell(bot, result.get("notify"))

    @team.command(name="apply", description="Apply for a new team; the admins decide")
    @app_commands.describe(name="Team name, 3-24 characters", tag="Tag shown before your names, 2-5 letters or numbers")
    async def apply(i: discord.Interaction, name: str, tag: str):
        await act(i, "apply", name=name, tag=tag)

    @team.command(name="info", description="A team and its members (yours by default)")
    async def info(i: discord.Interaction, team: str = ""):
        await act(i, "info", team=team)

    @team.command(name="list", description="All teams")
    async def list_(i: discord.Interaction):
        await act(i, "list")

    @team.command(name="join", description="Join an open team, or one that invited you")
    async def join(i: discord.Interaction, team: str):
        await act(i, "join", team=team)

    @team.command(name="leave", description="Leave your team")
    async def leave(i: discord.Interaction):
        await act(i, "leave")

    @team.command(name="cancel", description="Withdraw your open application")
    async def cancel(i: discord.Interaction):
        await act(i, "cancel")

    @team.command(name="invite", description="Owner: invite a player (name of someone who played here, or SteamID64)")
    async def invite(i: discord.Interaction, player: str):
        await act(i, "invite", player=player)

    @team.command(name="kick", description="Owner: remove a member")
    async def kick(i: discord.Interaction, player: str):
        await act(i, "kick", player=player)

    @team.command(name="owner", description="Owner: hand the team to another member")
    async def owner(i: discord.Interaction, player: str):
        await act(i, "owner", player=player)

    @team.command(name="set", description="Owner: change the tag, who may join (open) or the motto")
    @app_commands.choices(setting=[app_commands.Choice(name=n, value=n) for n in ("tag", "open", "motto")])
    @app_commands.describe(setting="What to change", value="tag: 2-5 characters · open: yes or no · motto: text")
    async def set_(i: discord.Interaction, setting: str, value: str):
        await act(i, "set", key=setting, value=value)

    @team.command(name="disband", description="Owner: remove the team for everybody")
    @app_commands.describe(confirm="Type yes to confirm")
    async def disband(i: discord.Interaction, confirm: str = ""):
        await act(i, "disband", confirm=confirm)

    @team.command(name="link", description="Link your Discord account to your player: type /team link in game for the code")
    async def link(i: discord.Interaction, code: str):
        await act(i, "link", code=code)

    bot.tree.add_command(team)


def start(bot):
    """After the client is built: the buttons and the application loop."""
    bot.add_dynamic_items(Decision)
    bot.loop.create_task(run_applications(bot))
