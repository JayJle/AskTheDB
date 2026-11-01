import base64

import requests

from sql_graph.log_utils import log

TITLE = 'AskTheDB workflow'

# Node display info: node name -> (what it does, kind, group, node type)
# Each box shows three things: what the node does, its type (LLM, code or tool) and its name in the code.
# Kinds (box colors): terminal=start/end, gen_code=code node, gen_llm=LLM node that decides or writes,
# execute=tool node, check=LLM node that checks
NODE_META = {
    '__start__': ('User question', 'terminal', None, None),
    'call_list_all_tables': ('Prepare the call that lists all tables', 'gen_code', 'tables', 'Code node'),
    'list_all_tables_tool': ('List all table names', 'execute', 'tables', 'Tool node · MCP server'),
    'call_get_schema': ('Choose the tables the question needs', 'gen_llm', 'schema', 'LLM node'),
    'get_schema_tool': ('Fetch the schema of the chosen tables', 'execute', 'schema', 'Tool node · local'),
    'validate_question': ('Decide: answer, ask to clarify, or refuse', 'check', 'question', 'LLM node'),
    'generate_query': ('Write the SQL or the final answer, fetching any missing table schema first',
                       'gen_llm', 'sql', 'LLM node + tool node'),
    'check_query': ('Fix mistakes in the SQL before it runs', 'check', 'sql', 'LLM node'),
    'run_query': ('Run the SQL on the read-only database', 'execute', 'sql', 'Tool node · MCP server'),
    'verify_result': ('Check the result makes sense, or ask to fix the SQL', 'check', 'sql', 'LLM node'),
}

# Nodes drawn as part of another node instead of as a box of their own, to keep the diagram
# simple: node -> host node. The host lists both names, and their edges are not drawn.
# get_more_schema only fetches the schema that generate_query asks for before it writes the SQL
MERGED_NODES = {
    'get_more_schema': 'generate_query',
}

# The graph has one end node; the diagram splits it into one ending per outcome, placed next to
# the step that leads to it, so no exit line has to cross the diagram: ending -> (title, style, group)
ENDINGS = {
    'end_answer': ('Final answer', 'terminal', 'sql'),
    'end_clarify': ('Clarifying question or refusal', 'ending_clarify', None),
    'end_error_schema': ('Error message · nothing run', 'ending_error', 'schema'),
    'end_error_sql': ('Error message · nothing run', 'ending_error', 'sql'),
}
# Which ending each exit leads to (any other exit is a failed generation in the SQL loop)
ENDING_FOR_SOURCE = {
    'generate_query': 'end_answer',
    'validate_question': 'end_clarify',
    'call_get_schema': 'end_error_schema',
    'check_query': 'end_error_sql',
}
DEFAULT_ENDING = 'end_error_sql'
ERROR_ENDINGS = {'end_error_schema', 'end_error_sql'}

# Routing functions for conditional edges: source -> (function name, what it decides, {target: branch label})
ROUTES = {
    'generate_query': ('should_continue', 'SQL or final answer?', {
        'check_query': 'SQL',
        '__end__': 'final answer',
    }),
}

# Labels on edges: (source, target) -> label ('__end__' stands for the ending of that source)
EDGE_LABELS = {
    ('check_query', 'run_query'): 'checked SQL',
    ('verify_result', 'generate_query'): 'result, error or feedback',
    ('validate_question', 'generate_query'): 'answer',
    ('validate_question', '__end__'): 'clarify or refuse',
    ('call_get_schema', '__end__'): 'LLM call failed',
    ('check_query', '__end__'): 'LLM call failed',
}

# Each frame is one stage of the workflow
GROUPS = {
    'tables': 'List the tables',
    'schema': 'Fetch the schema of the relevant tables',
    'question': 'Check the question',
    'sql': 'Write, check, run and verify the SQL',
}

