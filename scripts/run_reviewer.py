"""GitHub Actions runner: auto-review a submitted artifact.

Triggered when a push contains an `artifact_submitted` EventEnvelope.
Extracts the task_id from the envelope, loads the artifact + task contract +
glossary + upstream artifacts, calls Claude API with the reviewer prompt,
and writes `reviews/<task_id>-review.json` + updates task status.

Anti-reentry: skips if `reviews/<task_id>-review.json` already exists AND
was written by a human (actor != 'github-action').
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
REVIEWER_PROMPT_PATH = ROOT / "agents" / "reviewer.md"
ACTOR = "github-action"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_envelope(commit_msg: str) -> dict | None:
    lines = commit_msg.splitlines()
    if not lines or not lines[0].startswith("[teamcollab]"):
        return None
    try:
        first = lines.index("---", 1)
        second = lines.index("---", first + 1)
    except ValueError:
        return None
    yaml_text = "\n".join(lines[first + 1 : second])
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) and "type" in data else None


def _load_task(task_id: str) -> dict | None:
    p = ROOT / "tasks" / f"{task_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _load_artifact(member: str, task_id: str) -> tuple[dict, str] | None:
    art_dir = ROOT / "artifacts" / member / task_id
    meta_path = art_dir / "meta.json"
    content_path = art_dir / "content.md"
    if not meta_path.exists() or not content_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    content = content_path.read_text(encoding="utf-8")
    return meta, content


def _load_glossary() -> dict:
    p = ROOT / "glossary.json"
    if not p.exists():
        return {"entries": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _review_exists(task_id: str) -> bool:
    p = ROOT / "reviews" / f"{task_id}-review.json"
    if not p.exists():
        return False
    review = json.loads(p.read_text(encoding="utf-8"))
    return review.get("reviewer") != ACTOR


def _load_upstream_artifacts(task: dict) -> dict[str, str]:
    upstream = {}
    for dep_id in task.get("deps", []):
        dep_task = _load_task(dep_id)
        if dep_task and dep_task.get("owner"):
            result = _load_artifact(dep_task["owner"], dep_id)
            if result:
                _, content = result
                upstream[dep_id] = content
    return upstream


def _build_reviewer_message(task: dict, content: str, meta: dict, glossary: dict, upstream: dict) -> str:
    parts = [
        "You are reviewing a TeamCollab artifact. Follow the reviewer instructions exactly.",
        "",
        "## Task Contract",
        f"```json\n{json.dumps(task, indent=2)}\n```",
        "",
        "## Submitted Artifact Content",
        content,
        "",
        "## Artifact Metadata",
        f"```json\n{json.dumps(meta, indent=2)}\n```",
        "",
        "## Glossary",
        f"```json\n{json.dumps(glossary, indent=2)}\n```",
    ]
    if upstream:
        parts.append("")
        parts.append("## Upstream Artifacts")
        for uid, ucontent in upstream.items():
            parts.append(f"\n### {uid}\n{ucontent}")

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
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _write_review(task_id: str, review_data: dict) -> None:
    review_path = ROOT / "reviews" / f"{task_id}-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_data, indent=2, ensure_ascii=False), encoding="utf-8")

    task_path = ROOT / "tasks" / f"{task_id}.json"
    if task_path.exists():
        task = json.loads(task_path.read_text(encoding="utf-8"))
        verdict = review_data.get("verdict", "needs_revision")
        task["status"] = "approved" if verdict == "approved" else "needs_revision"
        task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")


def _git_commit_push(task_id: str, verdict: str) -> None:
    envelope = {
        "type": "review_posted",
        "actor": ACTOR,
        "task_id": task_id,
        "ts": _utcnow(),
        "schema_version": 1,
    }
    yaml_block = yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True).strip()
    msg = f"[teamcollab] review_posted\n---\n{yaml_block}\n---\n{ACTOR} reviewed {task_id}: {verdict}"

    os.system("git config user.name 'teamcollab-bot'")
    os.system("git config user.email 'teamcollab-bot@users.noreply.github.com'")
    os.system(f'git add reviews/{task_id}-review.json tasks/{task_id}.json')
    os.system(f'git commit -m "{msg}"')
    os.system("git push")


def main() -> None:
    commit_msg = os.environ.get("COMMIT_MSG", "")
    envelope = _parse_envelope(commit_msg)
    if not envelope:
        print("No valid envelope in commit message, skipping.")
        return

    if envelope.get("type") != "artifact_submitted":
        print(f"Event type is '{envelope.get('type')}', not artifact_submitted. Skipping.")
        return

    task_id = envelope.get("task_id")
    if not task_id:
        print("No task_id in envelope, skipping.")
        return

    if _review_exists(task_id):
        print(f"Review for {task_id} already exists (written by human). Skipping.")
        return

    task = _load_task(task_id)
    if not task:
        print(f"Task {task_id} not found. Skipping.")
        return

    owner = task.get("owner")
    if not owner:
        print(f"Task {task_id} has no owner. Skipping.")
        return

    artifact = _load_artifact(owner, task_id)
    if not artifact:
        print(f"Artifact for {owner}/{task_id} not found. Skipping.")
        return

    meta, content = artifact
    glossary = _load_glossary()
    upstream = _load_upstream_artifacts(task)

    system_prompt = ""
    if REVIEWER_PROMPT_PATH.exists():
        raw = REVIEWER_PROMPT_PATH.read_text(encoding="utf-8")
        if "---" in raw:
            parts = raw.split("---", 2)
            system_prompt = parts[2].strip() if len(parts) > 2 else raw
        else:
            system_prompt = raw

    user_message = _build_reviewer_message(task, content, meta, glossary, upstream)
    print(f"Calling LLM API to review {task_id}...")
    response_text = _call_llm(system_prompt, user_message)

    try:
        review_data = json.loads(response_text)
    except json.JSONDecodeError:
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            review_data = json.loads(response_text[start:end])
        else:
            print(f"Failed to parse reviewer response as JSON:\n{response_text[:500]}")
            sys.exit(1)

    review_data["task_id"] = task_id
    review_data["reviewer"] = ACTOR
    review_data["reviewed_at"] = _utcnow()
    review_data["schema_version"] = 1

    verdict = review_data.get("verdict", "needs_revision")
    _write_review(task_id, review_data)
    _git_commit_push(task_id, verdict)
    print(f"Review posted for {task_id}: {verdict} (score: {review_data.get('score', '?')})")


if __name__ == "__main__":
    main()
