
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

import yt_dlp


OUTPUT_DIR = Path("legendas")

# Idioma das legendas quando o usuário não escolhe outro.
DEFAULT_LANGUAGE = "pt"

# Variantes preferidas quando o idioma pedido é genérico, nesta ordem.
PREFERRED_VARIANTS = {"pt": ["pt-BR", "pt-PT"]}

# Código de idioma aceito, ex.: pt, en, es, pt-BR ou zh-Hans.
LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*")

# Tamanho máximo de cada nome de pasta; o Windows limita caminhos a 260 caracteres.
MAX_NAME_CHARS = 80

# Início da linha do Markdown que informa a faixa usada.
LABEL_PREFIX = "Legenda: "

# Arquivos e pastas não podem conter estes caracteres.
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Linha de tempo do SRT, ex.: 00:00:01,000 --> 00:00:04,000
TIME_LINE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)

# Tags de formatação das legendas automáticas, ex.: <c> ou <00:00:01.000>.
MARKUP = re.compile(r"<[^>]+>")

# Pausa (em segundos) que separa dois parágrafos.
PARAGRAPH_PAUSE = 1.5

# Tamanho a partir do qual um parágrafo é encerrado no fim de uma frase.
MAX_PARAGRAPH_CHARS = 400


def safe_name(value: str, limit: int = MAX_NAME_CHARS) -> str:
    """Normaliza um nome para uso no sistema de arquivos."""
    value = INVALID_CHARS.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:limit].rstrip(" .") or "Sem título"


