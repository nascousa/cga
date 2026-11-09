"""Local semantic contracts for the relationships CGA actually indexes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


CONTRACT_VERSION = "1.0.0"
RelationName = Literal[
    "CONTAINS", "DEFINES", "IMPORTS", "CALLS", "USES_VARIABLE", "FLOWS_TO"
]
Direction = Literal["outbound", "inbound"]


@dataclass(frozen=True)
class Entity:
    label: str
    identity_property: str
    description: str


@dataclass(frozen=True)
class Relation:
    name: str
    source: Entity
    target: Entity
    description: str
    limitations: str


REPOSITORY = Entity("Repository", "path", "An indexed repository root.")
FILE = Entity("File", "path", "A discovered source file or supported format.")
SYMBOL = Entity("Symbol", "qualified_name", "A parser-extracted declaration.")
VARIABLE = Entity("Variable", "qualified_name", "A variable in a symbol scope.")
ENTITIES = (REPOSITORY, FILE, SYMBOL, VARIABLE)
RELATIONS = (
    Relation(
        "CONTAINS", REPOSITORY, FILE, "Repository contains an indexed file.",
        "Does not describe directories or arbitrary containment.",
    ),
    Relation(
        "DEFINES", FILE, SYMBOL, "File defines a symbol.",
        "Class/method ownership is not a separate DEFINES edge.",
    ),
    Relation(
        "IMPORTS", FILE, FILE, "File imports a resolved local file.",
        "Unresolved and external imports are not represented as target files.",
    ),
    Relation(
        "CALLS", SYMBOL, SYMBOL, "A parsed call resolves to another symbol.",
        "Static resolution is not proof of runtime execution or exact overload selection.",
    ),
    Relation(
        "USES_VARIABLE", SYMBOL, VARIABLE, "Symbol owns a scoped variable.",
        "Requires a parser with variable-flow support.",
    ),
    Relation(
        "FLOWS_TO", VARIABLE, VARIABLE, "A parsed value flow connects variables.",
        "Lightweight static flow, including resolved argument/return flows; not full taint analysis.",
    ),
)
LIMITATIONS = (
    "Contract support is not evidence that a project is indexed or a relationship exists.",
    "Coverage depends on parser, language, successful indexing and graph freshness.",
    "No matches does not prove absence; inspect source or refresh the index when needed.",
)


def graph_ontology() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "entities": [asdict(entity) for entity in ENTITIES],
        "relations": [
            {
                "name": relation.name,
                "source": relation.source.label,
                "target": relation.target.label,
                "description": relation.description,
                "limitations": relation.limitations,
            }
            for relation in RELATIONS
        ],
        "limitations": list(LIMITATIONS),
        "workflow": [
            "Read this contract before selecting a relationship.",
            "Resolve an exact path or qualified_name with existing retrieval tools.",
            "Call query_graph_relation with the canonical relation and exact identity.",
            "Review indexed evidence, then inspect source and run validation.",
        ],
    }


def compile_relation_query(
    relation: RelationName, identity: str, direction: Direction, limit: int
) -> tuple[Relation, str, dict]:
    contract = next((item for item in RELATIONS if item.name == relation), None)
    if contract is None:
        raise ValueError(f"Unsupported graph relation: {relation}")
    if direction not in ("outbound", "inbound"):
        raise ValueError("direction must be outbound or inbound")
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 4096:
        raise ValueError("identity must be a non-empty string of at most 4096 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")

    anchor, entity = (
        ("source", contract.source)
        if direction == "outbound"
        else ("target", contract.target)
    )

    def projection(alias: str, node: Entity) -> str:
        file_path = f"{alias}.path" if node.label == "File" else f"{alias}.file_path"
        return (
            f"{alias}.{node.identity_property} AS {alias}_identity, "
            f"{file_path} AS {alias}_file_path, "
            f"coalesce({alias}.line_start, {alias}.line_number) AS {alias}_line_start, "
            f"coalesce({alias}.line_end, {alias}.line_number) AS {alias}_line_end"
        )

    # Only contract-owned identifiers enter Cypher; caller values remain parameters.
    cypher = (
        f"MATCH (source:{contract.source.label})-[edge:{contract.name}]->"
        f"(target:{contract.target.label})\n"
        f"WHERE {anchor}.{entity.identity_property} = $identity\n"
        f"RETURN {projection('source', contract.source)}, "
        f"{projection('target', contract.target)}, "
        "edge.scope_qname AS scope_qname, edge.line_number AS line_number, "
        "edge.flow_type AS flow_type\n"
        "ORDER BY source_identity, target_identity, scope_qname, line_number, flow_type\n"
        "LIMIT $fetch_limit"
    )
    return contract, cypher, {"identity": identity, "fetch_limit": limit + 1}
