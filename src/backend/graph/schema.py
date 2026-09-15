"""Cypher MERGE/MATCH templates for the ContextGraph schema.

Node labels   : Repository, File, Symbol
Edge types    : CONTAINS, DEFINES, IMPORTS, CALLS, REFERENCES

All write queries use MERGE to guarantee idempotency on re-index.
"""

# ---------------------------------------------------------------------------
# Node MERGE
# ---------------------------------------------------------------------------

MERGE_REPO = """
MERGE (r:Repository {path: $path})
SET r.name = $name
"""

QUERY_REPO_EXISTS = """
MATCH (r:Repository {path: $repo_path})
RETURN count(r) AS cnt
"""

QUERY_REPO_FILE_PATHS = """
MATCH (:Repository {path: $repo_path})-[:CONTAINS]->(f:File)
RETURN DISTINCT f.path
"""

DELETE_REPO = """
MATCH (r:Repository {path: $repo_path})
DETACH DELETE r
"""

MERGE_FILE = """
MERGE (f:File {path: $path})
SET f.language = $language,
    f.content_hash = $content_hash,
    f.symbols_hash = $symbols_hash,
    f.calls_hash = $calls_hash,
  f.imports_hash = $imports_hash,
  f.variables_hash = $variables_hash
"""

QUERY_FILE_SNAPSHOTS = """
MATCH (f:File)
RETURN f.path, f.content_hash, f.index_metadata, f.index_version
"""

SET_FILE_SNAPSHOT = """
MATCH (f:File {path: $path})
SET f.index_metadata = $metadata, f.index_version = $version
"""

DELETE_FILE_VARIABLES = """
MATCH (v:Variable {file_path: $file_path})
DETACH DELETE v
"""

DELETE_FILE_SYMBOLS = """
MATCH (s:Symbol {file_path: $file_path})
DETACH DELETE s
"""

DELETE_FILE = """
MATCH (f:File {path: $file_path})
DETACH DELETE f
"""

MERGE_SYMBOL = """
MERGE (s:Symbol {qualified_name: $qualified_name})
SET s.name           = $name,
    s.symbol_type    = $symbol_type,
    s.file_path      = $file_path,
    s.line_start     = $line_start,
    s.line_end       = $line_end
"""

MERGE_VARIABLE = """
MERGE (v:Variable {qualified_name: $qualified_name})
SET v.name        = $name,
    v.scope_qname = $scope_qname,
    v.file_path   = $file_path,
    v.line_number = $line_number,
    v.role        = $role
"""

# ---------------------------------------------------------------------------
# Edge MERGE
# ---------------------------------------------------------------------------

EDGE_REPO_CONTAINS_FILE = """
MATCH (r:Repository {path: $repo_path})
MATCH (f:File {path: $file_path})
MERGE (r)-[:CONTAINS]->(f)
"""

EDGE_FILE_DEFINES_SYMBOL = """
MATCH (f:File {path: $file_path})
MATCH (s:Symbol {qualified_name: $qualified_name})
MERGE (f)-[:DEFINES]->(s)
"""

EDGE_FILE_IMPORTS = """
MATCH (src:File {path: $src_path})
MATCH (tgt:File {path: $target_path})
MERGE (src)-[:IMPORTS]->(tgt)
"""

EDGE_SYMBOL_CALLS = """
MATCH (caller:Symbol {qualified_name: $caller_qname})
MATCH (callee:Symbol {qualified_name: $callee_qname})
MERGE (caller)-[:CALLS]->(callee)
"""

EDGE_SYMBOL_HAS_VARIABLE = """
MATCH (s:Symbol {qualified_name: $scope_qname})
MATCH (v:Variable {qualified_name: $variable_qname})
MERGE (s)-[:USES_VARIABLE]->(v)
"""

EDGE_VARIABLE_FLOWS = """
MATCH (source:Variable {qualified_name: $source_qname})
MATCH (target:Variable {qualified_name: $target_qname})
MERGE (source)-[:FLOWS_TO {scope_qname: $scope_qname, line_number: $line_number, flow_type: $flow_type}]->(target)
"""

QUERY_FILE_HASH = """
MATCH (f:File {path: $path})
RETURN f.content_hash AS hash
"""

