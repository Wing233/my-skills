"""Opt-in Linux Docker integration test, using only our authored empty-list fixture."""
import argparse
import json
from pathlib import Path

from test_workflow import WorkflowTests
from core import State
from pipeline import verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = WorkflowTests()
    fixture.root = root
    fixture.state = State(root / "state")
    try:
        _, finding = fixture.fixture()
        result = verify(fixture.state, finding["id"])
        print(json.dumps({"status": result["status"], "error": result.get("error"),
                          "evidence": str(root / "state/findings" / finding["id"] / "verification.json")}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "verified" else 1
    finally:
        fixture.state.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