def get_info(url: str) -> dict:
    """Obtém informações do vídeo ou da playlist."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("Não foi possível obter os metadados.")

    return info


def get_videos(info: dict) -> tuple[str, list[dict]]:
    """Identifica se o link corresponde a um vídeo ou playlist."""
    if info.get("_type") == "playlist" or "entries" in info:
        title = safe_name(info.get("title") or "Playlist")
        entries = [entry for entry in (info.get("entries") or []) if entry]
        return title, entries

    return safe_name(info.get("title") or "Vídeo"), [info]


def get_video_url(video: dict) -> str | None:
    """Obtém uma URL utilizável para baixar a legenda."""
    url = video.get("webpage_url") or video.get("original_url")

    if url:
        return url

    video_id = video.get("id")
    if video_id and video.get("ie_key", "Youtube") == "Youtube":
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def find_srt(directory: Path) -> list[Path]:
    """Localiza os SRTs produzidos pelo yt-dlp."""
    return sorted(
        path
        for path in directory.glob("legenda*.srt")
        if path.is_file() and path.name != "legenda.srt"
    )


def to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_srt(text: str) -> list[tuple[float, float, list[str]]]:
    """Devolve (início, fim, linhas de texto) de cada bloco do SRT."""
    cues = []

    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()

        for position, line in enumerate(lines):
            match = TIME_LINE.search(line)
            if match:
                break
        else:
            continue

        start = to_seconds(*match.group(1, 2, 3, 4))
        end = to_seconds(*match.group(5, 6, 7, 8))
        texts = [MARKUP.sub("", line).strip() for line in lines[position + 1:]]
        cues.append((start, end, [line for line in texts if line]))

    return cues


def build_paragraphs(cues: list[tuple[float, float, list[str]]]) -> list[str]:
    """Junta as falas em parágrafos, removendo as linhas repetidas."""
    paragraphs = []
    current = []
    previous_end = 0.0
    previous_line = None

    for start, end, lines in cues:
        new_lines = []

        for line in lines:
            if line != previous_line:
                new_lines.append(line)
            previous_line = line

        if not new_lines:
            previous_end = end
            continue

        text = " ".join(current)
        long_enough = len(text) >= MAX_PARAGRAPH_CHARS and text.endswith((".", "?", "!"))

        if current and (start - previous_end >= PARAGRAPH_PAUSE or long_enough):
            paragraphs.append(text)
            current = []

        current.extend(new_lines)
        previous_end = end

    if current:
        paragraphs.append(" ".join(current))

    return paragraphs


def write_markdown(
    srt_file: Path,
    md_file: Path,
    title: str,
    url: str | None,
    label: str | None = None,
) -> None:
    """Gera um Markdown com o texto completo da legenda, sem os tempos."""
    cues = parse_srt(srt_file.read_text(encoding="utf-8", errors="replace"))
    paragraphs = build_paragraphs(cues)

    lines = [f"# {title}", ""]

    if url:
        lines += [f"Fonte: {url}", ""]

    if label:
        lines += [f"{LABEL_PREFIX}{label}", ""]

    lines += ["\n\n".join(paragraphs), ""]
    md_file.write_text("\n".join(lines), encoding="utf-8")


def read_label(md_file: Path) -> str | None:
    """Lê a faixa registrada em um Markdown gerado antes, se houver."""
    if not md_file.exists():
        return None

    for line in md_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(LABEL_PREFIX):
            return line.removeprefix(LABEL_PREFIX)

    return None


def describe_track(video: dict, track: str) -> str:
    """Descreve a faixa usada, ex.: 'Portuguese (Brazil), manual'."""
    manual = video.get("subtitles") or {}
    automatic = video.get("automatic_captions") or {}
    formats = manual.get(track) or automatic.get(track) or [{}]
    name = formats[0].get("name") or track
    kind = "manual" if track in manual else "automática"
    return f"{name}, {kind}"


def choose_subtitle_track(video: dict, language: str) -> str | None:
    """
    Escolhe a faixa de legenda do vídeo para o idioma pedido. Manuais vêm
    antes das automáticas. Entre as manuais, as variantes preferidas (pt-BR,
    pt-PT) vêm antes do idioma exato. Entre as automáticas, o idioma exato e
    a original (-orig) vêm antes das traduções.
    """
    manual = video.get("subtitles") or {}
    automatic = video.get("automatic_captions") or {}
    preferred = [code.lower() for code in PREFERRED_VARIANTS.get(language, [])]

    def priority(key: str) -> tuple[int, int, int, str]:
        code = key.lower()
        position = preferred.index(code) if code in preferred else len(preferred)

        if key in manual:
            tier = 0
        elif code == language or code.endswith("-orig"):
            tier = 1
        else:
            tier = 2

        return (tier, position, 0 if code == language else 1, code)

    candidates = [
        key
        for key in [*manual, *automatic]
        if key.lower() == language or key.lower().startswith(f"{language}-")
    ]

    return min(candidates, key=priority, default=None)


def download_subtitle(
    video: dict,
    destination: Path,
    index: int,
    language: str,
    numbered: bool,
) -> bool:
    """
    Baixa a legenda no idioma escolhido, incluindo legendas automáticas
    e faixas traduzidas que o YouTube disponibilizar nesse idioma.
    Sem numeração (vídeo único), salva direto em destination.
    """
    title = safe_name(video.get("title") or f"Vídeo {index}")
    url = get_video_url(video)

    print(f"\n[{index:03d}] {title}")

    if not url:
        print("  [ERRO] Não foi possível identificar a URL do vídeo.")
        return False

    video_dir = destination / f"{index:03d} - {title}" if numbered else destination

    # O idioma padrão mantém os nomes antigos; os demais recebem o código.
    suffix = "" if language == DEFAULT_LANGUAGE else f".{language}"
    final_file = video_dir / f"legenda{suffix}.srt"
    text_file = video_dir / f"legenda{suffix}.md"

    # Não refaz o download se a legenda já existir; apenas atualiza o texto.
    if final_file.exists() and final_file.stat().st_size > 0:
        write_markdown(final_file, text_file, title, url, read_label(text_file))
        print("  [OK] Legenda já existe; ignorando.")
        return True

    track = choose_subtitle_track(video, language)

    if not track:
        print(f"  [SEM LEGENDA] Nenhuma legenda em '{language}'.")
        return False

    print(f"  Faixa escolhida: {track}")

    # Usa uma pasta temporária para não misturar arquivos de
    # idiomas diferentes ou resultados de tentativas anteriores.
    with tempfile.TemporaryDirectory(prefix="yt-legenda-") as temp:
        temp_dir = Path(temp)

        options = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "noplaylist": True,
            "skip_download": True,
            "remote_components": ["ejs:github"],
            "sleep_requests": 3,
            "sleep_subtitles": 5,

            # Sem barra de progresso, para não misturar com as mensagens.
            "noprogress": True,

            # Legendas manuais e automáticas.
            "writesubtitles": True,
            "writeautomaticsub": True,

            # Solicita somente a faixa escolhida.
            "subtitleslangs": [re.escape(track)],

            # Prefere SRT; converte outro formato compatível.
            "subtitlesformat": "srt/best",
            "postprocessors": [
                {
                    "key": "FFmpegSubtitlesConvertor",
                    "format": "srt",
                }
            ],

            # Nomes temporários padronizados.
            "outtmpl": str(temp_dir / "legenda.%(ext)s"),
            "overwrites": True,
        }

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
        except Exception as exc:
            print(f"  [ERRO] Falha ao baixar legenda: {exc}")
            return False

        candidates = find_srt(temp_dir)

        if not candidates:
            print(f"  [SEM LEGENDA] Nenhuma legenda em SRT para '{track}'.")
            return False

        try:
            # Cria a pasta só agora, quando já há legenda para salvar.
            video_dir.mkdir(parents=True, exist_ok=True)

            # Copia para garantir que o arquivo temporário não
            # seja movido para fora do ciclo de vida da pasta.
            shutil.copy2(candidates[0], final_file)

            if final_file.stat().st_size == 0:
                final_file.unlink(missing_ok=True)
                print("  [ERRO] O arquivo de legenda está vazio.")
                return False

        except OSError as exc:
            print(f"  [ERRO] Não foi possível salvar o SRT: {exc}")
            return False

    write_markdown(final_file, text_file, title, url, describe_track(video, track))

    print(f"  [OK] {final_file}")
    print(f"  [OK] {text_file}")
    return True


def normalize_language(value: str) -> str:
    """Padroniza o idioma digitado; vazio vira o padrão."""
    return value.strip().lower() or DEFAULT_LANGUAGE


def show_languages(videos: list[dict]) -> None:
    """Lista os idiomas com legenda manual ou original automática nos vídeos."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    kinds: dict[str, str] = {}

    for video in videos:
        manual = video.get("subtitles") or {}
        original = {
            key: formats
            for key, formats in (video.get("automatic_captions") or {}).items()
            if key.endswith("-orig")
        }

        for kind, tracks in (("manual", manual), ("automática", original)):
            for key, formats in tracks.items():
                code = key.lower()
                counts[code] = counts.get(code, 0) + 1
                names.setdefault(code, (formats[0].get("name") if formats else None) or key)
                kinds.setdefault(code, kind)

    if not counts:
        print("Nenhuma legenda manual ou original encontrada.")
        return

    print("\nIdiomas com legenda nos vídeos:")
    order = sorted(counts, key=lambda code: (kinds[code] != "manual", -counts[code], code))

    for code in order:
        print(f"  {code:<10} {names[code]:<30} {kinds[code]:<10} {counts[code]}/{len(videos)} vídeos")