QUERY_FILE_SYMBOL_HASHES = """
MATCH (f:File {path: $path})
RETURN f.content_hash, f.symbols_hash, f.calls_hash, f.imports_hash, f.variables_hash
"""

# ---------------------------------------------------------------------------
# Retrieval queries
# ---------------------------------------------------------------------------

QUERY_FIND_SYMBOL = """
MATCH (s:Symbol)
WHERE s.name = $name OR s.qualified_name CONTAINS $name
RETURN s.qualified_name, s.symbol_type, s.file_path, s.line_start, s.line_end
ORDER BY s.qualified_name
LIMIT $limit
"""

QUERY_FIND_CALLERS = """
MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol {qualified_name: $qualified_name})
RETURN caller.qualified_name, caller.file_path, caller.line_start
LIMIT $limit
"""

QUERY_FIND_CALLEES = """
MATCH (caller:Symbol {qualified_name: $qualified_name})-[:CALLS]->(callee:Symbol)
RETURN callee.qualified_name, callee.file_path, callee.line_start
LIMIT $limit
"""

QUERY_RETRIEVE_CONTEXT = """
MATCH (s:Symbol)
WHERE s.name CONTAINS $query OR s.qualified_name CONTAINS $query
MATCH (f:File {path: s.file_path})
RETURN s.qualified_name, s.symbol_type, s.file_path, s.line_start, s.line_end
ORDER BY s.name
LIMIT $limit
"""

QUERY_COUNT_SYMBOLS = """
MATCH (s:Symbol) RETURN count(s) AS cnt
"""

QUERY_COUNT_VARIABLES = """
MATCH (v:Variable) RETURN count(v) AS cnt
"""

# ---------------------------------------------------------------------------
# Aggregation queries (architecture analysis)
# ---------------------------------------------------------------------------

QUERY_FILE_STATS = """
MATCH (f:File {path: $file_path})
WITH f
OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
WITH f, count(s) AS symbol_count
OPTIONAL MATCH (f)<-[:CALLS]-(caller:Symbol)
WITH f, symbol_count, count(DISTINCT caller) AS incoming_calls
OPTIONAL MATCH (f)-[:DEFINES]-(s:Symbol)-[:CALLS]->()
RETURN symbol_count AS symbols, incoming_calls, count(DISTINCT s) AS symbols_with_outgoing_calls
"""

QUERY_KEY_FILES = """
MATCH (f:File)
WITH f
OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
WITH f, count(s) AS symbol_count
OPTIONAL MATCH (f)<-[:CALLS]-(caller:Symbol)
WITH f, symbol_count, count(DISTINCT caller) AS incoming_calls
OPTIONAL MATCH (f)-[:DEFINES]-(s:Symbol)-[:CALLS]->()
WITH f, symbol_count, incoming_calls, count(DISTINCT s) AS symbols_with_calls,
     (symbol_count * 0.3 + coalesce(incoming_calls, 0) * 0.7) AS importance_score
ORDER BY importance_score DESC
LIMIT $limit
RETURN f.path AS file_path, f.language AS language, symbol_count, incoming_calls, symbols_with_calls, importance_score
"""

QUERY_MODULE_DEPENDENCIES = """
MATCH (f1:File)-[:DEFINES]->(s1:Symbol)-[:CALLS]->(s2:Symbol)<-[:DEFINES]-(f2:File)
WHERE f1.path <> f2.path
WITH f1.path AS from_file, f2.path AS to_file, count(DISTINCT s1) AS caller_symbols, count(DISTINCT s2) AS callee_symbols
RETURN from_file, to_file, caller_symbols, callee_symbols
ORDER BY caller_symbols DESC
LIMIT $limit
"""

QUERY_ARCHITECTURE_OVERVIEW = """
MATCH (f:File)
WITH f
OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
WITH f, count(s) AS symbol_count
OPTIONAL MATCH (f)<-[:CALLS]-(caller:Symbol)
WITH f, symbol_count, count(DISTINCT caller) AS callers
OPTIONAL MATCH (f)-[:DEFINES]-(s:Symbol)-[:CALLS]->()
WITH f, symbol_count, callers, count(s) AS functions_with_calls
OPTIONAL MATCH (f)<-[:DEFINES]-(s:Symbol)<-[:CALLS]-()
RETURN 
  count(DISTINCT f) AS total_files,
  sum(symbol_count) AS total_symbols,
  count(DISTINCT f.language) AS languages,
  sum(CASE WHEN callers > 0 THEN 1 ELSE 0 END) AS files_with_incoming_calls,
  avg(symbol_count) AS avg_symbols_per_file,
  avg(callers) AS avg_callers_per_file
"""

