import json
import re
from collections import Counter

forms = ["он", "она", "они", "оно", "ее", "её", "их", "его"]
pattern = re.compile(
    r"(?iu)(?<![А-Яа-яЁёA-Za-z0-9_])("
    + "|".join(map(re.escape, forms))
    + r")(?![А-Яа-яЁёA-Za-z0-9_])"
)

rows = []
counts = Counter()

with open("docs/paragraphs_pages.jsonl", encoding="utf-8-sig") as f:
    for line in f:
        obj = json.loads(line)
        text = obj["text"]
        found = [m.group(1) for m in pattern.finditer(text)]
        if not found:
            continue
        counts.update(x.lower().replace("ё", "е") for x in found)
        fragment = text[:280].replace("\n", " ")
        if len(text) > 280:
            fragment += "..."
        rows.append((obj["page"], obj["paragraph"], sorted(set(found), key=str.lower), fragment))

with open("docs/core_pronouns_report.txt", "w", encoding="utf-8") as out:
    out.write(f"Найдено абзацев: {len(rows)}\n")
    out.write(f"Найдено употреблений: {sum(counts.values())}\n\n")
    out.write("Частые формы:\n")
    for word, count in counts.most_common():
        out.write(f"{word}: {count}\n")
    out.write("\n")
    for page, paragraph, found, fragment in rows:
        out.write(f"Страница {page}, абзац {paragraph}: {', '.join(found)}\n")
        out.write(f"{fragment}\n\n")

print(f"rows={len(rows)} occurrences={sum(counts.values())}")
print(counts)
