import asyncio
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

# ============================================================
# CONFIG
# ============================================================

PREFIX = "g!"
TOKEN = os.getenv("DISCORD_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = BASE_DIR / "source-bot"
DEOB_SCRIPT = SOURCE_DIR / "deVirtualizer.py"

# Discord's default upload limit varies by server/boost level.
# This keeps the bot from trying to process absurdly large traces.
MAX_TRACE_SIZE = 8 * 1024 * 1024  # 8 MB

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
)

analysis_lock = asyncio.Semaphore(1)


# ============================================================
# HELPERS
# ============================================================

def clean_output():
    output = SOURCE_DIR / "output"
    if output.exists():
        shutil.rmtree(output, ignore_errors=True)


def run_devirtualizer(trace_path: Path):
    """
    Runs the original project exactly as a CLI program.

    The original program expects:
        python deVirtualizer.py -t trace.txt

    We run it with source-bot as cwd because its modules and ./output
    paths are relative to that directory.
    """
    if not DEOB_SCRIPT.exists():
        raise FileNotFoundError(
            f"Não encontrei {DEOB_SCRIPT}. "
            "Coloque o bot.py ao lado da pasta source-bot."
        )

    clean_output()

    process = subprocess.run(
        [sys.executable, str(DEOB_SCRIPT.name), "-t", str(trace_path.name)],
        cwd=str(SOURCE_DIR),
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )

    stdout = process.stdout or ""
    stderr = process.stderr or ""

    if process.returncode != 0:
        raise RuntimeError(
            f"O devirtualizador terminou com código {process.returncode}.\n"
            f"STDOUT:\n{stdout[-5000:]}\n"
            f"STDERR:\n{stderr[-5000:]}"
        )

    return stdout, stderr


def zip_output(output_dir: Path, destination: Path):
    if not output_dir.exists():
        return False

    files = [p for p in output_dir.rglob("*") if p.is_file()]
    if not files:
        return False

    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        for file in files:
            zf.write(file, file.relative_to(output_dir))

    return True


async def run_analysis_attachment(attachment: discord.Attachment):
    if attachment.size > MAX_TRACE_SIZE:
        raise ValueError(
            f"Esse arquivo tem {attachment.size / 1024 / 1024:.1f} MB. "
            f"O limite é {MAX_TRACE_SIZE / 1024 / 1024:.0f} MB."
        )

    work_dir = Path(tempfile.mkdtemp(prefix="miku_trace_"))
    safe_name = Path(attachment.filename).name
    trace_path = SOURCE_DIR / f"_discord_{safe_name}"

    try:
        data = await attachment.read()
        trace_path.write_bytes(data)

        stdout, stderr = await asyncio.to_thread(
            run_devirtualizer,
            trace_path,
        )

        return stdout, stderr, SOURCE_DIR / "output"

    finally:
        try:
            trace_path.unlink(missing_ok=True)
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)


def make_code_block(text: str, limit: int = 3800):
    text = text.strip()
    if not text:
        return "```text\n(nenhuma saída)\n```"

    if len(text) > limit:
        text = text[-limit:]
        text = "[... saída cortada ...]\n" + text

    return f"```text\n{text}\n```"


