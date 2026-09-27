"""Opt-in official scanner compatibility check on an intentionally old dependency manifest."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import State, write_json
from pipeline import Budget, scanner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("requests==2.19.1\n", encoding="utf-8")
    (source / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    state = State(root / "state")
    job = {"id": "scanner-fixture", "spent": 0}
    budget = Budget(state, job)
    results = {}
    try:
        for engine in ("semgrep", "osv"):
            try:
                results[engine] = scanner(state, source, engine, root / f"{engine}.json", budget)
            except Exception as exc:
                results[engine] = {"error": str(exc)}
        write_json(root / "result.json", results)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return int(any("error" in value for value in results.values()))
    finally:
        budget.save()
        state.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
