"""Read saved PotPlayer playlists without changing the running player.

Saved albums are snapshots, not a live queue API.  Folder navigator albums do
not contain a queue at all; their folder is enumerated with an explicit warning.
Only the two playlist settings are read from the registry.  Playback history and
RememberFiles are deliberately never used to construct a queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


MEDIA_EXTENSIONS = frozenset(
    ".3g2 .3gp .asf .avi .divx .flv .m2t .m2ts .m2v .m4v .mkv .mov .mp4 "
    ".mpe .mpeg .mpg .mts .ogm .ogv .rm .rmvb .ts .vob .webm .wmv "
    ".aac .ac3 .aiff .alac .ape .dts .eac3 .flac .m4a .mka .mp2 .mp3 "
    ".ogg .opus .wav .wma .wv".split()
)
_FILE_ENTRY = re.compile(r"^(\d+)\*file=(.*)$", re.IGNORECASE)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass
class PlaylistSelection:
    paths: list[Path]
    source: str
    warning: str | None = None


def _natural_key(path: Path) -> tuple:
    # Tagged pieces keep comparisons valid even for names beginning with digits.
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", path.name)
    )


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def _local_path(value: str, base: Path) -> Path | None:
    value = value.strip().strip('"')
    if not value:
        return None
    # A drive letter is a local Windows path, not a URI scheme.
    if _URL.match(value) and not re.match(r"^[a-z]:[\\/]", value, re.IGNORECASE):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _active_saved_playlist() -> Path | None:
    """Read the configured saved album, never the recent-file registry keys."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\DAUM\PotPlayerMini64\Settings"
        ) as key:
            name, _ = winreg.QueryValueEx(key, "LastPlayListName")
            folder, _ = winreg.QueryValueEx(key, "PlayListFolder")
    except OSError:
        return None
    if not isinstance(name, str) or not isinstance(folder, str):
        return None
    return _local_path(name, Path(folder))


def _directory_selection(folder: Path, current_file: Path, reason: str) -> PlaylistSelection:
    try:
        paths = sorted(
            (
                item.resolve()
                for item in folder.iterdir()
                if item.is_file() and item.suffix.casefold() in MEDIA_EXTENSIONS
            ),
            key=_natural_key,
        )
    except OSError:
        paths = []
        reason += " Папку прочитать не удалось."
    if current_file not in paths:
        paths.append(current_file)
    return PlaylistSelection(
        paths,
        "directory",
        reason + " Порядок построен по именам файлов и может отличаться от очереди PotPlayer.",
    )


def _parse_playlist(path: Path, current_file: Path) -> tuple[PlaylistSelection, bool]:
    lines = _read_text(path).splitlines()
    candidates: list[str] = []
    folder_values: dict[str, str] = {}
    if path.suffix.casefold() == ".dpl":
        numbered: list[tuple[int, str]] = []
        for line in lines:
            match = _FILE_ENTRY.match(line.strip())
            if match:
                numbered.append((int(match[1]), match[2]))
            elif "=" in line:
                name, value = line.split("=", 1)
                folder_values[name.strip().casefold()] = value.strip()
        if numbered:
            candidates = [value for _, value in sorted(numbered, key=lambda item: item[0])]
        else:
            for key in ("workfolder", "currentfolder", "basefolder"):
                folder = _local_path(folder_values.get(key, ""), path.parent)
                if folder and folder.is_dir():
                    selection = _directory_selection(
                        folder,
                        current_file,
                        "Альбом PotPlayer использует навигацию папки; сохраненной очереди в DPL нет.",
                    )
                    return selection, folder == current_file.parent
    elif path.suffix.casefold() in {".m3u", ".m3u8"}:
        candidates = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    else:
        raise ValueError("Поддерживаются только DPL, M3U и M3U8")

    paths: list[Path] = []
    omitted = 0
    for value in candidates:
        candidate = _local_path(value, path.parent)
        if candidate and candidate.is_file():
            paths.append(candidate)
        else:
            omitted += 1
    warnings = ["Используется сохраненный плейлист; изменения в открытом PotPlayer могут еще не быть сохранены."]
    if omitted:
        warnings.append(f"Пропущено недоступных файлов или сетевых URL: {omitted}.")
    contains_current = current_file in paths
    if not contains_current:
        paths.append(current_file)
        warnings.append("Текущий файл отсутствовал в сохраненном плейлисте и добавлен в конец.")
    return PlaylistSelection(paths, "saved-playlist", " ".join(warnings)), contains_current


def discover_playlist(
    current_file: Path, explicit_playlist: Path | None = None
) -> PlaylistSelection:
    """Return existing local media, preserving saved playlist order when available.

    Explicit lists may intentionally contain items outside the current folder.
    An automatically discovered stale album is ignored when it does not contain
    the current file (or navigate its directory).  The current file is always in
    the returned list; a nonexistent current file raises FileNotFoundError.
    """
    current_file = Path(current_file).resolve()
    if not current_file.is_file():
        raise FileNotFoundError(f"Текущий медиафайл недоступен: {current_file}")
    chosen = Path(explicit_playlist).resolve() if explicit_playlist is not None else _active_saved_playlist()
    reason = "Сохраненный плейлист PotPlayer не найден; показаны медиафайлы текущей папки."
    if chosen is not None:
        try:
            selection, matches_current = _parse_playlist(chosen, current_file)
        except (OSError, UnicodeError, ValueError) as exc:
            if explicit_playlist is not None:
                raise ValueError(f"Не удалось прочитать плейлист {chosen}: {exc}") from exc
            reason = "Сохраненный плейлист PotPlayer недоступен; показаны медиафайлы текущей папки."
        else:
            if explicit_playlist is not None:
                return selection
            # Never treat a saved album for a different film as an active queue.
            if matches_current:
                return selection
            reason = "Сохраненный альбом относится к другим файлам; показаны медиафайлы текущей папки."
    return _directory_selection(current_file.parent, current_file, reason)