async def get_miku_image():
    """
    Waifu.im provides random SFW images and tag filtering.
    We try Hatsune Miku first and have a tag-discovery fallback.
    """
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Primary guess for the character tag.
        urls = [
            "https://api.waifu.im/images?IncludedTags=hatsune_miku&IsNsfw=False",
            "https://api.waifu.im/images?IncludedTags=miku&IsNsfw=False",
        ]

        for url in urls:
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        continue

                    data = await response.json()
                    items = data.get("items", [])

                    if items:
                        item = items[0]
                        return item["url"], item.get("source")

            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                continue

        # Last fallback: inspect the public tag list and find a Miku tag.
        try:
            async with session.get("https://api.waifu.im/tags") as response:
                if response.status == 200:
                    tags = await response.json()

                    # API versions can expose tags as a list or grouped object.
                    candidates = []

                    if isinstance(tags, list):
                        candidates = tags
                    elif isinstance(tags, dict):
                        for value in tags.values():
                            if isinstance(value, list):
                                candidates.extend(value)

                    for tag in candidates:
                        slug = str(tag.get("slug", "")).lower()
                        name = str(tag.get("name", "")).lower()

                        if "miku" in slug or "miku" in name:
                            slug = tag.get("slug")
                            if not slug:
                                continue

                            url = (
                                "https://api.waifu.im/images"
                                f"?IncludedTags={slug}&IsNsfw=False"
                            )

                            async with session.get(url) as image_response:
                                if image_response.status == 200:
                                    data = await image_response.json()
                                    items = data.get("items", [])

                                    if items:
                                        return (
                                            items[0]["url"],
                                            items[0].get("source"),
                                        )
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError):
            pass

    raise RuntimeError("Não consegui encontrar uma imagem da Hatsune Miku agora.")


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"Logado como {bot.user} | prefixo: {PREFIX}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(
            f"⚠️ Faltou um argumento. Use `{PREFIX}help` para ver como usar."
        )
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(
            f"⏳ Calma aí. Tente novamente em {error.retry_after:.1f}s."
        )
        return

    print(f"[ERRO] {type(error).__name__}: {error}")
    await ctx.reply("❌ Deu um erro ao executar esse comando.")


# ============================================================
# BASIC COMMANDS
# ============================================================

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="🎀 Miku Bot",
        description=f"Prefixo: `{PREFIX}`",
    )

    embed.add_field(
        name="🔬 Análise",
        value=(
            f"`{PREFIX}analise <arquivo>`\n"
            "Analisa um trace usando o projeto original."
        ),
        inline=False,
    )

    embed.add_field(
        name="🧪 Deob",
        value=(
            f"`{PREFIX}deob <arquivo>`\n"
            "Executa a análise e envia o conteúdo gerado em ZIP."
        ),
        inline=False,
    )

    embed.add_field(
        name="🛠️ Util",
        value=(
            f"`{PREFIX}util`\n"
            "Mostra utilidades disponíveis."
        ),
        inline=False,
    )

    embed.add_field(
        name="🎵 Miku",
        value=(
            f"`{PREFIX}miku`\n"
            "Manda uma imagem aleatória SFW da Hatsune Miku."
        ),
        inline=False,
    )

    embed.add_field(
        name="📡 Status",
        value=f"`{PREFIX}status`",
        inline=False,
    )

    embed.set_footer(text="Miku Bot • feito para o seu source-bot")

    await ctx.reply(embed=embed)


@bot.command(name="status")
async def status_command(ctx):
    latency = round(bot.latency * 1000)

    embed = discord.Embed(
        title="📡 Status do Miku Bot",
        description="🟢 Online e funcionando.",
    )
    embed.add_field(name="Ping", value=f"`{latency} ms`")
    embed.add_field(name="Servidores", value=f"`{len(bot.guilds)}`")
    embed.add_field(name="Python", value=f"`{sys.version.split()[0]}`")
    embed.add_field(name="Discord.py", value=f"`{discord.__version__}`")

    await ctx.reply(embed=embed)


@bot.command(name="util")
async def util_command(ctx):
    embed = discord.Embed(
        title="🛠️ Utilidades",
        description=(
            f"`{PREFIX}status` — status e ping\n"
            f"`{PREFIX}miku` — imagem aleatória da Miku\n"
            f"`{PREFIX}help` — lista de comandos\n\n"
            "Os comandos de análise usam o seu `source-bot`."
        ),
    )
    await ctx.reply(embed=embed)


@bot.command(name="miku", aliases=["foto", "mikuimg"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def miku_command(ctx):
    async with ctx.typing():
        image_url, source = await get_miku_image()

    embed = discord.Embed(
        title="💙 Hatsune Miku",
        description="Uma Miku aleatória para você 🎶",
    )
    embed.set_image(url=image_url)

    if source:
        embed.set_footer(text="Fonte da arte disponível no link abaixo.")
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Ver fonte",
                url=source,
                style=discord.ButtonStyle.link,
            )
        )
        await ctx.reply(embed=embed, view=view)
    else:
        await ctx.reply(embed=embed)


