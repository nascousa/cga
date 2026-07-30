"""Declarative language definitions for structural repository indexing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageCapabilities:
    symbols: bool = True
    imports: bool = True
    calls: bool = True
    variable_flows: bool = True
    embedded_regions: bool = False


@dataclass(frozen=True)
class LanguageDefinition:
    language: str
    grammar: str
    extensions: frozenset[str]
    filenames: frozenset[str] = frozenset()
    symbol_types: dict[str, str] = field(default_factory=dict)
    callable_symbol_types: frozenset[str] = frozenset({"constructor", "function", "method"})
    import_nodes: frozenset[str] = frozenset()
    import_pattern: str | None = None
    call_nodes: frozenset[str] = frozenset()
    assignment_nodes: frozenset[str] = frozenset()
    parameter_nodes: frozenset[str] = frozenset()
    parameter_list_nodes: frozenset[str] = frozenset()
    return_nodes: frozenset[str] = frozenset()
    parser_kind: str = "tree_sitter"
    capabilities: LanguageCapabilities = LanguageCapabilities()


def _language(
    language: str,
    grammar: str,
    extensions: set[str],
    *,
    filenames: set[str] | None = None,
    symbols: dict[str, str],
    imports: set[str],
    import_pattern: str,
    calls: set[str],
    assignments: set[str],
    parameters: set[str],
    parameter_lists: set[str],
    returns: set[str],
    parser_kind: str = "tree_sitter",
) -> LanguageDefinition:
    return LanguageDefinition(
        language=language,
        grammar=grammar,
        extensions=frozenset(extensions),
        filenames=frozenset(name.lower() for name in (filenames or set())),
        symbol_types=symbols,
        import_nodes=frozenset(imports),
        import_pattern=import_pattern,
        call_nodes=frozenset(calls),
        assignment_nodes=frozenset(assignments),
        parameter_nodes=frozenset(parameters),
        parameter_list_nodes=frozenset(parameter_lists),
        return_nodes=frozenset(returns),
        parser_kind=parser_kind,
    )


CSHARP = _language(
    "csharp",
    "csharp",
    {".cs"},
    symbols={
        "class_declaration": "class",
        "record_declaration": "record",
        "struct_declaration": "struct",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "delegate_declaration": "delegate",
        "constructor_declaration": "constructor",
        "method_declaration": "method",
        "local_function_statement": "function",
        "property_declaration": "property",
    },
    imports={"using_directive"},
    import_pattern=r"\busing\s+(?:static\s+)?(?:\w+\s*=\s*)?(?P<module>[\w.]+)",
    calls={"invocation_expression", "object_creation_expression"},
    assignments={"variable_declarator", "assignment_expression"},
    parameters={"parameter"},
    parameter_lists={"parameter_list"},
    returns={"return_statement"},
)

C = _language(
    "c",
    "c",
    {".c", ".h"},
    symbols={
        "function_definition": "function",
        "struct_specifier": "struct",
        "union_specifier": "union",
        "enum_specifier": "enum",
        "type_definition": "type",
    },
    imports={"preproc_include"},
    import_pattern=r"[#]\s*include\s*[<\"](?P<module>[^>\"]+)",
    calls={"call_expression"},
    assignments={"init_declarator", "assignment_expression"},
    parameters={"parameter_declaration"},
    parameter_lists={"parameter_list"},
    returns={"return_statement"},
)

CPP = _language(
    "cpp",
    "cpp",
    {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"},
    symbols={
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "union_specifier": "union",
        "enum_specifier": "enum",
        "namespace_definition": "namespace",
        "type_definition": "type",
    },
    imports={"preproc_include", "module_import_declaration"},
    import_pattern=r"(?:[#]\s*include\s*[<\"]|\bimport\s+)(?P<module>[^>\";]+)",
    calls={"call_expression", "new_expression"},
    assignments={"init_declarator", "assignment_expression"},
    parameters={"parameter_declaration", "optional_parameter_declaration"},
    parameter_lists={"parameter_list"},
    returns={"return_statement"},
)

KOTLIN = _language(
    "kotlin",
    "kotlin",
    {".kt", ".kts"},
    symbols={
        "class_declaration": "class",
        "object_declaration": "object",
        "function_declaration": "function",
        "type_alias": "type",
    },
    imports={"import_header"},
    import_pattern=r"\bimport\s+(?P<module>[\w.*]+)",
    calls={"call_expression", "constructor_invocation"},
    assignments={"property_declaration", "assignment"},
    parameters={"parameter"},
    parameter_lists={"function_value_parameters"},
    returns={"jump_expression"},
)

SCALA = _language(
    "scala",
    "scala",
    {".scala", ".sc"},
    symbols={
        "class_definition": "class",
        "object_definition": "object",
        "trait_definition": "trait",
        "function_definition": "function",
        "function_declaration": "function",
        "type_definition": "type",
    },
    imports={"import_declaration"},
    import_pattern=r"\bimport\s+(?P<module>[\w.]+)",
    calls={"call_expression", "generic_function"},
    assignments={"val_definition", "var_definition", "assignment_expression"},
    parameters={"parameter", "class_parameter"},
    parameter_lists={"parameters", "class_parameters"},
    returns={"return_expression"},
)

SWIFT = _language(
    "swift",
    "swift",
    {".swift"},
    symbols={
        "class_declaration": "class",
        "struct_declaration": "struct",
        "protocol_declaration": "interface",
        "enum_declaration": "enum",
        "function_declaration": "function",
        "typealias_declaration": "type",
    },
    imports={"import_declaration"},
    import_pattern=r"\bimport\s+(?P<module>[\w.]+)",
    calls={"call_expression"},
    assignments={"property_declaration", "assignment"},
    parameters={"parameter"},
    parameter_lists={"parameter_clause"},
    returns={"control_transfer_statement"},
)

RUBY = _language(
    "ruby",
    "ruby",
    {".rb", ".rake", ".gemspec"},
    filenames={"Gemfile", "Rakefile"},
    symbols={
        "class": "class",
        "module": "module",
        "method": "method",
        "singleton_method": "method",
    },
    imports={"call"},
    import_pattern=r"\b(?:require|require_relative|load)\s*\(?\s*['\"](?P<module>[^'\"]+)",
    calls={"call"},
    assignments={"assignment", "operator_assignment"},
    parameters={"identifier", "optional_parameter", "keyword_parameter"},
    parameter_lists={"method_parameters", "lambda_parameters"},
    returns={"return"},
)

PHP = _language(
    "php",
    "php",
    {".php", ".phtml"},
    symbols={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "enum_declaration": "enum",
        "function_definition": "function",
        "method_declaration": "method",
    },
    imports={"require_expression", "include_expression", "namespace_use_declaration"},
    import_pattern=r"(?:require|require_once|include|include_once|use)\s*\(?\s*['\"]?(?P<module>[^'\";\)]+)",
    calls={"function_call_expression", "member_call_expression", "scoped_call_expression", "object_creation_expression"},
    assignments={"assignment_expression", "augmented_assignment_expression"},
    parameters={"simple_parameter", "variadic_parameter"},
    parameter_lists={"formal_parameters"},
    returns={"return_statement"},
)

DART = _language(
    "dart",
    "dart",
    {".dart"},
    symbols={
        "class_definition": "class",
        "mixin_declaration": "mixin",
        "extension_declaration": "extension",
        "enum_declaration": "enum",
        "function_signature": "function",
        "method_signature": "method",
    },
    imports={"library_import", "import_or_export"},
    import_pattern=r"\b(?:import|export)\s+['\"](?P<module>[^'\"]+)",
    calls={"selector", "function_expression_invocation"},
    assignments={"initialized_variable_definition", "assignment_expression"},
    parameters={"formal_parameter", "default_formal_parameter"},
    parameter_lists={"formal_parameter_list"},
    returns={"return_statement"},
)

LUA = _language(
    "lua",
    "lua",
    {".lua"},
    symbols={"function_declaration": "function"},
    imports={"function_call"},
    import_pattern=r"\brequire\s*\(?\s*['\"](?P<module>[^'\"]+)",
    calls={"function_call", "method_call"},
    assignments={"assignment_statement", "variable_declaration"},
    parameters={"identifier"},
    parameter_lists={"parameters"},
    returns={"return_statement"},
)

PERL = _language(
    "perl",
    "perl",
    {".pl", ".pm"},
    symbols={"subroutine_declaration_statement": "function", "package_statement": "module"},
    imports={"use_statement", "require_expression"},
    import_pattern=r"\b(?:use|require)\s+(?P<module>[\w:.-]+)",
    calls={"function_call_expression", "method_invocation"},
    assignments={"assignment_expression"},
    parameters={"scalar"},
    parameter_lists={"signature"},
    returns={"return_expression"},
)

BASH = _language(
    "bash",
    "bash",
    {".sh", ".bash", ".zsh"},
    symbols={"function_definition": "function"},
    imports={"command"},
    import_pattern=r"(?:^|\s)(?:source|\.)\s+(?P<module>[^\s;]+)",
    calls={"command"},
    assignments={"variable_assignment"},
    parameters=set(),
    parameter_lists=set(),
    returns={"return_statement"},
)

GROOVY = _language(
    "groovy",
    "groovy",
    {".groovy", ".gradle"},
    symbols={
        "command": "function",
        "class_declaration": "class",
        "class_definition": "class",
        "method_declaration": "method",
        "function_definition": "function",
    },
    imports={"command", "import_declaration"},
    import_pattern=r"\bimport\s+(?:static\s+)?(?P<module>[\w.*]+)",
    calls={"func", "method_invocation", "call_expression"},
    assignments={"command", "variable_declaration", "assignment_expression"},
    parameters={"formal_parameter", "identifier", "parameter"},
    parameter_lists={"arg_block", "formal_parameters", "parameters"},
    returns={"return_statement"},
)

FSHARP = _language(
    "fsharp",
    "fsharp",
    {".fs", ".fsx", ".fsi"},
    symbols={
        "function_or_value_defn": "function",
        "type_definition": "type",
        "module_defn": "module",
    },
    imports={"import_decl"},
    import_pattern=r"\bopen\s+(?P<module>[\w.]+)",
    calls={"application_expression"},
    assignments={"function_or_value_defn"},
    parameters={"identifier", "long_identifier_or_op"},
    parameter_lists={"function_declaration_left"},
    returns=set(),
)

ZIG = _language(
    "zig",
    "zig",
    {".zig"},
    symbols={"Decl": "function"},
    imports={"VarDecl"},
    import_pattern=r"@import\s*\(\s*[\"'](?P<module>[^\"']+)",
    calls={"SuffixExpr"},
    assignments={"VarDecl"},
    parameters={"ParamDecl"},
    parameter_lists={"ParamDeclList"},
    returns={"AssignExpr"},
)

NIM = _language(
    "nim",
    "nim",
    {".nim", ".nims"},
    symbols={
        "proc_declaration": "function",
        "func_declaration": "function",
        "method_declaration": "method",
        "iterator_declaration": "function",
        "type_definition": "type",
    },
    imports={"import_statement", "from_statement", "include_statement"},
    import_pattern=r"\b(?:import|from|include)\s+(?P<module>[\w./]+)",
    calls={"call", "command"},
    assignments={"variable_declaration", "assignment"},
    parameters={"parameter_declaration"},
    parameter_lists={"parameter_declaration_list"},
    returns={"return_statement"},
)

D_LANGUAGE = _language(
    "d",
    "d",
    {".d", ".di"},
    symbols={
        "class_declaration": "class",
        "struct_declaration": "struct",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "function_declaration": "function",
    },
    imports={"import_declaration"},
    import_pattern=r"\bimport\s+(?P<module>[\w.]+)",
    calls={"call_expression", "new_expression"},
    assignments={"auto_declaration", "declarator", "assignment_expression"},
    parameters={"parameter"},
    parameter_lists={"parameters"},
    returns={"return_statement"},
)

FORTRAN = _language(
    "fortran",
    "fortran",
    {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"},
    symbols={
        "module": "module",
        "function": "function",
        "subroutine": "function",
        "derived_type_definition": "type",
    },
    imports={"use_statement", "include_statement"},
    import_pattern=r"\b(?:use|include)\s*(?:::\s*)?[\"']?(?P<module>[\w./]+)",
    calls={"call_expression"},
    assignments={"assignment_statement"},
    parameters={"identifier"},
    parameter_lists={"parameters"},
    returns={"return_statement"},
)

PASCAL = _language(
    "pascal",
    "pascal",
    {".pas", ".pp", ".inc"},
    symbols={
        "defProc": "function",
        "declType": "type",
    },
    imports={"declUses"},
    import_pattern=r"\buses\s+(?P<module>[\w.]+)",
    calls={"exprCall"},
    assignments={"assignment"},
    parameters={"declArg"},
    parameter_lists={"declArgs"},
    returns=set(),
)

R_LANGUAGE = _language(
    "r",
    "r",
    {".r", ".rmd", ".rnw"},
    symbols={"binary_operator": "function"},
    imports={"call"},
    import_pattern=r"\b(?:library|require)\s*\(\s*[\"']?(?P<module>[\w.]+)",
    calls={"call"},
    assignments={"binary_operator"},
    parameters={"parameter"},
    parameter_lists={"parameters"},
    returns=set(),
)

JULIA = _language(
    "julia",
    "julia",
    {".jl"},
    symbols={"function_definition": "function", "struct_definition": "struct"},
    imports={"using_statement", "import_statement"},
    import_pattern=r"\b(?:using|import)\s+(?P<module>[\w.]+)",
    calls={"call_expression"},
    assignments={"assignment"},
    parameters={"identifier"},
    parameter_lists={"argument_list"},
    returns={"return_statement"},
)

MATLAB = _language(
    "matlab",
    "matlab",
    {".m"},
    symbols={"function_definition": "function", "class_definition": "class"},
    imports={"command", "function_call"},
    import_pattern=r"\b(?:import|addpath)\s*\(?\s*[\"']?(?P<module>[\w./]+)",
    calls={"function_call"},
    assignments={"assignment"},
    parameters={"identifier"},
    parameter_lists={"function_arguments"},
    returns={"return_statement"},
)

HASKELL = _language(
    "haskell",
    "haskell",
    {".hs", ".lhs"},
    symbols={"function": "function", "data_type": "type", "newtype": "type"},
    imports={"import"},
    import_pattern=r"\bimport\s+(?:qualified\s+)?(?P<module>[\w.]+)",
    calls={"apply"},
    assignments={"bind"},
    parameters={"variable"},
    parameter_lists={"patterns"},
    returns=set(),
)

OCAML = _language(
    "ocaml",
    "ocaml",
    {".ml", ".mli"},
    symbols={"let_binding": "function", "module_definition": "module", "type_definition": "type"},
    imports={"open_module", "include_module"},
    import_pattern=r"\b(?:open|include)\s+(?P<module>[\w.]+)",
    calls={"application_expression"},
    assignments={"let_binding"},
    parameters={"parameter"},
    parameter_lists={"let_binding"},
    returns=set(),
)

ERLANG = _language(
    "erlang",
    "erlang",
    {".erl", ".hrl"},
    symbols={"function_clause": "function", "module_attribute": "module", "record_decl": "type"},
    imports={"import_attribute", "include_attribute", "include_lib_attribute"},
    import_pattern=r"-(?:import|include|include_lib)\s*\(\s*[\"']?(?P<module>[\w./]+)",
    calls={"call"},
    assignments={"match_expr"},
    parameters={"var"},
    parameter_lists={"expr_args"},
    returns=set(),
)

OBJECTIVE_C = _language(
    "objective_c",
    "objc",
    {".m", ".mm"},
    symbols={
        "class_interface": "class",
        "protocol_declaration": "interface",
        "class_implementation": "class",
        "method_declaration": "method",
        "method_definition": "method",
        "function_definition": "function",
    },
    imports={"preproc_include"},
    import_pattern=r"[#]\s*(?:import|include)\s*[<\"](?P<module>[^>\"]+)",
    calls={"call_expression", "message_expression"},
    assignments={"init_declarator", "assignment_expression"},
    parameters={"method_parameter", "parameter_declaration"},
    parameter_lists={"parameter_list", "method_definition", "method_declaration"},
    returns={"return_statement"},
)

CRYSTAL = _language(
    "crystal",
    "ruby",
    {".cr"},
    symbols={"class": "class", "module": "module", "method": "method"},
    imports={"call"},
    import_pattern=r"\brequire\s*\(?\s*[\"'](?P<module>[^\"']+)",
    calls={"call"},
    assignments={"assignment", "operator_assignment"},
    parameters={"identifier", "optional_parameter"},
    parameter_lists={"method_parameters"},
    returns={"return"},
)

VISUAL_BASIC = _language(
    "visual_basic",
    "",
    {".vb"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

ADA = _language(
    "ada",
    "",
    {".adb", ".ads", ".ada"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

COBOL = _language(
    "cobol",
    "",
    {".cob", ".cbl", ".cpy"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

MOJO = _language(
    "mojo",
    "",
    {".mojo", ".🔥"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

CMAKE = _language(
    "cmake",
    "",
    {".cmake"},
    filenames={"CMakeLists.txt"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

SOLIDITY = _language(
    "solidity",
    "solidity",
    {".sol"},
    symbols={
        "contract_declaration": "contract",
        "interface_declaration": "interface",
        "library_declaration": "module",
        "function_definition": "function",
        "constructor_definition": "constructor",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
    },
    imports={"import_directive"},
    import_pattern=r"\bimport\s+(?:[^\"']+\s+from\s+)?[\"'](?P<module>[^\"']+)",
    calls={"call_expression"},
    assignments={"variable_declaration_statement", "assignment_expression"},
    parameters={"parameter"},
    parameter_lists={"function_definition", "constructor_definition"},
    returns={"return_statement"},
)

SQL = _language(
    "sql",
    "sql",
    {".sql"},
    symbols={
        "create_table": "table",
        "create_view": "view",
        "create_function": "function",
        "create_procedure": "function",
    },
    imports=set(),
    import_pattern="",
    calls={"invocation"},
    assignments={"assignment"},
    parameters={"function_argument"},
    parameter_lists={"function_arguments"},
    returns=set(),
)

GRAPHQL = _language(
    "graphql",
    "graphql",
    {".graphql", ".gql", ".graphqls"},
    symbols={
        "object_type_definition": "type",
        "interface_type_definition": "interface",
        "input_object_type_definition": "input",
        "enum_type_definition": "enum",
        "scalar_type_definition": "scalar",
        "directive_definition": "directive",
        "operation_definition": "function",
        "fragment_definition": "fragment",
    },
    imports=set(),
    import_pattern="",
    calls={"field"},
    assignments=set(),
    parameters={"variable_definition"},
    parameter_lists={"variable_definitions"},
    returns=set(),
)

PROTOBUF = _language(
    "protobuf",
    "proto",
    {".proto"},
    symbols={
        "message": "message",
        "enum": "enum",
        "service": "service",
        "rpc": "function",
    },
    imports={"import"},
    import_pattern=r"\bimport\s+(?:public\s+|weak\s+)?[\"'](?P<module>[^\"']+)",
    calls=set(),
    assignments=set(),
    parameters={"message_or_enum_type"},
    parameter_lists={"rpc"},
    returns=set(),
)

STARLARK = _language(
    "starlark",
    "starlark",
    {".bzl", ".star"},
    filenames={"BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel"},
    symbols={"function_definition": "function"},
    imports={"call"},
    import_pattern=r"\bload\s*\(\s*[\"'](?P<module>[^\"']+)",
    calls={"call"},
    assignments={"assignment", "augmented_assignment"},
    parameters={"identifier", "default_parameter"},
    parameter_lists={"parameters"},
    returns={"return_statement"},
)

NIX = _language(
    "nix",
    "nix",
    {".nix"},
    symbols={"binding": "function"},
    imports={"apply_expression"},
    import_pattern=r"\bimport\s+(?P<module><[^>]+>|[^\s;]+)",
    calls={"apply_expression"},
    assignments={"binding"},
    parameters={"formal", "identifier"},
    parameter_lists={"formals", "function_expression"},
    returns=set(),
)

SCSS = _language(
    "scss",
    "scss",
    {".scss"},
    symbols={"mixin_statement": "function", "function_statement": "function"},
    imports={"use_statement", "import_statement", "forward_statement"},
    import_pattern=r"@(?:use|import|forward)\s+[\"'](?P<module>[^\"']+)",
    calls={"include_statement", "call_expression"},
    assignments={"declaration"},
    parameters={"parameter"},
    parameter_lists={"parameters"},
    returns={"return_statement"},
)

VYPER = _language(
    "vyper",
    "",
    {".vy"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

MOVE = _language(
    "move",
    "",
    {".move"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

CAIRO = _language(
    "cairo",
    "",
    {".cairo"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

CLARITY = _language(
    "clarity",
    "",
    {".clar"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)

CADENCE = _language(
    "cadence",
    "",
    {".cdc"},
    symbols={},
    imports=set(),
    import_pattern="",
    calls=set(),
    assignments=set(),
    parameters=set(),
    parameter_lists=set(),
    returns=set(),
    parser_kind="pattern",
)


LANGUAGE_DEFINITIONS = (
    CSHARP,
    C,
    CPP,
    KOTLIN,
    SCALA,
    SWIFT,
    RUBY,
    PHP,
    DART,
    LUA,
    PERL,
    BASH,
    GROOVY,
    FSHARP,
    ZIG,
    NIM,
    D_LANGUAGE,
    FORTRAN,
    PASCAL,
    R_LANGUAGE,
    JULIA,
    MATLAB,
    HASKELL,
    OCAML,
    ERLANG,
    OBJECTIVE_C,
    CRYSTAL,
    VISUAL_BASIC,
    ADA,
    COBOL,
    MOJO,
    CMAKE,
    SOLIDITY,
    SQL,
    GRAPHQL,
    PROTOBUF,
    STARLARK,
    NIX,
    SCSS,
    VYPER,
    MOVE,
    CAIRO,
    CLARITY,
    CADENCE,
)

REGISTERED_EXTENSIONS = frozenset(
    extension
    for definition in LANGUAGE_DEFINITIONS
    for extension in definition.extensions
)
REGISTERED_FILENAMES = frozenset(
    filename
    for definition in LANGUAGE_DEFINITIONS
    for filename in definition.filenames
)
