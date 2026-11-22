"""Hashtags for a clip without asking an AI: free, instant, and the same every time.

Three to five tags, picked from what is really there:
  - the game's main tag (#GTA6), when it is known
  - the words said most in the clip, and a game or product named in the title ("GTA 6" becomes #gta6)
  - what kind of moment it is (#funny, #fails, #clutch)
  - #shorts and #clips, only if too few others were found

Tags are returned WITHOUT the '#' prefix (the convention in both parent
tools). A description the person wrote themselves is never touched: see apply().

Ported from PeakCut studio/hashtags.py (MIT); the small game table from
PeakCut studio/games.py is inlined here so this module stays standalone.
"""
import re

MOST, LEAST = 5, 3

STOP = set("""
about above after again against all also always among and another any anyone anything are around because been before being
between both but came can cannot come could did does doing done dont down each else even ever every everyone everything
find first for from gave get gets getting give going gone gonna good got great guys had has have having here him his how
into its just keep kind know last left let like little look looking made make makes many may maybe mean more most much must
myself need never next nobody not nothing now off okay once one only onto other our out over own people put really right
said same say says see she should since some someone something still such sure take than that the their them then there
these they thing things think this those though through time too took try under until upon use very want wanna was way we
well went were what when where whether which while who whole why will with without won would yeah yes you your
hai kya yaar nahi nahin bhai toh aur mein tha kar karo kiya ho hain bhi koi abhi main mera tera apna isko usko jab tab phir
woh wo ye yeh hum tum aap kuch sab bohot bahut matlab accha acha theek lekin par kyun kaise
actual actually literally basically honestly probably definitely obviously seriously totally bro dude man chat stream video
fuck fucking fucked shit damn bitch bitches ass dick pussy sex nigga nigger niggas porn rape
""".split())

CATEGORY_TAGS = {"funny": "funny", "fail": "fails", "clutch": "clutch", "rage": "rage", "shock": "shocking",
                 "take": "hottake", "story": "storytime", "humour": "funny", "reaction": "reaction",
                 "opinion": "hottake", "insight": "storytime", "action": "clutch"}
FILLER = ("shorts", "clips", "viral")
CHANNEL_NOISE = {"live", "official", "tv", "gaming", "clips", "channel", "highlights", "youtube", "twitch", "kick", "tiktok", "stream"}
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{2,19}")
_NUMBERED = re.compile(r"\b([A-Za-z]{2,12})\s+(\d{1,2})\b")
_TAG_IN_TEXT = re.compile(r"(?:^|\s)#\w+")

# Inlined from PeakCut games.py: name, aliases, tags.
_GAMES = [
    ("Grand Theft Auto VI", ["grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6", "gta6"],
     ["GTA6", "GTA6Game", "ViceCity", "RockstarGames", "GTA6Launch"]),
    ("Grand Theft Auto V", ["grand theft auto v", "grand theft auto 5", "gta online", "gta v", "gta 5", "gta5", "gtav"],
     ["GTA5", "GTAOnline", "RockstarGames", "GTA5RP"]),
    ("Grand Theft Auto IV", ["grand theft auto iv", "gta 4", "gta4"], ["GTA4", "RockstarGames"]),
    ("Fortnite", ["fortnite"], ["Fortnite", "FortniteOG", "BattleRoyale"]),
    ("Minecraft", ["minecraft"], ["Minecraft", "MinecraftGame"]),
    ("Valorant", ["valorant"], ["Valorant", "ValRadiant"]),
    ("League of Legends", ["league of legends", "league of legend"], ["LeagueOfLegends", "LoL", "RiotGames"]),
    ("Counter-Strike", ["counter-strike", "counter strike", "cs2", "csgo"], ["CS2", "CounterStrike", "Esports"]),
    ("Elden Ring", ["elden ring"], ["EldenRing", "SoulsLike", "FromSoftware"]),
    ("Palworld", ["palworld"], ["Palworld", "Survival"]),
    ("Call of Duty", ["call of duty", "warzone"], ["CallOfDuty", "Warzone", "COD"]),
    ("Helldivers 2", ["helldivers 2", "helldivers2"], ["Helldivers2", "StarshipTroopers", "CoOp"]),
    ("Cyberpunk 2077", ["cyberpunk 2077", "cyberpunk"], ["Cyberpunk2077", "NightCity"]),
    ("Starfield", ["starfield"], ["Starfield", "Bethesda"]),
    ("Black Myth Wukong", ["black myth", "wukong"], ["BlackMythWukong", "ActionRPG"]),
    ("Marvel Rivals", ["marvel rivals"], ["MarvelRivals", "Heroes"]),
    ("Overwatch 2", ["overwatch 2", "overwatch"], ["Overwatch2", "OW2", "Blizzard"]),
    ("ARK", ["ark: survival", "ark survival"], ["ARK", "Survival"]),
    ("Dead by Daylight", ["dead by daylight"], ["DeadByDaylight", "Asymmetric"]),
    ("Rocket League", ["rocket league"], ["RocketLeague", "CarSports"]),
    ("Apex Legends", ["apex legends"], ["ApexLegends", "EApex"]),
    ("PUBG", ["pubg", "playerunknown"], ["PUBG", "BattleRoyale"]),
    ("Grand Theft Auto: San Andreas", ["grand theft auto san andreas", "gta sa"], ["GTA", "SanAndreas"]),
]
_WORD_NORM = re.compile(r"[^a-z0-9]+")


