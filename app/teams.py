"""Teams: players apply for a team, admins accept or deny the application, members join, leave and
configure their team in game (/team ...) or in Discord (the bot's /team commands).

The bridge owns the file (TEAMS_FILE, default /shared/teams.db, beside the leaderboard databases) and is
the only writer; the bot, the leaderboard module and scripts talk to it over HTTP (rollover/bridge.py
/v1/teams). Nothing here touches a game server: tags are shown by the bot (players, boards) and by the
leaderboard module (/top, /rank, /team who), so it works with the vanilla ReSkate server.

A Discord user is tied to a SteamID64 by a link code: `/team link` in game shows a code, `/team-link
<code>` in Discord claims it. Only linked Discord users can apply for, join or run a team there.

Standard library only (sqlite3)."""
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing

FILE = os.environ.get("TEAMS_FILE") or "/shared/teams.db"
MAX_MEMBERS = max(0, int(os.environ.get("TEAMS_MAX_MEMBERS", "0") or 0))      # 0: no limit
LINK_TTL = 15 * 60
LEAGUES = ("ranked", "unranked", "both")
CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'-]{0,19}$")     # e.g. Casual, Competitive, Street, Park, S.K.A.T.E.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.'-]{2,23}$")
TAG = re.compile(r"^[A-Za-z0-9]{2,5}$")
STEAM = re.compile(r"^\d{17}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS known (steam_id TEXT PRIMARY KEY, name TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS links (steam_id TEXT PRIMARY KEY, discord_id TEXT NOT NULL UNIQUE, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS link_codes (code TEXT PRIMARY KEY, steam_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, tag TEXT NOT NULL UNIQUE COLLATE NOCASE,
  motto TEXT NOT NULL DEFAULT '', open INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
  description TEXT NOT NULL DEFAULT '', logo TEXT NOT NULL DEFAULT '', league TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS members (
  steam_id TEXT PRIMARY KEY, team INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member', joined REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invites (
  team INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE, steam_id TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (team, steam_id));
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, steam_id TEXT NOT NULL, applicant TEXT NOT NULL, name TEXT NOT NULL,
  tag TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, decided_by TEXT, decided_at REAL,
  message TEXT NOT NULL DEFAULT '');
