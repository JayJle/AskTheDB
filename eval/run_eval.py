"""Run the AskTheDB evaluation set through the full workflow and score it.

Usage (with the MCP server running):
    python eval/run_eval.py              # all 100 cases
    python eval/run_eval.py E01 H11      # only the given case ids
    python eval/run_eval.py --rescore eval/results/results_<timestamp>.json
                                         # re-score saved answers after changing the scoring (no LLM calls)

Writes eval/results/results_<timestamp>.json and prints a summary.
"""
import asyncio
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from eval.test_set import CASES  # noqa: E402
from sql_graph.askthedb_graph import make_graph  # noqa: E402

CONCURRENCY = 4
DB = sqlite3.connect(f'file:{(ROOT / "chinook.db").as_posix()}?mode=ro', uri=True)

# Accepted translations of database values that may appear in Chinese answers
ALIASES = {
    'usa': ['usa', 'united states', '美国'], 'canada': ['canada', '加拿大'], 'france': ['france', '法国'],
    'brazil': ['brazil', '巴西'], 'germany': ['germany', '德国'], 'united kingdom': ['united kingdom', 'uk', '英国'],
    'chile': ['chile', '智利'], 'czech republic': ['czech', '捷克'],
    'finland': ['finland', '芬兰'], 'hungary': ['hungary', '匈牙利'], 'ireland': ['ireland', '爱尔兰'],
    'india': ['india', '印度'],
    'rock': ['rock', '摇滚'], 'jazz': ['jazz', '爵士'], 'metal': ['metal', '金属'], 'latin': ['latin', '拉丁'],
    'alternative & punk': ['alternative & punk', 'alternative and punk', '另类', '朋克'],
    'electronica/dance': ['electronica', '电子'], 'opera': ['opera', '歌剧'], 'world': ['world', '世界'],
    'sci fi & fantasy': ['sci fi & fantasy', 'sci-fi & fantasy', '科幻'],
    'general manager': ['general manager', '总经理'], 'sales manager': ['sales manager', '销售经理'],
    'sales support agent': ['sales support agent', '销售支持'], 'it manager': ['it manager', 'it经理', 'it 经理'],
    'it staff': ['it staff', 'it员工', 'it 员工', 'it职员', 'it 职员'],
}


def norm(text) -> str:
    text = unicodedata.normalize('NFKD', str(text))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace('’', "'").replace('　', ' ').lower()