QUERY_FILE_DEPENDENCY_CHAIN = """
MATCH path = (f1:File)-[:DEFINES]->(s1:Symbol)-[:CALLS*1..3]->(s2:Symbol)<-[:DEFINES]-(f2:File)
WHERE f1.path = $source_path AND f2.path = $target_path
WITH relationships(path) AS edges
RETURN length(edges) AS hops, count(*) AS paths
ORDER BY hops
LIMIT 10
"""

# ---------------------------------------------------------------------------
# Call-graph analysis queries (v1.17.0)
# ---------------------------------------------------------------------------

QUERY_FAN_IN = """
MATCH (s:Symbol {qualified_name: $qualified_name})<-[:CALLS]-(caller:Symbol)
RETURN DISTINCT caller.qualified_name AS caller
"""

QUERY_FAN_OUT = """
MATCH (s:Symbol {qualified_name: $qualified_name})-[:CALLS]->(callee:Symbol)
RETURN DISTINCT callee.qualified_name AS callee
"""

QUERY_CRITICAL_FUNCTIONS = """
MATCH (s:Symbol)
OPTIONAL MATCH (s)<-[:CALLS]-(callers:Symbol)
OPTIONAL MATCH (s)-[:CALLS]->(callees:Symbol)
WITH s, count(DISTINCT callers) AS fan_in, count(DISTINCT callees) AS fan_out
WHERE fan_in > 0 OR fan_out > 0
WITH s, fan_in, fan_out, (fan_in * 0.6) + ((fan_out / (MAX(fan_out) OVER ())) * 0.4) AS score
RETURN s.qualified_name, s.symbol_type, fan_in, fan_out, round(score * 100) AS importance_score
ORDER BY importance_score DESC
LIMIT $limit
"""

QUERY_CYCLIC_DEPENDENCIES = """
MATCH (s1:Symbol)-[:CALLS*2..]->(s2:Symbol)-[:CALLS*1..]->(s1:Symbol)
RETURN DISTINCT s1.qualified_name AS symbol
"""

# ---------------------------------------------------------------------------
# Variable-flow queries
# ---------------------------------------------------------------------------

QUERY_FIND_VARIABLE = """
MATCH (v:Variable)
WHERE v.name = $name OR v.qualified_name CONTAINS $name
RETURN v.qualified_name, v.scope_qname, v.file_path, v.line_number, v.role
ORDER BY v.qualified_name
LIMIT $limit
"""

QUERY_VARIABLE_FLOWS_FOR_SCOPE = """
MATCH (source:Variable)-[r:FLOWS_TO {scope_qname: $scope_qname}]->(target:Variable)
RETURN source.qualified_name, target.qualified_name, r.flow_type, r.line_number
ORDER BY r.line_number, source.qualified_name, target.qualified_name
LIMIT $limit
"""

QUERY_VARIABLE_LINEAGE = """
MATCH (v:Variable {qualified_name: $qualified_name})
OPTIONAL MATCH (upstream:Variable)-[in_rel:FLOWS_TO]->(v)
OPTIONAL MATCH (v)-[out_rel:FLOWS_TO]->(downstream:Variable)
RETURN collect(DISTINCT upstream.qualified_name), collect(DISTINCT downstream.qualified_name)
"""

QUERY_RETURN_INFLUENCE = """
MATCH path = (param:Variable {scope_qname: $scope_qname, role: 'parameter'})-[:FLOWS_TO*1..]->(ret:Variable {scope_qname: $scope_qname, name: '__return__'})
RETURN param.qualified_name, [node IN nodes(path) | node.qualified_name] AS flow_path
LIMIT $limit
"""

QUERY_SCOPE_VARIABLE_METRICS = """
MATCH (s:Symbol {qualified_name: $scope_qname})-[:USES_VARIABLE]->(v:Variable)
OPTIONAL MATCH (incoming:Variable)-[:FLOWS_TO {scope_qname: $scope_qname}]->(v)
WITH v, count(DISTINCT incoming) AS incoming_count
OPTIONAL MATCH (v)-[:FLOWS_TO {scope_qname: $scope_qname}]->(outgoing:Variable)
RETURN v.qualified_name, v.name, v.role, incoming_count, count(DISTINCT outgoing) AS outgoing_count
ORDER BY v.role, v.name
"""

