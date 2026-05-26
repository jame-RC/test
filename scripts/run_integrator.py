"""GitHub Actions runner: auto-integrate all approved artifacts into a draft deliverable.

Triggered when a push contains a `review_posted` EventEnvelope. Checks if
ALL tasks are now approved; if so, assembles inputs and calls Claude API with
the integrator prompt, writing `final/deliverable.draft.md`.

Anti-reentry rules:
1. If `final/deliverable.md` already exists → skip (leader already finalized).
2. If `final/deliverable.draft.md` exists AND no new reviews since last
   integration (compare timestamps) → skip (draft is current).
3. If a review was posted by a human (actor != 'github-action') after the
   draft was written → re-integrate (leader's review may have changed things).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

ROOT = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
INTEGRATOR_PROMPT_PATH = ROOT / "agents" / "integrator.md"
ACTOR = "github-action"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_all_tasks() -> list[dict]:
    tasks_dir = ROOT / "tasks"
    if not tasks_dir.exists():
        return []
    tasks = []
    for p in sorted(tasks_dir.glob("task-*.json")):
        tasks.append(json.loads(p.read_text(encoding="utf-8")))
    return tasks


def _all_approved(tasks: list[dict]) -> bool:
    return all(t.get("status") == "approved" for t in tasks) and len(tasks) > 0


def _load_artifact(member: str, task_id: str) -> str | None:
    content_path = ROOT / "artifacts" / member / task_id / "content.md"
    if not content_path.exists():
        return None
    return content_path.read_text(encoding="utf-8")


def _load_glossary() -> dict:
    p = ROOT / "glossary.json"
    if not p.exists():
        return {"entries": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _load_project() -> dict:
    p = ROOT / "project.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _should_skip() -> str | None:
    final_path = ROOT / "final" / "deliverable.md"
    if final_path.exists():
        return "final/deliverable.md already exists (leader finalized)"

    draft_path = ROOT / "final" / "deliverable.draft.md"
    if draft_path.exists():
        draft_mtime = draft_path.stat().st_mtime
        reviews_dir = ROOT / "reviews"
        if reviews_dir.exists():
            latest_review_mtime = 0.0
            for rp in reviews_dir.glob("*-review.json"):
                latest_review_mtime = max(latest_review_mtime, rp.stat().st_mtime)
            if latest_review_mtime <= draft_mtime:
                return "draft is current (no new reviews since last integration)"

    return None


def _topological_sort(tasks: list[dict]) -> list[dict]:
    by_id = {t["task_id"]: t for t in tasks}
    visited: set[str] = set()
    order: list[str] = []

    def visit(tid: str) -> None:
        if tid in visited:
            return
        visited.add(tid)
        task = by_id.get(tid)
        if task:
            for dep in task.get("deps", []):
                visit(dep)
            order.append(tid)

    for tid in by_id:
        visit(tid)

    return [by_id[tid] for tid in order if tid in by_id]


def _build_integrator_message(
    project: dict,
    tasks: list[dict],
    artifacts: dict[str, dict],
    glossary: dict,
    existing_draft: str | None,
) -> str:
    parts = [
        "You are integrating approved TeamCollab artifacts into a final deliverable.",
        "Follow the integrator instructions exactly.",
        "",
        "## Project",
        f"```json\n{json.dumps(project, indent=2)}\n```",
        "",
        "## Tasks (topological order)",
        f"```json\n{json.dumps(tasks, indent=2)}\n```",
        "",
        "## Glossary",
        f"```json\n{json.dumps(glossary, indent=2)}\n```",
        "",
        "## Artifacts",
    ]

    for task_id, art_info in artifacts.items():
        parts.append(f"\n### {task_id} (by {art_info['owner']})")
        parts.append(art_info["content"])

    if existing_draft:
        parts.append("")
        parts.append("## Existing Draft (for reference)")
        parts.append(existing_draft)

    return "\n".join(parts)


def _call_llm(system_prompt: str, user_message: str) -> str:
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")

    if base_url:
        client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    else:
        client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _git_commit_push() -> None:
    envelope = {
        "type": "final_integrated",
        "actor": ACTOR,
        "ts": _utcnow(),
        "schema_version": 1,
    }
    yaml_block = yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True).strip()
    msg = f"[teamcollab] final_integrated\n---\n{yaml_block}\n---\nintegrator produced draft deliverable"

    os.system("git config user.name 'teamcollab-bot'")
    os.system("git config user.email 'teamcollab-bot@users.noreply.github.com'")
    os.system("git add final/deliverable.draft.md")
    os.system(f'git commit -m "{msg}"')
    os.system("git push")


def _notify_leader(project: dict) -> None:
    members = project.get("members", [])
    leaders = [m["name"] for m in members if m.get("role") == "leader"]
    if leaders:
        leader_mention = ", ".join(f"@{l}" for l in leaders)
        title = project.get("title", "TeamCollab Project")
        body = (
            f"The integrator has produced a draft deliverable at `final/deliverable.draft.md`.\n\n"
            f"{leader_mention} — please review and run `/team-finalize` to promote it."
        )
        os.system(f'gh issue create --title "Draft deliverable ready: {title}" --body "{body}" 2>/dev/null || true')


def main() -> None:
    tasks = _load_all_tasks()
    if not tasks:
        print("No tasks found. Skipping.")
        return

    if not _all_approved(tasks):
        not_approved = [t["task_id"] for t in tasks if t.get("status") != "approved"]
        print(f"Not all tasks approved. Waiting on: {not_approved}")
        return

    skip_reason = _should_skip()
    if skip_reason:
        print(f"Skipping integration: {skip_reason}")
        return

    project = _load_project()
    glossary = _load_glossary()
    sorted_tasks = _topological_sort(tasks)

    artifacts: dict[str, dict] = {}
    for task in sorted_tasks:
        task_id = task["task_id"]
        owner = task.get("owner", "")
        content = _load_artifact(owner, task_id)
        if content:
            artifacts[task_id] = {"owner": owner, "content": content}

    if not artifacts:
        print("No artifacts found despite all tasks approved. Skipping.")
        return

    existing_draft = None
    draft_path = ROOT / "final" / "deliverable.draft.md"
    if draft_path.exists():
        existing_draft = draft_path.read_text(encoding="utf-8")

    system_prompt = ""
    if INTEGRATOR_PROMPT_PATH.exists():
        raw = INTEGRATOR_PROMPT_PATH.read_text(encoding="utf-8")
        if "---" in raw:
            parts = raw.split("---", 2)
            system_prompt = parts[2].strip() if len(parts) > 2 else raw
        else:
            system_prompt = raw

    task_dicts = [t for t in sorted_tasks]
    user_message = _build_integrator_message(project, task_dicts, artifacts, glossary, existing_draft)

    print("Calling LLM API to integrate artifacts...")
    deliverable = _call_llm(system_prompt, user_message)

    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(deliverable, encoding="utf-8")

    _git_commit_push()
    _notify_leader(project)
    print("Draft deliverable written to final/deliverable.draft.md")


if __name__ == "__main__":
    main()