# Mermaid lays the diagram out in the order nodes and edges are written. This order keeps the
# main path straight and the lines from crossing.
# '<source>_route' is the decision hexagon of that source's routing function.
DRAW_ORDER = [
    '__start__', 'call_list_all_tables', 'list_all_tables_tool', 'call_get_schema', 'get_schema_tool',
    'end_error_schema', 'validate_question', 'generate_query', 'generate_query_route', 'check_query',
    'run_query', 'verify_result', 'end_error_sql', 'end_answer', 'end_clarify',
]
EDGE_ORDER = [
    ('__start__', 'call_list_all_tables'),
    ('call_list_all_tables', 'list_all_tables_tool'),
    ('list_all_tables_tool', 'call_get_schema'),
    ('call_get_schema', 'get_schema_tool'),
    ('call_get_schema', '__end__'),
    ('get_schema_tool', 'validate_question'),
    ('validate_question', '__end__'),
    ('validate_question', 'generate_query'),
    ('generate_query', 'check_query'),
    ('check_query', 'run_query'),
    ('run_query', 'verify_result'),
    ('verify_result', 'generate_query'),
    ('check_query', '__end__'),
    ('generate_query', '__end__'),
]

LEGEND = [
    ('#6366F1', 'LLM node that decides or writes'),
    ('#0EA5E9', 'LLM node that checks'),
    ('#F59E0B', 'Code node (no LLM)'),
    ('#10B981', 'Tool node'),
    ('#64748B', 'Routing rule in code'),
    ('#DC2626', 'Error exit: nothing is run'),
]

STYLES = '''
    classDef terminal fill:#1E293B,stroke:#1E293B,color:#FFFFFF
    classDef ending_clarify fill:#475569,stroke:#475569,color:#FFFFFF
    classDef ending_error fill:#FEF2F2,stroke:#DC2626,stroke-width:2px,color:#991B1B
    classDef gen_code fill:#FFF7ED,stroke:#F59E0B,stroke-width:2px,color:#78350F
    classDef gen_llm fill:#EEF2FF,stroke:#6366F1,stroke-width:2px,color:#312E81
    classDef execute fill:#ECFDF5,stroke:#10B981,stroke-width:2px,color:#064E3B
    classDef check fill:#F0F9FF,stroke:#0EA5E9,stroke-width:2px,color:#0C4A6E
    classDef decision fill:#FFFFFF,stroke:#64748B,stroke-width:2px,color:#0F172A
    classDef default fill:#F1F5F9,stroke:#94A3B8,color:#0F172A
    style tables fill:#F8FAFC,stroke:#94A3B8,stroke-dasharray:5 5,color:#334155
    style schema fill:#F8FAFC,stroke:#94A3B8,stroke-dasharray:5 5,color:#334155
    style question fill:#F8FAFC,stroke:#94A3B8,stroke-dasharray:5 5,color:#334155
    style sql fill:#F8FAFC,stroke:#94A3B8,stroke-dasharray:5 5,color:#334155
    classDef legend fill:#FFFFFF,stroke:#E2E8F0,color:#334155,text-align:left
'''


def _box_label(does: str, node_type: str, name: str) -> str:
    """The three lines of a box: what it does, its type, and its name in the code."""
    return f'<b>{does}</b><br/><small>{node_type}</small><br/><small><i>{name}</i></small>'


def _node_line(node_id: str) -> str:
    does, kind, _, node_type = NODE_META.get(node_id, (node_id, 'default', None, ''))
    if kind == 'terminal':
        return f'    {node_id}(["<b>{does}</b>"]):::terminal'
    names = [node_id] + [name for name, host in MERGED_NODES.items() if host == node_id]
    return f'    {node_id}("{_box_label(does, node_type, "node: " + " + ".join(names))}"):::{kind}'


def _ending_line(ending_id: str) -> str:
    title, style, _ = ENDINGS[ending_id]
    return f'    {ending_id}(["<b>{title}</b>"]):::{style}'


