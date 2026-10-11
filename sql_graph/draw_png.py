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
    'validate_question': ('④ Validate the question', 'check', None, 'CHECK · LLM'),
    'generate_query': ('⑤ Write SQL or answer', 'gen_llm', 'sql', 'GENERATE · LLM'),
    'check_query': ('⑥ Check the SQL', 'check', 'sql', 'CHECK · LLM, rewrites the call'),
    'run_query': ('⑦ Run the SQL', 'execute', 'sql', 'EXECUTE · MCP tool, read-only'),
    'verify_result': ('⑧ Verify the result', 'check', None, 'CHECK · LLM'),
}

# The graph has one end node; the diagram splits it into one ending per outcome, placed near
# the step that leads to it, so the exit lines do not cross: ending -> (title, style, group)
ENDINGS = {
    'end_answer': ('Answer', 'terminal', None),
    'end_clarify': ('Clarification or refusal', 'ending_clarify', None),
    'end_error': ('Error message · nothing executed', 'ending_error', None),
}
# Which ending each conditional exit leads to (any other exit is a failed generation)
ENDING_FOR_SOURCE = {'generate_query': 'end_answer', 'validate_question': 'end_clarify'}
ERROR_ENDING = 'end_error'

# Routing functions for conditional edges: source -> (function name, question, {target: branch label})
ROUTES = {
    'generate_query': ('should_continue', 'Needs another query?', {
        'check_query': 'Yes · tool call',
        '__end__': 'No · ⑩ answer',
    }),
}

# Labels on edges: (source, target) -> label ('__end__' stands for the ending of that source)
EDGE_LABELS = {
    ('call_list_all_tables', 'list_all_tables_tool'): 'tool call',
    ('call_get_schema', 'get_schema_tool'): 'tool call',
    ('check_query', 'run_query'): 'checked tool call',
    ('verify_result', 'generate_query'): '⑨ result, error or feedback',
    ('validate_question', '__end__'): 'clarify / reject',
    ('call_get_schema', '__end__'): 'failed',
    ('check_query', '__end__'): 'failed',
}

# Each frame is one tool call: a GENERATE step writes the instruction, an EXECUTE step runs it
GROUPS = {
    'tables': 'Tool call · list the tables',
    'schema': 'Tool call · fetch the schema',
    'sql': 'Tool call · run SQL (checked before it runs)',
}

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
        return ENDING_FOR_SOURCE.get(source, ERROR_ENDING)

    # Endings actually reached in this graph
    used_endings = {ending_for(e.source) for e in g.edges if e.target == '__end__'}

    # Nodes (placed into subgraphs by group); __end__ is drawn as the separate endings
    grouped = {}
    for node_id in g.nodes:
        if node_id == '__end__':
            continue
        group = NODE_META.get(node_id, (None, None, None, None))[2]
        if group:
            grouped.setdefault(group, []).append(_node_line(node_id))
        else:
            lines.append(_node_line(node_id))
    for ending_id in sorted(used_endings):
        group = ENDINGS[ending_id][2]
        if group:
            grouped.setdefault(group, []).append(_ending_line(ending_id))
        else:
            lines.append(_ending_line(ending_id))
    for group, node_lines in grouped.items():
        lines.append(f'    subgraph {group}["{GROUPS[group]}"]')
        lines.extend('    ' + line for line in node_lines)
        if group == 'sql':
            # Draw routing functions as decision hexagons inside the SQL tool call
            for source, (func, question, _) in ROUTES.items():
                lines.append(f'        {source}_route{{{{"<b>{question}</b><br/><small><i>{func}</i></small>"}}}}:::decision')
        lines.append('    end')

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

    for edge in g.edges:
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
            dotted = target in (ERROR_ENDING, 'end_clarify')
            if label:
                arrow = f'-. {label} .->' if dotted else f'-- {label} -->'
            else:
                arrow = '-.->' if dotted else '-->'
            add_link(f'    {edge.source} {arrow} {target}', is_error=(target == ERROR_ENDING))

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
