import os
import asyncio
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import os
os.environ["PATH"] += os.pathsep + "/usr/bin"

import discord
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.5"))
AUTO_DISCONNECT_SECONDS = int(os.getenv("AUTO_DISCONNECT_SECONDS", "20"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が設定されていません。")

AUDIO_DIR = Path("audio")

TRACKS = {
    "animal": {
        "label": "どうぶつの森🌳",
        "file": AUDIO_DIR / "animal.mp3",
        "description": "あのマスターの喫茶店です",
    },
    "fire": {
        "label": "焚き火🔥",
        "file": AUDIO_DIR / "fire.mp3",
        "description": "やさしい火の音",
    },
    "water": {
        "label": "雨の日☔",
        "file": AUDIO_DIR / "water.mp3",
        "description": "静かな雨音",
    },
    "brown": {
        "label": "ブラウンノイズ",
        "file": AUDIO_DIR / "brown.mp3",
        "description": "集中向けの低域ノイズ",
    },
}

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@dataclass
class GuildPlayerState:
    current_track_key: Optional[str] = None
    loop_enabled: bool = False
    volume: float = DEFAULT_VOLUME
    panel_channel_id: Optional[int] = None
    panel_message_id: Optional[int] = None
    disconnect_task: Optional[asyncio.Task] = None


guild_states: dict[int, GuildPlayerState] = {}


def get_state(guild_id: int) -> GuildPlayerState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildPlayerState()
    return guild_states[guild_id]


def track_exists(track_key: str) -> bool:
    return TRACKS[track_key]["file"].exists()


def make_embed(guild: discord.Guild) -> discord.Embed:
    state = get_state(guild.id)

    now_playing = "なし"
    if state.current_track_key:
        now_playing = TRACKS[state.current_track_key]["label"]

    loop_text = "ON" if state.loop_enabled else "OFF"
    volume_percent = int(state.volume * 100)

    embed = discord.Embed(
        title="BGM選択パネル",
        description="VCに参加してから、下のメニューでBGMを選んでください。",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="選べるBGM",
        value="\n".join(f"・{TRACKS[key]['label']}" for key in TRACKS),
        inline=False,
    )
    embed.add_field(name="再生中", value=now_playing, inline=True)
    embed.add_field(name="ループ", value=loop_text, inline=True)
    embed.add_field(name="音量", value=f"{volume_percent}%", inline=True)
    embed.set_footer(text="VCにいる人が選ぶと、そのVCで再生されます。")
    return embed


async def update_panel_message(guild: discord.Guild):
    state = get_state(guild.id)
    if not state.panel_channel_id or not state.panel_message_id:
        return

    channel = guild.get_channel(state.panel_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(state.panel_message_id)
        await message.edit(embed=make_embed(guild), view=MusicPanelView())
    except Exception:
        return


async def ensure_user_in_voice(interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return None

    voice_state = interaction.user.voice
    if not voice_state or not voice_state.channel:
        if not interaction.response.is_done():
            await interaction.response.send_message("先にVCへ参加してください。", ephemeral=True)
        else:
            await interaction.followup.send("先にVCへ参加してください。", ephemeral=True)
        return None

    if not isinstance(voice_state.channel, discord.VoiceChannel):
        if not interaction.response.is_done():
            await interaction.response.send_message("通常のボイスチャンネルで使ってください。", ephemeral=True)
        else:
            await interaction.followup.send("通常のボイスチャンネルで使ってください。", ephemeral=True)
        return None

    return voice_state.channel


async def connect_or_move_to_user(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.guild:
        return None

    target_channel = await ensure_user_in_voice(interaction)
    if not target_channel:
        return None

    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await target_channel.connect()

    if voice_client.channel and voice_client.channel.id != target_channel.id:
        await voice_client.move_to(target_channel)

    return voice_client


def build_audio_source(guild_id: int, track_key: str) -> discord.AudioSource:
    state = get_state(guild_id)
    file_path = TRACKS[track_key]["file"]

    return discord.FFmpegOpusAudio(
        source=str(file_path),
        options=f'-vn -filter:a "volume={state.volume}"'
    )


async def start_playback(guild: discord.Guild, voice_client: discord.VoiceClient, track_key: str):
    state = get_state(guild.id)
    state.current_track_key = track_key

    def after_playing(error: Optional[Exception]):
        if error:
            print(f"再生エラー: {error}")

        async def _continue():
            if not guild.voice_client:
                return

            current_state = get_state(guild.id)
            if current_state.loop_enabled and current_state.current_track_key == track_key:
                try:
                    new_source = build_audio_source(guild.id, track_key)
                    guild.voice_client.play(new_source, after=after_playing)
                except Exception as exc:
                    print(f"ループ再生エラー: {exc}")
            else:
                await update_panel_message(guild)

        asyncio.run_coroutine_threadsafe(_continue(), bot.loop)

    if voice_client.is_playing():
        voice_client.stop()

    source = build_audio_source(guild.id, track_key)
    voice_client.play(source, after=after_playing)
    await update_panel_message(guild)


async def schedule_disconnect_if_empty(guild: discord.Guild):
    state = get_state(guild.id)

    if state.disconnect_task and not state.disconnect_task.done():
        state.disconnect_task.cancel()

    async def _task():
        try:
            await asyncio.sleep(AUTO_DISCONNECT_SECONDS)
            voice_client = guild.voice_client
            if not voice_client or not voice_client.channel:
                return

            human_members = [m for m in voice_client.channel.members if not m.bot]
            if len(human_members) == 0:
                if voice_client.is_playing():
                    voice_client.stop()
                await voice_client.disconnect()
                state.current_track_key = None
                await update_panel_message(guild)
        except asyncio.CancelledError:
            return

    state.disconnect_task = asyncio.create_task(_task())


def cancel_disconnect_task(guild_id: int):
    state = get_state(guild_id)
    if state.disconnect_task and not state.disconnect_task.done():
        state.disconnect_task.cancel()


class MusicSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=TRACKS[key]["label"],
                value=key,
                description=TRACKS[key]["description"][:100],
            )
            for key in TRACKS
        ]

        super().__init__(
            placeholder="流したいBGMを選んでください",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="persistent_bgm_select",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return

        await interaction.response.defer(ephemeral=True)

        track_key = self.values[0]

        if not track_exists(track_key):
            await interaction.followup.send(
                f"ファイルが見つかりません: `{TRACKS[track_key]['file']}`",
                ephemeral=True,
            )
            return

        voice_client = await connect_or_move_to_user(interaction)
        if voice_client is None:
            return

        cancel_disconnect_task(interaction.guild.id)
        await start_playback(interaction.guild, voice_client, track_key)

        await interaction.followup.send(
            f"再生開始: **{TRACKS[track_key]['label']}**",
            ephemeral=True,
        )


class StopButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="停止",
            emoji="⏹️",
            style=discord.ButtonStyle.secondary,
            custom_id="persistent_bgm_stop",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.guild.voice_client:
            await interaction.response.send_message("今は再生していません。", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.stop()

        state = get_state(interaction.guild.id)
        state.current_track_key = None
        await update_panel_message(interaction.guild)
        await interaction.response.send_message("再生を停止しました。", ephemeral=True)


class LeaveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="退出",
            emoji="👋",
            style=discord.ButtonStyle.danger,
            custom_id="persistent_bgm_leave",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.guild.voice_client:
            await interaction.response.send_message("ボットはVCに入っていません。", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.stop()

        await vc.disconnect()
        state = get_state(interaction.guild.id)
        state.current_track_key = None
        await update_panel_message(interaction.guild)
        await interaction.response.send_message("VCから退出しました。", ephemeral=True)


class LoopToggleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="ループ切替",
            emoji="🔁",
            style=discord.ButtonStyle.primary,
            custom_id="persistent_bgm_loop_toggle",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return

        state = get_state(interaction.guild.id)
        state.loop_enabled = not state.loop_enabled
        await update_panel_message(interaction.guild)

        status = "ON" if state.loop_enabled else "OFF"
        await interaction.response.send_message(f"ループを **{status}** にしました。", ephemeral=True)


class NowPlayingButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="再生中を表示",
            emoji="📊",
            style=discord.ButtonStyle.success,
            custom_id="persistent_bgm_now_playing",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return

        state = get_state(interaction.guild.id)
        if state.current_track_key:
            text = TRACKS[state.current_track_key]["label"]
        else:
            text = "現在、再生中の曲はありません。"

        await interaction.response.send_message(text, ephemeral=True)


class MusicPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(MusicSelect())
        self.add_item(LoopToggleButton())
        self.add_item(NowPlayingButton())
        self.add_item(StopButton())
        self.add_item(LeaveButton())


@bot.event
async def on_ready():
    bot.add_view(MusicPanelView())

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Guild sync complete: {len(synced)} commands")
        else:
            synced = await bot.tree.sync()
            print(f"Global sync complete: {len(synced)} commands")
    except Exception as exc:
        print(f"コマンド同期エラー: {exc}")

    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or not member.guild:
        return

    guild = member.guild
    voice_client = guild.voice_client
    if not voice_client or not voice_client.channel:
        return

    human_members = [m for m in voice_client.channel.members if not m.bot]
    if len(human_members) > 0:
        cancel_disconnect_task(guild.id)
        return

    await schedule_disconnect_if_empty(guild)


@bot.tree.command(name="panel", description="このチャンネルに常設BGMパネルを設置します")
async def panel(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("サーバーのテキストチャンネルで使ってください。", ephemeral=True)
        return

    state = get_state(interaction.guild.id)
    embed = make_embed(interaction.guild)
    view = MusicPanelView()

    await interaction.response.send_message(embed=embed, view=view)
    sent_message = await interaction.original_response()

    state.panel_channel_id = interaction.channel.id
    state.panel_message_id = sent_message.id

    await sent_message.reply("このメッセージをピン留めすると使いやすくなります。", mention_author=False)


@bot.tree.command(name="volume", description="音量を変更します（0〜200）")
@discord.app_commands.describe(percent="音量をパーセントで指定します。例: 50")
async def volume(interaction: discord.Interaction, percent: int):
    if not interaction.guild:
        return

    if percent < 0 or percent > 200:
        await interaction.response.send_message("0〜200 の範囲で指定してください。", ephemeral=True)
        return

    state = get_state(interaction.guild.id)
    state.volume = percent / 100.0

    vc = interaction.guild.voice_client
    if vc and state.current_track_key and vc.is_playing():
        current_track = state.current_track_key
        await start_playback(interaction.guild, vc, current_track)

    await update_panel_message(interaction.guild)
    await interaction.response.send_message(f"音量を **{percent}%** にしました。", ephemeral=True)


@bot.tree.command(name="loop", description="ループ再生をON/OFFします")
async def loop(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = get_state(interaction.guild.id)
    state.loop_enabled = not state.loop_enabled
    await update_panel_message(interaction.guild)

    status = "ON" if state.loop_enabled else "OFF"
    await interaction.response.send_message(f"ループを **{status}** にしました。", ephemeral=True)


@bot.tree.command(name="nowplaying", description="今流れている曲を表示します")
async def nowplaying(interaction: discord.Interaction):
    if not interaction.guild:
        return

    state = get_state(interaction.guild.id)
    if state.current_track_key:
        text = f"再生中: **{TRACKS[state.current_track_key]['label']}**"
    else:
        text = "現在、再生中の曲はありません。"

    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="join", description="自分がいるVCにボットを参加させます")
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    voice_client = await connect_or_move_to_user(interaction)
    if voice_client is None:
        return

    cancel_disconnect_task(interaction.guild.id)
    await interaction.followup.send(f"**{voice_client.channel.name}** に参加しました。", ephemeral=True)


@bot.tree.command(name="stop", description="再生を停止します")
async def stop(interaction: discord.Interaction):
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message("今は再生していません。", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc.is_playing():
        vc.stop()

    state = get_state(interaction.guild.id)
    state.current_track_key = None
    await update_panel_message(interaction.guild)
    await interaction.response.send_message("再生を停止しました。", ephemeral=True)


@bot.tree.command(name="leave", description="VCから退出します")
async def leave(interaction: discord.Interaction):
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message("ボットはVCに入っていません。", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc.is_playing():
        vc.stop()

    await vc.disconnect()
    state = get_state(interaction.guild.id)
    state.current_track_key = None
    await update_panel_message(interaction.guild)
    await interaction.response.send_message("VCから退出しました。", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)