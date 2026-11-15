"""'While you were away': a short report on what happened between two ticks, in a sports-desk voice.

`events_of()` turns one tick's output into plain event dicts (battles, captures, loot); the session keeps
them. `digest()` picks the biggest stories in a window, from the reader's point of view, and writes them
up from templates. It's pure and seeded by the window, so the same window always reads the same.
"""

import random
from collections import Counter, defaultdict

from atlas_realms.rules.combat import Outcome
from atlas_realms.tick.engine import TickOutput
from atlas_realms.tick.world import World

# The paper and how it names its map: `on` for "All quiet on the Strait", `where` for "Around the Strait". Cook Strait's
# is the default (the server's solo mode); the browser arena gives each region its own (arena/regions.json "news").
STRAIT = {"paper": "The Strait Courier", "on": "on the Strait", "where": "the Strait"}


def events_of(before: World, out: TickOutput) -> list[dict]:
    """The newsworthy part of one tick: every battle, every hex that changed hands, every bounty paid."""
    w, tick = out.world, out.world.tick
    events: list[dict] = []
    for b in out.battles:
        r = b.result
        events.append(
            {
                "tick": tick,
                "kind": "battle",
                "hex": b.hex,
                "attacker": w.units[b.attacker_unit].user_id,
                "defender": b.defender_user,
                "garrison": b.defender_unit is None,
                "outcome": r.outcome.value,
                "rolls": len(r.rolls),
                "shield": b.shield_absorbed,
                "attacker_hp": r.attacker.hp,
                "defender_hp": r.defender.hp,
                "attacker_left": r.attacker_hp_after,
                "defender_left": r.defender_hp_after,
            }
        )
    for cell, h in sorted(w.hexes.items()):
        was = before.hexes[cell].owner_id
        if h.owner_id != was:
            events.append({"tick": tick, "kind": "capture", "hex": cell, "from": was, "to": h.owner_id})
    ledger = out.ledger
    for a, b in zip(ledger, ledger[1:], strict=False):
        if a.reason == "looted" and b.reason == "loot":
            events.append({"tick": tick, "kind": "loot", "from": a.user_id, "to": b.user_id, "amount": b.amount / 1000})
    return events


