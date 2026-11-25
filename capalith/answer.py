"""The agent's side of a search: honest status, a short answer, and one step to the skill.

`judge` is the only place that decides whether an answer is exact, good, weak, close or no
match. It uses the signals the search gives today (the semantic side, name and description word
hits, the semantic gap between the first two results). A better ranking rule replaces that one
function; nothing else here depends on how the signals are made.

`shape` turns a full discovery record into a short answer for an agent: plain text for the host
(`_text`) and a small structured record. The full record is still available with detail "full".
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from capalith.settings import load_settings

_WORD = re.compile(r"\w+")
# "For choosing easing or duration use animate": a skill naming its better sibling.
_REDIRECT = re.compile(
    r"\bfor\s+([^.;:]{3,140}?)[,\s]+use\s+[`'\"]?([a-z0-9]+(?:-[a-z0-9]+)*)[`'\"]?",
    re.IGNORECASE,
)


from capalith.textsafe import is_safe as _safe, visible as _clean


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text.casefold()))


def judge(result: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    """(status, signal). Replace this function to change what each status means.

    Order: an empty result, an exact name, an accepted AI result, then the local `answer.refuse_below` gate.
    `good` is shown only where something judged the result: the AI ranking chose it, or the user set
    `answer.good_from` and the meaning score is at or above it and a query word is in the name or
    description (a common word in the description does not count: `answer.hide_words`). On the free search alone, that is `possible`: on the
    development sets today's "good" was on a wrong first result or a no-answer task as often as on a
    right one. `weak` is for a first result with no such word (only its meaning, or only its instructions
    text, relates); `close` is that case with a near tie; `no_match` is an empty result or a best meaning
    score below `answer.refuse_below`.
    """
    candidates = result["candidates"]
    if not candidates:
        return "no_match", "nothing matched"
    top = candidates[0]
    tokens = tuple(dict.fromkeys(_words(" ".join(result["query_terms"]))))
    if tuple(dict.fromkeys(_words(str(top["name"])))) == tokens:
        return "exact", "name equals the query"
    if result.get("ranking", {}).get("path") == "ai":
        return "good", "the AI ranking chose it"   # an accepted AI result outranks the local gate, which is for free results
    best = result.get("semantic", {}).get("best_similarity")
    if config["refuse_below"] and best is not None and best < config["refuse_below"]:
        return "no_match", f"best meaning score {best:.3f} is below answer.refuse_below {config['refuse_below']:g}"
    score = top["score"]
    similarity = score.get("semantic_similarity")
    hide = {word.casefold() for word in config["hide_words"]}
    # A word in the name always counts; a word in the description counts unless it is one of the common words
    # (`answer.hide_words`): "a" in "Tune a violin" says nothing about a task that merely contains "a".
    description_words = set(_words(str(top.get("description", ""))))
    distinctive = bool(score.get("description")) and any(t in description_words and t not in hide for t in tokens)
    if score.get("name") or distinctive:
        if config["good_from"] and similarity is not None and similarity >= config["good_from"]:
            return "good", f"meaning score {similarity:.3f} is at or above answer.good_from {config['good_from']:g}, and a query word is in the name or description"
        return "possible", "a query word is in the name or description; nothing confirms it"
    if similarity is not None and len(candidates) > 1:
        other = candidates[1]["score"].get("semantic_similarity")
        if other is not None and abs(similarity - other) <= config["close_margin"]:
            return "close", "the first two are within the semantic margin"
    if similarity is not None or "ai_relevance" in score:
        return "weak", "it is close in meaning but no word of yours is in its name or description"
    return "weak", "only the skill's body mentions your words"


def _summary(description: str, limit: int) -> str:
    text = " ".join(str(description).split())
    sentence = re.match(r"(.+?[.!?])(?:\s|$)", text)
    first = sentence.group(1) if sentence else text
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _why(candidate: dict[str, Any], status: str, config: dict[str, Any], rank: int) -> str:
    hidden = {word.casefold() for word in config["hide_words"]}
    terms = [t for t in candidate["matched_terms"] if t.casefold() not in hidden]
    terms = terms[: config["max_matched_words"]]
    if status == "exact" and rank == 0:
        return "exact name"
    quoted = ", ".join(f"'{term}'" for term in terms)
    meaning = "semantic_similarity" in candidate["score"] or "ai_relevance" in candidate["score"]
    if meaning and terms:
        return f"matched by meaning and your words {quoted}"
    if meaning:
        return "matched by meaning"
    return f"matched by your words {quoted}" if terms else "matched by words only"


def _read_path(source: Any, bundle_path: str) -> str | None:
    """The real, absolute SKILL.md of a skill in a local source, only while it is a file inside that source."""
    if source is None or source.kind != "local":
        return None
    try:
        root = Path(os.path.realpath(source.locator))
        path = Path(os.path.realpath(os.path.join(source.locator, bundle_path, "SKILL.md")))
        if path.is_absolute() and path.is_relative_to(root) and path.is_file():
            return str(path)
    except (OSError, ValueError):
        pass
    return None


def _redirects(candidate: dict[str, Any], tokens: set[str], config: dict[str, Any]) -> list[tuple[str, str]]:
    hidden = {word.casefold() for word in config["hide_words"]}
    found: list[tuple[str, str]] = []
    for clause, target in _REDIRECT.findall(str(candidate["description"])):
        relevant = (tokens - hidden) & set(_words(clause))
        if relevant and target != candidate["name"]:
            found.append((" ".join(clause.split()), target))
    return found


def _inspect(candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "artifact_id": candidate["artifact_id"],
        "content_digest": candidate["content_digest"],
        "resource_path": "SKILL.md",
    }


def _entry(candidate: dict[str, Any], sources: dict[str, Any], status: str, config: dict[str, Any], rank: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": candidate["name"],
        "summary": _summary(candidate["description"], config["summary_chars"]),
        "why": _why(candidate, status, config, rank),
    }
    path = _read_path(sources.get(str(candidate["source_id"])), str(candidate["bundle_path"]))
    if path is not None:
        entry["read"] = path
    if path is None or not _safe(path):
        # No local file (a Git source, or a file that is gone or not inside its source), or a path
        # the text can't carry safely: one inspect call reads it.
        entry["inspect"] = _inspect(candidate)
    related = [f"{item['type']} {item['target_name']}" for item in candidate["relationships"]]
    if related:
        entry["related"] = related
    return entry


def _line(index: str, entry: dict[str, Any], *, summary: bool, notes: bool, long_path: bool) -> list[str]:
    head = f"{index} {_clean(entry['name'])}"
    if summary and entry["summary"]:
        head += f": {_clean(entry['summary'])}"
    lines = [f"{head} ({_clean(entry['why'])})"]
    inspect = entry.get("inspect")
    if "read" in entry and _safe(entry["read"]) and not long_path:
        lines.append(f"   read: {entry['read']}")
    elif "read" in entry and long_path:
        lines.append(f"   read: path too long ({len(entry['read'])} characters); see the structured result")
    elif "read" in entry:
        lines.append(f"   read: path has control characters; inspect {_clean(inspect['artifact_id'])} {_clean(inspect['content_digest'])} SKILL.md")
    else:
        lines.append(f"   read: no local file; inspect {_clean(inspect['artifact_id'])} {_clean(inspect['content_digest'])} SKILL.md")
    if notes and "instead" in entry:
        lines.append(f"   instead: {_clean(entry['instead'])}")
    if notes and "related" in entry:
        lines.append("   related: " + "; ".join(_clean(item) for item in entry["related"]))
    return lines


def _stages(entries: list[dict[str, Any]], summary_chars: int):
    """What to give up, in order, until the text fits: summaries, notes, long paths, then list length."""
    floor = min(50, summary_chars)
    sizes = [summary_chars]
    while sizes[-1] - 10 >= floor:
        sizes.append(sizes[-1] - 10)
    base = {"notes": True, "extras": True, "long": frozenset(), "shown": len(entries)}
    for size in sizes:
        yield {**base, "size": size}
    yield {**base, "size": 0}
    quiet = {**base, "size": 0, "notes": False, "extras": False}
    yield quiet
    by_length = sorted((i for i, e in enumerate(entries) if "read" in e), key=lambda i: -len(entries[i]["read"]))
    for count in range(1, len(by_length) + 1):
        yield {**quiet, "long": frozenset(by_length[:count])}
    long = frozenset(by_length)
    for shown in range(len(entries) - 1, -1, -1):
        yield {**quiet, "long": long, "shown": shown}


def _render(status: str, message: str, signal: str, entries: list[dict[str, Any]], first_extra: int,
            more: dict[str, Any] | None, state: dict[str, Any]) -> str:
    lines = [f"{status}: {_clean(message)}"]
    if status != "no_match":   # the rule's own words stay in the structured record, not the agent's text
        listed = 0
        for position, entry in enumerate(entries):
            is_extra = position >= first_extra
            if position >= state["shown"] or (is_extra and not state["extras"]):
                continue
            label = "+" if is_extra else f"{position + 1}."
            lines.extend(_line(label, entry, summary=state["size"] > 0, notes=state["notes"],
                               long_path=position in state["long"]))
            listed += 1
        if listed < len(entries):
            lines.append(f"{len(entries) - listed} more in the structured result")
    if more is not None:
        lines.append(f"more: offset {more['offset']}, view_id {_clean(more['view_id'])}")
    return "\n".join(lines)


def shape(store: Any, result: dict[str, Any], detail: str | None, source_ids: Any = None) -> dict[str, Any]:
    """Return the full record for detail "full", else the short answer with `_text` for the host."""
    config = load_settings()["answer"]
    if (detail or config["detail"]) == "full":
        return result
    sources = {source.id: source for source in store.list_sources()}
    status, signal = judge(result, config)
    candidates = result["candidates"]
    messages = config["messages"]
    nearest = _clean(candidates[0]["name"]) if candidates else "none"
    ranking_record = result.get("ranking", {})
    roster = ranking_record.get("roster") or {}
    # Only an AI answer from a trimmed roster covers just the nearest skills the AI was shown.
    trimmed = ranking_record.get("path") == "ai" and roster.get("sent", 0) < roster.get("total", 0)
    key = "no_match_nearest" if status == "no_match" and trimmed else status
    message = messages[key].format(name=nearest, nearest=nearest, signal=signal, sent=roster.get("sent", 0), total=roster.get("total", 0))
    entries = [_entry(c, sources, status, config, rank) for rank, c in enumerate(candidates)]
    present = {entry["name"] for entry in entries}
    tokens = set(_words(" ".join(result["query_terms"])))
    extras: list[dict[str, Any]] = []
    found_extras: list[dict[str, Any]] = []
    if config["redirects"] and status != "no_match":
        looked: dict[str, Any] = {}
        for candidate, entry in zip(candidates, entries):
            for clause, target in _redirects(candidate, tokens, config):
                # Only a skill that exists in the searched sources is ever named, and only by an exact name lookup.
                if target in present:
                    found = True
                else:
                    if target not in looked:
                        looked[target] = store.lookup_name(target, source_ids=source_ids)
                    found = looked[target]
                if not found:
                    continue
                entry.setdefault("instead", f"for {clause} use {target}")
                if target not in present and len(extras) < config["max_redirects"]:
                    extra = _entry(found, sources, "good", config, 1)
                    extra["why"] = f"named by {candidate['name']}"
                    extras.append(extra)
                    found_extras.append(found)
                    present.add(target)
    page = result["retrieval"]
    more = None
    if page.get("has_more"):
        more = {"offset": page["next_offset"], "view_id": page["view_id"]}
    items = entries + extras
    descriptions = [c["description"] for c in candidates] + [f["description"] for f in found_extras]
    floor = min(50, config["summary_chars"])
    text = ""
    for state in _stages(items, config["summary_chars"]):
        for item, description in zip(items, descriptions):
            item["summary"] = _summary(description, max(state["size"], floor))
        text = _render(status, message, signal, items, len(entries), more, state)
        if not config["max_chars"] or len(text) <= config["max_chars"]:
            break
    for index in state["long"]:
        items[index].setdefault("inspect", _inspect(candidates[index] if index < len(candidates) else found_extras[index - len(candidates)]))
    answer: dict[str, Any] = {
        "status": status,
        "message": message,
        "signal": signal,
        "candidates": entries + [dict(extra, via="redirect") for extra in extras],
        "retrieval": {
            **{key: page[key] for key in ("method", "limit", "offset", "returned", "has_more", "next_offset", "view_id") if key in page},
            "semantic": result.get("semantic", {}).get("status"),
            **({"best_similarity": result["semantic"]["best_similarity"]} if "best_similarity" in result.get("semantic", {}) else {}),
        },
        "detail": "short",
    }
    if more is not None:
        answer["more"] = more
    answer["_text"] = text
    return answer