def to_mermaid(graph) -> str:
    """Convert the LangGraph workflow into styled Mermaid code."""
    g = graph.get_graph()
    lines = [
        '---',
        f'title: {TITLE}',
        'config:',
        '  theme: base',
        '  themeVariables:',
        '    lineColor: "#64748B"',
        '    edgeLabelBackground: "#FFFFFF"',
        '  flowchart:',
        '    curve: basis',
        '    wrappingWidth: 320',
        '    subGraphTitleMargin: {top: 8, bottom: 16}',
        '    nodeSpacing: 50',
        '    rankSpacing: 45',
        '---',
        'flowchart TD',
    ]

    def ending_for(source: str) -> str:
        return ENDING_FOR_SOURCE.get(source, DEFAULT_ENDING)

    def draw_rank(item_id: str) -> int:
        return DRAW_ORDER.index(item_id) if item_id in DRAW_ORDER else len(DRAW_ORDER)

    # Everything to draw: the graph's nodes (__end__ is drawn as the separate endings),
    # a decision hexagon per routing function, and the endings actually reached in this graph
    items = {}  # id -> (group, Mermaid line)
    for node_id in g.nodes:
        if node_id != '__end__' and node_id not in MERGED_NODES:
            items[node_id] = (NODE_META.get(node_id, (None, None, None, None))[2], _node_line(node_id))
    for source, (func, question, _) in ROUTES.items():
        if source in g.nodes:
            label = _box_label(question, 'Routing rule in code', f'function: {func}')
            line = f'    {source}_route{{{{"{label}"}}}}:::decision'
            items[f'{source}_route'] = (NODE_META[source][2], line)
    for ending_id in {ending_for(e.source) for e in g.edges if e.target == '__end__'}:
        items[ending_id] = (ENDINGS[ending_id][2], _ending_line(ending_id))
    ordered = sorted(items, key=draw_rank)

    # The start node first, then one frame per stage, then the endings outside the frames
    ungrouped = [i for i in ordered if items[i][0] is None]
    lines.extend(items[i][1] for i in ungrouped if i == '__start__')
    for group, title in GROUPS.items():
        members = [i for i in ordered if items[i][0] == group]
        if members:
            lines.append(f'    subgraph {group}["{title}"]')
            lines.extend('    ' + items[i][1] for i in members)
            lines.append('    end')
    lines.extend(items[i][1] for i in ungrouped if i != '__start__')

    # Edges
    routed = set()
    error_links = []  # Indexes of failure links, used to color them red
    link_count = 0

    def add_link(line: str, is_error: bool = False):
        nonlocal link_count
        lines.append(line)
        if is_error:
            error_links.append(link_count)
        link_count += 1

    def edge_rank(edge) -> int:
        key = (edge.source, edge.target)
        return EDGE_ORDER.index(key) if key in EDGE_ORDER else len(EDGE_ORDER)

    for edge in sorted(g.edges, key=edge_rank):
        if edge.source in MERGED_NODES or edge.target in MERGED_NODES:
            continue  # Drawn as part of the host node
        target = ending_for(edge.source) if edge.target == '__end__' else edge.target
        if edge.conditional and edge.source in ROUTES:
            if edge.source not in routed:
                add_link(f'    {edge.source} --> {edge.source}_route')
                routed.add(edge.source)
            label = ROUTES[edge.source][2].get(edge.target)
            arrow = f'-- {label} -->' if label else '-->'
            add_link(f'    {edge.source}_route {arrow} {target}')
        else:
            label = EDGE_LABELS.get((edge.source, edge.target))
            # Solid lines for the main path, dotted only for exits that stop early
            dotted = target in ERROR_ENDINGS or target == 'end_clarify'
            if label:
                arrow = f'-. {label} .->' if dotted else f'-- {label} -->'
            else:
                arrow = '-.->' if dotted else '-->'
            add_link(f'    {edge.source} {arrow} {target}', is_error=(target in ERROR_ENDINGS))

    if error_links:
        lines.append(f'    linkStyle {",".join(map(str, error_links))} stroke:#DC2626,color:#DC2626')

    # Legend: one compact box
    items = '<br/>'.join(f"<font color='{color}'>■</font> {text}" for color, text in LEGEND)
    lines.append(f'    legend["<b>Legend</b><br/>{items}"]:::legend')

    return '\n'.join(lines) + STYLES


def draw_graph(graph, file_name: str):
    try:
        mermaid_code = to_mermaid(graph)
        encoded = base64.urlsafe_b64encode(mermaid_code.encode('utf-8')).decode('ascii')
        # Render a high-resolution PNG via mermaid.ink (needs internet access)
        url = f'https://mermaid.ink/img/{encoded}?type=png&bgColor=!white&width=900&scale=2'
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(file_name, 'wb') as f:
            f.write(response.content)

    except Exception as e:
        # Drawing needs internet access and is optional, so just log the error
        log.exception(e)