def digest(
    events: list[dict], names: dict[int, str], you: int, since: int, until: int, cities=None, desk: dict = STRAIT
) -> dict:
    """The report on (since, until]: a headline, a few stories (biggest first) and the numbers behind them.

    `cities` maps a hex to (name or None, tier). Your own stories always run; of the rest, the biggest of
    each kind gets a write-up and the others go into one round-up per kind, so a busy night isn't six
    copies of the same siege.
    """
    cities = cities or {}
    rng = random.Random(f"{since}:{until}")
    pick = rng.choice
    window = [e for e in events if since < e["tick"] <= until]
    battles = [e for e in window if e["kind"] == "battle"]
    captures = [e for e in window if e["kind"] == "capture"]
    loot = [e for e in window if e["kind"] == "loot"]

    def who(uid: int | None) -> str:
        return "you" if uid == you else names.get(uid, "a stranger")

    def Who(uid: int | None) -> str:  # noqa: N802 - sentence case
        w = who(uid)
        return w[0].upper() + w[1:]

    def whose(uid: int | None) -> str:
        return "your" if uid == you else f"{names.get(uid, 'a stranger')}'s"

    def does(uid: int | None, it: str, you_: str) -> str:
        """The verb that agrees with the subject: 'Raider downs', 'You down'."""
        return you_ if uid == you else it

    def them(uid: int | None) -> str:
        return "you" if uid == you else "them"

    def city(cell: str, owner: int | None = None) -> str:
        """A city by its name, else "Raider's tier-2 city" (or "a tier-2 city" with no owner given)."""
        name, tier = cities.get(cell, (None, 1))
        if name:
            return name
        return f"{whose(owner)} tier-{tier} city" if owner is not None else f"a tier-{tier} city"

    def at(e: dict) -> str:
        m = e["tick"] % 1440
        return f"{m // 60:02d}:{m % 60:02d}"

    stories: list[dict] = []

    def story(drama: int, family: str, mine: bool, head: str, text: str, brief: str) -> None:
        stories.append(
            {"drama": drama + 5 * mine, "family": family, "mine": mine, "head": head, "text": text, "brief": brief}
        )

    kills = Counter()
    for e in battles:
        a, d = e["attacker"], e["defender"]
        won = e["outcome"] == Outcome.ATTACKER_WON.value
        lost = e["outcome"] == Outcome.DEFENDER_WON.value
        if won and not e["garrison"]:
            kills[a] += 1
        if lost:
            kills[d] += 1
        mine = you in (a, d)
        if e["garrison"]:
            walls = city(e["hex"], d)
            if won:
                text = pick(
                    [
                        f"{Who(a)} broke the walls of {walls} at {at(e)}; the garrison is in ruins.",
                        f"At {at(e)} the garrison of {walls} fell to {who(a)} after {e['rolls']} rounds.",
                        f"The gates of {walls} gave way at {at(e)}. {Who(a)} {does(a, 'was', 'were')} through in {e['rolls']} rounds.",
                        f"{Who(a)} laid siege to {walls} at {at(e)} and the defenders didn't last {e['rolls'] + 1} rounds.",
                    ]
                )
                head = f"{Who(a)} {does(a, 'storms', 'storm')} {walls}"
                story(5, "siege", mine, head, text, f"{who(a)} took the walls of {walls}")
            elif lost:
                text = pick(
                    [
                        f"The garrison of {walls} stood firm and sent {who(a)} packing at {at(e)}.",
                        f"{Who(a)} hurled {'yourself' if a == you else 'themselves'} at {walls} at {at(e)} "
                        "and came away with nothing but bruises.",
                        f"A wall too far: {who(a)} fell under the walls of {walls} at {at(e)}.",
                    ]
                )
                walls_cap = walls[0].upper() + walls[1:]
                story(5, "siege", mine, f"{walls_cap} holds", text, f"{walls} held off {who(a)}")
            else:
                text = f"A siege of {walls} ended in a stalemate at {at(e)}; both sides licking their wounds."
                story(3, "siege", mine, "Stalemate at the walls", text, f"{who(a)} and {walls} fought to a draw")
            continue
        upset = won and e["attacker_hp"] < e["defender_hp"] or lost and e["defender_hp"] < e["attacker_hp"]
        shield = f" {Who(d)}'s shield soaked {e['shield']} before it was over." if e["shield"] else ""
        if won:
            text = pick(
                [
                    f"{Who(a)} caught {who(d)} at {at(e)} and finished the job in {e['rolls']} rolls.",
                    f"A clinical display from {who(a)}: {who(d)} down at {at(e)}, {e['attacker_left']} HP to spare.",
                    f"{Who(d)} never saw it coming. {Who(a)} struck at {at(e)} and walked away the winner.",
                ]
            )
            head, brief = f"{Who(a)} {does(a, 'downs', 'down')} {who(d)}", f"{who(a)} beat {who(d)}"
        elif lost:
            text = pick(
                [
                    f"{Who(a)} picked the wrong fight at {at(e)}: {who(d)} turned it round and sent {them(a)} to respawn.",
                    f"What a reversal! {Who(a)} attacked at {at(e)}, and {who(d)} put {them(a)} on the canvas.",
                ]
            )
            head, brief = f"{Who(d)} {does(d, 'turns', 'turn')} the tables on {who(a)}", f"{who(d)} saw off {who(a)}"
        else:
            text = pick(
                [
                    f"{Who(a)} and {who(d)} went the full {e['rolls']} rounds at {at(e)}. No knockout; both limped home.",
                    f"A bruising draw at {at(e)}: {who(a)} on {e['attacker_left']} HP, {who(d)} on {e['defender_left']}.",
                ]
            )
            head, brief = f"{Who(a)} and {who(d)} fight to a standstill", f"{who(a)} and {who(d)} drew"
        if upset:
            text = pick(["Upset of the night! ", "Nobody had this one on their card: "]) + text
            head = "Upset: " + head
        story(4 + 3 * upset + 2 * (won or lost), "fight", mine, head, text + shield, brief)

    gained, lost_hexes = defaultdict(int), defaultdict(int)
    for e in captures:
        gained[e["to"]] += 1
        if e["from"] is None:
            continue
        lost_hexes[e["from"]] += 1
        place = city(e["hex"], e["from"])
        if e["from"] == you:
            text = f"Bad news from {place}: {who(e['to'])} has raised their flag over it."
            head = f"{Who(e['to'])} {does(e['to'], 'seizes', 'seize')} {place}"
        else:
            text = f"{Who(e['to'])} took {place}."
            head = f"{Who(e['to'])} {does(e['to'], 'takes', 'take')} {place}"
        story(6, "capture", you in (e["from"], e["to"]), head, text, f"{who(e['to'])} took {place}")
    for uid, n in gained.items():
        claims = n - sum(1 for e in captures if e["to"] == uid and e["from"] is not None)
        if claims >= 2:
            story(
                3,
                "grab",
                uid == you,
                f"{Who(uid)} {does(uid, 'grabs', 'grab')} {claims} hexes",
                f"{Who(uid)} went on a land grab, claiming {claims} new hexes.",
                f"{who(uid)} claimed {claims}",
            )
    for e in loot:
        if e["amount"] < 1:
            continue  # pocket change isn't news
        coins = f"{e['amount']:.0f} coin{'s' * (round(e['amount']) != 1)}"
        story(
            4,
            "loot",
            you in (e["to"], e["from"]),
            f"{Who(e['to'])} {does(e['to'], 'collects', 'collect')} on {who(e['from'])}",
            f"{Who(e['to'])} cashed in a {e['amount']:.0f}-coin bounty on {who(e['from'])}.",
            f"{who(e['to'])} collected {coins} on {who(e['from'])}",
        )
    if kills:
        top, n = max(kills.items(), key=lambda kv: (kv[1], kv[0] == you))
        if n >= 2:
            text = pick(
                [
                    f"{Who(top)} {does(top, 'is', 'are')} on a tear: {n} kills.",
                    f"Nobody could stop {who(top)}: {n} kills and counting.",
                ]
            )
            story(7, "streak", top == you, f"{Who(top)} on a {n}-kill tear", text, "")

    written = _edit(stories, pick, desk)
    if not written:
        headline = pick([f"All quiet {desk['on']}", "A quiet night: nothing to report", "Calm seas, sleeping armies"])
        lead = "Not a blade drawn while you were away."
    else:
        headline = written[0]["headline"]
        lead = f"{len(battles)} battle{'s' * (len(battles) != 1)}, {len(captures)} hex{'es' * (len(captures) != 1)} changed hands."
    return {
        "paper": desk["paper"],
        "headline": headline,
        "lead": lead,
        "stories": written,
        "since": since,
        "until": until,
        "stats": {
            "battles": len(battles),
            "captures": len(captures),
            "kills": dict(kills),
            "your_gains": gained.get(you, 0),
            "your_losses": lost_hexes.get(you, 0),
        },
    }