def _norm(text) -> str:
    return _WORD_NORM.sub(" ", str(text or "").lower()).strip()


def _game_tags(game) -> list:
    """The tags for a game, whether it is named in full, short, or by one of its tags."""
    if not game:
        return []
    g = _norm(game)
    for name, aliases, tags in _GAMES:
        if g == _norm(name) or g in {_norm(a) for a in aliases} or g in {_norm(t) for t in tags}:
            return list(tags)
    cleaned = re.sub(r"\W", "", str(game))[:30]
    return [cleaned] if cleaned else []


def detect_game(*texts):
    """The game named in the text, and the tags that belong on its posts.
    Returns (None, []) when no known game is named."""
    hay = _norm(" ".join(str(t or "") for t in texts))
    if not hay:
        return None, []
    aliases = sorted(((alias, name, tags) for name, al, tags in _GAMES for alias in al), key=lambda a: -len(a[0]))
    for alias, name, tags in aliases:
        if re.search(r"(?:^| )" + re.escape(_norm(alias)) + r"(?: |$)", hay):
            return name, list(tags)
    return None, []


def _tag(s) -> str:
    """One tag: letters and digits only, lower case, not too short or long, never just a number or a stop word."""
    t = re.sub(r"[^A-Za-z0-9]", "", str(s or "")).lower()[:30]
    return "" if len(t) < 3 or t.isdigit() or t in STOP else t


def channel_tag(uploader) -> str:
    """The channel as one tag: "Kai Cenat Live" becomes kaicenat."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", str(uploader or "")) if w.lower() not in CHANNEL_NOISE]
    t = _tag("".join(words))
    return t if 3 <= len(t) <= 25 else ""


def numbered(title) -> list:
    """Names with a number in the title ("GTA 6", "Fortnite 2") as one tag each."""
    return [_tag(a + b) for a, b in _NUMBERED.findall(str(title or ""))
            if a.lower() not in STOP and a.lower() != "highlight"]


def keywords(text, title="", count=2) -> list:
    """The words that matter most in what was said: said more than once, or a name (capitalised in the middle
    of a sentence), and more so if the title has them too."""
    raw = str(text or "").split()
    in_title = {w.lower() for w in _WORD.findall(str(title or ""))}
    score = {}
    for i, r in enumerate(raw):
        m = _WORD.search(r)
        if not m:
            continue
        w = m.group(0).strip("'").lower()
        if w in STOP or len(w) < 4 or not _tag(w):
            continue
        name = r[:1].isupper() and i > 0 and raw[i - 1][-1:] not in ".?!"
        score[w] = score.get(w, 0) + 1 + (2 if name else 0) + (2 if w in in_title else 0)
    best = sorted((w for w in score if score[w] >= 2), key=lambda w: (-score[w], -len(w), w))
    return best[:count]


def make(title, transcript, category="", game=None, uploader="", ai_tags=(), limit=MOST) -> list:
    """Three to five tags for one clip, most relevant first. Tags have NO '#' prefix.

    `game` may be a game name/alias (its main tag is used) or None, in which
    case a game named in the title/transcript is detected automatically.
    """
    out = []

    def add(t):
        t = _tag(t)
        if t and t not in out and len(out) < limit:
            out.append(t)

    game_name, game_tags = (game, _game_tags(game)) if game else detect_game(title, transcript)
    for t in game_tags[:1]:       # the game's main tag; its look-alikes would only use up the room
        add(t)
    add(channel_tag(uploader))
    for t in list(ai_tags or [])[:2]:
        add(t)
    for t in keywords(transcript, title):
        add(t)
    for t in numbered(title)[:1]:
        add(t)
    add(CATEGORY_TAGS.get(str(category or "").lower(), ""))
    for t in FILLER:
        if len(out) < LEAST:
            add(t)
    return out


def apply(clip: dict, title=None, transcript=None, uploader="", game=None) -> dict:
    """Give a clip its tags and put them at the end of its description.

    Sets clip["hashtags"] = make(...) if it is empty (existing tags are kept
    and used as AI tags otherwise), then appends a " #tag #tag" line to
    clip["description"] (existing hashtag tokens in the description are
    stripped first so re-applying never duplicates them). A description the
    person wrote themselves (`fixed_description`) is left exactly as written.
    """
    if not isinstance(clip, dict) or clip.get("fixed_description"):
        return clip
    title = clip.get("title", "") if title is None else title
    if transcript is None:
        transcript = clip.get("transcript", "") or clip.get("description", "")
    existing = [str(t) for t in (clip.get("hashtags") or [])]
    if existing:
        tags = []
        for t in existing:
            t = _tag(t)
            if t and t not in tags:
                tags.append(t)
        tags = tags[:MOST]
    else:
        tags = make(title, transcript, clip.get("kind", "") or clip.get("category", "other"),
                    game, uploader, existing)
    clip["hashtags"] = tags
    if tags:
        desc = _TAG_IN_TEXT.sub("", str(clip.get("description") or "")).strip()
        line = " ".join("#" + t for t in tags)
        clip["description"] = (desc + " " + line).strip() if desc else line
        clip["hashtags_in_description"] = True
    return clip
