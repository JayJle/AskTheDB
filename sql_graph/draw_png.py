import base64

import requests

from sql_graph.log_utils import log

TITLE = 'AskTheDB workflow'

# Node display info: node name -> (title, kind, group, detail)
# Kinds: terminal=start/end, fixed=fixed step (no LLM call), llm=LLM step, tool=tool step
NODE_META = {
    '__start__': ('User question', 'terminal', None, None),
    'call_list_all_tables': ('Request table list', 'fixed', 'prepare', None),
    'list_all_tables_tool': ('① Get all table names', 'tool', 'prepare', 'MCP · list_all_tables_tool'),
    'call_get_schema': ('② Pick relevant tables', 'llm', 'prepare', None),
    'get_schema_tool': ('③ Get table DDL', 'tool', 'prepare', 'Local · sql_db_schema'),
    'validate_question': ('④ Validate the question', 'llm', 'prepare', 'ok / clarify / reject'),
    'generate_query': ('⑤ Write SQL or answer', 'llm', 'loop', None),
    'check_query': ('⑥ Check and fix SQL', 'llm', 'loop', None),
    'run_query': ('⑦ Run SQL', 'tool', 'loop', 'MCP · db_query_tool'),
    '__end__': ('Answer, clarification or error', 'terminal', 'loop', None),
}

# Routing functions for conditional edges: source -> (function name, question, {target: branch label})
ROUTES = {
    'generate_query': ('should_continue', 'Tool call?', {
        'check_query': 'Yes',
        '__end__': 'No · ⑨ final answer',
    }),
}

# Labels on normal edges: (source, target) -> label
EDGE_LABELS = {
    ('run_query', 'generate_query'): '⑧ rows or error',
}

ERROR_LABEL = 'generation failed'

# Exits to the end that are not errors: source -> label (drawn without the red error style)
EXIT_LABELS = {
    'validate_question': 'clarify / reject',
}

GROUPS = {
    'prepare': 'Stage 1 · Learn the database',
    'loop': 'Stage 2 · Query loop',
}

LEGEND = [
    ('#F59E0B', 'Fixed step (no LLM)'),
    ('#6366F1', 'LLM step'),
    ('#10B981', 'Tool step'),
    ('#64748B', 'Routing function'),
    ('#DC2626', 'Error exit (nothing is executed)'),
]

STYLES = '''
    classDef terminal fill:#1E293B,stroke:#1E293B,color:#FFFFFF
    classDef fixed fill:#FFF7ED,stroke:#F59E0B,stroke-width:2px,color:#78350F
    classDef llm fill:#EEF2FF,stroke:#6366F1,stroke-width:2px,color:#312E81
    classDef tool fill:#ECFDF5,stroke:#10B981,stroke-width:2px,color:#064E3B
    classDef decision fill:#FFFFFF,stroke:#64748B,stroke-width:2px,color:#0F172A
    classDef default fill:#F1F5F9,stroke:#94A3B8,color:#0F172A
    style prepare fill:#F8FAFC,stroke:#CBD5E1,stroke-dasharray:5 5,color:#475569
    style loop fill:#F8FAFC,stroke:#CBD5E1,stroke-dasharray:5 5,color:#475569
    classDef legend fill:#FFFFFF,stroke:#E2E8F0,color:#334155,text-align:left
'''


def _node_line(node_id: str) -> str:
    title, kind, _, detail = NODE_META.get(node_id, (node_id, 'default', None, None))
    if kind == 'terminal':
        return f'    {node_id}(["<b>{title}</b>"]):::terminal'
    label = f'<b>{title}</b>'
    if detail:
        label += f'<br/>{detail}'
    label += f'<br/><small><i>node: {node_id}</i></small>'
    return f'    {node_id}("{label}"):::{kind}'


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
        '    nodeSpacing: 40',
        '    rankSpacing: 40',
        '---',
        'flowchart TD',
    ]

    # Nodes (placed into subgraphs by group)
    grouped = {}
    for node_id in g.nodes:
        group = NODE_META.get(node_id, (None, None, None, None))[2]
        if group:
            grouped.setdefault(group, []).append(node_id)
        else:
            lines.append(_node_line(node_id))
    for group, node_ids in grouped.items():
        lines.append(f'    subgraph {group}["{GROUPS[group]}"]')
        lines.extend('    ' + _node_line(node_id) for node_id in node_ids)
        if group == 'loop':
            # Draw routing functions as decision hexagons inside the query loop
            for source, (func, question, _) in ROUTES.items():
                lines.append(f'        {source}_route{{{{"<b>{question}</b><br/><small><i>{func}</i></small>"}}}}:::decision')
        lines.append('    end')

    # Edges
    routed = set()
    error_links = []  # Indexes of error-branch links, used to color them red
    link_count = 0

    def add_link(line: str, is_error: bool = False):
        nonlocal link_count
        lines.append(line)
        if is_error:
            error_links.append(link_count)
        link_count += 1

    for edge in g.edges:
        if edge.conditional and edge.source in ROUTES:
            if edge.source not in routed:
                add_link(f'    {edge.source} --> {edge.source}_route')
                routed.add(edge.source)
            label = ROUTES[edge.source][2].get(edge.target)
            arrow = f'-. {label} .->' if label else '-.->'
            add_link(f'    {edge.source}_route {arrow} {edge.target}')
        elif edge.conditional and edge.target == '__end__' and edge.source in EXIT_LABELS:
            add_link(f'    {edge.source} -. {EXIT_LABELS[edge.source]} .-> {edge.target}')
        elif edge.conditional and edge.target == '__end__':
            # has_tool_calls route: end right away when generating the tool call failed
            add_link(f'    {edge.source} -. {ERROR_LABEL} .-> {edge.target}', is_error=True)
        else:
            label = EDGE_LABELS.get((edge.source, edge.target))
            arrow = f'-- {label} -->' if label else '-->'
            add_link(f'    {edge.source} {arrow} {edge.target}')

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