ROUNDUPS = {
    "siege": ["Walls fall across {where}", "Siege season"],
    "fight": ["Around {where}", "Elsewhere on the field"],
    "capture": ["Changing hands", "The map redrawn"],
    "grab": ["Land rush", "Flags in the ground"],
    "loot": ["Bounty board", "Paid in full"],
}
MAX_STORIES = 4
MAX_ROUNDUPS = 2  # in this order of importance
ROUNDUP_ORDER = ("capture", "siege", "fight", "grab", "loot")
ROUNDUP_ITEMS = 4


def _edit(stories: list[dict], pick, desk: dict = STRAIT) -> list[dict]:
    """The front page: every story of yours, the biggest of each other kind, and a round-up of the rest."""
    stories = sorted(stories, key=lambda s: -s["drama"])
    lead: list[dict] = [s for s in stories if s["mine"]]
    rest: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        if not s["mine"]:
            rest[s["family"]].append(s)
    for family, group in rest.items():
        lead.append(group[0])
        others = group[1:]
        if len(others) == 1:
            lead.append(others[0])
        elif others:
            counted = Counter(o["brief"] for o in others if o["brief"])  # keeps first-seen order
            briefs = [b if n == 1 else f"{b} (×{n})" for b, n in counted.items()]
            more = len(briefs) - ROUNDUP_ITEMS
            text = "; ".join(briefs[:ROUNDUP_ITEMS]) + (f"; and {more} more" if more > 0 else "") + "."
            lead.append(
                {
                    "drama": 0,
                    "family": family,
                    "head": pick(ROUNDUPS[family]).format(where=desk["where"]),
                    "text": text[0].upper() + text[1:],
                }
            )
    top = sorted((s for s in lead if s["drama"]), key=lambda s: -s["drama"])[:MAX_STORIES]
    roundups = sorted((s for s in lead if not s["drama"]), key=lambda s: ROUNDUP_ORDER.index(s["family"]))[
        :MAX_ROUNDUPS
    ]
    return [{"headline": s["head"], "text": s["text"]} for s in top + roundups]