def answer_has(answer: str, value, tol: float, signless: bool = False) -> bool:
    text = norm(answer)
    if isinstance(value, (int, float)):
        numbers = [float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', text.replace(',', ''))]
        if signless:
            return any(abs(abs(n) - abs(value)) <= tol for n in numbers)
        return any(abs(n - value) <= tol for n in numbers)
    target = norm(value).strip()
    return any(alias in text for alias in ALIASES.get(target, [target]))


def row_values(row) -> set:
    """Values of a result row, plus adjacent string columns joined (so 'Jane', 'Peacock' matches 'Jane Peacock')."""
    values = {round(v, 2) if isinstance(v, float) else norm(v) if isinstance(v, str) else v for v in row}
    for a, b in zip(row, row[1:]):
        if isinstance(a, str) and isinstance(b, str):
            values.add(norm(f'{a} {b}'))
    return values


def rows_match(gold, pred) -> bool:
    if len(gold) != len(pred):
        return False
    unused = [row_values(r) for r in pred]
    for g in gold:
        wanted = {round(v, 2) if isinstance(v, float) else norm(v) if isinstance(v, str) else v for v in g}
        hit = next((i for i, cand in enumerate(unused) if wanted <= cand), None)
        if hit is None:
            return False
        unused.pop(hit)
    return True


async def run_case(graph, case, semaphore):
    async with semaphore:
        start, error, state = time.time(), None, None
        try:
            async for state in graph.astream({"messages": [{"role": "user", "content": case['question']}]},
                                             stream_mode="values"):
                pass
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
        seconds = round(time.time() - start, 1)

    messages = state["messages"] if state else []
    results = {m.tool_call_id: str(m.content) for m in messages if isinstance(m, ToolMessage)}
    executed = [{'sql': c['args'].get('query'), 'result': results.get(c['id'], '')[:300]}
                for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c['name'] == 'db_query_tool']
    answer = messages[-1].content if messages and isinstance(messages[-1], AIMessage) else ''
    status = (state or {}).get('question_status')
    return score(case, dict(status=status, assumption=(state or {}).get('assumption', ''), answer=answer,
                            executed=executed, seconds=seconds, error=error,
                            verify_rounds=(state or {}).get('verify_rounds', 0)))


def score(case, run):
    expect, status, answer = case['expect'], run['status'], run['answer']
    run.update(id=case['id'], category=case['category'], lang=case['lang'], dims=case['dims'],
               question=case['question'], expect=expect, ex=None)
    if run['error']:
        run['passed'], run['reason'] = False, 'workflow error'
    elif expect == 'reject':
        stopped = status in ('clarify', 'reject')
        run['passed'] = stopped and not run['executed']
        run['reason'] = '' if run['passed'] else ('ran SQL' if run['executed'] else f'not stopped (status={status})')
    elif expect == 'clarify_or_assume':
        run['passed'] = status == 'clarify' or (status == 'ok' and bool(run['assumption']))
        run['reason'] = '' if run['passed'] else 'answered without clarifying or stating an interpretation'
    else:
        gold = DB.execute(case['sql']).fetchall() if case.get('sql') else None
        run['gold'] = gold
        if case.get('clarify_ok') and status in ('clarify', 'reject'):
            run['passed'], run['reason'] = True, f'{status} with explanation (accepted)'
        elif status in ('clarify', 'reject'):
            run['passed'], run['reason'] = False, f'wrongly {status}ed an answerable question'
        elif case.get('accept'):
            run['passed'] = any(norm(a).replace(',', '') in norm(answer).replace(',', '') for a in case['accept'])
            run['reason'] = '' if run['passed'] else 'answer does not contain an accepted value'
        else:
            cols = case.get('check')
            values = [v for row in gold for i, v in enumerate(row) if cols is None or i in cols]
            missing = [v for v in values if not answer_has(answer, v, case.get('tol', 0.011), case.get('signless', False))]
            run['passed'] = not missing
            run['reason'] = '' if run['passed'] else f'missing in answer: {missing[:5]}'
        ok_sql = [e['sql'] for e in run['executed'] if not e['result'].startswith('Error')]
        if gold is not None and ok_sql:
            try:
                run['ex'] = rows_match(gold, DB.execute(ok_sql[-1]).fetchall())
            except Exception:
                run['ex'] = False
        elif gold is not None:
            run['ex'] = False
    return run


def summarize(results):
    def line(name, rs):
        passed = sum(r['passed'] for r in rs)
        return f'  {name:16} {passed:3}/{len(rs):<3} ({100 * passed / len(rs):5.1f}%)'

    print(f"\nOverall: {sum(r['passed'] for r in results)}/{len(results)}")
    print('By category:')
    for cat in ('easy', 'medium', 'hard', 'edge', 'ambiguous', 'out_of_scope'):
        rs = [r for r in results if r['category'] == cat]
        if rs:
            print(line(cat, rs))
    print('By language:')
    for lang in ('zh', 'en'):
        print(line(lang, [r for r in results if r['lang'] == lang]))
    ex = [r for r in results if r['ex'] is not None]
    if ex:
        print(f"SQL execution accuracy (EX): {sum(r['ex'] for r in ex)}/{len(ex)}")
    by_dim = defaultdict(list)
    for r in results:
        for d in r['dims']:
            by_dim[d].append(r)
    weak = [(d, rs) for d, rs in sorted(by_dim.items()) if any(not r['passed'] for r in rs)]
    if weak:
        print('Dimensions with failures:')
        for d, rs in weak:
            print(line(d, rs))
    times = [r['seconds'] for r in results]
    print(f'Time per question: avg {sum(times) / len(times):.1f}s, max {max(times):.1f}s')
    print('Failures:')
    for r in results:
        if not r['passed']:
            print(f"  {r['id']} [{r['category']}] {r['question']}\n      -> {r['reason']}")


async def main(ids):
    cases = [c for c in CASES if not ids or c['id'] in ids]
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with make_graph() as graph:
        results = await asyncio.gather(*(run_case(graph, c, semaphore) for c in cases))
    out = ROOT / 'eval' / 'results'
    out.mkdir(exist_ok=True)
    path = out / f'results_{datetime.now():%Y%m%d_%H%M%S}.json'
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    summarize(results)
    print(f'\nDetails: {path}')


def rescore(path):
    cases = {c['id']: c for c in CASES}
    runs = json.loads(Path(path).read_text(encoding='utf-8'))
    results = [score(cases[r['id']], {k: r.get(k, 0 if k == 'verify_rounds' else None) for k in ('status', 'assumption', 'answer', 'executed', 'seconds', 'error', 'verify_rounds')})
               for r in runs]
    Path(path).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    summarize(results)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--rescore']:
        rescore(sys.argv[2])
    else:
        asyncio.run(main(set(sys.argv[1:])))