# ---------------------------------------------------------------------------
# Import tracking queries
# ---------------------------------------------------------------------------

QUERY_FILE_IMPORTS = """
MATCH (f:File {path: $file_path})-[:IMPORTS]->(target:File)
RETURN target.path AS target_file, target.language AS language
"""

QUERY_IMPORT_DEPENDENTS = """
MATCH (dependent:File)-[:IMPORTS]->(f:File {path: $file_path})
RETURN dependent.path AS dependent_file, dependent.language AS language
"""

QUERY_DEPENDENCY_GRAPH = """
MATCH (f:File)-[r:IMPORTS]->(target:File)
RETURN f.path AS from_file, target.path AS to_file, f.language AS from_language, target.language AS to_language
LIMIT $limit
"""

QUERY_EXTERNAL_DEPENDENCIES = """
MATCH (f:File)
WITH f
OPTIONAL MATCH (f)-[r:IMPORTS]->(target:File)
WITH f, count(target) AS internal_imports
OPTIONAL MATCH (f)<-[r2:IMPORTS]-(dependent:File)
RETURN f.path AS file_path, f.language AS language, internal_imports, count(DISTINCT dependent) AS incoming_imports
ORDER BY internal_imports DESC, incoming_imports DESC
LIMIT $limit
"""

# ---------------------------------------------------------------------------
# Batch UNWIND write queries (perf: replaces N round-trips with 1 per type)
# ---------------------------------------------------------------------------

BATCH_MERGE_SYMBOLS = """
UNWIND $rows AS row
MERGE (s:Symbol {qualified_name: row.qualified_name})
SET s.name        = row.name,
    s.symbol_type = row.symbol_type,
    s.file_path   = row.file_path,
    s.line_start  = row.line_start,
    s.line_end    = row.line_end
"""

BATCH_EDGE_FILE_DEFINES_SYMBOL = """
UNWIND $rows AS row
MATCH (f:File {path: row.file_path})
MATCH (s:Symbol {qualified_name: row.qualified_name})
MERGE (f)-[:DEFINES]->(s)
"""

BATCH_MERGE_VARIABLES = """
UNWIND $rows AS row
MERGE (v:Variable {qualified_name: row.qualified_name})
SET v.name        = row.name,
    v.scope_qname = row.scope_qname,
    v.file_path   = row.file_path,
    v.line_number = row.line_number,
    v.role        = row.role,
    v.parameter_index = row.parameter_index
"""

BATCH_EDGE_SYMBOL_HAS_VARIABLE = """
UNWIND $rows AS row
MATCH (s:Symbol {qualified_name: row.scope_qname})
MATCH (v:Variable {qualified_name: row.variable_qname})
MERGE (s)-[:USES_VARIABLE]->(v)
"""

BATCH_EDGE_VARIABLE_FLOWS = """
UNWIND $rows AS row
MATCH (source:Variable {qualified_name: row.source_qname})
MATCH (target:Variable {qualified_name: row.target_qname})
MERGE (source)-[:FLOWS_TO {scope_qname: row.scope_qname, line_number: row.line_number, flow_type: row.flow_type}]->(target)
"""

BATCH_EDGE_SYMBOL_CALLS = """
UNWIND $rows AS row
MATCH (caller:Symbol {qualified_name: row.caller_qname})
MATCH (callee:Symbol {qualified_name: row.callee_qname})
MERGE (caller)-[:CALLS]->(callee)
"""

BATCH_EDGE_FILE_IMPORTS = """
UNWIND $rows AS row
MATCH (src:File {path: row.src_path})
MATCH (tgt:File {path: row.target_path})
MERGE (src)-[:IMPORTS]->(tgt)
"""

BATCH_QUERY_SCOPE_PARAMETERS = """
UNWIND $scope_qnames AS sq
MATCH (:Symbol {qualified_name: sq})-[:USES_VARIABLE]->(v:Variable {role: 'parameter'})
RETURN sq, v.qualified_name, v.line_number, v.name
ORDER BY sq, v.parameter_index, v.line_number, v.name
"""
