import base64

import requests

from sql_graph.log_utils import log

TITLE = 'AskTheDB workflow'

# Node display info: node name -> (title, kind, group, role tag)
# Kinds (by role): terminal=start/end, gen_code=generates a tool call with fixed code,
# gen_llm=generates a tool call with the LLM, execute=runs a tool call, check=LLM judges something
NODE_META = {
    '__start__': ('User question', 'terminal', None, None),
    'call_list_all_tables': ('Request the table list', 'gen_code', 'tables', 'GENERATE · fixed code'),
    'list_all_tables_tool': ('① Get all table names', 'execute', 'tables', 'EXECUTE · MCP tool'),
    'call_get_schema': ('② Pick relevant tables', 'gen_llm', 'schema', 'GENERATE · LLM'),
    'get_schema_tool': ('③ Get table DDL', 'execute', 'schema', 'EXECUTE · local tool'),
    'validate_question': ('④ Validate the question', 'check', 'question', 'CHECK · LLM'),
    'generate_query': ('⑤ Write SQL or answer', 'gen_llm', 'sql', 'GENERATE · LLM'),
    'get_more_schema': ('Get DDL of more tables', 'execute', 'sql', 'EXECUTE · local tool'),
    'check_query': ('⑥ Check the SQL', 'check', 'sql', 'CHECK · LLM, rewrites the call'),
    'run_query': ('⑦ Run the SQL', 'execute', 'sql', 'EXECUTE · MCP tool, read-only'),
    'verify_result': ('⑧ Verify the result', 'check', 'sql', 'CHECK · LLM'),
}

# The graph has one end node; the diagram splits it into one ending per outcome, placed next to
# the step that leads to it, so no exit line has to cross the diagram: ending -> (title, style, group)
ENDINGS = {
    'end_answer': ('Answer', 'terminal', 'sql'),
    'end_clarify': ('Clarification or refusal', 'ending_clarify', None),
    'end_error_schema': ('Error message · nothing executed', 'ending_error', 'schema'),
    'end_error_sql': ('Error message · nothing executed', 'ending_error', 'sql'),
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

# Routing functions for conditional edges: source -> (function name, question, {target: branch label})
ROUTES = {
    'generate_query': ('should_continue', 'Next step?', {
        'check_query': 'SQL',
        'get_more_schema': 'more tables',
        '__end__': '⑩ answer',
    }),
}

# Labels on edges: (source, target) -> label ('__end__' stands for the ending of that source)
EDGE_LABELS = {
    ('call_list_all_tables', 'list_all_tables_tool'): 'tool call',
    ('call_get_schema', 'get_schema_tool'): 'tool call',
    ('check_query', 'run_query'): 'checked tool call',
    ('verify_result', 'generate_query'): '⑨ result, error or feedback',
    ('get_more_schema', 'generate_query'): 'DDL',
    ('validate_question', 'generate_query'): 'ok',
    ('validate_question', '__end__'): 'clarify / reject',
    ('call_get_schema', '__end__'): 'failed',
    ('check_query', '__end__'): 'failed',
}

# Each frame is one stage of the workflow
GROUPS = {
    'tables': 'List the tables',
    'schema': 'Fetch the schema of the relevant tables',
    'question': 'Check the question',
    'sql': 'Write, check, run and verify the SQL',
}

# Mermaid lays the diagram out in the order nodes and edges are written. This order keeps the
# main path straight and the lines from crossing: in the SQL loop "more tables" goes left, the
# answer straight down and the SQL right, and the lines back to ⑤ run along the left side.
# '<source>_route' is the decision hexagon of that source's routing function.
DRAW_ORDER = [
    '__start__', 'call_list_all_tables', 'list_all_tables_tool', 'call_get_schema', 'get_schema_tool',
    'end_error_schema', 'validate_question', 'generate_query', 'generate_query_route', 'check_query',
    'run_query', 'verify_result', 'end_error_sql', 'get_more_schema', 'end_answer', 'end_clarify',
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
    ('generate_query', 'get_more_schema'),
    ('get_more_schema', 'generate_query'),
    ('generate_query', '__end__'),
]

LEGEND = [
    ('#6366F1', 'GENERATE a tool call (LLM)'),
    ('#F59E0B', 'GENERATE a tool call (fixed code)'),
    ('#10B981', 'EXECUTE the tool call'),
    ('#0EA5E9', 'CHECK the question, SQL or result'),
    ('#64748B', 'Decision'),
    ('#DC2626', 'Failure exit (nothing is executed)'),
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


def _node_line(node_id: str) -> str:
    title, kind, _, role = NODE_META.get(node_id, (node_id, 'default', None, None))
    if kind == 'terminal':
        return f'    {node_id}(["<b>{title}</b>"]):::terminal'
    label = f'<small><b>{role}</b></small><br/>' if role else ''
    label += f'<b>{title}</b><br/><small><i>node: {node_id}</i></small>'
    return f'    {node_id}("{label}"):::{kind}'


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
        if node_id != '__end__':
            items[node_id] = (NODE_META.get(node_id, (None, None, None, None))[2], _node_line(node_id))
    for source, (func, question, _) in ROUTES.items():
        if source in g.nodes:
            line = f'    {source}_route{{{{"<b>{question}</b><br/><small><i>{func}</i></small>"}}}}:::decision'
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