"""

HELP = [
    "Team commands: /team apply TAG Team Name | info [team] | list | who | join <team> | leave",
    "Leaders: /team invite|kick <player> | set open|motto|description|logo|league|category <value>",
    "Owner: /team leader <player> (make or drop a leader) | set tag <TAG> | owner <player> | disband",
    "Also: /team cancel (withdraw your application), /team link (code for Discord).",
]


class Teams:
    def __init__(self, path=None, max_members=None):
        self.path = path or FILE
        self.max_members = MAX_MEMBERS if max_members is None else max_members

    # ---- the file ---------------------------------------------------------------------------------
    def run(self, fn, write=True):
        with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            cur = db.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                result = fn(cur)
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")
            return result

    def setup(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        def init(cur):
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            have = {r["name"] for r in cur.execute("PRAGMA table_info(teams)")}
            for column in ("description", "logo", "league", "category"):      # a file from before the profiles
                if column not in have:
                    cur.execute(f"ALTER TABLE teams ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.run(init)
        return self

    # ---- who is who -------------------------------------------------------------------------------
    def seen(self, steam_id, name):
        """A player was on a server (the bridge calls this for every join): lets owners find them by name."""
        if STEAM.match(str(steam_id)) and name:
            self.run(lambda cur: cur.execute("INSERT OR REPLACE INTO known VALUES (?, ?, ?)", (steam_id, name[:64], time.time())))

    def tags(self):
        """{steam_id: tag} of every member: what the boards and lists show."""
        return self.run(lambda cur: {r["steam_id"]: r["tag"].upper() for r in cur.execute(
            "SELECT m.steam_id, t.tag FROM members m JOIN teams t ON t.id = m.team")}, write=False)

    def discord_of(self, steam_id):
        row = self.run(lambda cur: cur.execute("SELECT discord_id FROM links WHERE steam_id = ?", (steam_id,)).fetchone(), write=False)
        return row["discord_id"] if row else None

    def steam_of(self, cur, who):
        if who.get("steam_id"):
            return str(who["steam_id"])
        row = cur.execute("SELECT steam_id FROM links WHERE discord_id = ?", (str(who.get("discord") or ""),)).fetchone()
        return row["steam_id"] if row else None

    @staticmethod
    def find_player(cur, text):
        """SteamID64 of a player named by SteamID64 or (part of) a name seen on a server; error text if unclear."""
        text = str(text or "").strip()
        if STEAM.match(text):
            return text, ""
        rows = cur.execute("SELECT steam_id, name FROM known WHERE lower(name) = lower(?)", (text,)).fetchall()
        if not rows:
            rows = cur.execute("SELECT steam_id, name FROM known WHERE lower(name) LIKE '%' || lower(?) || '%' "
                               "ORDER BY seen DESC LIMIT 6", (text,)).fetchall()
        if len(rows) == 1:
            return rows[0]["steam_id"], ""
        if not rows:
            return None, f"No player called '{text}' has been on a server yet (a SteamID64 works too)."
        return None, "Which one? " + ", ".join(r["name"] for r in rows[:5])

    @staticmethod
    def name_of(cur, steam_id):
        row = cur.execute("SELECT name FROM known WHERE steam_id = ?", (steam_id,)).fetchone()
        return row["name"] if row else steam_id

    @staticmethod
    def team_of(cur, steam_id):
        return cur.execute("SELECT t.*, m.role FROM members m JOIN teams t ON t.id = m.team WHERE m.steam_id = ?",
                           (steam_id,)).fetchone()

    @staticmethod
    def find_team(cur, text):
        text = str(text or "").strip()
        return (cur.execute("SELECT * FROM teams WHERE name = ?", (text,)).fetchone()
                or cur.execute("SELECT * FROM teams WHERE tag = ?", (text,)).fetchone())

    # ---- commands ---------------------------------------------------------------------------------
    def act(self, who, verb, args=None):
        """Run one team command for `who` ({"steam_id", "name"} in game, {"discord", "name"} in Discord).
        Returns {"ok": bool, "lines": [str], "notify": [{"steam_id", "text"}], "application": id or None}."""
        args = {k: str(v).strip() for k, v in (args or {}).items() if v is not None}
        out = {"ok": True, "lines": [], "notify": [], "application": None}

        def fail(text):
            out.update(ok=False, lines=[text])
            return out

        def work(cur):
            if verb in ("help", ""):
                out["lines"] = HELP
                return out
            if verb == "list":
                rows = cur.execute("SELECT t.*, (SELECT COUNT(*) FROM members m WHERE m.team = t.id) n FROM teams t "
                                   "ORDER BY n DESC, t.name COLLATE NOCASE LIMIT 25").fetchall()
                out["lines"] = [f"[{r['tag'].upper()}] {r['name']} - {r['n']} member{'s' if r['n'] != 1 else ''}"
                                f"{'' if r['open'] else ' (invite only)'}" for r in rows] or ["No teams yet. /team apply TAG Name"]
                return out
            if verb == "link" and who.get("steam_id"):
                for old in cur.execute("SELECT code FROM link_codes WHERE steam_id = ?", (who["steam_id"],)).fetchall():
                    cur.execute("DELETE FROM link_codes WHERE code = ?", (old["code"],))
                cur.execute("DELETE FROM link_codes WHERE expires < ?", (time.time(),))
                code = secrets.token_hex(3).upper()
                cur.execute("INSERT INTO link_codes VALUES (?, ?, ?)", (code, who["steam_id"], time.time() + LINK_TTL))
                out["lines"] = [f"Your code is {code} (15 minutes). In Discord: /team-link {code}"]
                return out
            if verb == "link":
                code = args.get("code", "").upper()
                row = cur.execute("SELECT * FROM link_codes WHERE code = ? AND expires > ?", (code, time.time())).fetchone()
                if not row or not who.get("discord"):
                    return fail("That code is not valid (anymore). Type /team link in game for a new one.")
                cur.execute("DELETE FROM links WHERE steam_id = ? OR discord_id = ?", (row["steam_id"], str(who["discord"])))
                cur.execute("INSERT INTO links VALUES (?, ?, ?)", (row["steam_id"], str(who["discord"]), time.time()))
                cur.execute("DELETE FROM link_codes WHERE code = ?", (code,))
                out["lines"] = [f"Linked to {self.name_of(cur, row['steam_id'])}. You can now use the team commands here."]
                return out
            if verb == "info" and args.get("team"):
                return self.info(cur, out, self.find_team(cur, args["team"]), args["team"])
            steam_id = self.steam_of(cur, who)
            if not steam_id:
                return fail("Your Discord account is not linked to a player yet: type /team link in game, then /team-link <code> here.")
            if who.get("steam_id") and who.get("name"):
                cur.execute("INSERT OR REPLACE INTO known VALUES (?, ?, ?)", (steam_id, str(who["name"])[:64], time.time()))
            mine = self.team_of(cur, steam_id)
            owner = bool(mine and mine["role"] == "owner")
            lead = bool(mine and mine["role"] in ("owner", "leader"))
            if verb == "info":
                return self.info(cur, out, mine, "") if mine else fail("You are not in a team. /team list shows the teams.")
            if verb == "apply":
                return self.apply(cur, out, steam_id, who, args, mine, fail)
            if verb == "cancel":
                n = cur.execute("UPDATE applications SET status='cancelled', decided_at=? WHERE steam_id=? AND status='pending'",
                                (time.time(), steam_id)).rowcount
                out["lines"] = ["Application withdrawn."] if n else ["You have no open application."]
                return out
            if verb == "join":
                if mine:
                    return fail(f"You are in [{mine['tag'].upper()}] {mine['name']} already. /team leave first.")
                team = self.find_team(cur, args.get("team"))
                if not team:
                    return fail("No such team. /team list shows them.")
                invited = cur.execute("SELECT 1 FROM invites WHERE team = ? AND steam_id = ?", (team["id"], steam_id)).fetchone()
                if not team["open"] and not invited:
                    return fail(f"[{team['tag'].upper()}] {team['name']} is invite only. Ask its owner for an invite.")
                if self.max_members and self.size(cur, team["id"]) >= self.max_members:
                    return fail(f"That team is full ({self.max_members} members).")
                self.add(cur, team["id"], steam_id)
                out["lines"] = [f"You joined [{team['tag'].upper()}] {team['name']}."]
                out["notify"] = self.owner_note(cur, team["id"], f"{self.name_of(cur, steam_id)} joined your team.")
                return out
            if verb == "leave":
                if not mine:
                    return fail("You are not in a team.")
                return self.leave(cur, out, steam_id, mine)
            if verb in ("invite", "kick", "set") and not lead:
                return fail("Only the leaders of a team can do that." if mine else "You are not in a team.")
            if verb in ("owner", "leader", "disband") and not owner:
                return fail("Only the owner of a team can do that." if mine else "You are not in a team.")
            if verb == "invite":
                target, why = self.find_player(cur, args.get("player"))
                if not target:
                    return fail(why)
                if self.team_of(cur, target):
                    return fail(f"{self.name_of(cur, target)} is in a team already.")
                cur.execute("INSERT OR REPLACE INTO invites VALUES (?, ?, ?)", (mine["id"], target, time.time()))
                out["lines"] = [f"Invited {self.name_of(cur, target)}. They join with /team join {mine['tag'].upper()}."]
                out["notify"] = [{"steam_id": target, "text": f"You are invited to [{mine['tag'].upper()}] {mine['name']}: /team join {mine['tag'].upper()}"}]
                return out
            if verb == "kick":
                target, why = self.find_player(cur, args.get("player"))
                member = target and cur.execute("SELECT role FROM members WHERE steam_id = ? AND team = ?", (target, mine["id"])).fetchone()
                if not member:
                    return fail(why or "That player is not in your team.")
                if target == steam_id:
                    return fail("Use /team leave, or /team disband.")
                if member["role"] == "owner" or (member["role"] == "leader" and not owner):
                    return fail("Only the owner can remove a leader.")
                cur.execute("DELETE FROM members WHERE steam_id = ?", (target,))
                out["lines"] = [f"{self.name_of(cur, target)} left the team."]
                out["notify"] = [{"steam_id": target, "text": f"You were removed from [{mine['tag'].upper()}] {mine['name']}."}]
                return out
            if verb == "owner":
                target, why = self.find_player(cur, args.get("player"))
                member = target and cur.execute("SELECT 1 FROM members WHERE steam_id = ? AND team = ?", (target, mine["id"])).fetchone()
                if not member or target == steam_id:
                    return fail(why or "Name another member of your team.")
                cur.execute("UPDATE members SET role='member' WHERE steam_id=?", (steam_id,))
                cur.execute("UPDATE members SET role='owner' WHERE steam_id=?", (target,))
                out["lines"] = [f"{self.name_of(cur, target)} owns the team now."]
                out["notify"] = [{"steam_id": target, "text": f"You own [{mine['tag'].upper()}] {mine['name']} now."}]
                return out
            if verb == "leader":
                target, why = self.find_player(cur, args.get("player"))
                member = target and cur.execute("SELECT role FROM members WHERE steam_id = ? AND team = ?", (target, mine["id"])).fetchone()
                if not member or target == steam_id:
                    return fail(why or "Name another member of your team.")
                role = "member" if member["role"] == "leader" else "leader"
                cur.execute("UPDATE members SET role = ? WHERE steam_id = ?", (role, target))
                out["lines"] = [f"{self.name_of(cur, target)} {'leads the team now' if role == 'leader' else 'is a member again'}."]
                out["notify"] = [{"steam_id": target, "text": f"You {'lead' if role == 'leader' else 'no longer lead'} [{mine['tag'].upper()}] {mine['name']}."}]
                return out
            if verb == "set":
                return self.config(cur, out, mine, args, fail)
            if verb == "disband":
                if args.get("confirm", "").lower() != "yes":
                    return fail("This removes the team for everybody. Type /team disband yes to confirm.")
                out["notify"] = [{"steam_id": r["steam_id"], "text": f"[{mine['tag'].upper()}] {mine['name']} was disbanded."}
                                 for r in cur.execute("SELECT steam_id FROM members WHERE team = ? AND steam_id != ?", (mine["id"], steam_id))]
                cur.execute("DELETE FROM teams WHERE id = ?", (mine["id"],))
                out["lines"] = ["Team disbanded."]
                return out
            return fail("Unknown team command. /team help")
        return self.run(work)

    def size(self, cur, team):
        return cur.execute("SELECT COUNT(*) FROM members WHERE team = ?", (team,)).fetchone()[0]

    def add(self, cur, team, steam_id, role="member"):
        cur.execute("INSERT INTO members VALUES (?, ?, ?, ?)", (steam_id, team, role, time.time()))
        cur.execute("DELETE FROM invites WHERE steam_id = ?", (steam_id,))

    def owner_note(self, cur, team, text):
        return [{"steam_id": r["steam_id"], "text": text} for r in
                cur.execute("SELECT steam_id FROM members WHERE team = ? AND role IN ('owner', 'leader')", (team,))]

    def info(self, cur, out, team, asked):
        if not team:
            out.update(ok=False, lines=[f"No team called '{asked}'."])
            return out
        profile = self.profile_of(cur, team)
        out["team"] = profile["tag"]
        members = profile["members"]
        head = f"[{profile['tag']}] {profile['name']} - {len(members)} members, {'open' if profile['open'] else 'invite only'}"
        facts = ", ".join(x for x in (profile["league"] and f"{profile['league'].title()} league", profile["category"]) if x)
        out["lines"] = [head] + ([facts] if facts else []) + ([f"\"{profile['motto']}\""] if profile["motto"] else []) + [
            "Members: " + ", ".join(f"{m['name']}{' (owner)' if m['role'] == 'owner' else ' (leader)' if m['role'] == 'leader' else ''}"
                                   for m in members)]
        return out

    @staticmethod
    def profile_of(cur, team):
        """The team's profile: everything but the stats (those come from the leaderboards), leaders first."""
        members = cur.execute("SELECT m.steam_id, m.role, COALESCE(k.name, m.steam_id) name FROM members m LEFT JOIN known k "
                              "USING (steam_id) WHERE m.team = ? ORDER BY m.role = 'owner' DESC, m.role = 'leader' DESC, m.joined",
                              (team["id"],)).fetchall()
        return {"name": team["name"], "tag": team["tag"].upper(), "motto": team["motto"], "description": team["description"],
                "logo": team["logo"], "league": team["league"], "category": team["category"], "open": bool(team["open"]),
                "created": team["created"], "members": [dict(m) for m in members]}

    def profile(self, text):
        """profile_of the team called text (name or tag), or None."""
        def get(cur):
            team = self.find_team(cur, text)
            return team and self.profile_of(cur, team)
        return self.run(get, write=False)

    def profiles(self):
        """Every team's profile, biggest first."""
        def get(cur):
            return sorted((self.profile_of(cur, t) for t in cur.execute("SELECT * FROM teams")),
                          key=lambda p: (-len(p["members"]), p["name"].lower()))
        return self.run(get, write=False)

    def apply(self, cur, out, steam_id, who, args, mine, fail):
        if mine:
            return fail(f"You are in [{mine['tag'].upper()}] {mine['name']} already. /team leave first.")
        name, tag = args.get("name", ""), args.get("tag", "")
        if not NAME.match(name):
            return fail("Team name: 3-24 letters, numbers, spaces or - _ . '")
        if not TAG.match(tag):
            return fail("Tag: 2-5 letters or numbers.")
        if cur.execute("SELECT 1 FROM applications WHERE steam_id = ? AND status = 'pending'", (steam_id,)).fetchone():
            return fail("You have an application waiting for the admins already. /team cancel withdraws it.")
        if cur.execute("SELECT 1 FROM teams WHERE name = ? OR tag = ?", (name, tag)).fetchone() or cur.execute(
                "SELECT 1 FROM applications WHERE status = 'pending' AND (lower(name) = lower(?) OR lower(tag) = lower(?))",
                (name, tag)).fetchone():
            return fail("That team name or tag is taken (or waiting for approval).")
        cur.execute("INSERT INTO applications (steam_id, applicant, name, tag, created) VALUES (?, ?, ?, ?, ?)",
                    (steam_id, who.get("name") or self.name_of(cur, steam_id), name, tag.upper(), time.time()))
        out["application"] = cur.lastrowid
        out["lines"] = [f"Application for [{tag.upper()}] {name} sent. The admins decide in Discord; you get a message here."]
        return out

    def leave(self, cur, out, steam_id, mine):
        cur.execute("DELETE FROM members WHERE steam_id = ?", (steam_id,))
        rest = cur.execute("SELECT steam_id FROM members WHERE team = ? ORDER BY joined LIMIT 1", (mine["id"],)).fetchone()
        label = f"[{mine['tag'].upper()}] {mine['name']}"
        if not rest:
            cur.execute("DELETE FROM teams WHERE id = ?", (mine["id"],))
            out["lines"] = [f"You left {label}. It had no members left and is gone."]
        else:
            if mine["role"] == "owner":
                cur.execute("UPDATE members SET role='owner' WHERE steam_id = ?", (rest["steam_id"],))
                out["notify"] = [{"steam_id": rest["steam_id"], "text": f"You own {label} now."}]
            out["lines"] = [f"You left {label}."]
            out["notify"] += self.owner_note(cur, mine["id"], f"{self.name_of(cur, steam_id)} left your team.")
        return out

    def config(self, cur, out, mine, args, fail):
        key = (args.get("key") or args.get("setting") or args.get("field") or "").lower()
        value = args.get("value", "")
        if key == "tag":
            if mine["role"] != "owner":
                return fail("Only the owner can change the tag.")
            if not TAG.match(value):
                return fail("Tag: 2-5 letters or numbers.")
            if cur.execute("SELECT 1 FROM teams WHERE tag = ? AND id != ?", (value, mine["id"])).fetchone() or cur.execute(
                    "SELECT 1 FROM applications WHERE status='pending' AND lower(tag) = lower(?)", (value,)).fetchone():
                return fail("That tag is taken.")
            cur.execute("UPDATE teams SET tag = ? WHERE id = ?", (value.upper(), mine["id"]))
        elif key == "open":
            if value.lower() not in ("yes", "no", "true", "false", "on", "off"):
                return fail("/team set open yes (anyone can join) or no (invite only)")
            cur.execute("UPDATE teams SET open = ? WHERE id = ?", (int(value.lower() in ("yes", "true", "on")), mine["id"]))
        elif key == "motto":
            cur.execute("UPDATE teams SET motto = ? WHERE id = ?", (value[:60], mine["id"]))
        elif key == "description":
            cur.execute("UPDATE teams SET description = ? WHERE id = ?", (value[:300], mine["id"]))
        elif key == "logo":
            if value.lower() in ("", "none", "off"):
                value = ""
            elif not re.match(r"^https://[^\s]{4,290}$", value):
                return fail("Logo: the link (https://...) of a picture, or 'none'.")
            cur.execute("UPDATE teams SET logo = ? WHERE id = ?", (value, mine["id"]))
        elif key == "league":
            value = value.lower()
            if value in ("", "none", "off"):
                value = ""
            elif value not in LEAGUES:
                return fail("League: ranked, unranked, both or none.")
            cur.execute("UPDATE teams SET league = ? WHERE id = ?", (value, mine["id"]))
        elif key == "category":
            if value.lower() in ("", "none", "off"):
                value = ""
            elif not CATEGORY.match(value):
                return fail("Category: up to 20 letters or numbers, like Casual, Competitive, Street, Park.")
            cur.execute("UPDATE teams SET category = ? WHERE id = ?", (value, mine["id"]))
        else:
            return fail("/team set open yes/no | motto | description | logo <link> | league ranked/unranked/both | category <text> (owner: tag)")
        out["lines"] = [f"Team {key} changed."]
        return out

    # ---- applications (the admins' side) ------------------------------------------------------------
    def applications(self, status="pending"):
        return [dict(r) for r in self.run(lambda cur: cur.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY id", (status,)).fetchall(), write=False)]

    def posted(self, application, message):
        """Remember the Discord message that carries an application (so it is posted once)."""
        self.run(lambda cur: cur.execute("UPDATE applications SET message = ? WHERE id = ?", (str(message), application)))

    def decide(self, application, accept, by):
        """Accept (the applicant founds the team) or deny. Returns {"ok", "text", "notify": [...]}."""
        out = {"ok": True, "text": "", "notify": []}

        def work(cur):
            row = cur.execute("SELECT * FROM applications WHERE id = ?", (application,)).fetchone()
            if not row:
                out.update(ok=False, text="No such application.")
                return out
            if row["status"] != "pending":
                out.update(ok=False, text=f"That application was {row['status']} already.")
                return out
            label = f"[{row['tag']}] {row['name']}"
            if accept:
                if self.team_of(cur, row["steam_id"]):
                    out.update(ok=False, text=f"{row['applicant']} joined another team in the meantime.")
                    return out
                if cur.execute("SELECT 1 FROM teams WHERE name = ? OR tag = ?", (row["name"], row["tag"])).fetchone():
                    out.update(ok=False, text="The name or tag was taken in the meantime.")
                    return out
                cur.execute("INSERT INTO teams (name, tag, created) VALUES (?, ?, ?)", (row["name"], row["tag"], time.time()))
                self.add(cur, cur.lastrowid, row["steam_id"], "owner")
            cur.execute("UPDATE applications SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                        ("accepted" if accept else "denied", by[:64], time.time(), application))
            out["text"] = f"{label} {'accepted' if accept else 'denied'}"
            out["notify"] = [{"steam_id": row["steam_id"], "text": f"Your team {label} was accepted! /team info, /team invite <player>"
                              if accept else f"Your team application {label} was denied."}]
            return out
        return self.run(work)
