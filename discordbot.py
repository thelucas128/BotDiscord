import os
import discord
from discord.ext import commands
import aiohttp
import asyncio
from datetime import datetime, timezone

TOKEN = os.getenv('DISCORD_TOKEN')
API_URL = os.getenv('API_URL', 'http://localhost:10000').rstrip('/')

intents = discord.Intents.default()
intents.message_content = True

# Utilisation de commands.Bot pour gérer les cooldowns et slash commands
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Whitelist des utilisateurs ---
WHITELIST = {822139237184962561, 967323810716069909}

def is_whitelisted(user_id: int) -> bool:
    return user_id in WHITELIST


# =====================================================================
# Config menu getbans
# =====================================================================
PER_PAGE = 5
VIEW_TIMEOUT = 180  # secondes

TYPE_LABELS = {
    "all": "Tous",
    "perm": "Permanent",
    "temp": "Temporaire",
}
SORT_LABELS = {
    "recent": "Plus récents",
    "old": "Plus anciens",
    "az": "A → Z",
    "za": "Z → A",
}


# =====================================================================
# Helpers API
# =====================================================================
async def fetch_bans() -> list:
    """Récupère la banlist brute depuis l'API."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(f"{API_URL}/get-bans") as resp:
            if resp.status != 200:
                raise RuntimeError(f"API HTTP {resp.status}")
            data = await resp.json()
            bans = data.get("banlist", [])
            return bans if isinstance(bans, list) else []


async def post_command(action: str, username: str, moderator) -> dict:
    """Envoie une commande unban/untimeban à l'API. Retourne le JSON de réponse."""
    # NOTE: payload volontairement "plat" car apiserver.py et le script
    # Roblox s'attendent à {action, username, ...} (pas {type, args}).
    payload = {
        "action": action,
        "username": username,
        "moderator": moderator.display_name,
        "moderator_id": moderator.id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.post(f"{API_URL}/add-command", json=payload) as resp:
            # L'API peut renvoyer du non-JSON en cas d'erreur proxy : on sécurise.
            try:
                return await resp.json()
            except Exception:
                return {"status": "error", "message": f"HTTP {resp.status}"}


# =====================================================================
# Helpers bans : normalisation / tri / filtre
# =====================================================================
def _first_present(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_ts(value) -> int | None:
    """Tente de convertir une date hétérogène en timestamp int. None si impossible."""
    if value is None:
        return None
    # timestamp numérique direct
    if isinstance(value, (int, float)):
        ts = int(value)
        # millisecondes -> secondes
        if ts > 10_000_000_000:
            ts //= 1000
        return ts if ts > 0 else None
    s = str(value).strip()
    if not s:
        return None
    # numérique en string ?
    try:
        num = float(s)
        ts = int(num)
        if ts > 10_000_000_000:
            ts //= 1000
        if ts > 0:
            return ts
    except ValueError:
        pass
    # ISO 8601 (avec ou sans Z)
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass
    # formats courants
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def detect_ban_type(raw: dict) -> str:
    """Retourne 'perm', 'temp' ou 'unknown'."""
    if not isinstance(raw, dict):
        return "unknown"
    lowered = {str(k).lower(): v for k, v in raw.items()}

    for k in ("ispermanent", "permanent", "is_perm", "isperma"):
        if k in lowered:
            return "perm" if bool(lowered[k]) else "temp"

    t = str(lowered.get("type", lowered.get("bantype", lowered.get("ban_type", "")))).lower()
    if t in ("perm", "permanent", "perma", "definitive", "definitif"):
        return "perm"
    if t in ("temp", "temporary", "temporaire", "timeban", "time-ban", "timed"):
        return "temp"

    # expiry / duration => temporaire
    expiry = _first_present(raw, "expiry", "expiresAt", "expires_at", "unbanAt", "unban_at", "endsAt", "endAt")
    if expiry is not None and parse_ts(expiry) is not None:
        return "temp"
    duration = _first_present(raw, "duration", "banDuration", "timeLeft")
    if isinstance(duration, (int, float)) and duration > 0:
        return "temp"

    return "unknown"


def normalize_ban(raw, index: int) -> dict:
    """Normalise une entrée (dict ou string) en structure homogène."""
    if not isinstance(raw, dict):
        return {
            "index": index,
            "username": str(raw),
            "reason": "Aucune raison",
            "date_raw": "Date inconnue",
            "ts": None,
            "ban_type": "unknown",
            "moderator": None,
            "expiry_raw": None,
            "expiry_ts": None,
            "user_id": None,
            "raw": raw,
        }
    username = str(_first_present(raw, "username", "user", "name", "player", "roblox_username", default="Inconnu"))
    reason = str(_first_present(raw, "reason", "banReason", "ban_reason", default="Aucune raison"))
    date_raw = _first_present(raw, "date", "createdAt", "created_at", "timestamp", "bannedAt", "banned_at", "banDate", default="Date inconnue")
    moderator = _first_present(raw, "moderator", "moderatedBy", "bannedBy", "banned_by", "by", "staff", default=None)
    expiry_raw = _first_present(raw, "expiry", "expiresAt", "expires_at", "unbanAt", "unban_at", "endsAt", default=None)
    user_id = _first_present(raw, "userId", "user_id", "robloxId", "playerId", default=None)

    return {
        "index": index,
        "username": username,
        "reason": reason,
        "date_raw": date_raw,
        "ts": parse_ts(date_raw) if date_raw != "Date inconnue" else None,
        "ban_type": detect_ban_type(raw),
        "moderator": str(moderator) if moderator is not None else None,
        "expiry_raw": expiry_raw,
        "expiry_ts": parse_ts(expiry_raw) if expiry_raw is not None else None,
        "user_id": str(user_id) if user_id is not None else None,
        "raw": raw,
    }


TYPE_EMOJI = {"perm": "🔴", "temp": "🟠", "unknown": "⚪"}


def format_date(ts: int | None, fallback) -> str:
    if ts is None:
        return str(fallback)
    return f"<t:{ts}:f> (<t:{ts}:R>)"


def truncate(s: str, n: int = 180) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def filter_bans(bans: list[dict], search: str | None, ban_type: str) -> list[dict]:
    out = bans
    if ban_type in ("perm", "temp"):
        out = [b for b in out if b["ban_type"] == ban_type]
    if search:
        q = search.strip().lower()
        if q:
            out = [
                b for b in out
                if q in b["username"].lower()
                or q in str(b["reason"]).lower()
                or q in str(b["date_raw"]).lower()
                or (b["moderator"] and q in b["moderator"].lower())
                or (b["user_id"] and q in b["user_id"].lower())
            ]
    return out


def sort_bans(bans: list[dict], sort: str) -> list[dict]:
    if sort == "az":
        return sorted(bans, key=lambda b: b["username"].lower())
    if sort == "za":
        return sorted(bans, key=lambda b: b["username"].lower(), reverse=True)
    if sort == "old":
        return sorted(bans, key=lambda b: (b["ts"] is None, b["ts"] or 0))
    # recent par défaut : ts desc, sans date à la fin
    return sorted(bans, key=lambda b: (b["ts"] is None, -(b["ts"] or 0)))


def compute_stats(bans: list[dict]) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "total": len(bans),
        "perm": sum(1 for b in bans if b["ban_type"] == "perm"),
        "temp": sum(1 for b in bans if b["ban_type"] == "temp"),
        "day": sum(1 for b in bans if b["ts"] and b["ts"] >= now - 86400),
    }


def build_list_embed(bans_page: list[dict], *, page: int, total_pages: int,
                     total_results: int, stats: dict, search: str | None,
                     ban_type: str, sort: str, requester) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔨 Bannissements — {total_results} résultat(s)",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    filter_line = f"Filtre : **{TYPE_LABELS.get(ban_type, ban_type)}** • Tri : **{SORT_LABELS.get(sort, sort)}**"
    if search:
        filter_line += f" • 🔎 `{truncate(search, 40)}`"
    embed.description = (
        f"🔴 **{stats['perm']}** perm • 🟠 **{stats['temp']}** temp • 🆕 **{stats['day']}** dernières 24h\n"
        f"{filter_line}"
    )
    if not bans_page:
        embed.add_field(
            name="😶 Aucun résultat",
            value="Essaie d'élargir la recherche, de changer le filtre de type, "
                  "ou clique sur **🔄 Actualiser** après un changement en jeu.",
            inline=False,
        )
    else:
        start_num = page * PER_PAGE
        for i, b in enumerate(bans_page, start=start_num + 1):
            emoji = TYPE_EMOJI.get(b["ban_type"], "⚪")
            type_word = {"perm": "Perm", "temp": "Temp"}.get(b["ban_type"], "—")
            name = f"{emoji} #{i} • {truncate(b['username'], 30)} [{type_word}]"
            lines = [
                f"📝 {truncate(b['reason'], 180)}",
                f"📅 {format_date(b['ts'], b['date_raw'])}",
            ]
            if b["moderator"]:
                lines.append(f"👮 Par {truncate(b['moderator'], 30)}")
            if b["ban_type"] == "temp" and b["expiry_ts"]:
                lines.append(f"⏳ Expire <t:{b['expiry_ts']}:R>")
            embed.add_field(name=name, value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Page {page + 1}/{max(total_pages, 1)} • Demandé par {requester.display_name}")
    return embed


def build_detail_embed(b: dict) -> discord.Embed:
    emoji = TYPE_EMOJI.get(b["ban_type"], "⚪")
    type_word = {"perm": "Permanent 🔴", "temp": "Temporaire 🟠"}.get(b["ban_type"], "Inconnu ⚪")
    embed = discord.Embed(
        title=f"{emoji} {b['username']}",
        color=discord.Color.red() if b["ban_type"] == "perm" else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="📝 Raison", value=str(b["reason"])[:1024] or "Aucune raison", inline=False)
    embed.add_field(name="📌 Type", value=type_word, inline=True)
    embed.add_field(name="📅 Date du ban", value=format_date(b["ts"], b["date_raw"]), inline=True)
    if b["expiry_ts"]:
        embed.add_field(name="⏳ Expire", value=f"<t:{b['expiry_ts']}:f> (<t:{b['expiry_ts']}:R>)", inline=True)
    if b["moderator"]:
        embed.add_field(name="👮 Modérateur", value=str(b["moderator"])[:256], inline=True)
    if b["user_id"]:
        embed.add_field(name="🆔 User ID", value=f"`{b['user_id']}`", inline=True)
    embed.set_footer(text="Utilise les boutons ci-dessous pour unban / untimeban")
    return embed


# =====================================================================
# UI : modal recherche
# =====================================================================
class SearchModal(discord.ui.Modal):
    def __init__(self, parent_view: "BanListView"):
        super().__init__(title="Rechercher un ban")
        self.parent_view = parent_view
        self.add_item(discord.ui.InputText(
            label="Recherche (pseudo, raison, modo, date…)",
            placeholder="Ex: xX_Kevin, exploit, 12/03… (vide = tout afficher)",
            required=False,
            max_length=100,
        ))

    async def callback(self, interaction: discord.Interaction):
        query = (self.children[0].value or "").strip() or None
        self.parent_view.search = query
        self.parent_view.page = 0
        await self.parent_view.refresh_message(interaction)


# =====================================================================
# UI : actions rapides sur le détail (éphémère)
# =====================================================================
class BanDetailActionsView(discord.ui.View):
    def __init__(self, username: str):
        super().__init__(timeout=120)
        self.username = username

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_whitelisted(interaction.user.id):
            await interaction.response.send_message("❌ Non autorisé.", ephemeral=True)
            return False
        return True

    async def _run(self, interaction: discord.Interaction, action: str, ok_title: str, ok_desc: str):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await post_command(action, self.username, interaction.user)
        except Exception as e:
            await interaction.followup.send(f"❌ Erreur lors de la requête : `{e}`", ephemeral=True)
            return
        if result.get("status") == "success":
            embed = discord.Embed(title=ok_title, description=ok_desc, color=discord.Color.green(),
                                  timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=f"Action par {interaction.user.display_name}",
                             icon_url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Erreur : `{result.get('message', 'Unknown error')}`", ephemeral=True)

    @discord.ui.button(label="Unban", emoji="✅", style=discord.ButtonStyle.success)
    async def unban_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._run(interaction, "unban", "✅ Unban envoyé",
                        f"Unban pour **{self.username}** envoyé au jeu.")

    @discord.ui.button(label="Un-timeban", emoji="⏳", style=discord.ButtonStyle.primary)
    async def untimeban_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._run(interaction, "untimeban", "✅ Timeban retiré",
                        f"Timeban retiré pour **{self.username}**.")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# =====================================================================
# UI : selects (filtre / tri / détail)
# =====================================================================
class BanTypeSelect(discord.ui.Select):
    def __init__(self, parent_view: "BanListView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="Tous les bans", value="all",
                                 description="Permanents + temporaires", emoji="📋",
                                 default=parent_view.ban_type == "all"),
            discord.SelectOption(label="Permanents", value="perm",
                                 description="Bans définitifs uniquement", emoji="🔴",
                                 default=parent_view.ban_type == "perm"),
            discord.SelectOption(label="Temporaires", value="temp",
                                 description="Timebans uniquement", emoji="🟠",
                                 default=parent_view.ban_type == "temp"),
        ]
        super().__init__(placeholder="📋 Filtrer par type…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.ban_type = self.values[0]
        self.parent_view.page = 0
        await self.parent_view.refresh_message(interaction)


class BanSortSelect(discord.ui.Select):
    def __init__(self, parent_view: "BanListView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="Plus récents", value="recent", emoji="🆕",
                                 default=parent_view.sort == "recent"),
            discord.SelectOption(label="Plus anciens", value="old", emoji="📜",
                                 default=parent_view.sort == "old"),
            discord.SelectOption(label="Pseudo A → Z", value="az", emoji="🔤",
                                 default=parent_view.sort == "az"),
            discord.SelectOption(label="Pseudo Z → A", value="za", emoji="🔡",
                                 default=parent_view.sort == "za"),
        ]
        super().__init__(placeholder="↕️ Trier par…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.sort = self.values[0]
        self.parent_view.page = 0
        await self.parent_view.refresh_message(interaction)


class BanDetailSelect(discord.ui.Select):
    def __init__(self, parent_view: "BanListView", page_bans: list[dict], page_start: int):
        self.parent_view = parent_view
        if page_bans:
            options = [
                discord.SelectOption(
                    label=truncate(b["username"], 50),
                    value=str(b["index"]),
                    description=truncate(b["reason"], 60),
                    emoji=TYPE_EMOJI.get(b["ban_type"], "⚪"),
                )
                for b in page_bans
            ]
            super().__init__(placeholder="🔍 Voir le détail d'un ban…", options=options, row=2)
        else:
            super().__init__(placeholder="🔍 Aucun ban sur cette page", options=[
                discord.SelectOption(label="Aucun résultat", value="none")
            ], disabled=True, row=2)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("Aucun ban à afficher.", ephemeral=True)
            return
        idx = int(self.values[0])
        target = next((b for b in self.parent_view.all_bans if b["index"] == idx), None)
        if target is None:
            await interaction.response.send_message("❌ Ban introuvable (liste actualisée ?).", ephemeral=True)
            return
        embed = build_detail_embed(target)
        await interaction.response.send_message(
            embed=embed,
            view=BanDetailActionsView(target["username"]),
            ephemeral=True,
        )


# =====================================================================
# UI : vue principale paginée
# =====================================================================
class BanListView(discord.ui.View):
    def __init__(self, author, all_bans: list[dict], search: str | None,
                 ban_type: str, sort: str):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.author_id = author.id
        self.author = author
        self.all_bans = all_bans
        self.search = search
        self.ban_type = ban_type if ban_type in TYPE_LABELS else "all"
        self.sort = sort if sort in SORT_LABELS else "recent"
        self.page = 0
        self.message = None  # défini après ctx.respond
        self.rebuild()

    # -- logique --
    def filtered(self) -> list[dict]:
        return sort_bans(filter_bans(self.all_bans, self.search, self.ban_type), self.sort)

    def total_pages(self) -> int:
        n = len(self.filtered())
        return max(1, (n + PER_PAGE - 1) // PER_PAGE)

    def current_page_bans(self) -> list[dict]:
        data = self.filtered()
        # clamp si la liste a rétréci (refresh / filtre)
        if self.page >= self.total_pages():
            self.page = self.total_pages() - 1
        if self.page < 0:
            self.page = 0
        return data[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]

    def make_embed(self) -> discord.Embed:
        return build_list_embed(
            self.current_page_bans(),
            page=self.page,
            total_pages=self.total_pages(),
            total_results=len(self.filtered()),
            stats=compute_stats(self.all_bans),
            search=self.search,
            ban_type=self.ban_type,
            sort=self.sort,
            requester=self.author,
        )

    def rebuild(self):
        """Reconstruit selects + états des boutons (à appeler avant chaque edit)."""
        self.clear_items()
        self.add_item(BanTypeSelect(self))
        self.add_item(BanSortSelect(self))
        self.add_item(BanDetailSelect(self, self.current_page_bans(), self.page * PER_PAGE))
        # --- navigation (row 3) ---
        first = discord.ui.Button(label="⏮️", style=discord.ButtonStyle.secondary, row=3,
                                  disabled=self.page == 0)
        prev = discord.ui.Button(label="◀️", style=discord.ButtonStyle.secondary, row=3,
                                 disabled=self.page == 0)
        counter = discord.ui.Button(label=f"{self.page + 1}/{self.total_pages()}",
                                    style=discord.ButtonStyle.secondary, row=3, disabled=True)
        nxt = discord.ui.Button(label="▶️", style=discord.ButtonStyle.secondary, row=3,
                                disabled=self.page >= self.total_pages() - 1)
        last = discord.ui.Button(label="⏭️", style=discord.ButtonStyle.secondary, row=3,
                                 disabled=self.page >= self.total_pages() - 1)
        first.callback = self.go_first
        prev.callback = self.go_prev
        nxt.callback = self.go_next
        last.callback = self.go_last
        # --- actions (row 4) ---
        refresh = discord.ui.Button(label="Actualiser", emoji="🔄",
                                    style=discord.ButtonStyle.primary, row=4)
        search_btn = discord.ui.Button(label="Rechercher", emoji="🔎",
                                       style=discord.ButtonStyle.secondary, row=4)
        close = discord.ui.Button(label="Fermer", emoji="❌",
                                  style=discord.ButtonStyle.danger, row=4)
        refresh.callback = self.do_refresh
        search_btn.callback = self.do_search
        close.callback = self.do_close
        for b in (first, prev, counter, nxt, last, refresh, search_btn, close):
            self.add_item(b)

    # -- sécurité : seul le demandeur (whitelisté) peut cliquer --
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_whitelisted(interaction.user.id):
            await interaction.response.send_message("❌ Non autorisé.", ephemeral=True)
            return False
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Seul le demandeur peut utiliser ce menu. Relance `/getbans`.", ephemeral=True)
            return False
        return True

    async def refresh_message(self, interaction: discord.Interaction):
        self.rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    # -- callbacks navigation --
    async def go_first(self, interaction: discord.Interaction):
        self.page = 0
        await self.refresh_message(interaction)

    async def go_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        await self.refresh_message(interaction)

    async def go_next(self, interaction: discord.Interaction):
        if self.page < self.total_pages() - 1:
            self.page += 1
        await self.refresh_message(interaction)

    async def go_last(self, interaction: discord.Interaction):
        self.page = self.total_pages() - 1
        await self.refresh_message(interaction)

    async def do_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            raw = await fetch_bans()
        except Exception as e:
            await interaction.followup.send(f"❌ Actualisation impossible : `{e}`", ephemeral=True)
            return
        self.all_bans = [normalize_ban(b, i) for i, b in enumerate(raw)]
        self.rebuild()
        try:
            await interaction.edit_original_response(embed=self.make_embed(), view=self)
        except Exception:
            pass

    async def do_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self))

    async def do_close(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="🗑️ Menu fermé.", embed=None, view=self)


# --- Slash command: getbans ---
@bot.slash_command(
    name="getbans",
    description="Affiche la liste des bannissements avec filtres, tri et pagination."
)
@commands.cooldown(1, 10, commands.BucketType.user)
async def getbans(
    ctx: discord.ApplicationContext,
    search: discord.Option(str, "Recherche (pseudo, raison, modo, date…)", required=False, default=None) = None,
    type: discord.Option(str, "Filtrer par type de ban", required=False, default="all",
                         choices=["all", "perm", "temp"]) = "all",
    tri: discord.Option(str, "Tri de la liste", required=False, default="recent",
                        choices=["recent", "old", "az", "za"]) = "recent",
):
    if not is_whitelisted(ctx.author.id):
        await ctx.respond("❌ Non autorisé.", ephemeral=True)
        return

    await ctx.defer()

    try:
        raw_bans = await fetch_bans()
    except Exception as e:
        await ctx.respond(f"❌ Erreur lors de la récupération : `{e}`")
        return

    if not raw_bans:
        await ctx.respond("📭 Aucun ban enregistré.")
        return

    all_bans = [normalize_ban(b, i) for i, b in enumerate(raw_bans)]
    view = BanListView(ctx.author, all_bans, search, type, tri)

    if not view.filtered():
        # On affiche quand même le menu (avec état vide) pour permettre
        # de changer le filtre / la recherche sans relancer la commande.
        pass

    await ctx.respond(embed=view.make_embed(), view=view)
    # Mémorise le message pour pouvoir griser le menu au timeout.
    try:
        view.message = await ctx.interaction.original_response()
    except Exception:
        view.message = None


# --- Slash command: unban ---
@bot.slash_command(name="unban", description="Déban un joueur Roblox.")
@commands.cooldown(1, 10, commands.BucketType.user)
async def unban(ctx: discord.ApplicationContext, roblox_username: str):
    if not is_whitelisted(ctx.author.id):
        await ctx.respond("❌ Non autorisé.", ephemeral=True)
        return

    await ctx.defer()
    try:
        result = await post_command("unban", roblox_username, ctx.author)
        if result.get("status") == "success":
            embed = discord.Embed(
                title="✅ Unban envoyé",
                description=f"Unban pour **{roblox_username}** envoyé au jeu.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Unban par {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            await ctx.respond(embed=embed)
        else:
            await ctx.respond(f"❌ Erreur: {result.get('message', 'Unknown error')}", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Erreur lors de la requête: {e}")

# --- Slash command: untimeban ---
@bot.slash_command(name="untimeban", description="Retire un timeban sur un joueur Roblox.")
@commands.cooldown(1, 10, commands.BucketType.user)
async def untimeban(ctx: discord.ApplicationContext, roblox_username: str):
    if not is_whitelisted(ctx.author.id):
        await ctx.respond("❌ Non autorisé.", ephemeral=True)
        return

    await ctx.defer()
    try:
        result = await post_command("untimeban", roblox_username, ctx.author)
        if result.get("status") == "success":
            embed = discord.Embed(
                title="✅ Timeban retiré",
                description=f"Timeban retiré pour **{roblox_username}**.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Action par {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            await ctx.respond(embed=embed)
        else:
            await ctx.respond(f"❌ Erreur: {result.get('message', 'Unknown error')}", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Erreur lors de la requête: {e}")

# --- Event: gérer les erreurs de cooldown proprement ---
@bot.event
async def on_application_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.respond(f"⏳ Cooldown: réessayez dans {int(error.retry_after)}s.", ephemeral=True)
    else:
        raise error

# --- Event on_ready ---
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")

# --- Lancement sécurisé du bot ---
async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN manquant dans l'environnement.")
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot arrêté proprement")
