"""Canonical parser language metadata and file-to-language matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet

from backend.indexer.language_definitions import LANGUAGE_DEFINITIONS


@dataclass(frozen=True)
class ParserLanguage:
    language_id: str
    label: str
    extensions: tuple[str, ...]
    filenames: tuple[str, ...] = ()
    parser_kind: str = "dedicated"

    def payload(self, enabled: bool) -> dict[str, object]:
        return {
            "id": self.language_id,
            "label": self.label,
            "extensions": list(self.extensions),
            "filenames": list(self.filenames),
            "parser_kind": self.parser_kind,
            "enabled": enabled,
        }


_DEDICATED_PARSER_LANGUAGES = (
    ParserLanguage("python", "Python", (".py",)),
    ParserLanguage("typescript", "TypeScript", (".ts", ".tsx")),
    ParserLanguage("javascript", "JavaScript", (".js", ".jsx")),
    ParserLanguage("powershell", "PowerShell", (".ps1", ".psm1", ".psd1")),
    ParserLanguage("go", "Go", (".go",)),
    ParserLanguage("rust", "Rust", (".rs",)),
    ParserLanguage("java", "Java", (".java",)),
)

_LANGUAGE_LABELS = {
    "csharp": "C#",
    "c": "C",
    "cpp": "C++",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "swift": "Swift",
    "ruby": "Ruby",
    "php": "PHP",
    "dart": "Dart",
    "lua": "Lua",
    "perl": "Perl",
    "bash": "Bash / Shell",
    "groovy": "Groovy / Gradle",
    "fsharp": "F#",
    "zig": "Zig",
    "nim": "Nim",
    "d": "D",
    "fortran": "Fortran",
    "pascal": "Pascal",
    "r": "R",
    "julia": "Julia",
    "matlab": "MATLAB",
    "haskell": "Haskell",
    "ocaml": "OCaml",
    "erlang": "Erlang",
    "objective_c": "Objective-C / Objective-C++",
    "crystal": "Crystal",
    "visual_basic": "Visual Basic .NET",
    "ada": "Ada",
    "cobol": "COBOL",
    "mojo": "Mojo",
    "cmake": "CMake",
    "solidity": "Solidity",
    "sql": "SQL",
    "graphql": "GraphQL",
    "protobuf": "Protocol Buffers",
    "starlark": "Starlark / Bazel",
    "nix": "Nix",
    "scss": "SCSS",
    "vyper": "Vyper",
    "move": "Move",
    "cairo": "Cairo",
    "clarity": "Clarity",
    "cadence": "Cadence",
}

_FILENAME_LABELS = {
    "build": "BUILD",
    "build.bazel": "BUILD.bazel",
    "cmakelists.txt": "CMakeLists.txt",
    "gemfile": "Gemfile",
    "module.bazel": "MODULE.bazel",
    "rakefile": "Rakefile",
    "workspace": "WORKSPACE",
    "workspace.bazel": "WORKSPACE.bazel",
}

_REGISTERED_PARSER_LANGUAGES = tuple(
    ParserLanguage(
        language_id=definition.language,
        label=_LANGUAGE_LABELS[definition.language],
        extensions=tuple(sorted(definition.extensions)),
        filenames=tuple(
            _FILENAME_LABELS.get(filename, filename)
            for filename in sorted(definition.filenames)
        ),
        parser_kind=definition.parser_kind,
    )
    for definition in LANGUAGE_DEFINITIONS
)

PARSER_LANGUAGE_CATALOG = _DEDICATED_PARSER_LANGUAGES + _REGISTERED_PARSER_LANGUAGES
PARSER_LANGUAGE_IDS = frozenset(item.language_id for item in PARSER_LANGUAGE_CATALOG)

_EXTENSION_LANGUAGE_IDS = {
    extension: frozenset(
        item.language_id
        for item in PARSER_LANGUAGE_CATALOG
        if extension in item.extensions
    )
    for extension in {
        extension
        for item in PARSER_LANGUAGE_CATALOG
        for extension in item.extensions
    }
}
_FILENAME_LANGUAGE_IDS = {
    filename.lower(): frozenset(
        item.language_id
        for item in PARSER_LANGUAGE_CATALOG
        if filename in item.filenames
    )
    for filename in {
        filename
        for item in PARSER_LANGUAGE_CATALOG
        for filename in item.filenames
    }
}


def parser_language_ids_for_path(file_path: str | Path) -> frozenset[str]:
    path = Path(file_path)
    filename_matches = _FILENAME_LANGUAGE_IDS.get(path.name.lower())
    if filename_matches:
        return filename_matches
    return _EXTENSION_LANGUAGE_IDS.get(path.suffix.lower(), frozenset())


def is_supported_parser_file(file_path: str | Path) -> bool:
    return bool(parser_language_ids_for_path(file_path))


def is_parser_file_enabled(
    file_path: str | Path,
    disabled_languages: AbstractSet[str],
) -> bool:
    language_ids = parser_language_ids_for_path(file_path)
    return bool(language_ids and not language_ids.issubset(disabled_languages))


def parser_language_payload(disabled_languages: AbstractSet[str]) -> list[dict[str, object]]:
    return [
        item.payload(enabled=item.language_id not in disabled_languages)
        for item in PARSER_LANGUAGE_CATALOG
    ]
