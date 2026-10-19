"""Expands a JIRA Feature into every Story beneath it (Feature -> Epic ->
Story, Stories attached directly to the Feature included) and runs the Test
Case Writer (Agent 1) once per Story, each pulling its own comments, linked
Zephyr Test issue steps and attachments via testcase_writer.fetch_jira_hierarchy.

Standalone, not wired into qa_pipeline's orchestrator: the CLI's `run`
command only carries one story_key through RunContext, while
testcase_writer.run() already accepts a list and writes one sheet per story.
This script supplies that list from the real JIRA tree instead of the single
synthesized DOC-1 story.

Usage:
    python scripts/generate_cases_for_feature.py --profile example \
        --feature PROJ-68294 --figma "profiles/Example App/figma.docx"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from utils import jira_client as jc  # noqa: E402


def resolve_story_tree(feature_key: str) -> list[str]:
    client = jc.JiraClient()
    if not client.configured:
        raise SystemExit("JIRA not configured — check JIRA_BASE_URL/JIRA_API_TOKEN in .env")

    feature = client.get_issue(feature_key)
    if not feature:
        raise SystemExit(f"Could not fetch {feature_key} — check the key and credentials.")

    direct_children = client.children_of(feature_key)
    epics = [i["key"] for i in direct_children
             if i.get("fields", {}).get("issuetype", {}).get("name") == "Epic"]
    story_keys = {i["key"] for i in direct_children
                  if i.get("fields", {}).get("issuetype", {}).get("name") in ("Story", "Task")}

    for epic_key in epics:
        epic_children = client.children_of(epic_key, issue_types=("Story", "Task"))
        story_keys.update(i["key"] for i in epic_children)

    print(f"{feature_key}: {len(epics)} Epic(s), {len(story_keys)} Story/Task(s) total.")
    return sorted(story_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--feature", required=True, help="JIRA Feature key, e.g. PROJ-68294")
    parser.add_argument("--figma", default="", help="Figma URL or local .docx/.xlsx/.pdf path")
    parser.add_argument("--design-doc", default="")
    parser.add_argument("--api-spec", default="")
    parser.add_argument("--limit", type=int, default=0,
                         help="process only the first N stories (0 = all)")
    parser.add_argument("--only", default="",
                         help="comma-separated story keys to process, skipping tree "
                              "resolution entirely — for resuming a partial run")
    args = parser.parse_args()

    if args.only:
        story_keys = [k.strip() for k in args.only.split(",") if k.strip()]
        print(f"--only given — processing exactly: {story_keys}")
    else:
        story_keys = resolve_story_tree(args.feature)
        if args.limit:
            story_keys = story_keys[:args.limit]
            print(f"--limit {args.limit} given — processing only: {story_keys}")

    from agents import testcase_writer

    feature_dir = Path(__file__).resolve().parent.parent / "runs" / args.profile / f"{args.feature}_FEATURE"
    feature_dir.mkdir(parents=True, exist_ok=True)

    out_paths = testcase_writer.run(
        story_keys=story_keys,
        epic_key=args.feature,
        figma_url=args.figma,
        feature_dir=feature_dir,
        app_design_doc_path=args.design_doc,
        api_spec_path=args.api_spec,
        feature_link=args.feature,
    )
    # The run's ONE visible output: the merged sheet plus its JSON twin. The
    # per-story sheets stay under _internal/per_story/ for traceability only.
    from qa_pipeline.agents import adapters
    from qa_pipeline.contracts import write_validated
    from utils.run_layout import artifact_path

    final_sheet = artifact_path(feature_dir, f"{args.feature}_TestCases.xlsx")
    if final_sheet.is_file():
        cases = adapters.read_test_cases(final_sheet)
        write_validated(artifact_path(feature_dir, "test_cases.json"), "test_case",
                        cases, many=True)
        print(f"\nDone. {len(cases)} test cases:")
        print(" -", final_sheet)
        print(" -", artifact_path(feature_dir, "test_cases.json"))
    else:  # a single story: its own sheet is the final output
        print("\nDone. Sheet written:")
        for p in (out_paths if isinstance(out_paths, list) else [out_paths]):
            print(" -", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