# ============================================================
# ANALYSIS COMMANDS
# ============================================================

async def require_attachment(ctx):
    if not ctx.message.attachments:
        await ctx.reply(
            f"📎 Anexe um arquivo de trace junto com `{PREFIX}analise` "
            f"ou `{PREFIX}deob`."
        )
        return None

    attachment = ctx.message.attachments[0]

    if attachment.size > MAX_TRACE_SIZE:
        await ctx.reply(
            f"❌ Arquivo grande demais. Limite: "
            f"{MAX_TRACE_SIZE // 1024 // 1024} MB."
        )
        return None

    return attachment


@bot.command(name="analise", aliases=["analisar", "analyze"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def analise_command(ctx):
    attachment = await require_attachment(ctx)
    if not attachment:
        return

    if analysis_lock.locked():
        await ctx.reply("⏳ Já existe uma análise em andamento. Aguarde terminar.")
        return

    async with analysis_lock:
        status_msg = await ctx.reply(
            "🔎 **Analisando...**\n"
            "O devirtualizador está processando seu trace."
        )

        try:
            stdout, stderr, output_dir = await run_analysis_attachment(attachment)

            result = stdout.strip()

            if not result:
                result = "O programa não produziu saída."

            embed = discord.Embed(
                title="🔬 Análise concluída",
                description=make_code_block(result),
            )
            embed.add_field(
                name="Arquivo",
                value=f"`{attachment.filename}`",
                inline=False,
            )

            await status_msg.edit(content="", embed=embed)

        except subprocess.TimeoutExpired:
            await status_msg.edit(
                content="⏰ A análise demorou mais de 5 minutos e foi encerrada."
            )
        except Exception as exc:
            print(f"[ANALISE] {exc}")
            await status_msg.edit(
                content=f"❌ Não foi possível analisar o arquivo:\n`{exc}`"
            )
        finally:
            clean_output()


@bot.command(name="deob", aliases=["deobfuscate", "desob"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def deob_command(ctx):
    attachment = await require_attachment(ctx)
    if not attachment:
        return

    if analysis_lock.locked():
        await ctx.reply("⏳ Já existe uma análise em andamento. Aguarde terminar.")
        return

    async with analysis_lock:
        status_msg = await ctx.reply(
            "🧪 **Processando...**\n"
            "Vou executar o devirtualizador e preparar os arquivos gerados."
        )

        try:
            stdout, stderr, output_dir = await run_analysis_attachment(attachment)

            zip_path = SOURCE_DIR / "_discord_output.zip"
            if zip_path.exists():
                zip_path.unlink()

            created = zip_output(output_dir, zip_path)

            if not created:
                result = stdout.strip() or "Nenhum arquivo foi gerado."
                await status_msg.edit(
                    content=(
                        "⚠️ A execução terminou, mas não encontrei arquivos "
                        "para enviar.\n\n" + make_code_block(result)
                    )
                )
                return

            await status_msg.edit(
                content="✅ **Processamento concluído.** Enviando os arquivos..."
            )

            await ctx.send(
                content=(
                    "📦 Resultado do `deob`.\n"
                    "Obs.: o projeto original faz análise/devirtualização "
                    "e remoção de junk; ele não é um deobfuscador universal."
                ),
                file=discord.File(zip_path, filename="miku-output.zip"),
            )

        except subprocess.TimeoutExpired:
            await status_msg.edit(
                content="⏰ O processamento passou de 5 minutos e foi encerrado."
            )
        except Exception as exc:
            print(f"[DEOB] {exc}")
            await status_msg.edit(
                content=f"❌ Não foi possível processar o arquivo:\n`{exc}`"
            )
        finally:
            try:
                (SOURCE_DIR / "_discord_output.zip").unlink(missing_ok=True)
            except Exception:
                pass
            clean_output()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "A variável DISCORD_TOKEN não foi definida. "
            "No Wispbyte, coloque seu token em Startup > Environment Variables."
        )

    if not SOURCE_DIR.exists():
        raise RuntimeError(
            "A pasta 'source-bot' não existe ao lado do bot.py."
        )

    bot.run(TOKEN)