def choose_language(videos: list[dict], given: str | None) -> str | None:
    """
    Pede o idioma até algum vídeo ter legenda nele. Com o idioma já informado
    na linha de comando, não pergunta de novo.
    """
    while True:
        answer = given if given is not None else input(
            "Idioma das legendas (ex.: pt, en, es, pt-BR) [pt]: "
        )
        language = normalize_language(answer)

        if not LANGUAGE_CODE.fullmatch(language):
            print(f"Idioma inválido: {language}. Use um código como pt, en, es ou pt-BR.")
        else:
            found = sum(choose_subtitle_track(video, language) is not None for video in videos)

            if found:
                print(f"Legenda em '{language}' disponível em {found} de {len(videos)} vídeos.")
                return language

            print(f"Nenhum vídeo tem legenda em '{language}'.")

        if given is not None:
            return None


def main() -> None:
    # Títulos com emoji quebram o console do Windows (cp1252).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    url = sys.argv[1].strip() if len(sys.argv) > 1 else input(
        "Cole o link do YouTube: "
    ).strip()

    if not url:
        print("Nenhum link informado.")
        return

    given = normalize_language(sys.argv[2]) if len(sys.argv) > 2 else None

    if given is not None and not LANGUAGE_CODE.fullmatch(given):
        print(f"Idioma inválido: {given}. Use um código como pt, en, es ou pt-BR.")
        return

    print("Obtendo informações do YouTube...")

    try:
        info = get_info(url)
    except Exception as exc:
        print(f"Falha ao consultar o link: {exc}")
        return

    collection, videos = get_videos(info)

    if not videos:
        print("Nenhum vídeo encontrado.")
        return

    total = len(videos)
    print(f"\nColeção: {collection}")
    print(f"Vídeos encontrados: {total}")
    show_languages(videos)

    language = choose_language(videos, given)

    if not language:
        return

    destination = OUTPUT_DIR / collection
    destination.mkdir(parents=True, exist_ok=True)

    numbered = total > 1
    successful = 0
    failed = []

    print(f"Destino: {destination.resolve()}")

    for index, video in enumerate(videos, start=1):
        try:
            if download_subtitle(video, destination, index, language, numbered=numbered):
                successful += 1
            else:
                failed.append(video.get("title") or f"Vídeo {index}")
        except Exception as exc:
            failed.append(video.get("title") or f"Vídeo {index}")
            print(f"  [ERRO INESPERADO] {exc}")

    # Salva um relatório para facilitar novas tentativas.
    if failed:
        report = destination / "falhas.txt"
        report.write_text("\n".join(failed) + "\n", encoding="utf-8")

    print("\n" + "=" * 45)
    print("PROCESSAMENTO CONCLUÍDO")
    print(f"Sucesso: {successful}/{total}")
    print(f"Falhas: {len(failed)}")
    print(f"Diretório: {destination.resolve()}")

    if failed:
        print(f"Lista de falhas: {(destination / 'falhas.txt').resolve()}")


if __name__ == "__main__":
    main()
